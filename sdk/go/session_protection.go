package loom

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"time"
)

var (
	// ErrSessionProtectionUnavailable means the durable authority cannot be proved.
	ErrSessionProtectionUnavailable = errors.New("session protection proof is unavailable")
	// ErrAutomaticSessionProtected refuses an automatic operation after protected
	// content, or while another request's observation is pending.
	ErrAutomaticSessionProtected = errors.New("automatic operation refused by session protection")
)

// SessionProtectionPointer belongs in trusted application conversation storage,
// independently of client configuration. Preserve every field on restore.
// Filesystem identities detect replacement; they do not prevent storage rollback.
type SessionProtectionPointer struct {
	Path string `json:"path"`
	ID string `json:"id"`
	DirectoryIdentity string `json:"directory_identity"`
	ManifestIdentity string `json:"manifest_identity"`
	StateIdentity string `json:"state_identity"`
	LockIdentity string `json:"lock_identity"`
}

// SessionProtection is a monotonic, content-free application conversation family.
// Share its pointer across clients and retain captures across queued work.
type SessionProtection struct {
	pointer SessionProtectionPointer
	latched atomic.Bool
	failed atomic.Bool
}

// CreateSessionProtection provisions a new conversation in a new private
// directory under a trusted private parent. Never call it to restore a session.
func CreateSessionProtection(path string) (*SessionProtection, error) {
	if !sessionPlatformSupported() {
		return nil, ErrSessionProtectionUnavailable
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, ErrSessionProtectionUnavailable
	}
	parent, err := filepath.EvalSymlinks(filepath.Dir(absolute))
	if err != nil {
		return nil, ErrSessionProtectionUnavailable
	}
	parentInfo, err := os.Lstat(parent)
	if err != nil || !parentInfo.IsDir() || !sessionPrivate(parentInfo) {
		return nil, ErrSessionProtectionUnavailable
	}
	absolute = filepath.Join(parent, filepath.Base(absolute))
	if err := os.Mkdir(absolute, 0700); err != nil {
		return nil, ErrSessionProtectionUnavailable
	}
	id, err := sessionRandom(32)
	if err != nil {
		return nil, err
	}
	for _, item := range []struct{name, value string}{{"identity", id}, {"state", "0"}, {"lock", ""}} {
		if err := sessionCreateFile(filepath.Join(absolute, item.name), item.value); err != nil {
			return nil, err
		}
	}
	if sessionSync(absolute) != nil || sessionSync(parent) != nil {
		return nil, ErrSessionProtectionUnavailable
	}
	pointer := SessionProtectionPointer{Path: absolute, ID: id}
	for name, target := range map[string]*string{"": &pointer.DirectoryIdentity, "identity": &pointer.ManifestIdentity, "state": &pointer.StateIdentity, "lock": &pointer.LockIdentity} {
		info, err := os.Lstat(filepath.Join(absolute, name))
		if err != nil {
			return nil, ErrSessionProtectionUnavailable
		}
		*target, err = sessionFileIdentity(info)
		if err != nil {
			return nil, err
		}
	}
	return OpenSessionProtection(pointer)
}

// OpenSessionProtection reopens exactly the trusted authority. Pending attempts
// remain protected; missing, replaced or corrupt proof never creates a family.
func OpenSessionProtection(pointer SessionProtectionPointer) (*SessionProtection, error) {
	if !sessionPlatformSupported() || !filepath.IsAbs(pointer.Path) || !sessionHex(pointer.ID, 32) {
		return nil, ErrSessionProtectionUnavailable
	}
	resolved, err := filepath.EvalSymlinks(pointer.Path)
	if err != nil || resolved != pointer.Path {
		return nil, ErrSessionProtectionUnavailable
	}
	f := &SessionProtection{pointer: pointer}
	err = withSessionLocks(context.Background(), []*SessionProtection{f}, func() error {
		if err := f.validate(); err != nil {
			return err
		}
		pending, err := f.pending("")
		if err != nil {
			return err
		}
		if pending {
			return f.mark()
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return f, nil
}

func (f *SessionProtection) Pointer() SessionProtectionPointer { return f.pointer }

// Protected validates storage and reports the latch or an unacknowledged attempt.
func (f *SessionProtection) Protected(ctx context.Context) (bool, error) {
	var protected bool
	err := withSessionLocks(ctx, []*SessionProtection{f}, func() error {
		if err := f.validate(); err != nil {
			return err
		}
		pending, err := f.pending("")
		protected = f.latched.Load() || pending
		return err
	})
	return protected, err
}

// SessionCapture retains all families that contributed to deferred work.
// Values are immutable; pass Context's result to callbacks and goroutines.
type SessionCapture struct { families []*SessionProtection }
type sessionCaptureKey struct{}

// CaptureSession unions the current retained capture with the supplied families.
func CaptureSession(ctx context.Context, families ...*SessionProtection) SessionCapture {
	if len(families) > 64 {
		return SessionCapture{families: []*SessionProtection{nil}}
	}
	var all []*SessionProtection
	if current, ok := ctx.Value(sessionCaptureKey{}).(SessionCapture); ok {
		if len(current.families) > 64 {
			return SessionCapture{families: []*SessionProtection{nil}}
		}
		all = append(all, current.families...)
	}
	all = append(all, families...)
	seen := make(map[*SessionProtection]bool)
	unique := all[:0]
	for _, family := range all {
		if family == nil {
			return SessionCapture{families: []*SessionProtection{nil}}
		}
		if !seen[family] {
			seen[family] = true
			unique = append(unique, family)
		}
	}
	if len(unique) > 64 {
		return SessionCapture{families: []*SessionProtection{nil}}
	}
	return SessionCapture{families: unique}
}

// Context adds this capture to the context's existing contributors. It carries
// no human authority. An empty or invalid capture refuses protected dispatch.
func (capture SessionCapture) Context(ctx context.Context) context.Context {
	merged := CaptureSession(ctx, capture.families...)
	return context.WithValue(ctx, sessionCaptureKey{}, merged)
}

func requestFamilies(ctx context.Context, bound *SessionProtection) ([]*SessionProtection, error) {
	var families []*SessionProtection
	if capture, ok := ctx.Value(sessionCaptureKey{}).(SessionCapture); ok {
		if len(capture.families) == 0 {
			return nil, ErrSessionProtectionUnavailable
		}
		families = append(families, capture.families...)
	}
	if bound != nil {
		families = append(families, bound)
	}
	if len(families) > 64 {
		return nil, ErrSessionProtectionUnavailable
	}
	seen := make(map[*SessionProtection]bool)
	unique := families[:0]
	for _, f := range families {
		if f == nil {
			return nil, ErrSessionProtectionUnavailable
		}
		if !seen[f] {
			seen[f] = true
			unique = append(unique, f)
		}
	}
	sort.Slice(unique, func(i, j int) bool { return unique[i].pointer.Path < unique[j].pointer.Path })
	return unique, nil
}

func (f *SessionProtection) reject() error {
	f.failed.Store(true)
	return ErrSessionProtectionUnavailable
}

func (f *SessionProtection) file(name, identity string, size int64) (*os.File, error) {
	path := filepath.Join(f.pointer.Path, name)
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || !sessionPrivate(info) || info.Size() != size {
		return nil, f.reject()
	}
	actual, err := sessionFileIdentity(info)
	if err != nil || actual != identity || identity == "" {
		return nil, f.reject()
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return nil, f.reject()
	}
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) {
		file.Close()
		return nil, f.reject()
	}
	return file, nil
}

func (f *SessionProtection) validate() error {
	if f == nil || f.failed.Load() {
		return ErrSessionProtectionUnavailable
	}
	info, err := os.Lstat(f.pointer.Path)
	if err != nil || !info.IsDir() || !sessionPrivate(info) {
		return f.reject()
	}
	id, err := sessionFileIdentity(info)
	if err != nil || id != f.pointer.DirectoryIdentity || id == "" {
		return f.reject()
	}
	manifest, err := f.file("identity", f.pointer.ManifestIdentity, 64)
	if err != nil {
		return err
	}
	value, err := io.ReadAll(io.LimitReader(manifest, 65))
	closeErr := manifest.Close()
	if err != nil || closeErr != nil || string(value) != f.pointer.ID {
		return f.reject()
	}
	state, err := f.file("state", f.pointer.StateIdentity, 1)
	if err != nil {
		return err
	}
	value, err = io.ReadAll(io.LimitReader(state, 2))
	closeErr = state.Close()
	if err != nil || closeErr != nil {
		return f.reject()
	}
	switch string(value) {
	case "1":
		f.latched.Store(true)
	case "0":
		if f.latched.Load() {
			return f.reject()
		}
	default:
		return f.reject()
	}
	return nil
}

// mark runs with the family's storage lock held. The local latch precedes I/O.
func (f *SessionProtection) mark() error {
	f.latched.Store(true)
	file, err := f.file("state", f.pointer.StateIdentity, 1)
	if err != nil {
		return err
	}
	_, writeErr := file.WriteAt([]byte("1"), 0)
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil {
		return f.reject()
	}
	return nil
}

func (f *SessionProtection) pending(own string) (bool, error) {
	directory, err := os.Open(f.pointer.Path)
	if err != nil {
		return false, f.reject()
	}
	entries, readErr := directory.ReadDir(10001)
	closeErr := directory.Close()
	if (readErr != nil && readErr != io.EOF) || closeErr != nil || len(entries) > 10000 {
		return false, f.reject()
	}
	found := false
	for _, entry := range entries {
		name := entry.Name()
		if name == "identity" || name == "state" || name == "lock" {
			continue
		}
		if !strings.HasPrefix(name, "pending-") || !sessionHex(strings.TrimPrefix(name, "pending-"), 16) {
			return false, f.reject()
		}
		info, err := os.Lstat(filepath.Join(f.pointer.Path, name))
		if err != nil || !info.Mode().IsRegular() || !sessionPrivate(info) || info.Size() != 1 {
			return false, f.reject()
		}
		id, err := sessionFileIdentity(info)
		if err != nil {
			return false, f.reject()
		}
		file, err := f.file(name, id, 1)
		if err != nil {
			return false, err
		}
		value, readErr := io.ReadAll(io.LimitReader(file, 2))
		closeErr := file.Close()
		if readErr != nil || closeErr != nil || string(value) != "1" {
			return false, f.reject()
		}
		found = found || name != own
	}
	return found, nil
}

func withSessionLocks(ctx context.Context, families []*SessionProtection, work func() error) error {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	locks := make([]*os.File, 0, len(families))
	defer func() {
		for i := len(locks)-1; i >= 0; i-- {
			sessionUnlock(locks[i])
			locks[i].Close()
		}
	}()
	paths := make(map[string]SessionProtectionPointer)
	for _, f := range families {
		if f == nil || f.failed.Load() {
			return ErrSessionProtectionUnavailable
		}
		if !sessionPlatformSupported() || !filepath.IsAbs(f.pointer.Path) || !sessionHex(f.pointer.ID, 32) {
			return f.reject()
		}
		if old, exists := paths[f.pointer.Path]; exists {
			if old != f.pointer {
				return f.reject()
			}
			continue
		}
		paths[f.pointer.Path] = f.pointer
		file, err := f.file("lock", f.pointer.LockIdentity, 0)
		if err != nil {
			return err
		}
		if err := sessionLock(ctx, file); err != nil {
			file.Close()
			return err
		}
		locks = append(locks, file)
		// A path replacement while waiting cannot install a different lock.
		if err := f.validate(); err != nil {
			return err
		}
		check, err := f.file("lock", f.pointer.LockIdentity, 0)
		if err != nil {
			return err
		}
		check.Close()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return work()
}

func sessionCreateFile(path, value string) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return ErrSessionProtectionUnavailable
	}
	_, writeErr := io.WriteString(file, value)
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil {
		return ErrSessionProtectionUnavailable
	}
	return nil
}

func sessionSync(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return ErrSessionProtectionUnavailable
	}
	syncErr := file.Sync()
	closeErr := file.Close()
	if syncErr != nil || closeErr != nil {
		return ErrSessionProtectionUnavailable
	}
	return nil
}

func sessionRandom(size int) (string, error) {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		return "", ErrSessionProtectionUnavailable
	}
	return hex.EncodeToString(value), nil
}

func sessionHex(value string, size int) bool {
	if len(value) != size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
