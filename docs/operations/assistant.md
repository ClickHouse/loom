# Native assistant setup

The assistant runs inside Loom and uses the same dashboard, HTTP, MCP and SDK
interfaces. Authentication, viewer access and a configured model route are required.
Changing configuration does not verify a provider or authorize a deployment.

## Configuration

Set `LOOM_ASSISTANT_ENABLED=true`. Select `LOOM_ASSISTANT_PROVIDER` and
`LOOM_ASSISTANT_MODEL` explicitly. Empty provider and model select Anthropic and
`claude-haiku-4-5`; there is no fallback to the deployment's general LLM model.

| Provider    | Credentials and route                                                                                                                    |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `anthropic` | Existing Anthropic key or guarded inference gateway; Messages streaming tools                                                            |
| `openai`    | Existing OpenAI key or guarded inference gateway; chat-completions streaming tools                                                       |
| `bedrock`   | Standard AWS credential chain, `LOOM_ASSISTANT_BEDROCK_REGION`, Converse streaming permission and an explicit model or inference profile |
| `local`     | Existing `LOOM_LOCAL_LLM_BASE_URL`, explicit model and verified price reference                                                          |
| `fake`      | No credentials, no model network; explicit offline verification only                                                                     |

For a route alias, set `LOOM_ASSISTANT_PRICE_MODEL` to its verified price-table model.
An unknown price or missing credential refuses an answer. A price reference records
an estimate from the configured rate, not a provider invoice. A zero ceiling refuses
calls. Model calls never send temperature by default and do not retry a paid 400 to
learn which fields an endpoint supports. A provider must support the streaming tool
contract; an incompatible endpoint fails closed.

Configure the total deadline, model steps, tool count, context, output, token and
cost bounds in the [configuration reference](../reference/configuration.md).
The namespace's assistant share and concurrency limit still apply. Real model
verification spends budget and needs its owner's authorization.

Helm's `assistant.enabled: true` sets the server switch. Put provider credentials in
the existing release Secret and model settings in `env` or `settingsFile`. Compose
uses the server's existing environment. No extra database or assistant service is
created. Network and gateway rules for Loom's own model traffic apply.

## Verification

First run focused fake-provider and fake-tool tests. They prove deterministic control
flow, bounds and failure behavior; they do not prove live provider compatibility or a
successful deployment. Check readiness and Health after an approved rollout, then
exercise the native answer route with a caller allowed to read the test namespace.
Confirm that context calls precede the first model generation, every tool row has the
caller's subject and answer run, citations name returned sources, cancellation joins
the work, and required private cost delivery precedes the terminal marker.

Keep a deployment's existing failure warning visible until its serving build and
native path have been verified. Configuration readiness alone is not that evidence.

## Retention and cutover

This code change does not stop or start any service, delete any database, or remove
any existing volume. The operator performs cutover only in a separately approved
maintenance window.

1. Inventory the serving builds, configured assistant routes, database volumes,
   volume claims, retention policies and backups. Record exact resource names outside
   public documentation. Keep the old stack stopped; starting it can drain durable
   queues and execute model work. Do not use orphan removal or volume deletion.
2. Back up the retained database and external chat exports using an approved offline
   or snapshot procedure. Preserve the original bytes, owner mapping and integrity
   hashes. This server's built-in chat was stateless: its clients supply history.
   A separate chat application's user messages are external state, not evidence that
   this server stored none there. Never discard that state based on this distinction.
3. Convert already exported conversations with the offline history converter. Require
   an explicit binding from each export owner to the importing Loom subject; keep
   conversation, message and parent ids. The converter rejects unknown owners, files,
   tools, oversized input and broken branches. It neither connects to a database nor
   executes queued work. The archive remains caller-held; bounded user and assistant
   history can be supplied to the existing native chat route.
4. Verify the native build with fakes before rollout approval. Keep assistant traffic
   disabled through a mixed serving fleet. Schema compatibility does not establish
   native provider or mirrored-source capability compatibility. A rollback must not
   restart an old runtime or its queues without another explicit decision.
5. Before a Helm upgrade removes a retired StatefulSet, verify that its existing
   persistent-volume-claim retention policy keeps claims on deletion and scale-down,
   and that the backing volume's reclaim policy and backups match the retention plan.
   These manifests create no replacement store. Compose keeps existing named volumes
   when service definitions are removed; do not run volume or system pruning.
6. After approved deployment and verification, retain all old database volumes,
   archives and provisioning metadata for the owner's chosen period. Data retirement,
   key retirement and any irreversible deletion require separate approval. There is
   no automatic destructive migration or rollback through a retired runtime.

Removing two runtime images reduces the deployment's required images. It does not
measure memory savings or shrink the Loom executable: the removed runtimes were
separate containers. Measure the final native deployment before claiming a footprint
or latency benefit.

## Offline history conversion

The input is one exported JSON object with `conversationId`, `user` and `messages`.
Each message carries `messageId`, `parentMessageId`, `text` and either `role` or
`isCreatedByUser`. Other archive formats need a separately verified adapter; this
command does not claim to recover missing exports or arbitrary database contents.
The owner-bindings JSON maps export owner ids to Loom subjects. Treat that mapping
as an operator-reviewed authorization input, not information supplied by a model.

```bash
python -m scripts.ops.assistant_history --input exported-chat.json \
  --owner-bindings owner-bindings.json --subject importing-subject \
  --org example-org --namespace example-namespace --output retained-chat.json
```

The command refuses an existing output path. Keep both the source export and output
archive. `server.assistant.domain.history.history` selects a bounded parent chain for
the existing caller-supplied history input and checks its subject and scope again.
It imports no tool instruction, permission, session or queued work into the server.

## Source entry points

The [assistant architecture](../architecture/assistant.md) describes the runtime,
authorization, citation receipts, accounting and storage boundaries.
