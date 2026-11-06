import { createHash } from "node:crypto";
import { describe, expect, expectTypeOf, it } from "vitest";
import { LoomClient, type OpOutput } from "./client.js";
import { RateLimited, TimeoutError } from "./errors.js";
import {
  type FileData,
  INGEST_DONE,
  type IngestFileInput,
  type KnowledgeFiles,
  type UploadOptions,
  type WaitOptions,
} from "./files.js";
import { Transport } from "./transport.js";

interface Sent {
  url: URL;
  init: RequestInit;
}

/**
 * A scripted server: what each route was sent; an upload refused `refused`
 * times with a 429; jobs that end in `state` after `running` polls.
 */
interface Script {
  state?: string;
  running?: number;
  /** Uploads answered 429 before one is stored. */
  refused?: number;
  /** Uploads answered this status instead (once each, then stored). */
  failUpload?: Array<number | "connect">;
  ratelimitMaxWaitS?: number;
}

function server(opts: Script = {}) {
  const { state = "ready", running = 1 } = opts;
  let refused = opts.refused ?? 0;
  const failUpload = [...(opts.failUpload ?? [])];
  const sent: Sent[] = [];
  const slept: number[] = [];
  const fetch = async (url: string, init: RequestInit) => {
    const u = new URL(url);
    sent.push({ url: u, init });
    const op = u.pathname.replace("/v1/", "");
    const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
      new Response(JSON.stringify(body), { status, headers });
    if (op === "knowledge.upload") {
      const fail = failUpload.shift();
      if (fail === "connect") throw new TypeError("fetch failed");
      if (fail !== undefined) return json({ detail: { code: "INTERNAL" } }, fail);
      if (refused > 0) {
        refused--;
        return json({ detail: { code: "RATE_LIMITED" } }, 429, { "Retry-After": "2" });
      }
      return json({ upload_id: "up-1", format: "markdown" }, 201);
    }
    if (op === "knowledge.ingest") return json({ ingestion_id: "job-1", state: "pending", source_uri: "x" });
    if (op === "knowledge.ingest_status") {
      if (state === "gone") return json({ found: false, ingestion_id: "job-1" });
      const polls = sent.filter((s) => s.url.pathname.endsWith("ingest_status")).length;
      return json({ found: true, state: polls <= running ? "processing" : state, revision: 2 });
    }
    return json({});
  };
  const sleep = async (ms: number) => {
    slept.push(ms);
  };
  const loom = new LoomClient(
    new Transport({
      baseUrl: "http://loom.test",
      apiKey: "k",
      fetch,
      sleep,
      ratelimitMaxWaitS: opts.ratelimitMaxWaitS,
    }),
  );
  const body = (op: string) => {
    const hit = sent.filter((s) => s.url.pathname === `/v1/${op}`);
    return hit.map((s) => JSON.parse(s.init.body as string) as Record<string, unknown>);
  };
  return { loom, sent, slept, body };
}

const scope = { org: "acme", namespace: "support" };

describe("the file types", () => {
  it("are what loom.knowledge takes and answers", () => {
    const { loom } = server();
    expectTypeOf(loom.knowledge).toMatchTypeOf<KnowledgeFiles>();
    expectTypeOf<Uint8Array>().toMatchTypeOf<FileData>();
    expectTypeOf<Blob>().toMatchTypeOf<FileData>();
    expectTypeOf<{ contentType: string; timeoutMs: number }>().toMatchTypeOf<UploadOptions>();
    expectTypeOf<{ timeoutMs: number; pollMs: number }>().toMatchTypeOf<WaitOptions>();
    expectTypeOf<IngestFileInput>().toHaveProperty("waitTimeoutMs");
    expectTypeOf<IngestFileInput>().not.toHaveProperty("upload_id");
  });
});

describe("knowledge.upload", () => {
  it("sends the bytes as they are with their sha256, as octet-stream", async () => {
    const { loom, sent } = server();
    const data = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x00, 0xff]);
    const up = await loom.knowledge.upload(scope, data, "spec.pdf", { contentType: "application/pdf" });
    expect(up.upload_id).toBe("up-1");
    const [req] = sent;
    expect(req.init.method).toBe("POST");
    expect(new Uint8Array(req.init.body as Uint8Array)).toEqual(data);
    const headers = req.init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/octet-stream");
    expect(headers.Authorization).toBe("Bearer k");
    expect(Object.fromEntries(req.url.searchParams)).toEqual({
      org: "acme",
      namespace: "support",
      filename: "spec.pdf",
      content_type: "application/pdf",
      sha256: createHash("sha256").update(data).digest("hex"),
    });
  });

  it("takes a Blob or an ArrayBuffer, and a view of part of a buffer as just those bytes", async () => {
    const { loom, sent } = server();
    await loom.knowledge.upload(scope, new Blob(["# Notes"]), "notes.md");
    await loom.knowledge.upload(scope, new TextEncoder().encode("# Two").buffer as ArrayBuffer, "two.md");
    const part = new Uint8Array(new SharedArrayBuffer(8)).subarray(2, 5);
    part.set([1, 2, 3]);
    await loom.knowledge.upload(scope, part, "part.bin");
    const bodies = sent.map((s) => new TextDecoder().decode(s.init.body as Uint8Array));
    expect(bodies.slice(0, 2)).toEqual(["# Notes", "# Two"]);
    expect(Array.from(sent[2].init.body as Uint8Array)).toEqual([1, 2, 3]);
  });

  it("is sent again after a 429's hint, as an op is, and past the pacing budget raises RateLimited", async () => {
    const paced = server({ refused: 2 });
    await paced.loom.knowledge.upload(scope, new Uint8Array([1]), "n.txt");
    expect(paced.sent.length).toBe(3);
    expect(paced.slept).toEqual([2000, 2000]);

    const spent = server({ refused: 100, ratelimitMaxWaitS: 0 });
    const refused = spent.loom.knowledge.upload(scope, new Uint8Array([1]), "n.txt");
    await expect(refused).rejects.toBeInstanceOf(RateLimited);
    expect(spent.slept).toEqual([]);
  });
});

describe("knowledge.upload retries", () => {
  it("is sent once on a 503 (the server may have stored it), and again after a connection failure", async () => {
    const answered = server({ failUpload: [503] });
    await expect(answered.loom.knowledge.upload(scope, new Uint8Array([1]), "n.txt")).rejects.toMatchObject({
      status: 503,
    });
    expect(answered.sent.length).toBe(1);

    const unreached = server({ failUpload: ["connect"] });
    const up = await unreached.loom.knowledge.upload(scope, new Uint8Array([1]), "n.txt");
    expect(up.upload_id).toBe("up-1");
    expect(unreached.sent.length).toBe(2);
  });
});

describe("knowledge.waitForIngest", () => {
  it("polls until the job ends", async () => {
    const { loom, sent } = server({ state: "partial", running: 3 });
    const status = await loom.knowledge.waitForIngest(scope, "job-1", { pollMs: 0 });
    expect([status.state, status.revision]).toEqual(["partial", 2]);
    expect(sent.length).toBe(4);
  });

  it("names the four states a job ends in", () => {
    expect([...INGEST_DONE].sort()).toEqual(["cancelled", "failed", "partial", "ready"]);
  });

  it("stops at an abort, with the transport's aborted-by-caller error", async () => {
    const { loom, sent } = server({ running: 1_000 });
    const ctrl = new AbortController();
    const wait = loom.knowledge.waitForIngest(scope, "job-1", { pollMs: 60_000, signal: ctrl.signal });
    await new Promise((r) => setTimeout(r, 10));
    ctrl.abort();
    await expect(wait).rejects.toThrow(/knowledge.ingest_status: aborted by caller/);
    expect(sent.length).toBe(1); // the 60 s sleep was cut short, not waited out
  });

  it("returns at once for a job not found", async () => {
    const { loom, sent } = server({ state: "gone" });
    const status = await loom.knowledge.waitForIngest(scope, "job-1");
    expect(status.found).toBe(false);
    expect(sent.length).toBe(1);
  });

  it("throws TimeoutError while the job still runs", async () => {
    const { loom } = server({ running: 1_000 });
    const wait = loom.knowledge.waitForIngest(scope, "job-1", { timeoutMs: 0 });
    await expect(wait).rejects.toBeInstanceOf(TimeoutError);
    await expect(loom.knowledge.waitForIngest(scope, "job-1", { timeoutMs: 0 })).rejects.toThrow(
      /job-1 is still processing/,
    );
  });
});

describe("knowledge.ingestFile", () => {
  it("uploads, ingests and waits, titled by the file name less its extension", async () => {
    const { loom, body } = server();
    const status = await loom.knowledge.ingestFile({
      ...scope,
      data: new TextEncoder().encode("# Runbook"),
      filename: "runbook.md",
      source_uri: "repo://runbook.md",
      tags: ["ops"],
      pollMs: 0,
    });
    expect(status.state).toBe("ready");
    const [ingest] = body("knowledge.ingest");
    expect(ingest).toMatchObject({
      org: "acme",
      namespace: "support",
      upload_id: "up-1",
      source_uri: "repo://runbook.md",
      title: "runbook",
      tags: ["ops"],
    });
    expect(ingest).not.toHaveProperty("data");
    expect(ingest).not.toHaveProperty("filename");
  });

  it("types its answer by wait: the status, the job, or either when wait is not a literal", () => {
    const { loom } = server();
    const input = { ...scope, data: new Uint8Array([1]), filename: "a.md", source_uri: "repo://a" };
    const follow = Math.random() < 2;
    expectTypeOf(loom.knowledge.ingestFile(input)).resolves.toHaveProperty("found");
    expectTypeOf(loom.knowledge.ingestFile({ ...input, wait: false })).resolves.toHaveProperty("ingestion_id");
    expectTypeOf(loom.knowledge.ingestFile({ ...input, wait: follow })).resolves.toEqualTypeOf<
      OpOutput<"knowledge.ingest_status"> | OpOutput<"knowledge.ingest">
    >();
  });

  it("with wait: false returns the job, and keeps a title given", async () => {
    const { loom, body, sent } = server();
    const job = await loom.knowledge.ingestFile({
      ...scope,
      data: new Uint8Array([1]),
      filename: "a.b.csv",
      source_uri: "repo://a",
      title: "Orders",
      wait: false,
      call: { requestId: "ingest-a" },
    });
    expect(job.ingestion_id).toBe("job-1");
    expect(body("knowledge.ingest")[0].title).toBe("Orders");
    expect(body("knowledge.ingest")[0]).not.toHaveProperty("call");
    expect(sent.some((s) => s.url.pathname.endsWith("ingest_status"))).toBe(false);
    // The call options reach the upload and the ingest.
    const ids = sent.map((s) => (s.init.headers as Record<string, string>)["X-Request-Id"]);
    expect(ids).toEqual(["ingest-a", "ingest-a"]);
  });
});
