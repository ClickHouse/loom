package loom

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

// recorded is one request the fake server received.
type recorded struct {
	method string
	path   string
	header http.Header
	body   map[string]any
}

// fakeServer answers every request with status and body, and records the request.
func fakeServer(t *testing.T, status int, body string, headers map[string]string) (*httptest.Server, *recorded) {
	t.Helper()
	rec := &recorded{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
		}
		rec.method, rec.path, rec.header = r.Method, r.URL.Path, r.Header.Clone()
		if err := json.Unmarshal(raw, &rec.body); err != nil {
			t.Errorf("request body is not a JSON object: %s", raw)
		}
		for k, v := range headers {
			w.Header().Set(k, v)
		}
		w.WriteHeader(status)
		if _, err := io.WriteString(w, body); err != nil {
			t.Errorf("write response: %v", err)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, rec
}

func newClient(srv *httptest.Server) *Client {
	return New(Config{BaseURL: srv.URL, APIKey: "tok", Org: "acme", Namespace: "support", Agent: "assistant"})
}

func assertBody(t *testing.T, rec *recorded, want map[string]any) {
	t.Helper()
	if !reflect.DeepEqual(rec.body, want) {
		t.Errorf("request body\n got: %v\nwant: %v", rec.body, want)
	}
}

func TestSetSendsTheIdentityKeyAndValue(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"memory_id":"m-1","version":1763500000123456789,"content_sha256":"abc","status":"active","noop":false}`, nil)

	out, err := newClient(srv).Set(context.Background(), "deploy_window", "Deploys run Tue and Thu only.",
		&SetOptions{Tags: []string{"ops"}})
	if err != nil {
		t.Fatal(err)
	}

	if rec.method != http.MethodPost || rec.path != "/v1/memory.set" {
		t.Errorf("request line: %s %s", rec.method, rec.path)
	}
	for name, want := range map[string]string{
		"Authorization": "Bearer tok",
		"Content-Type":  "application/json",
		"Accept":        "application/json",
		"User-Agent":    "loom-go/" + Version,
		"X-Loom-SDK":    "loom-go/" + Version,
	} {
		if got := rec.header.Get(name); got != want {
			t.Errorf("header %s = %q, want %q", name, got, want)
		}
	}
	assertBody(t, rec, map[string]any{
		"org": "acme", "namespace": "support", "agent": "assistant",
		"memory_key": "deploy_window", "value": "Deploys run Tue and Thu only.",
		"tags": []any{"ops"},
	})
	want := SetResult{MemoryID: "m-1", Version: 1763500000123456789, ContentSHA256: "abc", Status: "active"}
	if *out != want {
		t.Errorf("result = %+v, want %+v", *out, want)
	}
}

func TestSetSendsAStructuredValueAsJSON(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"memory_id":"m-2","version":2}`, nil)

	value := map[string]any{"days": []string{"Tue", "Thu"}}
	if _, err := newClient(srv).Set(context.Background(), "deploy_window", value, &SetOptions{MemoryType: "procedural"}); err != nil {
		t.Fatal(err)
	}

	if got := rec.body["value"]; !reflect.DeepEqual(got, map[string]any{"days": []any{"Tue", "Thu"}}) {
		t.Errorf("value = %v", got)
	}
	if got := rec.body["memory_type"]; got != "procedural" {
		t.Errorf("memory_type = %v", got)
	}
}

func TestGetReturnsTheStoredValue(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"found":true,"memory_id":"m-1","value":{"days":["Tue","Thu"]},"memory_type":"semantic","version":7,"tags":["ops"]}`, nil)

	m, err := newClient(srv).Get(context.Background(), "deploy_window")
	if err != nil {
		t.Fatal(err)
	}

	if rec.path != "/v1/memory.get" {
		t.Errorf("path = %s", rec.path)
	}
	assertBody(t, rec, map[string]any{"org": "acme", "namespace": "support", "agent": "assistant", "memory_key": "deploy_window"})
	if !m.Found || m.MemoryID != "m-1" || m.Version != 7 || m.MemoryType != "semantic" || !reflect.DeepEqual(m.Tags, []string{"ops"}) {
		t.Errorf("memory = %+v", m)
	}
	var value struct{ Days []string }
	if err := json.Unmarshal(m.Value, &value); err != nil || !reflect.DeepEqual(value.Days, []string{"Tue", "Thu"}) {
		t.Errorf("value = %s (%v)", m.Value, err)
	}
}

func TestGetOfAMissingKeyIsNotAnError(t *testing.T) {
	srv, _ := fakeServer(t, 200, `{"found":false,"memory_id":"","value":null,"memory_type":null,"version":null}`, nil)

	m, err := newClient(srv).Get(context.Background(), "nothing_here")
	if err != nil {
		t.Fatal(err)
	}
	if m.Found || m.Version != 0 || m.MemoryType != "" {
		t.Errorf("memory = %+v", m)
	}
}

func TestSearchSendsTheQueryAndReturnsTheHits(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"results":[
		{"memory_key":"deploy_window","memory_type":"semantic","content_excerpt":"Deploys run Tue and Thu only.","distance":0.1,"similarity":0.9,"version":7},
		{"memory_key":"freeze","memory_type":"semantic","content_excerpt":"No deploys in December.","distance":0.4,"similarity":0.6}
	]}`, nil)

	out, err := newClient(srv).Search(context.Background(), "when can I deploy?", &SearchOptions{TopK: 2, Tags: []string{"ops"}})
	if err != nil {
		t.Fatal(err)
	}

	if rec.path != "/v1/memory.search" {
		t.Errorf("path = %s", rec.path)
	}
	assertBody(t, rec, map[string]any{
		"org": "acme", "namespace": "support", "agent": "assistant",
		"query": "when can I deploy?", "top_k": float64(2), "tags": []any{"ops"},
	})
	want := []SearchHit{
		{MemoryKey: "deploy_window", MemoryType: "semantic", ContentExcerpt: "Deploys run Tue and Thu only.", Similarity: 0.9, Version: 7},
		{MemoryKey: "freeze", MemoryType: "semantic", ContentExcerpt: "No deploys in December.", Similarity: 0.6},
	}
	if !reflect.DeepEqual(out.Results, want) {
		t.Errorf("results\n got: %+v\nwant: %+v", out.Results, want)
	}
}

func TestSearchWithoutOptionsSendsOnlyTheQuery(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"results":[]}`, nil)

	if _, err := New(Config{BaseURL: srv.URL, Org: "acme", Namespace: "support"}).Search(context.Background(), "deploys", nil); err != nil {
		t.Fatal(err)
	}
	// No agent bound: the server's default reads every agent's rows.
	assertBody(t, rec, map[string]any{"org": "acme", "namespace": "support", "query": "deploys"})
}

func TestDeleteTombstonesTheKey(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"tombstoned":true,"version":8,"found":true,"dependents_tombstoned":0,"dependents_downgraded":0}`, nil)

	out, err := newClient(srv).Delete(context.Background(), "deploy_window")
	if err != nil {
		t.Fatal(err)
	}

	if rec.path != "/v1/memory.delete" {
		t.Errorf("path = %s", rec.path)
	}
	assertBody(t, rec, map[string]any{"org": "acme", "namespace": "support", "agent": "assistant", "memory_key": "deploy_window"})
	if *out != (DeleteResult{Found: true, Tombstoned: true, Version: 8}) {
		t.Errorf("result = %+v", *out)
	}
}

func TestConfigFallsBackToTheEnvironment(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"found":false}`, nil)
	t.Setenv("LOOM_BASE_URL", srv.URL+"/")
	t.Setenv("LOOM_API_KEY", " env-tok ")

	if _, err := New(Config{Org: "acme"}).Get(context.Background(), "k"); err != nil {
		t.Fatal(err)
	}
	if got := rec.header.Get("Authorization"); got != "Bearer env-tok" {
		t.Errorf("Authorization = %q", got)
	}
	if rec.path != "/v1/memory.get" {
		t.Errorf("path = %s", rec.path)
	}
}

func TestABlankKeySendsNoAuthorization(t *testing.T) {
	srv, rec := fakeServer(t, 200, `{"found":false}`, nil)
	t.Setenv("LOOM_API_KEY", "   ")

	if _, err := New(Config{BaseURL: srv.URL}).Get(context.Background(), "k"); err != nil {
		t.Fatal(err)
	}
	if _, sent := rec.header["Authorization"]; sent {
		t.Errorf("Authorization sent: %q", rec.header.Get("Authorization"))
	}
}

func TestTheDefaultServerIsLocal(t *testing.T) {
	t.Setenv("LOOM_BASE_URL", "")
	if got := New(Config{}).baseURL; got != DefaultBaseURL {
		t.Errorf("baseURL = %q", got)
	}
}

func TestErrorsCarryTheServersClass(t *testing.T) {
	cases := []struct {
		status     int
		body       string
		code       string
		category   Category
		retryable  bool
		retryAfter time.Duration
	}{
		{409, `{"detail":{"code":"PRECONDITION_FAILED","message":"another writer changed it","category":"input","retryable":false,"current_sha256":"x"}}`,
			"PRECONDITION_FAILED", CategoryInput, false, 0},
		{403, `{"detail":{"code":"AUTH_INSUFFICIENT","message":"role viewer cannot write","category":"auth","retryable":false}}`,
			"AUTH_INSUFFICIENT", CategoryAuth, false, 0},
		{429, `{"detail":{"code":"RATE_LIMITED","message":"slow down","category":"capacity","retryable":true,"retry_after_s":2.5}}`,
			"RATE_LIMITED", CategoryCapacity, true, 2500 * time.Millisecond},
		{502, `{"detail":{"code":"ASSISTANT_UPSTREAM_FAILED","message":"model provider failed","category":"dependency","retryable":true}}`,
			"ASSISTANT_UPSTREAM_FAILED", CategoryDependency, true, 0},
		{503, `{"detail":{"code":"SCHEMA_PENDING","message":"run the migration","category":"configuration","retryable":false,"table":"t","migration":"m"}}`,
			"SCHEMA_PENDING", CategoryConfiguration, false, 0},
		{500, `{"detail":{"code":"INTERNAL_ERROR","message":"report the request id","category":"internal","retryable":false}}`,
			"INTERNAL_ERROR", CategoryInternal, false, 0},
	}
	for _, tc := range cases {
		t.Run(string(tc.category), func(t *testing.T) {
			srv, _ := fakeServer(t, tc.status, tc.body, map[string]string{"X-Request-Id": "req-1"})

			_, err := newClient(srv).Set(context.Background(), "k", "v", nil)

			var e *Error
			if !errors.As(err, &e) {
				t.Fatalf("error %v is not an *Error", err)
			}
			if e.Op != "memory.set" || e.Status != tc.status || e.Code != tc.code || e.Category != tc.category ||
				e.Retryable != tc.retryable || e.RetryAfter != tc.retryAfter || e.RequestID != "req-1" || e.Message == "" {
				t.Errorf("error = %+v", e)
			}
			if !strings.Contains(err.Error(), tc.code) {
				t.Errorf("message %q does not name the code", err.Error())
			}
		})
	}
}

func TestAnErrorWithoutAClassIsClassedByStatus(t *testing.T) {
	cases := []struct {
		status    int
		body      string
		headers   map[string]string
		category  Category
		retryable bool
		message   string
		after     time.Duration
	}{
		{401, `{"detail":"missing token"}`, nil, CategoryAuth, false, "missing token", 0},
		{404, `{"detail":{"code":"RESOURCE_NOT_FOUND","message":"no such namespace"}}`, nil, CategoryInput, false, "no such namespace", 0},
		{429, `{"detail":{"code":"RATE_LIMITED","message":"slow down"}}`, map[string]string{"Retry-After": "3"}, CategoryCapacity, true, "slow down", 3 * time.Second},
		{502, `<html>Bad Gateway</html>`, nil, CategoryDependency, true, "<html>Bad Gateway</html>", 0},
		{500, ``, nil, CategoryInternal, false, "HTTP 500", 0},
	}
	for _, tc := range cases {
		t.Run(http.StatusText(tc.status), func(t *testing.T) {
			srv, _ := fakeServer(t, tc.status, tc.body, tc.headers)

			_, err := newClient(srv).Get(context.Background(), "k")

			var e *Error
			if !errors.As(err, &e) {
				t.Fatalf("error %v is not an *Error", err)
			}
			if e.Category != tc.category || e.Retryable != tc.retryable || e.Message != tc.message || e.RetryAfter != tc.after {
				t.Errorf("error = %+v", e)
			}
		})
	}
}

func TestA2xxThatIsNotJSONIsAnInvalidResponse(t *testing.T) {
	srv, _ := fakeServer(t, 200, `<html>sign in to the proxy</html>`, nil)

	_, err := newClient(srv).Get(context.Background(), "k")

	var e *Error
	if !errors.As(err, &e) || e.Code != CodeInvalidResponse || e.Category != CategoryInternal || e.Retryable {
		t.Fatalf("error = %#v", err)
	}
}

func TestACanceledContextStopsTheCall(t *testing.T) {
	srv, _ := fakeServer(t, 200, `{"found":false}`, nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_, err := newClient(srv).Get(ctx, "k")

	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v, want context.Canceled", err)
	}
}
