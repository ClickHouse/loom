package loom

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// These tests own real loopback servers. They require hosted native execution.
type protectionRequest struct {
	method, path string
	header http.Header
	body map[string]any
}

type protectionWire struct {
	server *httptest.Server
	requests chan protectionRequest
	count atomic.Int64
}

func newProtectionWire(t *testing.T, respond http.HandlerFunc) *protectionWire {
	t.Helper()
	wire := &protectionWire{requests: make(chan protectionRequest, 32)}
	wire.server = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		wire.count.Add(1)
		raw, err := io.ReadAll(io.LimitReader(r.Body, maxProtectedBody+1))
		r.Body.Close()
		if err != nil || len(raw) > maxProtectedBody {
			t.Error("fixture request body exceeded its bound or could not be read")
			http.Error(w, "invalid fixture request", http.StatusBadRequest)
			return
		}
		var body map[string]any
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Error("fixture request was not a JSON object")
			http.Error(w, "invalid fixture request", http.StatusBadRequest)
			return
		}
		select {
		case wire.requests <- protectionRequest{r.Method, r.URL.Path, r.Header.Clone(), body}:
		case <-r.Context().Done():
			return
		case <-time.After(2*time.Second):
			t.Error("fixture request queue did not drain")
			return
		}
		respond(w, r)
	}))
	wire.server.Config.ReadHeaderTimeout = 2*time.Second
	wire.server.Config.ReadTimeout = 3*time.Second
	wire.server.Config.WriteTimeout = 4*time.Second
	wire.server.Config.IdleTimeout = time.Second
	wire.server.Start()
	t.Cleanup(func() {
		wire.server.CloseClientConnections()
		wire.server.Close()
	})
	return wire
}

func protectionJSON(status int, body string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}
}

func protectionReadArguments() map[string]any {
	return map[string]any{"name":"documents", "resource_id":"resource:example"}
}

func protectionMutationArguments() map[string]any {
	return map[string]any{"memory_key":"publication", "value":"automatic"}
}

func newGoProtection(t *testing.T) *SessionProtection {
	t.Helper()
	if !sessionPlatformSupported() {
		t.Skip("durable session store requires a supported POSIX platform")
	}
	family, err := CreateSessionProtection(filepath.Join(t.TempDir(), "conversation"))
	if err != nil {
		t.Fatal(err)
	}
	return family
}

func protectionClient(wire *protectionWire, family *SessionProtection, namespace, token string) *Client {
	return New(Config{
		BaseURL: wire.server.URL, APIKey: token, Org: "example", Namespace: namespace, Agent: "assistant",
		HTTPClient: &http.Client{Timeout: 3*time.Second}, SessionProtection: family,
	})
}

func protectionContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func protectionNext(t *testing.T, wire *protectionWire) protectionRequest {
	t.Helper()
	select {
	case request := <-wire.requests:
		return request
	case <-time.After(2*time.Second):
		t.Fatal("HTTP fixture did not receive the expected request")
		return protectionRequest{}
	}
}

func protectionAssertWire(t *testing.T, request protectionRequest, path, namespace, token string, body map[string]any, declared bool) {
	t.Helper()
	if request.method != http.MethodPost || request.path != path {
		t.Fatalf("request method/path = %s %s", request.method, request.path)
	}
	want := map[string]any{"org": "example", "namespace": namespace, "agent": "assistant"}
	for key, value := range body {
		want[key] = value
	}
	if !reflect.DeepEqual(request.body, want) {
		t.Fatalf("wire body = %#v; want %#v", request.body, want)
	}
	if request.header.Get("Authorization") != "Bearer "+token || request.header.Get("Content-Type") != "application/json" || request.header.Get("Accept") != "application/json" {
		t.Fatal("wire credentials or JSON headers differ")
	}
	declarations := request.header.Values("X-Loom-Session-Protection")
	if declared {
		if !reflect.DeepEqual(declarations, []string{"v1"}) {
			t.Fatalf("declarations = %#v", declarations)
		}
	} else if len(declarations) != 0 {
		t.Fatalf("ordinary client declared protection: %#v", declarations)
	}
}

func protectionAssertLatched(t *testing.T, family *SessionProtection) {
	t.Helper()
	protected, err := family.Protected(protectionContext(t))
	if err != nil || !protected {
		t.Fatalf("protected = %v, error = %v", protected, err)
	}
}

type protectionDecodeCallback struct {
	run func() error
	invoked bool
	result error
}

func (callback *protectionDecodeCallback) UnmarshalJSON([]byte) error {
	callback.invoked = true
	callback.result = callback.run()
	return nil
}

func TestGoProtectedSourceWireAndHumanInvocation(t *testing.T) {
	family := newGoProtection(t)
	wire := newProtectionWire(t, protectionJSON(200, `{"result":{"structuredContent":{"mirrored":true,"text":"synthetic private text"}}}`))
	client := protectionClient(wire, family, "documents", "source-token")
	ctx := protectionContext(t)
	args := map[string]any{"name": "documents", "resource_id": "resource:example"}
	var out map[string]any
	if err := client.Call(ctx, "knowledge.source.mcp_read", args, &out); err != nil {
		t.Fatal(err)
	}
	protectionAssertWire(t, protectionNext(t, wire), "/v1/knowledge.source.mcp_read", "documents", "source-token", args, true)
	protectionAssertLatched(t, family)
	if args["namespace"] != nil {
		t.Fatal("generic Call modified caller arguments")
	}
	resumed, err := OpenSessionProtection(family.Pointer())
	if err != nil {
		t.Fatal(err)
	}
	protectionAssertLatched(t, resumed)
	if _, err := client.Set(ctx, "publication", "automatic", nil); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("typed automatic mutation = %v", err)
	}
	clone := *client
	if err := clone.Call(ctx, "memory.set", protectionMutationArguments(), &out); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("clone mutation = %v", err)
	}
	otherWire := newProtectionWire(t, protectionJSON(200, `{"ok":true}`))
	other := protectionClient(otherWire, resumed, "published", "replacement-token")
	if err := other.Call(ctx, "memory.set", protectionMutationArguments(), &out); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("new origin/token/namespace mutation = %v", err)
	}
	retained := CaptureSession(ctx, family).Context(ctx)
	unbound := protectionClient(otherWire, nil, "published", "replacement-token")
	if err := unbound.Call(retained, "memory.set", protectionMutationArguments(), &out); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("retained capture mutation = %v", err)
	}
	if wire.count.Load() != 1 || otherWire.count.Load() != 0 {
		t.Fatal("refused mutation reached a wire")
	}
	manual := map[string]any{"memory_key": "publication", "value": "human selected text"}
	callback := &protectionDecodeCallback{run: func() error { return other.Call(retained,"memory.set",manual,&out) }}
	if err := other.CallHuman(retained, "memory.set", manual, callback); err != nil {
		t.Fatal(err)
	}
	if !callback.invoked || !errors.Is(callback.result,ErrAutomaticSessionProtected) {
		t.Fatalf("decode callback inherited human authority: %v",callback.result)
	}
	protectionAssertWire(t, protectionNext(t, otherWire), "/v1/memory.set", "published", "replacement-token", manual, true)
	if err := other.Call(retained, "custom.mutate", nil, &out); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("human invocation escaped into retained work: %v", err)
	}
	if err := unbound.Call(ctx, "memory.set", manual, &out); err != nil {
		t.Fatal(err)
	}
	protectionAssertWire(t, protectionNext(t, otherWire), "/v1/memory.set", "published", "replacement-token", manual, false)
}

func TestGoProtectedErrorStatusObservedBeforeAPIError(t *testing.T) {
	family := newGoProtection(t)
	wire := newProtectionWire(t, protectionJSON(403, `{"error":{"code":"FORBIDDEN","message":"refused","data":{"content":[{"type":"text","text":"{\"mirrored\":true}"}]}}}`))
	client := protectionClient(wire, family, "documents", "synthetic-token")
	ctx := protectionContext(t)
	var out map[string]any
	err := client.Call(ctx, "knowledge.source.mcp_read", map[string]any{"name":"documents", "resource_id":"resource:example"}, &out)
	var apiError *Error
	if !errors.As(err, &apiError) || apiError.Status != 403 {
		t.Fatalf("HTTP status error = %v", err)
	}
	protectionAssertLatched(t, family)
	if err := client.Call(ctx, "memory.delete", nil, &out); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("post-error mutation = %v", err)
	}
	if wire.count.Load() != 1 {
		t.Fatal("post-error mutation was sent")
	}
}

func TestGoGenericSourceControlsUseSharedWireBoundary(t *testing.T) {
	family := newGoProtection(t)
	wire := newProtectionWire(t,protectionJSON(200,`{"ok":true}`))
	client := protectionClient(wire,family,"documents","synthetic-token")
	ctx := protectionContext(t)
	for _, control := range []struct{op string; args map[string]any}{
		{"knowledge.connection.mcp_browse",map[string]any{"slot":"documents","mode":"resources"}},
		{"knowledge.source.mcp_list",map[string]any{}},
		{"knowledge.source.mcp_pause",map[string]any{"name":"documents"}},
		{"knowledge.source.mcp_resume",map[string]any{"name":"documents"}},
		{"knowledge.source.mcp_refresh",map[string]any{"name":"documents"}},
		{"knowledge.connection.mcp_disconnect",map[string]any{"slot":"documents"}},
	} {
		var out map[string]any
		if err := client.Call(ctx,control.op,control.args,&out); err!=nil { t.Fatal(err) }
		protectionAssertWire(t,protectionNext(t,wire),"/v1/"+control.op,"documents","synthetic-token",control.args,true)
	}
	protected,err := family.Protected(ctx)
	if err!=nil || protected { t.Fatalf("clean controls did not acknowledge observation: %v, %v",protected,err) }
	if wire.count.Load()!=6 { t.Fatal("control dispatch count differs") }
}

func TestGoBoundFamilyAndRetainedFamilyAreBothEnforced(t *testing.T) {
	ctx := protectionContext(t)
	a, b := newGoProtection(t), newGoProtection(t)
	wire := newProtectionWire(t, protectionJSON(200, `{"mirrored":true}`))
	client := protectionClient(wire, b, "documents", "synthetic-token")
	var out map[string]any
	if err := client.Call(ctx, "knowledge.source.mcp_read", map[string]any{"name":"documents", "resource_id":"resource:example"}, &out); err != nil {
		t.Fatal(err)
	}
	retainedA := CaptureSession(ctx, a).Context(ctx)
	if err := client.Call(retainedA, "memory.set", protectionMutationArguments(), &out); !errors.Is(err, ErrAutomaticSessionProtected) {
		t.Fatalf("clean retained A masked protected bound B: %v", err)
	}
	if wire.count.Load() != 1 {
		t.Fatal("bound-family refusal did not stop dispatch")
	}
	protectedA, err := a.Protected(ctx)
	if err != nil || protectedA {
		t.Fatalf("unused A = %v, %v", protectedA, err)
	}
	c, d := newGoProtection(t), newGoProtection(t)
	contributing := CaptureSession(ctx, c).Context(ctx)
	clientD := protectionClient(wire, d, "documents", "synthetic-token")
	if err := clientD.Call(contributing, "knowledge.source.mcp_read", map[string]any{"name":"documents", "resource_id":"resource:example"}, &out); err != nil {
		t.Fatal(err)
	}
	protectionAssertLatched(t, c)
	protectionAssertLatched(t, d)
	nested := CaptureSession(contributing, a).Context(contributing)
	result := make(chan error, 1)
	go func() { result <- protectionClient(wire, nil, "other", "new-token").Call(nested, "custom.write", nil, &out) }()
	select {
	case err := <-result:
		if !errors.Is(err, ErrAutomaticSessionProtected) {
			t.Fatalf("deferred contributor union = %v", err)
		}
	case <-time.After(3*time.Second):
		t.Fatal("retained task did not finish")
	}
}

func TestGoLostReplacedAndCorruptProofRefusesEvenHuman(t *testing.T) {
	cases := []struct{name string; damage func(*testing.T, SessionProtectionPointer)}{
		{"lost-state", func(t *testing.T, p SessionProtectionPointer) { if err := os.Remove(filepath.Join(p.Path,"state")); err != nil { t.Fatal(err) } }},
		{"lost-manifest", func(t *testing.T, p SessionProtectionPointer) { if err := os.Remove(filepath.Join(p.Path,"identity")); err != nil { t.Fatal(err) } }},
		{"lost-lock", func(t *testing.T, p SessionProtectionPointer) { if err := os.Remove(filepath.Join(p.Path,"lock")); err != nil { t.Fatal(err) } }},
		{"corrupt-state", func(t *testing.T, p SessionProtectionPointer) { if err := os.WriteFile(filepath.Join(p.Path,"state"),[]byte("?"),0600); err != nil { t.Fatal(err) } }},
		{"replaced-identical-state", func(t *testing.T, p SessionProtectionPointer) {
			path := filepath.Join(p.Path,"state")
			if err := os.Rename(path,path+".old"); err != nil { t.Fatal(err) }
			if err := os.WriteFile(path,[]byte("0"),0600); err != nil { t.Fatal(err) }
		}},
		{"replaced-directory", func(t *testing.T, p SessionProtectionPointer) {
			if err := os.Rename(p.Path,p.Path+".old"); err != nil { t.Fatal(err) }
			if err := os.Mkdir(p.Path,0700); err != nil { t.Fatal(err) }
			for _, item := range []struct{name,value string}{{"identity",p.ID},{"state","0"},{"lock",""}} {
				if err := os.WriteFile(filepath.Join(p.Path,item.name),[]byte(item.value),0600); err != nil { t.Fatal(err) }
			}
		}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			family := newGoProtection(t)
			wire := newProtectionWire(t, protectionJSON(200, `{"ok":true}`))
			pointer := family.Pointer()
			test.damage(t, pointer)
			if _, err := OpenSessionProtection(pointer); !errors.Is(err, ErrSessionProtectionUnavailable) {
				t.Fatalf("restore damaged proof = %v", err)
			}
			client := protectionClient(wire, family, "other", "new-token")
			var out map[string]any
			if err := client.CallHuman(protectionContext(t), "memory.set", protectionMutationArguments(), &out); !errors.Is(err, ErrSessionProtectionUnavailable) {
				t.Fatalf("human call with damaged proof = %v", err)
			}
			if wire.count.Load() != 0 {
				t.Fatal("damaged authority declared readiness")
			}
		})
	}
}

func TestGoMalformedOrUnboundedResponsesDoNotPublish(t *testing.T) {
	cases := []struct{name, media, body string}{
		{"invalid-json", "application/json", `{"private":`},
		{"invalid-metadata", "application/json", `{"mirrored":"private"}`},
		{"duplicate-metadata", "application/json", `{"mirrored":true,"mirrored":false}`},
		{"nested-duplicate", "application/json", `{"error":{"data":{"mirrored":true,"mirrored":false}}}`},
		{"non-object", "application/json", `[]`},
		{"large", "application/json", `{"text":"`+strings.Repeat("x",maxProtectedBody)+`"}`},
		{"deep", "application/json", strings.Repeat(`{"result":`,66)+`{}`+strings.Repeat(`}`,66)},
		{"unsupported", "application/octet-stream", "synthetic private bytes"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			family := newGoProtection(t)
			wire := newProtectionWire(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type",test.media)
				w.WriteHeader(500)
				_, _ = io.WriteString(w,test.body)
			})
			out := map[string]any{"unchanged":true}
			err := protectionClient(wire,family,"documents","synthetic-token").Call(protectionContext(t),"knowledge.source.mcp_read",protectionReadArguments(),&out)
			if !errors.Is(err,ErrSessionProtectionUnavailable) || !reflect.DeepEqual(out,map[string]any{"unchanged":true}) {
				t.Fatalf("response was published or failed open: %v",err)
			}
			protectionAssertLatched(t,family)
		})
	}
}

func protectionWait(t *testing.T, done <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(3*time.Second):
		t.Fatalf("%s did not finish",what)
	}
}

func TestGoPendingChunkCancellationClosesAndRetainsProof(t *testing.T) {
	family := newGoProtection(t)
	opened, peerClosed := make(chan struct{}), make(chan struct{})
	wire := newProtectionWire(t,func(w http.ResponseWriter,r *http.Request) {
		defer close(peerClosed)
		w.Header().Set("Content-Type","application/json")
		_, _ = io.WriteString(w,`{"result":`)
		w.(http.Flusher).Flush()
		close(opened)
		select {
		case <-r.Context().Done():
		case <-time.After(2*time.Second):
			t.Error("cancelled response retained its peer connection")
		}
	})
	client := protectionClient(wire,family,"documents","synthetic-token")
	ctx,cancel := context.WithCancel(protectionContext(t))
	defer cancel()
	finished := make(chan error,1)
	out := map[string]any{"unchanged":true}
	go func() { finished <- client.Call(ctx,"knowledge.source.mcp_read",protectionReadArguments(),&out) }()
	protectionWait(t,opened,"chunked response opening")
	var next map[string]any
	if err := client.Call(protectionContext(t),"memory.set",protectionMutationArguments(),&next); !errors.Is(err,ErrAutomaticSessionProtected) {
		t.Fatalf("in-flight pending proof allowed a send: %v",err)
	}
	if wire.count.Load()!=1 {
		t.Fatal("pending request did not refuse another send")
	}
	cancel()
	select {
	case err := <-finished:
		if err==nil || !reflect.DeepEqual(out,map[string]any{"unchanged":true}) {
			t.Fatalf("cancelled response published: %v",err)
		}
	case <-time.After(3*time.Second):
		t.Fatal("cancelled call did not join response cleanup")
	}
	protectionWait(t,peerClosed,"cancelled peer")
	protectionAssertLatched(t,family)
	restored,err := OpenSessionProtection(family.Pointer())
	if err!=nil { t.Fatal(err) }
	protectionAssertLatched(t,restored)
}

func TestGoStreamRefusalAndInterruptedJSONCloseTheWire(t *testing.T) {
	for _, stream := range []bool{true,false} {
		t.Run(fmt.Sprint("stream=",stream),func(t *testing.T) {
			family := newGoProtection(t)
			peerClosed := make(chan struct{})
			wire := newProtectionWire(t,func(w http.ResponseWriter,r *http.Request) {
				defer close(peerClosed)
				if stream {
					w.Header().Set("Content-Type","text/event-stream")
					_, _ = io.WriteString(w,"data: {\"mirrored\":true}\n\n")
					w.(http.Flusher).Flush()
					select {
					case <-r.Context().Done():
					case <-time.After(2*time.Second): t.Error("stream refusal did not close its peer")
					}
				} else {
					w.Header().Set("Content-Type","application/json")
					w.Header().Set("Content-Length","200")
					_, _ = io.WriteString(w,`{"mirrored":`)
					w.(http.Flusher).Flush()
				}
			})
			var out map[string]any
			err := protectionClient(wire,family,"documents","synthetic-token").Call(protectionContext(t),"knowledge.source.mcp_read",protectionReadArguments(),&out)
			if !errors.Is(err,ErrSessionProtectionUnavailable) || out!=nil {
				t.Fatalf("incomplete/stream response = %#v, %v",out,err)
			}
			protectionWait(t,peerClosed,"stream or interrupted peer")
			protectionAssertLatched(t,family)
		})
	}
}

func TestGoCallerRetryAfterPacingRechecksFamily(t *testing.T) {
	family := newGoProtection(t)
	var attempts atomic.Int64
	wire := newProtectionWire(t,func(w http.ResponseWriter,r *http.Request) {
		if attempts.Add(1)==1 {
			w.Header().Set("Retry-After","1")
			protectionJSON(429,`{"error":{"code":"RATE_LIMITED","message":"later"}}`)(w,r)
		} else {
			protectionJSON(200,`{"mirrored":true}`)(w,r)
		}
	})
	client := protectionClient(wire,family,"documents","synthetic-token")
	ctx := protectionContext(t)
	var out map[string]any
	err := client.Call(ctx,"memory.set",protectionMutationArguments(),&out)
	var apiError *Error
	if !errors.As(err,&apiError) || apiError.Status!=429 || apiError.RetryAfter!=time.Second {
		t.Fatalf("pacing response = %v",err)
	}
	if wire.count.Load()!=1 { t.Fatal("Go SDK retried without its caller") }
	// The caller's pacing wait admits an independently completed source read.
	// The next invocation must recheck instead of reusing the first admission.
	if err := client.Call(ctx,"knowledge.source.mcp_read",map[string]any{"name":"documents","resource_id":"resource:example"},&out); err!=nil { t.Fatal(err) }
	if err := client.Call(ctx,"memory.set",protectionMutationArguments(),&out); !errors.Is(err,ErrAutomaticSessionProtected) {
		t.Fatalf("retry after contributing read = %v",err)
	}
	if wire.count.Load()!=2 { t.Fatal("retry bypassed the family barrier") }
}

func TestGoChunkedResponseObservesBeforeTypedConversion(t *testing.T) {
	family := newGoProtection(t)
	opened, release := make(chan struct{}), make(chan struct{})
	wire := newProtectionWire(t,func(w http.ResponseWriter,r *http.Request) {
		w.Header().Set("Content-Type","application/json")
		_, _ = io.WriteString(w,`{"result":`)
		w.(http.Flusher).Flush()
		close(opened)
		select {
		case <-release:
			_, _ = io.WriteString(w,`{"mirrored":true}}`)
		case <-r.Context().Done():
		case <-time.After(2*time.Second): t.Error("chunk fixture release did not arrive")
		}
	})
	client := protectionClient(wire,family,"documents","synthetic-token")
	finished := make(chan error,1)
	var typed int
	ctx := protectionContext(t)
	go func() { finished <- client.Call(ctx,"knowledge.source.mcp_read",protectionReadArguments(),&typed) }()
	protectionWait(t,opened,"first response chunk")
	select {
	case err := <-finished: t.Fatalf("partial response escaped observation: %v",err)
	default:
	}
	close(release)
	select {
	case err := <-finished:
		var apiError *Error
		if !errors.As(err,&apiError) || apiError.Code!=CodeInvalidResponse {
			t.Fatalf("typed conversion failure = %v",err)
		}
	case <-time.After(3*time.Second): t.Fatal("chunked response did not join")
	}
	protectionAssertLatched(t,family)
}

func TestGoProofLostDuringResponseRefusesPublication(t *testing.T) {
	family := newGoProtection(t)
	opened, release := make(chan struct{}), make(chan struct{})
	wire := newProtectionWire(t,func(w http.ResponseWriter,r *http.Request) {
		w.Header().Set("Content-Type","application/json")
		_, _ = io.WriteString(w,`{"result":`)
		w.(http.Flusher).Flush()
		close(opened)
		select {
		case <-release: _, _ = io.WriteString(w,`{"mirrored":true}}`)
		case <-r.Context().Done():
		case <-time.After(2*time.Second): t.Error("proof-loss fixture release did not arrive")
		}
	})
	client := protectionClient(wire,family,"documents","synthetic-token")
	finished := make(chan error,1)
	out := map[string]any{"unchanged":true}
	ctx := protectionContext(t)
	go func() { finished <- client.Call(ctx,"knowledge.source.mcp_read",protectionReadArguments(),&out) }()
	protectionWait(t,opened,"proof-loss response")
	if err := os.Remove(filepath.Join(family.Pointer().Path,"state")); err!=nil { t.Fatal(err) }
	close(release)
	select {
	case err := <-finished:
		if !errors.Is(err,ErrSessionProtectionUnavailable) || !reflect.DeepEqual(out,map[string]any{"unchanged":true}) {
			t.Fatalf("lost proof published a response: %v",err)
		}
	case <-time.After(3*time.Second): t.Fatal("lost-proof call did not join")
	}
	if _,err := OpenSessionProtection(family.Pointer()); !errors.Is(err,ErrSessionProtectionUnavailable) {
		t.Fatalf("lost-proof restore = %v",err)
	}
	if wire.count.Load()!=1 { t.Fatal("unexpected proof-loss request count") }
}

type protectionCustomEncoder struct { calls *atomic.Int64 }

func (encoder protectionCustomEncoder) MarshalJSON() ([]byte,error) {
	encoder.calls.Add(1)
	return []byte(`{"encoded":true}`),nil
}

func TestGoProtectedRequestBoundBeforeCustomEncodingOrSend(t *testing.T) {
	family := newGoProtection(t)
	wire := newProtectionWire(t,protectionJSON(200,`{"ok":true}`))
	client := protectionClient(wire,family,"documents","synthetic-token")
	ctx := protectionContext(t)
	var out map[string]any
	var encodes atomic.Int64
	for _, args := range []map[string]any{
		{"value":strings.Repeat("x",maxProtectedBody)},
		{"value":protectionCustomEncoder{calls:&encodes}},
	} {
		if err := client.Call(ctx,"memory.set",args,&out); !errors.Is(err,ErrSessionProtectionUnavailable) {
			t.Fatalf("unbounded/custom request = %v",err)
		}
	}
	if encodes.Load()!=0 || wire.count.Load()!=0 { t.Fatal("bounded admission invoked a custom encoder or sent bytes") }
	ordinary := protectionClient(wire,nil,"documents","synthetic-token")
	if err := ordinary.Call(ctx,"memory.set",map[string]any{"value":protectionCustomEncoder{calls:&encodes}},&out); err!=nil { t.Fatal(err) }
	if encodes.Load()!=1 { t.Fatal("ordinary custom encoder behavior changed") }
	request := protectionNext(t,wire)
	if len(request.header.Values("X-Loom-Session-Protection"))!=0 { t.Fatal("ordinary custom encoder declared protection") }
}

func TestGoProtectionResumeInAnotherProcess(t *testing.T) {
	const probe = "LOOM_GO_PROTECTION_RESUME_PROBE"
	if encoded := os.Getenv(probe); encoded!="" {
		var pointer SessionProtectionPointer
		if err := json.Unmarshal([]byte(encoded),&pointer); err!=nil { t.Fatal(err) }
		family,err := OpenSessionProtection(pointer)
		if err!=nil { t.Fatal(err) }
		client := New(Config{BaseURL:os.Getenv("LOOM_GO_PROTECTION_RESUME_URL"),SessionProtection:family,Namespace:"changed",APIKey:"changed"})
		var out map[string]any
		ctx,cancel := context.WithTimeout(context.Background(),3*time.Second)
		defer cancel()
		if err := client.Call(ctx,"memory.set",protectionMutationArguments(),&out); !errors.Is(err,ErrAutomaticSessionProtected) { t.Fatalf("resumed process mutation = %v",err) }
		return
	}
	family := newGoProtection(t)
	wire := newProtectionWire(t,protectionJSON(200,`{"mirrored":true}`))
	var out map[string]any
	if err := protectionClient(wire,family,"documents","synthetic-token").Call(protectionContext(t),"knowledge.source.mcp_read",protectionReadArguments(),&out); err!=nil { t.Fatal(err) }
	encoded,err := json.Marshal(family.Pointer())
	if err!=nil { t.Fatal(err) }
	ctx,cancel := context.WithTimeout(context.Background(),5*time.Second)
	defer cancel()
	child := exec.CommandContext(ctx,os.Args[0],"-test.run=^TestGoProtectionResumeInAnotherProcess$")
	child.Env = append(os.Environ(),probe+"="+string(encoded),"LOOM_GO_PROTECTION_RESUME_URL="+wire.server.URL)
	if output,err := child.CombinedOutput(); err!=nil { t.Fatalf("resume probe: %v\n%s",err,output) }
	if wire.count.Load()!=1 { t.Fatal("resumed process sent an automatic mutation") }
}
