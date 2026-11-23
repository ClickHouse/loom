package loom

import (
	"context"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxProtectedBody = 4 * 1024 * 1024

func protectedDeadline(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, 30*time.Second)
}

func protectedHTTPClient(client *http.Client) (*http.Client, error) {
	copyClient := *client
	if copyClient.Transport == nil {
		copyClient.Transport = http.DefaultTransport
	}
	// Resolve the mutable default once; a wrapper could hide automatic sends.
	if transport, ok := copyClient.Transport.(*http.Transport); !ok || transport == nil {
		return nil, ErrSessionProtectionUnavailable
	}
	copyClient.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	return &copyClient, nil
}

type sessionObservation struct {
	families []*SessionProtection
	pending map[string]string
	complete bool
}

func beginObservation(ctx context.Context, families []*SessionProtection, human bool) (*sessionObservation, error) {
	if len(families) == 0 {
		return nil, nil
	}
	id, err := sessionRandom(16)
	if err != nil {
		return nil, err
	}
	observation := &sessionObservation{families: families, pending: make(map[string]string)}
	err = withSessionLocks(ctx, families, func() error {
		for _, f := range families {
			if err := f.validate(); err != nil {
				return err
			}
			pending, err := f.pending("")
			if err != nil {
				return err
			}
			if !human && (f.latched.Load() || pending) {
				return ErrAutomaticSessionProtected
			}
		}
		for _, f := range families {
			if _, exists := observation.pending[f.pointer.Path]; exists {
				continue
			}
			name := "pending-"+id
			observation.pending[f.pointer.Path] = name
			if err := sessionCreateFile(filepath.Join(f.pointer.Path, name), "1"); err != nil {
				return err
			}
			if err := sessionSync(f.pointer.Path); err != nil {
				return f.reject()
			}
		}
		// This is the final admission barrier, after all storage waits. No read
		// exemptions exist without exact generated operation metadata.
		for _, f := range families {
			if err := f.validate(); err != nil {
				return err
			}
			pending, err := f.pending(observation.pending[f.pointer.Path])
			if err != nil {
				return err
			}
			if !human && (f.latched.Load() || pending) {
				return ErrAutomaticSessionProtected
			}
		}
		return ctx.Err()
	})
	if err != nil {
		if len(observation.pending) != 0 {
			observation.abort()
		}
		return nil, err
	}
	return observation, nil
}

func (observation *sessionObservation) abort() {
	if observation == nil || observation.complete {
		return
	}
	// Cancellation cannot discard owned cleanup. The independent lock deadline
	// bounds this work; pending records survive even if storage cannot be marked.
	err := withSessionLocks(context.Background(), observation.families, func() error {
		var failed bool
		for _, f := range observation.families {
			failed = f.mark() != nil || failed
		}
		if failed {
			return ErrSessionProtectionUnavailable
		}
		return nil
	})
	for _, f := range observation.families {
		f.latched.Store(true)
		if err != nil {
			f.failed.Store(true)
		}
	}
}

func (observation *sessionObservation) readResponse(response *http.Response) ([]byte, error) {
	media, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	emptyAllowed := response.StatusCode == 204 || response.StatusCode == 304
	if err != nil || (media != "application/json" && !strings.HasSuffix(media, "+json")) {
		if !emptyAllowed || response.Header.Get("Content-Type") != "" {
			return nil, ErrSessionProtectionUnavailable
		}
	}
	if response.ContentLength > maxProtectedBody {
		return nil, ErrSessionProtectionUnavailable
	}
	raw, readErr := io.ReadAll(io.LimitReader(response.Body, maxProtectedBody+1))
	closeErr := response.Body.Close()
	if readErr != nil || closeErr != nil || len(raw) > maxProtectedBody {
		return nil, ErrSessionProtectionUnavailable
	}
	input := raw
	if len(input) == 0 && emptyAllowed {
		input = []byte("{}")
	}
	protected, err := protectedEnvelope(input)
	if err != nil {
		return nil, err
	}
	err = withSessionLocks(context.Background(), observation.families, func() error {
		for _, f := range observation.families {
			if err := f.validate(); err != nil {
				return err
			}
			if _, err := f.pending(""); err != nil {
				return err
			}
			if protected {
				if err := f.mark(); err != nil {
					return err
				}
			}
		}
		for path, name := range observation.pending {
			if err := os.Remove(filepath.Join(path, name)); err != nil {
				return ErrSessionProtectionUnavailable
			}
			if err := sessionSync(path); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, ErrSessionProtectionUnavailable
	}
	observation.complete = true
	return raw, nil
}
