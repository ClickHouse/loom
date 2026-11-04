# LibreChat

Needs LibreChat v0.8.8 (MIT), a Loom server that serves the
[assistant toolset](../../reference/mcp.md#the-assistant-toolset), and one Loom `viewer`
token per person.

LibreChat is an optional chat client for investigating a namespace: what it holds, how it
is used, and cited answers over its memories, documents and code. The model calls Loom's
assistant tools over MCP with the signed-in person's own token. Loom stays the authority
for memory, documents, retrieval and the diagnostic rules; LibreChat keeps the chat. The
dashboard's [Assistant page](../dashboard.md) answers the same inspect and tune questions
with no model and no second store.

The configuration, a local compose file and the qualification harness are in
[`integrations/librechat/`](../../../integrations/librechat/README.md). Other LibreChat
versions are not qualified: the configuration keys and the agents API below are the ones
v0.8.8 reads.

## How a call reaches Loom

```text
browser -> LibreChat (agent, the person's stored Loom token) -> POST /mcp?toolset=assistant
        -> Loom: verify the token, run the read under its org, namespaces and role
```

- **Transport.** One `mcpServers` entry of `type: streamable-http` pointing at
  `/mcp?toolset=assistant`. The query string reaches Loom; the person's tool list is the
  assistant roster and nothing else.
- **Identity delegation.** The entry declares a `customUserVars` field, `LOOM_TOKEN`, and
  sends `Authorization: Bearer {{LOOM_TOKEN}}`. Each person enters their own token in
  LibreChat's MCP settings; LibreChat stores it per user, encrypted with the deployment's
  `CREDS_KEY` (collection `pluginauths`), and opens a separate MCP connection per user with
  that header. There is no shared token: for a person who has not entered one, LibreChat
  opens no connection (`missing_custom_user_vars`) and lists no Loom tool. A user id
  written in a prompt changes nothing, since the model never sees or sets the header.
- **Role.** Mint the token with `--roles viewer`. The assistant toolset already refuses
  every op outside its reads; a viewer token makes the same limit hold if the URL is
  changed. A short `--ttl-days` bounds what a leaked LibreChat database exposes.
- **What the model receives.** Loom's `initialize` instructions (with
  `serverInstructions: true`), the assistant tools' names, descriptions and schemas, and
  each tool's full result as the [assistant toolset](../../reference/mcp.md#the-assistant-toolset)
  returns it: keys, versions, content hashes and identities of the rows the token may
  read. It receives no ClickHouse credentials, no SQL tool and no other connection.
- **Ledger.** Loom records every call as consumer `loom-assistant` under the person's
  token subject, so `agent_events` shows who asked what through the chat, and
  `namespace.audit` leaves those reads out of its usage counts.

Options that LibreChat v0.8.8 also offers, and why this configuration does not use them:
`apiKey: {source: admin}` is one key shared by every user; `{{LIBRECHAT_OPENID_ACCESS_TOKEN}}`
passes LibreChat's own sign-in token, which Loom verifies only when both trust the same
identity provider; `{{LIBRECHAT_BODY_*}}` headers carry request-body values a client
controls.

## Tenant boundaries

| Case                                                  | What happens                                                                                                                                                                                                                                                                                                                                                                                                |
| ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Question about another org's namespace                | Tested with the model naming only the other namespace: the org comes from the token, the call answers `isError` with `NAMESPACE_NOT_ACCESSIBLE`, and the model reports no access; no row of the other org reached it. A call that also names the other org answers `isError` with `ORG_MISMATCH` over MCP; that was not exercised, only its HTTP counterpart (`AUTH_INSUFFICIENT`, 403) on the direct path. |
| Revoked or expired token                              | Loom answers the MCP request with `unauthenticated: token jti '…' is revoked`; LibreChat retries the connection, then the tool call fails and the model says so.                                                                                                                                                                                                                                            |
| Person without a stored token                         | LibreChat opens no Loom connection and lists no Loom tool (`missing_custom_user_vars`).                                                                                                                                                                                                                                                                                                                     |
| Agent shared with another LibreChat user              | Sharing is off in this configuration and was not exercised. LibreChat reads `customUserVars` for the user who sends the message (`getUserMCPAuthMap` in `packages/api/src/mcp/auth.ts`), so a shared agent would call Loom with that user's token.                                                                                                                                                          |
| Prompt asks for a write, a wider namespace or a token | The assistant toolset lists no write and refuses any tool outside its roster; the token decides the namespace.                                                                                                                                                                                                                                                                                              |

LibreChat's own access control (agent and MCP server sharing, roles) governs who sees an
agent in the UI. It is not Loom authorization; the token on each call is.

## Tool errors, streaming and cancellation

- A refused or failed call comes back to the model as the tool's error text, which it
  quotes. LibreChat logs the error line, with the token's `jti` but not the token.
- **Streaming.** The chat UI streams through LibreChat's resumable generation stream
  (`/api/agents/chat/stream/:streamId`). The agents API (`/api/agents/v1/chat/completions`)
  streams OpenAI-style chunks, but compresses the response when the client accepts
  compression, and a compressed stream arrives only when the answer is complete: a client
  that needs incremental tokens sends `Accept-Encoding: identity`.
- **Cancellation.** In the UI path the generation runs server-side, apart from the
  browser connection: closing the tab does not stop it, and its tool calls still reach
  Loom. The stop button (`/api/agents/chat/abort`) stops it. On the agents API, closing
  the connection stops the run: in one probe, disconnecting after the first token left no
  Loom call in the next 20 s.

## What LibreChat stores

Loom's erasure saga does not reach LibreChat's database: erasing a user or a memory in
Loom leaves every copy LibreChat holds. LibreChat must not become a second store of
memory, so this configuration keeps its memory feature off and its search index out.

| Data                                    | Where (MongoDB collection or volume)              | Holds tenant content                                                                              | Retention in this configuration                                                                                                                                          | How it is deleted                                                                                                             |
| --------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| Conversations                           | `conversations`                                   | titles (title generation is off)                                                                  | `expiredAt` after `generalChatRetention` (720 h), TTL index                                                                                                              | the person deletes the chat; account deletion; TTL                                                                            |
| Messages, with every tool call's output | `messages` (`content[].tool_call.output`)         | yes: the questions, the answers and the full Loom results each tool returned                      | `expiredAt` after 720 h, TTL index                                                                                                                                       | as above                                                                                                                      |
| Tool call records                       | `toolcalls`                                       | tool outputs                                                                                      | `expiredAt`, TTL index                                                                                                                                                   | account deletion; TTL                                                                                                         |
| Token usage                             | `transactions`                                    | no: token counts per message                                                                      | kept                                                                                                                                                                     | account deletion                                                                                                              |
| Each person's Loom token                | `pluginauths`, encrypted with `CREDS_KEY`         | a credential                                                                                      | until the person removes it                                                                                                                                              | removing it in MCP settings; account deletion; revoke it in Loom with `loom revoke-token`                                     |
| Uploaded files                          | `files` and the `uploads` volume                  | yes, if people upload                                                                             | `expiredAt` under `retentionMode: all`; a sweep deletes the stored file, then the record (`packages/data-schemas/src/schema/file.ts`, `packages/api/src/files/sweep.ts`) | the person deletes the file; account deletion; the sweep                                                                      |
| Agents                                  | `agents`                                          | the agent's instructions, which may name an org and a namespace                                   | until deleted                                                                                                                                                            | the person deletes the agent (`DELETE /api/agents/:id`); account deletion                                                     |
| Agents API keys                         | `agentapikeys`, stored as a hash                  | a bearer credential: it runs the person's agents, and so Loom reads under their stored Loom token | until deleted                                                                                                                                                            | the person deletes the key (`DELETE /api/api-keys/:id`); account deletion; turn `interface.remoteAgents` off to stop new keys |
| LibreChat memories                      | `memoryentries`                                   | none: `memory.disabled: true`, writes refused                                                     | not written                                                                                                                                                              | -                                                                                                                             |
| Message search index                    | none: Meilisearch is not deployed, `SEARCH=false` | -                                                                                                 | -                                                                                                                                                                        | -                                                                                                                             |
| Server logs                             | the `logs` volume and container output            | errors with request ids, user ids and token `jti`s; no token, no message text                     | one file per day, kept 14 days (`packages/data-schemas/src/config/winston.ts`)                                                                                           | delete the files                                                                                                              |

- `retentionMode: all` gives every chat an expiry, not only temporary ones. It applies to
  chats created after it is set; existing rows keep their `expiredAt` (see the comment in
  LibreChat's `librechat.example.yaml`).
- Account deletion (`DELETE /api/user/delete`, allowed unless `ALLOW_ACCOUNT_DELETION=false`)
  removes the person's conversations, messages, tool calls, transactions, stored keys,
  files, agents, agent API keys and memories (`api/server/controllers/UserController.js`
  in LibreChat v0.8.8).
- **Limitation: old chats keep what earlier tool calls returned.** A follow-up in an
  existing chat sends the earlier messages, tool outputs included, back to the model, which
  can answer from them without calling Loom again; a revoked token or an erased row does
  not stop that. After a person's access is revoked or a row is erased, delete their
  LibreChat chats (or let the retention expire them) and start new ones. Only a new tool
  call is checked against the current token and rows. This follows from LibreChat
  sending the chat's history with each turn; it was not tested.
- Backups of LibreChat's MongoDB are outside Loom's backup and bundle inventory; an
  operator who backs it up keeps the same retention for the backup.

## Qualification

`integrations/librechat/qualification/qualify.py` answers a labeled case set twice: through
LibreChat's agents API with a model, and through the same reads over HTTP with no model,
with the arguments the assistant toolset forces (`memory.search` with `auto_touch: false`
and `explain: false`). The inventory, inspect and tune cases are the reads the dashboard's
Assistant page makes; the memory cases' `memory.search` is not on that page. It seeds two
orgs, mints a token per person, revokes one, and scores each answer:

- **correct:** the answer contains no forbidden text and, by kind: a memory, inventory,
  inspect or tune case contains the labeled value; a refusal case says access was refused
  or failed; an abstention case says the fact is not stored. The forbidden text is the
  other org's canary for the cross-tenant case, the person's own answer (`friday`) for the
  revoked-token case, and a stale or wrong value for some others.
- **citation valid:** every memory key the answer names exists in the person's namespace,
  and a version written next to it is the row's current version.
- **tool calls:** the `mcp` rows Loom ledgered under the person's subject during the answer.
- **latency and time to first token:** end to end, and to the first streamed delta, which
  is the model's one-line preamble before its first tool call, not the answer.

| Target (LibreChat path)                                 | Value         | Measured on v0.8.8, 14 cases, `claude-haiku-4-5-20251001`                 |
| ------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| Correct answers                                         | at least 90%  | 14 of 14                                                                  |
| Citations valid                                         | 100%          | 9 of 9                                                                    |
| Should-abstain cases that abstain                       | 100%          | 2 of 2, no false abstention                                               |
| Cross-tenant and revoked cases refused, no leak         | 100%          | 2 of 2                                                                    |
| End-to-end latency p95                                  | at most 10 s  | 6.8 s (p50 3.2 s)                                                         |
| First streamed delta (preamble) p95                     | at most 2 s   | 0.6 s                                                                     |
| Tool calls per question                                 | at most 3     | at most 3, 16 in all                                                      |
| Model cost per answer, every prompt token at list price | at most $0.05 | mean $0.029; max $0.064 (`abstain-cfo`, three searches) misses the target |

The LibreChat numbers are from one run of the 14 memory, inventory, inspect, tune, refusal
and abstention cases, scored against the current `cases.json` with `qualify.py rescore`;
the direct-path numbers are from a later run of that path alone
(`qualify.py run --native-only`) on a fresh seed. The document cases below were run
separately.

The direct path answered the same 14 cases correctly at p50 0.08 s and p95 0.24 s with one
read each. The model adds natural-language questions and a written answer; it costs seconds
and money per question, and every listed tool's schema is sent on each model call: about
11,000 prompt tokens for the assistant roster, about 25,000 for a two-call answer.

### Document questions

The document cases ask over a third namespace, `acme-library`, whose person holds a
`viewer` token on it alone. Its documents are written through `knowledge.upsert`, the text
path: a Markdown runbook with nested headings, a plain-text contract with none, a SQL
table definition and a TOML file, and a policy written twice so that its live revision
states a value the earlier one did not. The labels name each answer's value and the
section that states it; two questions ask for a fact no document holds.

An answer cites a document passage as `[source_uri § Section > Subsection]`, the
breadcrumb a `knowledge.search` hit carries in `section`, and adds `, revision 3` only
with the number `knowledge.get` returned, since a hit carries no revision. A document with
no headings (the contract, the SQL file) is cited by its `source_uri` alone. Every
document is plain text to the server: the harness sends no format, and no case runs
through extraction. On the text path a TOML
`# comment` line is a Markdown heading, so the TOML file's sections are its comments.

- **recall@k:** the rank of the labeled `(source_uri, section)` among the hits of the
  direct path's `knowledge.search`, with the question as the query and the case's
  `top_k`. The model's own searches are not scored: their results stay in LibreChat.
- **citation valid:** checked against the seeded server with the scorer's viewer token,
  no model: the document is live in the person's namespace, a named section is one of
  its headings (`knowledge.outline`), a named revision is its live one (`knowledge.get`),
  and the cited text (the section with its subsections, or the whole document) holds one
  of the labeled values. A revision that is not a number (`revision unknown`) names none.
  A locator, once hits carry one, is one more field each citation is checked on.
- **labeled section cited:** a valid citation names the labeled section exactly; a parent
  section that also holds the value is valid but does not count.
- **abstention:** the answer says the fact is not in the documents. `knowledge.search`
  returns the nearest passages and has no abstain signal, so the direct path abstains
  only when nothing is found at all.

| Measured on v0.8.8, `claude-haiku-4-5-20251001`, `text-embedding-3-large` | LibreChat                                  | Direct path              |
| ------------------------------------------------------------------------- | ------------------------------------------ | ------------------------ |
| Answerable questions answered correctly                                   | 7 of 7                                     | 7 of 7, from the top hit |
| Labeled section at rank 1 / within the top 5                              | -                                          | 7 of 7 / 7 of 7          |
| Document citations valid                                                  | 8 of 8                                     | -                        |
| Labeled section cited                                                     | 7 of 7                                     | 7 of 7 (the top hit)     |
| Citations naming a revision                                               | 0 of 8                                     | -                        |
| Unanswerable questions abstained                                          | 2 of 2, no false abstention                | 0 of 2                   |
| End-to-end latency p50 / p95                                              | 4.9 s / 6.4 s                              | 0.32 s / 0.48 s          |
| Tool calls                                                                | 21 for 9 questions, at most 3 each         | one read each            |
| Model cost, every prompt token at list price                              | $0.294, mean $0.033 per answer, max $0.043 | none                     |

The five documents make a small corpus, so rank 1 for every labeled section says the
breadcrumbs reach the hits, not that retrieval holds up at scale. No citation names a
revision: the model read `knowledge.get` only on the two unanswerable questions, and the
hits it cited from carry none.
