// Package loom calls Loom's memory operations over HTTP.
//
// Every operation is POST /v1/<op> with a JSON body. A Client binds the org,
// namespace and agent once and sends them with every call.
package loom

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
)

// Version is the SDK version sent in User-Agent and X-Loom-SDK.
const Version = "0.1.0"

const sdkIdent = "loom-go/" + Version

// DefaultBaseURL is the server used when neither Config.BaseURL nor LOOM_BASE_URL is set.
const DefaultBaseURL = "http://localhost:7777"

// Config describes the server and the identity a Client acts as.
type Config struct {
	// BaseURL is the server; blank reads LOOM_BASE_URL, then DefaultBaseURL.
	BaseURL string
	// APIKey is the bearer token; blank reads LOOM_API_KEY. With neither, no
	// Authorization header is sent, which a server with auth off accepts.
	APIKey string

	Org       string
	Namespace string
	Agent     string

	// HTTPClient sends the requests; nil uses http.DefaultClient. The Client
	// uses a copy that answers a redirect with an *Error instead of following
	// it, unless this client sets its own CheckRedirect.
	HTTPClient *http.Client
}

// Client calls the memory operations as one org, namespace and agent.
// It is safe for concurrent use.
type Client struct {
	baseURL  string
	apiKey   string
	identity identity
	http     *http.Client
}

type identity struct {
	Org       string `json:"org,omitempty"`
	Namespace string `json:"namespace,omitempty"`
	Agent     string `json:"agent,omitempty"`
}

// New returns a Client for cfg.
func New(cfg Config) *Client {
	baseURL := firstNonBlank(cfg.BaseURL, os.Getenv("LOOM_BASE_URL"), DefaultBaseURL)
	httpClient := http.DefaultClient
	if cfg.HTTPClient != nil {
		httpClient = cfg.HTTPClient
	}
	// A copy, so the caller's client and http.DefaultClient keep their own redirect policy.
	client := *httpClient
	if client.CheckRedirect == nil {
		// Following one would resend the memory body and the bearer token to wherever it points.
		client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	}
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		// An exported but empty LOOM_API_KEY must not send "Bearer " and turn a clear 401 into a puzzle.
		apiKey:   firstNonBlank(cfg.APIKey, os.Getenv("LOOM_API_KEY")),
		identity: identity{Org: cfg.Org, Namespace: cfg.Namespace, Agent: cfg.Agent},
		http:     &client,
	}
}

func firstNonBlank(values ...string) string {
	for _, v := range values {
		if v = strings.TrimSpace(v); v != "" {
			return v
		}
	}
	return ""
}

// call posts in to /v1/<op> and decodes a 2xx answer into out.
func (c *Client) call(ctx context.Context, op string, in, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return fmt.Errorf("%s: encode request: %w", op, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/v1/"+op, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("%s: %w", op, err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", sdkIdent)
	req.Header.Set("X-Loom-SDK", sdkIdent)
	if c.apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.apiKey)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s: %w", op, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("%s: read response: %w", op, err)
	}

	requestID := resp.Header.Get("X-Request-Id")
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return errorFromResponse(op, resp.StatusCode, raw, requestID, resp.Header.Get("Retry-After"))
	}
	if err := json.Unmarshal(raw, out); err != nil {
		// Most often a proxy's page. The call may have run, so it is not retryable.
		return &Error{
			Op:        op,
			Status:    resp.StatusCode,
			Code:      CodeInvalidResponse,
			Message:   snippet(raw, 200),
			Category:  CategoryInternal,
			RequestID: requestID,
		}
	}
	return nil
}
