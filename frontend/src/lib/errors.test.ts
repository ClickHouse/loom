import { describe, expect, it } from "vitest";
import { ApiError, apiErrorFromResponse, parseErrorBody } from "./errors";

// Every failed request becomes one typed error, read from the server's `{"detail":
// {"code", "message", …}}` body and the X-Request-Id header — the raw JSON never becomes
// the message.

function response(status: number, body: string, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers });
}

describe("parseErrorBody", () => {
  it("reads Loom's detail object", () => {
    const p = parseErrorBody(
      '{"detail":{"code":"AUTH_INSUFFICIENT","message":"principal cannot access dev/x","extra":1}}',
    );
    expect(p.code).toBe("AUTH_INSUFFICIENT");
    expect(p.message).toBe("principal cannot access dev/x");
    expect(p.detail.extra).toBe(1);
  });

  it("reads a string detail (FastAPI default)", () => {
    expect(parseErrorBody('{"detail":"Not Found"}')).toMatchObject({ code: "", message: "Not Found" });
  });

  it("flattens FastAPI's request-validation list", () => {
    const p = parseErrorBody('{"detail":[{"loc":["body","namespace"],"msg":"field required","type":"missing"}]}');
    expect(p.code).toBe("VALIDATION_FAILED");
    expect(p.message).toBe("namespace: field required");
  });

  it("skips validation items that are not objects and non-string msgs", () => {
    const p = parseErrorBody(
      JSON.stringify({
        detail: [null, "x", { loc: ["body", "g"], msg: { nested: 1 } }, { loc: ["body", "f"], msg: "bad" }],
      }),
    );
    expect(p.code).toBe("VALIDATION_FAILED");
    expect(p.message).toBe("g: ; f: bad");
  });

  it("drops a validation item with neither a field nor a string msg", () => {
    const p = parseErrorBody(JSON.stringify({ detail: [null, "x", { msg: 7 }, { loc: ["body", "f"], msg: "bad" }] }));
    expect(p.message).toBe("f: bad");
  });

  it("keeps a short plain-text body and drops HTML", () => {
    expect(parseErrorBody("upstream request timeout").message).toBe("upstream request timeout");
    expect(parseErrorBody("<html><body>502 Bad Gateway</body></html>").message).toBe("");
    expect(parseErrorBody("").message).toBe("");
  });

  it("never returns a JSON body as the message", () => {
    const p = parseErrorBody('{"unexpected":{"nested":true}}');
    expect(p.message).toBe("");
    expect(p.message).not.toContain("{");
  });

  it("reads a JSON array body as carrying nothing", () => {
    expect(parseErrorBody('[{"message":"x","code":"Y"}]')).toEqual({ code: "", message: "", detail: {} });
    expect(parseErrorBody("null")).toEqual({ code: "", message: "", detail: {} });
  });
});

describe("apiErrorFromResponse", () => {
  it("carries status, code, message and the request id", async () => {
    const err = await apiErrorFromResponse(
      response(403, '{"detail":{"code":"AUTH_INSUFFICIENT","message":"admin endpoints require ADMIN role"}}', {
        "X-Request-Id": "req-123",
      }),
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(403);
    expect(err.code).toBe("AUTH_INSUFFICIENT");
    expect(err.message).toBe("admin endpoints require ADMIN role");
    expect(err.requestId).toBe("req-123");
  });

  it("falls back to `HTTP <status>` when the body says nothing", async () => {
    const err = await apiErrorFromResponse(response(502, "<html>bad gateway</html>"));
    expect(err.message).toBe("HTTP 502");
    expect(err.code).toBe("");
    expect(err.requestId).toBe("");
  });
});
