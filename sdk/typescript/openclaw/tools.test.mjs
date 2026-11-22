import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { createTools, parseConfig } from "./tools.mjs";

const input = {
  baseUrl: "https://loom.example.org",
  tokenEnv: "LOOM_OPENCLAW_TOKEN",
  org: "example",
  namespace: "assistant-notes",
  agentId: "main",
  userId: "operator",
  sessionKey: "agent:main:main",
};
const config = parseConfig(input);
const context = { agentId: "main", sessionKey: "agent:main:main", assertInvocationCurrent() {} };

function setup(call = async (_operation, body) => ({ status: "active", memory_key: body.memory_key }), ctx = context) {
  return createTools(config, ctx, () => ({ call }));
}

test("configuration rejects missing identity, wildcard, unknown keys and credential-bearing URLs", () => {
  for (const field of Object.keys(input)) {
    for (const value of [undefined, "", " ", "*", " * ", 1]) {
      assert.throws(() => parseConfig({ ...input, [field]: value }), field);
    }
  }
  for (const baseUrl of [
    "ftp://loom.example.org",
    "http://loom.example.org",
    "https://user:secret@loom.example.org",
    "https://loom.example.org?token=secret",
    "https://loom.example.org/#secret",
  ]) {
    assert.throws(() => parseConfig({ ...input, baseUrl }));
  }
  assert.throws(() => parseConfig({ ...input, apiKey: "secret" }));
  assert.throws(() => parseConfig({ ...input, tokenEnv: "not-an-env-key" }));
  assert.equal(parseConfig({ ...input, baseUrl: "http://127.0.0.1:7777" }).baseUrl, "http://127.0.0.1:7777");
});

test("another or missing native agent/session gets no tools or IO", () => {
  for (const ctx of [{}, { ...context, agentId: "other" }, { ...context, sessionKey: "agent:main:other" }]) {
    assert.equal(
      setup(() => assert.fail("unexpected request"), ctx),
      null,
    );
  }
  assert.throws(() => setup(undefined, { ...context, assertInvocationCurrent: undefined }));
});

test("save fixes the complete identity and downgrades provenance", async () => {
  const calls = [];
  const [save] = setup(async (...args) => {
    calls.push(args);
    return { status: "active", memory_key: args[1].memory_key };
  });
  const abort = new AbortController();
  const out = await save.execute("call-1", { key: "editor", text: "Prefer compact output." }, abort.signal);
  assert.deepEqual(out.details, { saved: true, key: "editor" });
  const identity = {
    org: "example",
    namespace: "assistant-notes",
    agent: "main",
    user_id: "operator",
    session_id: "agent:main:main",
  };
  const prefix = createHash("sha256").update(JSON.stringify(identity)).digest("hex").slice(0, 32);
  assert.deepEqual(calls, [
    [
      "memory.set",
      {
        ...identity,
        memory_key: `openclaw/${prefix}/editor`,
        value: "Prefer compact output.",
        memory_type: "semantic",
        source_class: "derived",
      },
      { signal: abort.signal },
    ],
  ]);
});

test("different configured users and sessions cannot overwrite the same key", async () => {
  const keys = [];
  for (const patch of [{}, { userId: "other-user" }, { sessionKey: "agent:main:other" }]) {
    const cfg = parseConfig({ ...input, ...patch });
    const [save] = createTools(cfg, { ...context, sessionKey: cfg.sessionKey }, () => ({
      async call(_op, body) {
        keys.push(body.memory_key);
        return { status: "active", memory_key: body.memory_key };
      },
    }));
    await save.execute("call", { key: "editor", text: "fact" });
  }
  assert.equal(new Set(keys).size, 3);
});

test("save-search-save round trip revises the same fact and hides unrelated keys", async () => {
  const rows = new Map();
  const [save, search] = setup(async (operation, body) => {
    if (operation === "memory.set") {
      rows.set(body.memory_key, body.value);
      return { status: "active", memory_key: body.memory_key };
    }
    return {
      abstain: false,
      results: [
        ...Array.from(rows, ([memory_key, content_excerpt]) => ({ memory_key, content_excerpt })),
        { memory_key: "unrelated", content_excerpt: "Not stored by this integration." },
      ],
    };
  });
  await save.execute("save-1", { key: "editor", text: "Prefer compact output." });
  const firstKey = Array.from(rows.keys())[0];
  const recalled = await search.execute("search-1", { query: "editor" });
  assert.deepEqual(recalled.details.memories, [{ key: "editor", text: "Prefer compact output." }]);
  await save.execute("save-2", { key: recalled.details.memories[0].key, text: "Prefer detailed output." });
  assert.deepEqual(Array.from(rows), [[firstKey, "Prefer detailed output."]]);
});

test("model arguments cannot change identity or add request fields", async () => {
  const [save, search] = setup(() => assert.fail("unexpected request"));
  for (const field of ["org", "namespace", "agent", "user_id", "session_id", "source_class", "dry_run"]) {
    await assert.rejects(save.execute("call", { key: "x", text: "fact", [field]: "other" }));
    await assert.rejects(search.execute("call", { query: "fact", [field]: "other" }));
  }
  await assert.rejects(save.execute("call", { key: "x".repeat(257), text: "fact" }));
  await assert.rejects(save.execute("call", { key: "x", text: "x".repeat(4097) }));
});

test("closed invocation and pre-aborted work send no request", async () => {
  const [closed] = setup(() => assert.fail("unexpected request"), {
    ...context,
    assertInvocationCurrent() {
      throw new Error("invocation closed");
    },
  });
  await assert.rejects(closed.execute("call", { key: "x", text: "fact" }), /invocation closed/);
  const abort = new AbortController();
  abort.abort();
  const [save] = setup(() => assert.fail("unexpected request"));
  await assert.rejects(save.execute("call", { key: "x", text: "fact" }, abort.signal), { name: "AbortError" });
});

test("search is session-private, bounded and honours abstention", async () => {
  const calls = [];
  const [, search] = setup(async (...args) => {
    calls.push(args);
    return {
      abstain: false,
      results: [
        { memory_key: "low", content_excerpt: "excluded", below_threshold: true },
        {
          memory_key: "openclaw/6392ba5c16af82582863bd0ebe8443d6/editor",
          content_excerpt: "Prefer compact output.",
          private_field: "omitted",
        },
      ],
    };
  });
  const out = await search.execute("call", { query: "editor" });
  assert.deepEqual(out.details, {
    abstain: false,
    memories: [{ key: "editor", text: "Prefer compact output." }],
  });
  assert.deepEqual(calls[0].slice(0, 2), [
    "memory.search",
    {
      org: "example",
      namespace: "assistant-notes",
      agent: "main",
      user_id: "operator",
      session_id: "agent:main:main",
      query: "editor",
      scope: "private",
      namespace_scope: "this",
      read_scope: "own_session",
      memory_type: "semantic",
      legs: ["memory"],
      top_k: 5,
      bodies: "excerpt",
      source_span_chars: 1024,
      explain: false,
      auto_touch: false,
    },
  ]);
  const [, abstaining] = setup(async () => ({
    abstain: true,
    results: [{ memory_key: "ignored", content_excerpt: "ignored" }],
  }));
  assert.deepEqual((await abstaining.execute("call", { query: "editor" })).details, { abstain: true, memories: [] });
});

test("failed backend errors do not disclose response bodies or tokens", async () => {
  const [save] = setup(async () => {
    throw new Error("SECRET bearer token and private response");
  });
  await assert.rejects(save.execute("call", { key: "x", text: "fact" }), (error) => {
    assert.match(error.message, /namespace grant/);
    assert.doesNotMatch(error.message, /SECRET|bearer|private response/);
    assert.equal(error.cause, undefined);
    return true;
  });
});

test("save reports server merges and unconfirmed writes without inventing success or leaking keys", async () => {
  for (const [reply, expected] of [
    [
      (key) => ({ noop: true, merged_into_key: key.replace(/requested$/, "canonical") }),
      { saved: false, outcome: "already_present", key: "canonical" },
    ],
    [
      () => ({ noop: true, merged_into_key: "private-other-session-key" }),
      { saved: false, outcome: "not_saved_in_configured_scope" },
    ],
    [(key) => ({ status: "duplicate", memory_key: key }), { saved: false, outcome: "not_confirmed" }],
    [(key) => ({ status: "active", memory_key: key, dry_run: true }), { saved: false, outcome: "not_confirmed" }],
  ]) {
    const [save] = setup(async (_operation, body) => reply(body.memory_key));
    const response = await save.execute("call", { key: "requested", text: "A fact already held elsewhere." });
    assert.deepEqual(response.details, expected);
    assert.doesNotMatch(JSON.stringify(response), /private-other-session-key/);
  }
});

test("authority revoked during an awaited response hides recalled data", async () => {
  let current = true;
  const [, search] = setup(
    async () => {
      await Promise.resolve();
      current = false;
      return { abstain: false, results: [] };
    },
    {
      ...context,
      assertInvocationCurrent() {
        if (!current) throw new Error("invocation closed");
      },
    },
  );
  await assert.rejects(search.execute("call", { query: "editor" }));
});
