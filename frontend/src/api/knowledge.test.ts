import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadKnowledge, fetchKnowledgeInlineUrl, listKnowledge, previewKind } from "./knowledge";
import { ApiError } from "../lib/errors";

// knowledge.download is a GET the browser then saves; it rides the shared request path,
// so its failures come back as the same ApiError every post() throws: code and request
// id kept, and an object `detail` never stringified into the message.

const id = { org: "dev", namespace: "team" };

function stubFetch(status: number, body: string, headers: Record<string, string> = {}) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => new Response(body, { status, headers }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function rejection(p: Promise<unknown>): Promise<ApiError> {
  const err: unknown = await p.then(
    () => undefined,
    (e: unknown) => e,
  );
  if (!(err instanceof ApiError)) throw new Error(`expected an ApiError, got ${String(err)}`);
  return err;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("fetchKnowledgeInlineUrl", () => {
  it("answers null when the document has no preserved blob", async () => {
    stubFetch(404, '{"detail":{"code":"NOT_FOUND","message":"no blob"}}');
    await expect(fetchKnowledgeInlineUrl(id, "doc://a")).resolves.toBeNull();
  });

  it("throws an ApiError carrying the code and request id of an object detail", async () => {
    stubFetch(403, '{"detail":{"code":"AUTH_INSUFFICIENT"}}', { "X-Request-Id": "req-1" });
    const err = await rejection(fetchKnowledgeInlineUrl(id, "doc://a"));
    expect(err.status).toBe(403);
    expect(err.code).toBe("AUTH_INSUFFICIENT");
    expect(err.requestId).toBe("req-1");
    expect(err.message).not.toContain("[object Object]");
  });
});

describe("downloadKnowledge", () => {
  it("throws an ApiError for an object detail with no message", async () => {
    stubFetch(403, '{"detail":{"code":"AUTH_INSUFFICIENT"}}', { "X-Request-Id": "req-2" });
    const err = await rejection(downloadKnowledge(id, "doc://a", "a.md"));
    expect(err.code).toBe("AUTH_INSUFFICIENT");
    expect(err.requestId).toBe("req-2");
    expect(err.message).not.toContain("[object Object]");
  });

  it("keeps the server's message when the detail has one", async () => {
    stubFetch(403, '{"detail":{"code":"AUTH_INSUFFICIENT","message":"principal cannot access dev/team"}}');
    const err = await rejection(downloadKnowledge(id, "doc://a", "a.md"));
    expect(err.message).toBe("principal cannot access dev/team");
  });

  it("throws an ApiError with the status for a proxy's HTML 502", async () => {
    stubFetch(502, "<html><body>Bad Gateway</body></html>", {
      "Content-Type": "text/html",
      "X-Request-Id": "req-3",
    });
    const err = await rejection(downloadKnowledge(id, "doc://a", "a.md"));
    expect(err.status).toBe(502);
    expect(err.code).toBe("");
    expect(err.requestId).toBe("req-3");
  });
});

describe("downloadKnowledge success", () => {
  function captureDownload() {
    const createObjectURL = vi.fn((_b: Blob) => "blob:doc-1");
    const revokeObjectURL = vi.fn((_u: string) => undefined);
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = createObjectURL;
        static revokeObjectURL = revokeObjectURL;
      },
    );
    const saved: Array<{ href: string; download: string }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push({ href: this.href, download: this.download });
    });
    return { createObjectURL, revokeObjectURL, saved };
  }

  it("saves under the server's Content-Disposition filename and revokes the blob URL", async () => {
    vi.useFakeTimers();
    const fetchMock = stubFetch(200, "# notes", { "Content-Disposition": 'attachment; filename="report.pdf"' });
    const { createObjectURL, revokeObjectURL, saved } = captureDownload();
    await downloadKnowledge(id, "doc://a", "a.md");
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/knowledge.download?org=dev&namespace=team&source_uri=doc%3A%2F%2Fa");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(saved).toEqual([{ href: "blob:doc-1", download: "report.pdf" }]);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.runAllTimers();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:doc-1");
  });

  it("falls back to the caller's filename when the server sends none", async () => {
    stubFetch(200, "# notes");
    const { saved } = captureDownload();
    await downloadKnowledge(id, "doc://a", "a.md");
    expect(saved.map((s) => s.download)).toEqual(["a.md"]);
  });
});

describe("previewKind", () => {
  it("previews only passive text and raster image types", () => {
    for (const t of ["text/plain", "text/plain; charset=utf-8", "TEXT/MARKDOWN", "text/csv", "application/json"])
      expect(previewKind(t)).toBe("text");
    for (const t of ["image/png", "image/jpeg", "image/gif", "image/webp"]) expect(previewKind(t)).toBe("image");
  });

  it("downloads active, unknown and document types", () => {
    for (const t of [
      "text/html",
      "image/svg+xml",
      "application/xhtml+xml",
      "application/xml",
      "text/xml",
      "application/pdf",
      "application/octet-stream",
      "text/javascript",
      "",
      "garbage",
    ])
      expect(previewKind(t)).toBe("download");
  });
});

describe("fetchKnowledgeInlineUrl re-types the blob", () => {
  function captureBlobs() {
    const blobs: Blob[] = [];
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = vi.fn((b: Blob) => {
          blobs.push(b);
          return `blob:p-${blobs.length}`;
        });
        static revokeObjectURL = vi.fn();
      },
    );
    return blobs;
  }

  it("reads text as a string and makes no URL for it", async () => {
    stubFetch(200, "<script>alert(1)</script>", { "Content-Type": "text/markdown" });
    const blobs = captureBlobs();
    const p = await fetchKnowledgeInlineUrl(id, "doc://a");
    expect(p).toMatchObject({ kind: "text", url: null, text: "<script>alert(1)</script>" });
    expect(blobs).toEqual([]);
  });

  it("gives an image its own allowlisted type", async () => {
    stubFetch(200, "x", { "Content-Type": "image/png; foo=bar" });
    const blobs = captureBlobs();
    expect(await fetchKnowledgeInlineUrl(id, "doc://a")).toMatchObject({ kind: "image", url: "blob:p-1" });
    expect(blobs[0].type).toBe("image/png");
  });

  it("makes no URL for an active or unknown type", async () => {
    for (const t of ["text/html", "image/svg+xml", "application/pdf", "application/octet-stream"]) {
      stubFetch(200, "<svg onload=alert(1)>", { "Content-Type": t });
      const blobs = captureBlobs();
      expect(await fetchKnowledgeInlineUrl(id, "doc://a")).toMatchObject({ kind: "download", url: null });
      expect(blobs).toEqual([]);
    }
  });
});

describe("downloadKnowledge blob", () => {
  it("is application/octet-stream so the blob: URL never renders", async () => {
    stubFetch(200, "<html><script>alert(1)</script>", { "Content-Type": "text/html" });
    const blobs: Blob[] = [];
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL = vi.fn((b: Blob) => (blobs.push(b), "blob:d"));
        static revokeObjectURL = vi.fn();
      },
    );
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    await downloadKnowledge(id, "doc://a", "a.html");
    expect(blobs.map((b) => b.type)).toEqual(["application/octet-stream"]);
  });
});

describe("listKnowledge", () => {
  it("sends a uri prefix as uri_prefix, and none when unset", async () => {
    const fetchMock = stubFetch(200, '{"rows":[],"next_cursor":null}', { "Content-Type": "application/json" });
    await listKnowledge(id, { uriPrefix: ["docs/late.md"] });
    await listKnowledge(id);
    const bodies = fetchMock.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies[0].uri_prefix).toEqual(["docs/late.md"]);
    expect(bodies[1]).not.toHaveProperty("uri_prefix");
  });
});
