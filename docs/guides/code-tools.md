# Code tools for model integrations

An indexed code namespace and a token with read access let an SDK tool loop investigate
definitions, behavior and relationships with `loom_code_lookup`. Enable it explicitly on
`AgentTools`; the memory tools keep their own namespace. The model chooses whether to call
the tool. Availability and guidance do not guarantee adoption or a performance gain.

```python
tools = bot.tools(
    code_namespace="code-repo",
    only=["loom_code_lookup"],
    max_result_chars=12_000,
)
definitions = tools.definitions("anthropic")
# Give definitions to the model; dispatch its selected name and arguments:
result = await tools.execute("loom_code_lookup", {"kind": "symbol", "target": "pkg.Reader.read"})
```

```ts
const tools = bot.tools(
  { userId: "u1", sessionId: "thread-42" },
  { codeNamespace: "code-repo", only: ["loom_code_lookup"], maxResultChars: 12000 },
);
const definitions = tools.definitions("openai");
const result = await tools.execute("loom_code_lookup", {
  kind: "callers",
  target: "pkg.Reader.read",
  depth: 2,
});
```

Omit `only` to offer the code lookup first, followed by the default memory tools. Without
a code namespace, the default memory tools and their order are unchanged. Explicitly
selecting `loom_code_lookup` without its namespace fails at construction. Generic,
Anthropic and OpenAI definitions use the existing provider formats; no model dependency
is required.

## Choose evidence by intent

Use indexed investigation first for definitions, behavior, callers and prospective
impact, including bug reports that quote an identifier or error. A quoted name does not
make a bug report a literal-search task. Use local tools for an explicit literal-search
request, uncommitted or unindexed files, or insufficient or abstaining results. Reuse
sufficient evidence already supplied. Unrelated work needs no code lookup.

| `kind`    | `target`                                               | Request bound                                                                     |
| --------- | ------------------------------------------------------ | --------------------------------------------------------------------------------- |
| `symbol`  | Named definition, preferably qualified, or `path:line` | 2500 estimated tokens, body enabled; history, examples, lessons and uses disabled |
| `search`  | Natural-language question about behavior               | 3 results, 2500 estimated tokens, lessons disabled                                |
| `callers` | Symbol reference or `path:line`                        | `depth` 1–3, default 1; at most 20 edges, lessons disabled                        |
| `impact`  | Symbol reference or `path:line`                        | `depth` 1–2, default 2; at most 20 impacted symbols per hop, lessons disabled     |

Named lookups accept compact references such as `pkg.Reader.read`,
`src.pkg::Reader::read`, `DB::Context::getSettingsRef`, `std::vector<int>::push_back`,
`Foo::~Foo`, `Foo::operator()` and `src/Foo.cpp:123`. Use a path and line when a signature
needs whitespace. Prose is rejected before these reference-only operations; use `search`
for conceptual questions. `depth` is rejected for `symbol` and `search`.

The application fixes the organization and code namespace. The model may send only
`kind`, `target` and optional `depth`; extra fields, including tenant scope, wire operation
and budgets, are rejected before a request. Authorization errors propagate without
retrying a different scope. Memory tools still address the Agent's memory namespace.

## Read bounded results

The returned JSON contains `op`, `request_id`, `scope` and the server's `data`. Request IDs
come from the SDK transport and identify the call in the server ledger. Preserve source
commits, file and line citations, ambiguity, abstention, elision and graph uncertainty
when answering. Parser-tier edges and lower-bound counts do not establish an exhaustive
call graph; an empty edge list does not prove that no callers exist.

`max_result_chars` / `maxResultChars` defaults to 8000. With code lookup enabled it must
be an integer of at least 256. The 12000-character example above leaves more room for a
2500-token body response and its metadata; the character cap still applies.

Code responses remain valid JSON. If needed, the client removes whole bodies, retaining
their pointers and marking them `elided`, then removes whole evidence rows. A
`client_truncation` block reports `omitted_bodies`, `omitted_rows` by section,
`reason: "max_result_chars"` and `insufficient: true`. These client omissions make the
evidence incomplete even if the preserved server `epistemic` value says `exact`.
Do not infer absence from omitted evidence. Increase the application budget or read the
cited local source when needed.

If uncertainty metadata cannot fit, the result contains no partial `data` and explicitly
reports `metadata_omitted: true` and `insufficient: true`. If the scope and request
metadata also exceed the cap, only that insufficiency block is returned. The client never
slices JSON or silently turns a partial answer into an exact one. Memory-tool result
formatting retains its existing behavior.

The SDK examples demonstrate the dispatcher without an agent model:
[Python](../../sdk/python/examples/code_lookup.py) and
[TypeScript](../../sdk/typescript/examples/code-lookup/index.mjs). Supplying definitions
to a model still requires the application's ordinary tool loop. These helpers exercise
code lookup; they do not establish memory, imported coverage or test-gate effectiveness.
