# Remember a selected workflow fact

This Ruby workflow saves one synthetic release checklist fact to Loom and retrieves
it in a later process. It uses Roast's ordinary `ruby` cog and the existing
[Ruby SDK](../../sdk/ruby/README.md). No model or provider credentials are needed.

Use Ruby 3.4.4 and Bundler 2.6.9. The Gemfile pins the real Roast source at
`c55ba249d71db12f98e38dd254aeef0bb4ddc03b` (`roast-ai`, source version 1.4.1).
Bundler installs it from Git; this does not assume that version is on a gem
registry. `clickhouse-loom` comes from this checkout through its supported source
build route.

```sh
git clone --filter=blob:none --sparse https://github.com/ClickHouse/loom.git
cd loom
git sparse-checkout set examples/roast-memory sdk/ruby
git rev-parse HEAD
cd sdk/ruby
gem build clickhouse-loom.gemspec --output clickhouse-loom.gem
gem install ./clickhouse-loom.gem --no-document
cd ../../examples/roast-memory
gem install bundler --version 2.6.9 --no-document
bundle _2.6.9_ install
```

Bundler uses the SDK source at `../../sdk/ruby`. The separate gem build/install
checks the supported source packaging route. The workflow does not add a gem,
plugin, server setting, or operation.

First run the synthetic HTTP qualification, which starts and stops its own
loopback fixture:

```sh
bundle exec ruby qualification_test.rb
```

To use a Loom deployment, set all seven configuration values. Use a bearer token
authorized to write and read the chosen namespace. The URL must be HTTPS, or HTTP
on loopback for a development deployment, without credentials, query, or fragment.
Supply the token through your environment's secret mechanism; do not put it in
the workflow file or command history.

```sh
export LOOM_BASE_URL=https://loom.example.com
export LOOM_ORG=example-org
export LOOM_NAMESPACE=example-memory
export LOOM_AGENT=release-helper
export LOOM_USER_ID=synthetic-user
export LOOM_SESSION_ID=synthetic-session
: "${LOOM_API_KEY:?Supply an authorized token through your secret mechanism}"

# Explicitly select the synthetic fact in memory_step.rb for saving.
ROAST_MEMORY_ACTION=save bundle exec roast execute workflow.rb
# A separate process retrieves the same selected key. Recall is the default.
bundle exec roast execute workflow.rb
```

The first command reports `{"status":"saved","noop":false}` only after an
active save receipt with a memory id and positive version. `noop:true` reports an
already satisfied write. Recall reports `status:found` with the stored value, or
`status:abstain` only when the server answers `found:false`. An invalid receipt,
configuration refusal, or SDK error reports `status:error` and fails the workflow.
A backend error leaves the save outcome unknown; it does not prove no write
occurred. Server error bodies and tokens are omitted from these receipts.

Configuration fixes the full `(org, namespace, agent, user_id, session_id)` tuple.
The physical memory key hashes an unambiguous encoding of all five fields and
the logical key, so changing a user or session selects another key. Both calls
send the full tuple; no workflow input can override it. This key convention is
not an authorization boundary: the bearer token and server enforce access and
set the writer actor. The selected fact is marked `source_class:derived`; the
workflow never supplies an actor or captures a prompt, transcript, or cog output.
Keep using synthetic data when adapting this example.

The [hosted qualification](../../.github/workflows/roast-memory.yml) executes
the real Roast DSL and Ruby SDK in separate save/recall processes. Its owned HTTP
fixture checks the token header, identity, provenance, keys, request counts,
abstention, configuration refusals, and sanitized errors. It does not qualify a
live Loom deployment, token grants, storage durability, server tenant isolation,
or model behavior. No merchant account or app registration is involved.
