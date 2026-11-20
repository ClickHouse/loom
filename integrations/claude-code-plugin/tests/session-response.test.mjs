import "./temp-home.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, test } from "node:test";

import { requestJson } from "../lib/loom-client.mjs";
import { createProtectionRoot, protectSession, sessionIsProtected } from "../lib/session-protection.mjs";
import {
  observeSessionResponse,
  withAutomaticSessionProtection,
  withSessionProtection,
} from "../lib/session-response.mjs";

const owned = new Set();
function fixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "loom-response-test-"));
  owned.add(parent);
  const base = process.platform === "win32" ? path.join(parent, "private") : parent;
  const root = createProtectionRoot({ base });
  return { root, base };
}
afterEach(() => {
  for (const base of owned) fs.rmSync(base, { recursive: true, force: true });
  owned.clear();
});

const response = (body) => async () => ({ status: 200, json: async () => body });

test("automatic contexts refuse missing protection roots", () => {
  let ran = false;
  withAutomaticSessionProtection({ base: path.resolve("missing"), id: "00000000-0000-0000-0000-000000000000" }, () => {
    ran = true;
  });
  assert.equal(ran, false);
});
{
  test("a marked automatic response protects every contributor before withholding its body", async () => {
    const { root, base } = fixture();
    const contributor = createProtectionRoot({ base });
    const rendered = [];
    let sends = 0;
    const fetchImpl = async () => {
      sends++;
      return { status: 200, json: async () => ({ mirrored: true, answer: "private answer" }) };
    };
    await withAutomaticSessionProtection(root, () =>
      withAutomaticSessionProtection(contributor, async () => {
        const first = await requestJson("http://server/v1/context.pack", { fetchImpl });
        if (first.body) rendered.push(first.body.answer);
        assert.deepEqual(rendered, []);
        assert.equal(first.status, 0);
        assert.equal(first.body, null);
        assert.equal(first.error, "Session protection state could not be confirmed");
        assert.equal(sessionIsProtected(root), true);
        assert.equal(sessionIsProtected(contributor), true);
        const later = await requestJson("http://other/v1/code.repos", { fetchImpl, token: "other-token" });
        assert.deepEqual(later, { status: 0, body: null, suppressed: true });
      }),
    );
    assert.equal(sends, 1);
    const explicit = await withSessionProtection(root, () =>
      requestJson("http://other/v1/memory.set", { fetchImpl: response({ version: 2 }) }),
    );
    assert.equal(explicit.body.version, 2);
    assert.equal(sessionIsProtected(root), true);
  });

  for (const body of [
    { result: { structuredContent: { mirrored: true, items: [] } } },
    { content: [{ type: "text", text: '{"mirrored":true,"found":true}' }] },
    { items: [{ found: true, mirrored: true }] },
    { jsonrpc: "2.0", id: 1, error: { code: -32000, data: { mirrored: true, code: "REFUSED" } } },
    { mirrored: true, code: "REFUSED" },
  ]) {
    test(`canonical response envelopes protect the same family (${JSON.stringify(body)})`, () => {
      const { root } = fixture();
      assert.equal(observeSessionResponse(root, body), body);
      assert.equal(sessionIsProtected(root), true);
    });
  }

  test("a marked JSON-RPC error is withheld before its automatic consumer and blocks the next send", async () => {
    const { root } = fixture();
    const body = { jsonrpc: "2.0", id: 7, error: { code: -32000, data: { mirrored: true, code: "REFUSED" } } };
    let sends = 0;
    await withAutomaticSessionProtection(root, async () => {
      const reply = await requestJson("http://server/mcp", {
        fetchImpl: async () => {
          sends++;
          return { status: 403, json: async () => body };
        },
      });
      assert.equal(reply.status, 0);
      assert.equal(reply.body, null);
      assert.equal(reply.error, "Session protection state could not be confirmed");
      assert.equal(sessionIsProtected(root), true);
      const later = await requestJson("http://server/v1/code.repos", {
        fetchImpl: async () => {
          sends++;
          return { status: 200, json: async () => ({ repos: [] }) };
        },
      });
      assert.deepEqual(later, { status: 0, body: null, suppressed: true });
    });
    assert.equal(sends, 1);
  });

  test("a clean automatic response remains available to its consumer", async () => {
    const { root } = fixture();
    const body = { answer: "ordinary" };
    const reply = await withAutomaticSessionProtection(root, () =>
      requestJson("http://server/v1/context.pack", { fetchImpl: response(body) }),
    );
    assert.equal(reply.status, 200);
    assert.equal(reply.body, body);
    assert.equal(sessionIsProtected(root), false);
  });

  test("an explicit marked response remains available after durable observation", async () => {
    const { root } = fixture();
    const body = { mirrored: true, answer: "explicit private answer" };
    const reply = await withSessionProtection(root, () =>
      requestJson("http://server/v1/context.pack", { fetchImpl: response(body) }),
    );
    assert.equal(reply.status, 200);
    assert.equal(reply.body, body);
    assert.equal(sessionIsProtected(root), true);
  });

  test("protection during response decoding withholds even an unmarked automatic body", async () => {
    const { root } = fixture();
    const rendered = [];
    await withAutomaticSessionProtection(root, async () => {
      const reply = await requestJson("http://server/v1/context.pack", {
        fetchImpl: async () => ({
          status: 200,
          json: async () => {
            protectSession(root);
            return { answer: "unconfirmed" };
          },
        }),
      });
      if (reply.body) rendered.push(reply.body.answer);
      assert.deepEqual(rendered, []);
      assert.equal(reply.status, 0);
      assert.equal(reply.body, null);
      assert.equal(reply.error, "Session protection state could not be confirmed");
      assert.equal(sessionIsProtected(root), true);
    });
  });

  test("model arguments do not become response envelopes or choose protection identity", () => {
    const { root } = fixture();
    const input = { arguments: { mirrored: true, session_id: "another-session" }, answer: "ordinary" };
    assert.equal(observeSessionResponse(root, input), input);
    assert.equal(sessionIsProtected(root), false);
  });

  test("parallel unrelated contexts retain separate state while siblings share a mark", async () => {
    const { root, base } = fixture();
    const unrelated = createProtectionRoot({ base });
    await Promise.all([
      withSessionProtection(root, () =>
        requestJson("http://first/v1/knowledge.get", { fetchImpl: response({ mirrored: true }) }),
      ),
      withSessionProtection(unrelated, () =>
        requestJson("http://second/v1/knowledge.get", { fetchImpl: response({ found: true }) }),
      ),
    ]);
    let siblingRan = false;
    withAutomaticSessionProtection(root, () => {
      siblingRan = true;
    });
    assert.equal(siblingRan, false);
    assert.equal(sessionIsProtected(unrelated), false);
  });

  test("taint during an automatic callback's await prevents its later dispatch", async () => {
    const { root } = fixture();
    let resume;
    const waiting = new Promise((resolve) => {
      resume = resolve;
    });
    let sends = 0;
    const pending = withAutomaticSessionProtection(root, async () => {
      await waiting;
      return requestJson("http://server/v1/conversation.append", {
        fetchImpl: async () => {
          sends++;
          return { status: 200, json: async () => ({ stored: true }) };
        },
      });
    });
    protectSession(root);
    resume();
    assert.deepEqual(await pending, { status: 0, body: null, suppressed: true });
    assert.equal(sends, 0);
  });

  test("a failed durable mark withholds the response and leaves automatic capture suppressed", async () => {
    const { root, base } = fixture();
    fs.unlinkSync(path.join(base, root.id, "state"));
    const reply = await withSessionProtection(root, () =>
      requestJson("http://server/v1/knowledge.get", {
        fetchImpl: response({ mirrored: true, text: "must not escape" }),
      }),
    );
    assert.equal(reply.status, 0);
    assert.equal(reply.body, null);
    assert.equal(reply.error, "Session protection state could not be confirmed");
    let automaticRan = false;
    withAutomaticSessionProtection(root, () => {
      automaticRan = true;
    });
    assert.equal(automaticRan, false);
  });

  test("a malformed flag or truncated MCP envelope is withheld without exposing its text", async () => {
    const { root } = fixture();
    for (const body of [{ mirrored: "true" }, { content: [{ type: "text", text: '{"mirrored":true' }] }]) {
      const reply = await withSessionProtection(root, () =>
        requestJson("http://server/v1/knowledge.get", { fetchImpl: response(body) }),
      );
      assert.equal(reply.status, 0);
      assert.equal(reply.body, null);
      assert.equal(reply.error, "Session protection state could not be confirmed");
    }
  });

  test("unbound requests make no session-protection declaration", async () => {
    let headers;
    await requestJson("http://server/v1/context.pack", {
      fetchImpl: async (_url, options) => {
        headers = options.headers;
        return { status: 200, json: async () => ({ answer: "ordinary" }) };
      },
    });
    assert.equal(
      Object.keys(headers).some((name) => name.toLowerCase() === "x-loom-session-protection"),
      false,
    );
  });

  test("repeated moderate MCP blocks share one decoding budget", async () => {
    const { root } = fixture();
    const block = { type: "text", text: JSON.stringify({ answer: "x".repeat(256 * 1024) }) };
    const reply = await withSessionProtection(root, () =>
      requestJson("http://server/v1/context.pack", { fetchImpl: response({ content: Array(32).fill(block) }) }),
    );
    assert.equal(reply.status, 0);
    assert.equal(reply.body, null);
    assert.equal(reply.error, "Session protection state could not be confirmed");
  });

  test("excessive response fan-out and cyclic envelopes refuse without output", () => {
    const { root } = fixture();
    const cyclic = {};
    cyclic.result = cyclic;
    for (const body of [cyclic, { items: Array(20000).fill({ found: false }) }]) {
      assert.throws(() => observeSessionResponse(root, body), {
        message: "Session protection state could not be confirmed",
      });
    }
  });

  test("nested non-text blocks share the observation work budget", () => {
    const { root } = fixture();
    const blocks = Array(4999).fill({ type: "image" });
    const body = { items: Array.from({ length: 5000 }, () => ({ content: blocks })) };
    assert.throws(() => observeSessionResponse(root, body), {
      message: "Session protection state could not be confirmed",
    });
  });

  test("a nested explicit operation restores its parent's automatic dispatch guard", async () => {
    const { root } = fixture();
    await withAutomaticSessionProtection(root, async () => {
      protectSession(root);
      const explicit = await withSessionProtection(root, () =>
        requestJson("http://server/v1/memory.set", { fetchImpl: response({ version: 3 }) }),
      );
      assert.equal(explicit.body.version, 3);
      let sends = 0;
      const next = await requestJson("http://server/v1/code.repos", {
        fetchImpl: async () => {
          sends++;
          return { status: 200, json: async () => ({ repos: [] }) };
        },
      });
      assert.equal(sends, 0);
      assert.deepEqual(next, { status: 0, body: null, suppressed: true });
    });
  });
}
