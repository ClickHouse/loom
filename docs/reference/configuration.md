# Configuration reference

Every setting is an environment variable `LOOM_<FIELD>` read once at startup into
`server.settings.settings`. Set them in `.env` or the container environment;
`.env.example` lists the common ones. `LOOM_ENV_FILE` names a different dotenv file,
and empty reads none.

This page is generated from the comments above each field in `server/settings/`
by `python scripts/codegen/settings_reference.py`; edit the comment, not this file.


## Auth

Authentication, tokens and identity providers.

Source: `server/settings/_auth.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_PLATFORM_ADMINS` | `str` | `''` | Comma-separated login handles and emails promoted to platform_admin (org-wide visibility). A handle matches a password account's subject; an email matches only an IdP-verified (OIDC) email, never the unverified self-signup address. Empty = no platform admins. |
| `LOOM_AUTH_REQUIRED` | `bool` | `True` | True: every operation requires a valid bearer JWT. False (laptop demos only): missing tokens are not rejected and the SPA synthesizes a local identity. identity_provider=single-user forces auth off regardless. |
| `LOOM_DEFAULT_NAMESPACE` | `str` | `'local'` | The one namespace auth-off mode serves (SPA identity, dashboard bootstrap, MCP/SDK caller defaults). Ignored when auth is required; `personal-*` is reserved and rejected. |
| `LOOM_OAUTH_ENABLED` | `bool` | `False` | OAuth 2.1 authorization server for MCP one-click connect: /.well-known discovery + /oauth/{register,authorize,token}. Issues normal loom JWTs, subject-aligned to the signed-in user. Opt-in: a new authz surface. `oauth_issuer` = public base URL for discovery/redirects ("" = per request). |
| `LOOM_OAUTH_ISSUER` | `str` | `''` | Public base URL used in OAuth discovery and redirects; empty derives it from each request. Required when `oauth_login_provider` is "oidc" (the IdP callback URL is built from it). |
| `LOOM_OAUTH_LOGIN_PROVIDER` | `'password' \| 'oidc'` | `'password'` | Human sign-in for /oauth/authorize: "password" (built-in form) or "oidc" (redirect to an external IdP; loom maps the verified email to an account and still mints its own token). "oidc" requires all three oidc_* values. |
| `LOOM_OIDC_ISSUER` | `str` | `''` | External IdP issuer URL; its `/.well-known/openid-configuration` must name the same issuer. |
| `LOOM_OIDC_CLIENT_ID` | `str` | `''` | Client id registered at the IdP; also the audience each id_token must carry. |
| `LOOM_OIDC_CLIENT_SECRET` | `str` | *(unset)* | Client secret sent to the IdP token endpoint when exchanging the sign-in code. |
| `LOOM_OIDC_JIT_PROVISION` | `bool` | `False` | JIT provisioning: a first OIDC sign-in with no account creates a personal namespace in oidc_jit_org instead of being rejected. Off = fail closed. oidc_jit_email_domains (comma-separated) restricts it; empty admits any verified email the IdP authenticates. |
| `LOOM_OIDC_JIT_ORG` | `str` | `''` | Org that JIT-provisioned accounts are created in; required when `oidc_jit_provision` is on. |
| `LOOM_OIDC_JIT_EMAIL_DOMAINS` | `str` | `''` | Comma-separated email domains JIT provisioning admits; empty admits any verified email. |
| `LOOM_JWT_SECRET` | `str` | *(set)* | The root secret, and the one secret a deployment carries: it signs tokens as kid `default` and seals every stored credential and every signing key in loom.signing_keys (`loom signing-key rotate` adds those). Required when auth is on. The default is the checked-in DEV secret anyone can mint with: a deployment with auth on refuses to start with it, except the `make dev` stack (`loom doctor` warns everywhere else). |
| `LOOM_JWT_SECRET_PREVIOUS` | `str` | `''` | The root secret LOOM_JWT_SECRET replaced, while a rotation is under way: credentials and signing keys sealed under it still open (tokens it signed as kid `default` do not verify: a leaked root mints nothing). `loom secrets reseal` rewrites the stored ones under the new root; then remove this. "" = none. |
| `LOOM_JWT_KEYRING_REFRESH_SECONDS` | `float` | `30.0` | Seconds between re-reads of loom.signing_keys in every process (at least 1); `loom signing-key rotate` waits out two of the slowest process's. |
| `LOOM_JWT_ISSUER` | `str` | `'loom'` | The `iss` Loom stamps on every token it mints and the `aud` every verified token must carry; a token naming another audience is refused. |
| `LOOM_JWT_AUDIENCE` | `str` | `'loom-platform'` | The `aud` claim stamped on minted tokens and required on every verified token. |
| `LOOM_JWT_TRUSTED_ISSUERS` | `str` | `''` | Extra accepted `iss` values, for a gateway minting Loom-shaped JWTs with a key in `jwt_public_keys_json`. Loom's own tokens (iss = jwt_issuer) pass. |
| `LOOM_JWT_PUBLIC_KEYS_JSON` | `str` | *(unset)* | JSON (or base64 JSON) map kid → PEM public key of a gateway whose RS256 tokens this deployment accepts. Loom's own keys live in loom.signing_keys. |
| `LOOM_JWT_LEEWAY_SECONDS` | `int` | `30` | Clock-skew tolerance on iat/exp, seconds; small enough to give replay no useful post-exp window. |
| `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` | `float` | `30.0` | Seconds the `loom.revoked_tokens` snapshot is cached; verify checks both jti and sub against it, so a revocation takes effect on other processes within this TTL (at once on the one that wrote it). |
| `LOOM_JWT_REVOCATION_MAX_STALE_SECONDS` | `float` | `600.0` | Oldest revocation snapshot a process still trusts, seconds. Past it (refreshes keep failing) every bearer is refused with a retryable 503. A subject's cached grants are trusted as long when a re-read fails. |
| `LOOM_GRANT_CACHE_TTL_SECONDS` | `float` | `10.0` | Seconds a subject's grant rows are cached for the namespace gate, which caps a token's claim at the grant it holds there: a revoked or lowered grant takes effect on other processes within this TTL (at once on the one that wrote it). |
| `LOOM_AUTH_ATTEMPTS_CONCURRENCY` | `int` | `8` | Sign-ins and sign-ups one process checks at once (each costs ClickHouse statements and a password hash); past it a caller waits up to a second, then gets 429, so a burst cannot take the shared connection pool. |
| `LOOM_SELF_SIGNUP` | `'off' \| 'open'` | `'off'` | Browser self-signup: "off" (endpoint 404s) or "open" (/v1/auth/self-signup mints JWTs scoped to the requested namespace). |
| `LOOM_SELF_SIGNUP_ORG` | `str` | `'dev'` | Default org for self-signed tokens. |
| `LOOM_IDENTITY_PROVIDER` | `'' \| 'single-user' \| 'trust-network' \| 'gateway'` | `''` | Identity mode; decides how callers authenticate. single-user    — no auth, one anonymous identity. trust-network  — self-signup, first-claim-wins. gateway        — an upstream proxy mints short-lived Loom JWTs with the shared HS256 secret; Loom only validates. Empty = inferred from auth_required + self_signup at boot. |
| `LOOM_DASHBOARD_REDIRECT_URL` | `str` | `''` | When set, a dashboard page load redirects (302) here instead of serving the SPA (Loom behind an aggregator). Deployment-specific; never hardcode. |
| `LOOM_TRUSTED_CALLERS` | `str` | `''` | Comma-separated subjects allowed to send `X-Loom-On-Behalf-Of` (a trusted aggregator passing each end-user's identity under one service token). Empty disables the header. |
| `LOOM_RESTRICTED_SERVE` | `bool` | `True` | Kill switch for restricted namespaces. True: served to a principal whose token names the namespace AND who holds a live direct grant. False: every read is refused while ingest continues. |
| `LOOM_RESTRICTED_AUDIT_INLINE` | `bool` | `True` | Commit the `namespace.restricted_access` audit row every admitted read of a restricted namespace writes before answering (inline). True by default: a row written beside the request is lost when the process is killed before it lands. False trades that row for the seconds the awaited insert adds. |
| `LOOM_CORS_ORIGINS` | `str` | `''` | Comma-separated origin list. "*" allows all (acceptable when auth_required=True). Empty disables CORS entirely. |
| `LOOM_MAX_REQUEST_BYTES` | `int` | `33554432` | Request-body cap, bytes, enforced by ASGI middleware before any parser (413 PAYLOAD_TOO_LARGE); uvicorn has no limit of its own. 0 disables. |
| `LOOM_POLICY_MODE` | `'off' \| 'shadow' \| 'enforce'` | `'off'` | History policy over the agent trace: "off" reads/writes nothing; "shadow" records verdicts in loom.policy_decisions without blocking; "enforce" denies (403 POLICY_FORBIDDEN) and records them too. In enforce, writes fail closed on evaluation errors; reads never do. |
| `LOOM_POLICY_MAX_WINDOW_SECONDS` | `int` | `86400` | Cap on any rule's look-back window, in seconds. |
| `LOOM_POLICY_CACHE_TTL_SECONDS` | `int` | `30` | Per-(org, namespace) rule cache TTL, seconds; other replicas see a rule change within it. |

## ClickHouse

ClickHouse connection, query shaping and vector storage.

Source: `server/settings/_clickhouse.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CH_HOST` | `str` | `'localhost'` | ch_secure=None → wrapper infers TLS from port (8443 → True). |
| `LOOM_CH_PORT` | `int` | `8123` | ClickHouse HTTP port; 8443 (the Cloud HTTPS port) turns TLS on when `ch_secure` is unset. |
| `LOOM_CH_USER` | `str` | `'loom'` | User name the server connects to ClickHouse as. |
| `LOOM_CH_PASSWORD` | `str` | *(set)* | Password for `ch_user`; the default only suits a local development server. |
| `LOOM_CH_DATABASE` | `str` | `'loom'` | Database that holds every Loom table and that migrations run against. |
| `LOOM_CH_CLUSTER` | `str` | `'default'` | The cluster `clusterAllReplicas` reads to find a stopped Loom process's queries on every node (`default` on ClickHouse Cloud). A server without it answers for itself; '' reads only the connected server. |
| `LOOM_CH_SECURE` | `bool \| None` | `None` | Connect over HTTPS. Unset infers TLS from the port (8443 on, anything else off). |
| `LOOM_CH_LOG_COMMENT_STATEMENT_DIGEST` | `bool` | `False` | Append `sql:<shape12> <first 200 chars>` of each statement to its `log_comment` ("<op> <request_id>"), so query_log rows group by shape. Off by default: readers that split log_comment on spaces gain tokens. |
| `LOOM_CH_POOL_MAXSIZE` | `int` | `64` | HTTP connection pool size; the driver default (~8) exhausts under bulk writes plus workers. Size to expected request concurrency. |
| `LOOM_CH_KEEPALIVE_TIMEOUT` | `float` | `5.0` | Seconds an idle pooled connection is kept. Must stay BELOW the Cloud load balancer's idle cutoff (8 s) so loom closes first: a peer-closed pooled TLS connection is never reaped under uvloop (`LOOM_UVICORN_LOOP=uvloop`). |
| `LOOM_CH_READ_YOUR_WRITES` | `bool` | `True` | A point lookup that MISSES right after a write (get after set, verify, update_by_id, delete) re-reads once with select_sequential_consistency=1. Costs only on a miss; a no-op on single-node MergeTree. |
| `LOOM_CH_READ_YOUR_WRITES_WINDOW_S` | `float` | `20.0` | memory.search on a namespace this process wrote within this many seconds reads with select_sequential_consistency from the start. 0 = off (the empty-pool retry remains the backstop). |
| `LOOM_CH_TIMEOUT_READ_S` | `float` | `30.0` | Per-class statement deadline, seconds, applied as `max_execution_time` and as a client-side bound that frees the pool slot. 0 = no bound for that class. |
| `LOOM_CH_TIMEOUT_WRITE_S` | `float` | `60.0` | Deadline, seconds, for INSERT and UPDATE statements; 0 = no bound. |
| `LOOM_CH_TIMEOUT_DDL_S` | `float` | `300.0` | Deadline, seconds, for DDL statements (CREATE, ALTER, DROP); 0 = no bound. |
| `LOOM_CH_POOL_WAIT_MS` | `int` | `500` | Max wait, ms, for a pool slot before failing fast with 503 OVERLOADED. 0 = no slot gate. |
| `LOOM_CH_CALLER_SHAPED_MAX_EXECUTION_S` | `float` | `30.0` | Ceilings for the reads a caller shapes (code.grep, code.search with a pattern or globs, memory.aggregate, memory.record.query). A ceiling only lowers the statement's own value, so no request can widen it; 0 = none. The shipped loom_reader profile (nlp_user_settings.xml) caps these three at their defaults, with a floor no statement can go under (time: 0.05 s, code.grep's statement floor), so none can be set to 0 even in the SQL. Raising a setting above its cap needs the profile raised too; a 0 sends no ceiling, so the profile's cap applies (code.grep's deadline is then clamped to ch_timeout_read_s, which the profile's cap matches). Seconds one such read may run (the read class default). |
| `LOOM_CH_CALLER_SHAPED_MAX_MEMORY_BYTES` | `int` | `4000000000` | Bytes of memory one such read may use. |
| `LOOM_CH_CALLER_SHAPED_MAX_ROWS_TO_READ` | `int` | `500000000` | Rows one such read may scan; past it the read fails (422), never a partial answer. |
| `LOOM_CH_READER_USER` | `str` | `''` | ClickHouse user the caller-shaped reads run as, with `ch_password`; the shipped config defines `loom_reader` (SELECT only). Empty = `ch_user`. |
| `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` | `str` | `'{"production": {"priority": 1}, "interactive": {"priorit…` | JSON map {traffic class: ClickHouse settings} applied by `server.core.ch.statement_settings` to every read (an explicit per-call setting wins). `priority`: 1 served first, larger waits, 0 opts out; `max_threads` caps CPU per statement. Neither changes the rows returned. `background` (workers, operator CLIs) bypasses the filesystem cache on a miss so a backfill scan does not evict interactive data. |
| `LOOM_BOOT_MIGRATE` | `bool` | `True` | Apply pending migrations at boot. Set False for any server started from a branch against a shared service, or it applies unmerged migrations. |
| `LOOM_BOOT_MIGRATE_LEASE_TTL_S` | `int` | `300` | Seconds after which a holder that stopped renewing is replaced. |
| `LOOM_BOOT_MIGRATE_LEASE_WAIT_S` | `float` | `900.0` | Seconds a contender waits for the holder before boot fails (exit 1, the container restarts); also how long a booting process waits for the provider configuration lock (a sibling's rebuild step may hold it). |
| `LOOM_MIGRATE_MUTATION_WAIT_S` | `int` | `600` | Seconds a migration file waits for the mutations it started (MATERIALIZE, UPDATE, a column type change) to finish on every replica; past it the file fails unrecorded. |
| `LOOM_VECTOR_SEARCH_EF_SEARCH` | `int` | `256` | HNSW candidate list size (`ef_search`) for the helper legs, emitted via vector_search_settings(); tracks ClickHouse's default. The main leg scales its own value with top_k. |
| `LOOM_VECTOR_SEARCH_INDEX_FETCH_MULTIPLIER` | `float` | `10.0` | Post-filter over-fetch for a `vector_similarity` (HNSW) index: the index returns top-N before the tenant predicate drops rows. Sent on uncoded vector reads; coded reads use `vector_search_quantized_fetch_multiplier`. |
| `LOOM_VECTOR_SEARCH_QUANTIZED_CODES` | `bool` | `True` | Send `vector_search_use_quantized_codes = 1` on vector-ranked reads whose column has a `Quantized(...)` codec and whose table has no patch parts: a codes-first scan plus full-precision rescore. A replica that rejects the setting (code 115) drops the fragment and retries. |
| `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER` | `float` | `3.0` | Over-fetch for the codes-first read: stage 1 keeps LIMIT × this by code distance, stage 2 rescores. Only read when the codes setting is sent. |
| `LOOM_VECTOR_CODES_STATE_PROBE_SECONDS` | `int` | `60` | Seconds between re-reads of which vector columns are coded and which coded tables carry patch parts. A write that embeds reads again on its own when the reading is older than a few seconds (codes_state.FRESH_READING_S). |

## Code

The code plane: ingest, indexes, search, ask, review.

Source: `server/settings/_code.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_INTEL_ENABLED` | `bool` | `True` | Master kill switch, checked at the top of every code.* handler. |
| `LOOM_CODE_TEST_GATE_RUN_TEMPLATES` | `str` | `''` | code.test_gate run recipes (LOOM_CODE_TEST_GATE_RUN_TEMPLATES): JSON list of {"pattern": <regex over the repo-relative test path>, "run": <template>}, tried before the built-ins. Templates may use {path} {dir} {stem} {name} {parent}. A path no recipe matches is reported run_unknown, never guessed. |
| `LOOM_CODE_ASK_TOKEN_BUDGET` | `int` | `4000` | Retrieval context (tokens) code.ask hands to synthesis when the caller passes no token_budget. |
| `LOOM_CODE_ASK_CACHE_THRESHOLD` | `float` | `0.0` | The code.ask answer cache's hit cosine. 0 = the per-embedding-model table in code/app/answers.py; a value in (0, 1] pins one threshold for every model. |
| `LOOM_CODE_ASK_CACHE_MAX_AGE_DAYS` | `int` | `14` | A cached code.ask answer older than this many days is a miss (checked and tombstoned at read time; no table TTL). 0 = no bound. |
| `LOOM_CODE_ASK_NEGATIVE_CACHE_TTL_S` | `int` | `3600` | Seconds an abstain stays cached for its exact normalised question under the index head it abstained at. 0 = off. |
| `LOOM_CODE_ASK_LLM_CACHE` | `bool` | `True` | code.ask's pre-synthesis LLM calls (identifier proposal, HyDE, concept legs) read the per-namespace exact-prompt response cache; no_cache or a traffic class with llm_cache off bypasses it. |
| `LOOM_CODE_ORG` | `str` | `''` | The scalar repository entry: its loom org, its code namespace and its GitHub `owner/name`. Empty repo = no scalar entry (nothing indexed until a repository is configured); an empty org or namespace is never a default for a code.* request, which then must name its own. |
| `LOOM_CODE_NAMESPACE` | `str` | `''` | The scalar entry's code namespace, used by a code.* request or `loom code` command that names none; empty = the request must name one. |
| `LOOM_CODE_REPO` | `str` | `''` | The scalar entry's GitHub `owner/name`; empty = no scalar entry. |
| `LOOM_CODE_ASK_MEMORY_NAMESPACE` | `str` | `''` | The memory namespace code.ask fuses decision/rationale memories (the "why") from into its answer; empty = no enrichment, never guessed. |
| `LOOM_CODE_ASK_MEMORY_ORG` | `str` | `''` | The org of that namespace; empty = `code_org`. |
| `LOOM_CODE_ORG_READABLE` | `bool` | `False` | Who may READ the code index. False: the namespace's own visibility and grants. True: every authenticated principal of the org reads every configured code namespace (right for public or org-internal repos). Writes stay grant-driven. Per-entry "org_readable" in LOOM_CODE_REPOS_JSON. |
| `LOOM_CODE_REPO_PUBLIC` | `bool` | `False` | The scalar repo entry is public: its namespace registers join_policy=open and discoverable. Per-entry `"public": true` does the same. |
| `LOOM_CODE_COVERAGE_CONTROL_SYMBOL` | `str` | `''` | Positive-control symbol code.test_home / code.coverage_claim grep for (one every test suite of the repository names); empty makes every claim `unknown`. |
| `LOOM_CODE_SETTING_CONTROL` | `str` | `''` | The setting code.setting_facts must find before it claims `not_a_setting` for any other name; empty = it never claims it. |
| `LOOM_CODE_CONVENTIONS_DIFF_MAX_BYTES` | `int` | `1500000` | Max bytes of a PR patch code.conventions reads from the mirror; a longer patch is cut at a file boundary and flagged `diff_truncated`. |
| `LOOM_CODE_CONVENTIONS_WIDE_CALLER_DIRS` | `int` | `5` | A touched function is "widely called" when its hop-1 callers span more than this many `src/<dir>` directories (rule wide_callers_untested). |
| `LOOM_CODE_REPOS_JSON` | `str` | *(unset)* | Multi-repo: JSON array of repo configs (code/domain/index/repos.py); empty = the scalar single-repo settings. Missing per-entry fields fall back to the scalars. |
| `LOOM_CODE_SELF_SERVICE_ENABLED` | `bool` | `True` | Repositories added from the Repositories page (loom.code_repos) merge with the entries above; the environment wins on a namespace collision. Off = the operator's JSON is the only source. |
| `LOOM_CODE_REPOS_ROOT` | `str` | `'var/code_repos/self'` | Where the worker clones a self-service entry's bare mirror (one per GitHub repository, shared by its branches). |
| `LOOM_CODE_SELF_SERVICE_MAX_REPOS` | `int` | `20` | Active self-service entries per org (branches count); past it the add refuses with a message. |
| `LOOM_CODE_BOOTSTRAP_FILES_PER_PASS` | `int` | `400` | Files indexed per pass while a new entry's initial index builds; the cursor persists in code_index_state (400 ≈ 1 min at the live rate). |
| `LOOM_CODE_REPOS_REFRESH_SECONDS` | `int` | `30` | Seconds between re-reads of loom.code_repos. |
| `LOOM_CODE_BOOTSTRAP_CLONE_TIMEOUT_S` | `float` | `1800.0` | Seconds a bare clone may take before the worker gives up for the pass and retries next pass. |
| `LOOM_CODE_HISTORY_CLONE_ON_READ` | `bool` | `True` | Whether a history read (blame_range, bisect, first_build, lesson checks) on a host without a repository's mirror clones it there in the background. On: every serving host keeps its own full copy of every mirror it is asked about. Off: such a host answers `no_git_mirror` and only the worker's host clones. |
| `LOOM_GIT_HISTORY` | `'local' \| 'none'` | `'local'` | Where git-history reads (blame, citation commit checks, PR history, commit coverage) come from. local = this host's mirrors; none = an edge host with no mirrors: those reads answer without history. |
| `LOOM_CODE_MIRROR_WARMUP` | `bool` | `True` | Every serving process clones the configured entries' mirrors it lacks at boot, in the background (code/app/index/mirrors.py): a history read answers `no_git_mirror: cloning <entry>` until its mirror is there. A full bare clone per host, again on every new disk: keep code_repos_root on a volume that outlives a rollout. Off: a mirror is cloned only when a read asks for it. |
| `LOOM_CODE_MIRROR_WARMUP_CONCURRENCY` | `int` | `2` | Clones the warm-up runs at once on one host (each after a random wait), so N replicas starting together do not all clone from the remote at once. |
| `LOOM_CODE_MIRROR_FETCH_INTERVAL_S` | `float` | `300.0` | Seconds between this host's fetches of its mirrors and sweeps of the mirrors, checkouts and lock files no active entry names. A round fetches only the branch tips, open PR heads and ledger merges the leader's pass recorded in ClickHouse that the mirror lacks: nothing when it is current. 0 = no fetch loop and no sweep. |
| `LOOM_CODE_MIRROR_GC_INTERVAL_H` | `float` | `168.0` | Hours between `git gc` runs of each mirror on this host (low CPU and IO priority, only with room for a second copy plus the margin below). 0 = never. |
| `LOOM_CODE_MIRROR_GC_MARGIN_GB` | `float` | `5.0` | Free disk, in GB, a mirror's gc must leave beyond the mirror's own size; without it the gc waits another interval. |
| `LOOM_CODE_SANDBOX_CPU_SECONDS` | `int` | `7200` | rlimits for each process of a sandboxed repository job (clone, fetch, dependency install, compiler-tier harness; code/app/index/sandbox.py). They bound each process, not a job's total (max_pids aside): there is no cgroup. CPU seconds a process may use. |
| `LOOM_CODE_SANDBOX_MEMORY_MB` | `int` | `16384` | Data memory (heap and private mappings, RLIMIT_DATA) a process may use, in MiB. Not address space: node reserves far more of that than it uses. |
| `LOOM_CODE_SANDBOX_MAX_PIDS` | `int` | `2048` | Processes and threads one job may run at once (RLIMIT_NPROC, set inside the job's user namespace, so other jobs and the server do not count; Linux 5.14+, older kernels count the uid's tasks host-wide). |
| `LOOM_CODE_SANDBOX_REQUIRED` | `bool` | `False` | Set by deployments that ship the sandbox profiles: a repository job refuses to run rather than run unsandboxed, and /v1/health reports the missing sandbox as an error. False: without the sandbox, jobs fall back to plain processes as "Without the sandbox" in code-index-operations.md says. |
| `LOOM_CODE_SANDBOX_MAX_FILE_MB` | `int` | `32768` | Largest file a process may write, in MiB (a pack or index.scip). |
| `LOOM_CODE_GIT_DIR` | `str` | `''` | Bare clone the ingest worker fetches into (created by `loom code init`) and its remote. Empty = derived from code_repo: `var/code_repos/<name>.git` and `https://github.com/<owner>/<name>.git`. |
| `LOOM_CODE_GIT_REMOTE` | `str` | `''` | The scalar entry's fetch remote; empty = `https://github.com/<owner>/<name>.git`. |
| `LOOM_CODE_GIT_BRANCH` | `str` | `'main'` | The scalar entry's branch: the one the ingest worker fetches and indexes. |
| `LOOM_CODE_GIT_EXTRA_BRANCHES` | `str` | `''` | Comma-separated fnmatch globs of extra remote branches the mirror fetches besides the configured one (`release/*` = release branches, `backport/*` = backport staging). Refs are never pruned. Empty = the configured branch only. |
| `LOOM_CODE_GIT_FETCH_LEDGER_MERGES` | `bool` | `True` | Fetch ledger merge commits no branch reaches by sha and pin them under refs/loom/merges/<sha>: one ledger page and at most the batch below per entry per pass; the cursor wraps. Off = branches only. |
| `LOOM_CODE_GIT_LEDGER_MERGE_PAGE` | `int` | `500` | Ledger merge rows checked for a missing sha per entry per pass. |
| `LOOM_CODE_GIT_LEDGER_MERGE_BATCH` | `int` | `40` | 40 shas ≈ 8.5 s |
| `LOOM_CODE_GIT_FETCH_PR_HEADS` | `bool` | `True` | The mirror carries `refs/pull/N/head` for every PR the ledger holds OPEN (refetched when the tip moves, never pruned), so pre-merge reads diff the PR's own head. Only open heads: fetching every PR head is too costly. Per-entry `git_fetch_pr_heads` overrides. Off = the overlay reports `head_not_in_mirror`. |
| `LOOM_CODE_GIT_PR_HEAD_BATCH` | `int` | `200` | Max PR heads fetched per shared mirror per ingest pass. |
| `LOOM_CODE_GH_ITEMS_HEAD_FILL_BATCH` | `int` | `200` | Open-PR rows per pass whose head_sha / base_sha / files the PR-history worker fills from the mirror; rows the mirror lacks fall back to GitHub (batch below). |
| `LOOM_CODE_GH_ITEMS_HEAD_GITHUB_BATCH` | `int` | `20` | GitHub `GET /pulls/N` calls per pass for open PRs the mirror lacks; 0 = the mirror path only. |
| `LOOM_CODE_PATH_PREFIXES` | `str` | `''` | CSV path prefixes of the scalar entry that are indexed; everything else is skipped. Empty = the whole tree (the ingest denylist still applies). |
| `LOOM_CODE_GITHUB_TOKEN` | `str` | *(unset)* | GitHub token for the default repository: git fetches of a private remote, the PR-history and issue mirrors, and fork lookups. Empty = the REST calls go anonymously (60 requests/h per IP, public repos only; one warning) and the issue mirror is skipped. |
| `LOOM_CODE_INGEST_ENABLED` | `bool` | `False` | Per-merge fast tier: git-poll → diff → re-chunk/re-embed changed files → tree-sitter symbols/edges → context pack → advance watermark. |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS` | `int` | `120` | Seconds between fast-tier ingest passes; extra-branch fetches of a shared mirror are spaced by 0.9 of it (at least 30 s). |
| `LOOM_CODE_INGEST_LIVE_PATHS_TTL_S` | `float` | `60.0` | Seconds an ingesting process caches the namespace's live path set (the import-resolution oracle); files it writes join at once. |
| `LOOM_CODE_JIT_RISK_REFIT_INTERVAL_S` | `int` | `604800` | Seconds after which the weekly pass refits the jit-risk model. |
| `LOOM_CODE_JIT_RISK_WINDOW_MONTHS` | `int` | `12` | Months of merged PRs the fit reads. |
| `LOOM_CODE_JIT_RISK_MAX_AGE_DAYS` | `int` | `183` | Days after which a fit is served as `scored_stale` / `fit_stale`. |
| `LOOM_CODE_STALE_AFTER_COMMITS` | `int` | `5` | as_of.stale is true when the fast tier is more than this many first-parent commits behind the fetched branch tip (60 s cache). |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS` | `int` | `3600` | as_of.stale is also true when the mirror has not fetched for this many seconds (lag is counted against the local mirror). 0 = off. |
| `LOOM_CODE_CALLERS_MAX_DISTINCT_SEEDS` | `int` | `5` | code.callers/callees: a bare name with more distinct definitions than this returns `ambiguous` + candidates instead of walking them all. |
| `LOOM_CODE_INGEST_MAX_COMMITS_PER_PASS` | `int` | `20` | Commits past the watermark one pass reads; later ones wait for the next pass. |
| `LOOM_CODE_INGEST_SECONDS_PER_FILE` | `float` | `2.5` | Prior ingest cost per changed file (seconds), refined by observation. A commit predicted to overrun the pass deadline is not started unless it is the pass's first. |
| `LOOM_CODE_INGEST_MAX_FILES_PER_COMMIT` | `int` | `500` | Changed files indexed per commit; past it the rest are skipped with a warning and heal on the next backfill. |
| `LOOM_CODE_INGEST_MAX_PASS_SECONDS` | `int` | `900` | The ingest pass's own deadline (seconds), kept at the supervisor's cap; the budget below keeps a pass under it. |
| `LOOM_CODE_INGEST_SLICE_SHARE` | `float` | `0.5` | Share of the commit phase one oversized commit may take per pass while other entries wait (it is sliced across passes). |
| `LOOM_CODE_INGEST_PASS_BUDGET_FRACTION` | `float` | `0.75` | Fraction of the deadline after which the pass stops STARTING units, finishes the in-flight round, saves cursors and returns `partial: true`. |
| `LOOM_CODE_INGEST_WALK_BUDGET_SHARE` | `float` | `0.3` | Share of that budget the commit phase leaves to the walk (reconcile heal, extractor re-chunk). 0 = commits may take it all. |
| `LOOM_CODE_INGEST_ROUND_FILES` | `int` | `25` | Files per round in the walk, the bootstrap slice and a commit's file list; a round prefetches its per-file resolution reads in two statements. 0 = per-file reads. |
| `LOOM_CODE_INGEST_LANES` | `int` | `1` | Entries whose commits ingest side by side in the commit phase. Each entry's own commits stay one at a time, oldest first (the watermark contract); a lane waits on ClickHouse round trips, not on its capacity, so lanes add throughput. 1 = one commit at a time across all entries. |
| `LOOM_CODE_SCIP_APPLY_ENABLED` | `bool` | `False` | SCIP delta apply (compiler-grade tier uploaded via code.scip_ingest). |
| `LOOM_CODE_SCIP_APPLY_INTERVAL_SECONDS` | `int` | `60` | Seconds between SCIP apply passes. |
| `LOOM_CODE_SCIP_MAX_PART_BYTES` | `int` | `8388608` | Max bytes of one code.scip_ingest payload part; a larger part is refused. |
| `LOOM_CODE_SCIP_MAX_PARTS` | `int` | `512` | Max `total_parts` of one code.scip_ingest upload; more is refused. |
| `LOOM_CODE_SCIP_DELTA_ENABLED` | `bool` | `True` | C++ compiler-tier per-merge deltas: scip-clang over changed TUs when git_head outruns scip_commit. Needs the configured checkout and a scip-clang binary; CPU-heavy in bursts. |
| `LOOM_CODE_SCIP_DELTA_INTERVAL_SECONDS` | `int` | `300` | Seconds between C++ delta passes. |
| `LOOM_CODE_SCIP_CHECKOUT_DIR` | `str` | `'var/code_repos/checkout'` | The scalar entry's working tree the compiler tiers index; other entries default to a sibling named after their namespace, and an entry's `scip_checkout_dir` wins. |
| `LOOM_CODE_SCIP_CLANG_PATH` | `str` | `'scip-clang'` | The scip-clang binary: a name looked up on PATH, or a path. The image carries it on amd64 only (upstream builds no arm64 Linux binary). |
| `LOOM_CODE_SCIP_CLANG_JOBS` | `int` | `0` | scip-clang's parallel jobs in a delta or full run; 0 = half the CPUs the host shows (a container sees the node's). Each job holds a translation unit's AST: a few GiB for a large C++ tree. |
| `LOOM_CODE_SCIP_WORK_DIR` | `str` | `''` | Where the C++ harness writes its index and delta (several GB for a full run of a large tree); "" = the system temp dir. |
| `LOOM_CODE_SCIP_DELTA_MAX_TUS` | `int` | `200` | Max translation units one delta indexes; past it only the changed `.cpp` files are indexed and the header fan-out heals on the next full run. |
| `LOOM_CODE_SCIP_DELTA_TIMEOUT_SECONDS` | `int` | `3600` | Seconds the C++ delta harness may run before it is killed. |
| `LOOM_CODE_SCIP_FULL_ENABLED` | `bool` | `True` | C++ full re-index: seeds an entry the delta has no watermark for, and re-indexes each entry every `code_scip_full_every_hours` (heals deltas that hit the TU cap). One entry per pass, under its entry lock. |
| `LOOM_CODE_SCIP_FULL_INTERVAL_SECONDS` | `int` | `900` | Seconds between checks for an entry that is due. |
| `LOOM_CODE_SCIP_FULL_EVERY_HOURS` | `int` | `168` | Hours between full re-indexes of one entry. |
| `LOOM_CODE_SCIP_FULL_TIMEOUT_SECONDS` | `int` | `28800` | Seconds the full re-index harness (CMake configure, scip-clang, convert) may run; the checkout move and the apply get 6 h more in the pass deadline. |
| `LOOM_CODE_CROSSREF_ENABLED` | `bool` | `True` | SQL <-> C++ crossref (code_test_map from registrations and test queries) for each C++ entry whose git_head moved since its last run. |
| `LOOM_CODE_CROSSREF_INTERVAL_SECONDS` | `int` | `1800` | Seconds between crossref passes. |
| `LOOM_CODE_SCIP_LOCK_DIR` | `str` | `'var/cron/locks'` | Directory of the per-entry lock files every compiler-tier pass on this host takes (`scip.<org>.<namespace>.lock`): a full re-index and a delta never work on one entry at once; different entries run side by side. `loom code entry-lock` takes the same lock. |
| `LOOM_CODE_SCIP_TS_DELTA_ENABLED` | `bool` | `True` | TypeScript compiler-tier deltas: scip-typescript over an entry with `scip_producer` "typescript" when git_head outruns scip_commit_ts. The worker prepares dependencies with lifecycle scripts disabled; needs Node 22+ and the toolchain from scripts/install-code-toolchains.sh (in the image). |
| `LOOM_CODE_SCIP_TS_DELTA_INTERVAL_SECONDS` | `int` | `600` | Seconds between TypeScript delta passes. |
| `LOOM_CODE_SCIP_TYPESCRIPT_PATH` | `str` | `'scip-typescript'` | The scip-typescript binary: a name looked up on PATH, or a path. |
| `LOOM_CODE_SCIP_TS_DELTA_TIMEOUT_SECONDS` | `int` | `3600` | Seconds the TypeScript delta harness may run before it is killed. |
| `LOOM_CODE_SCIP_TS_PATHS` | `bool` | `True` | Rewrite consumers' tsconfig `paths` from workspace `exports` before indexing an unbuilt checkout; off for a built checkout. |
| `LOOM_CODE_SCIP_TS_NODE_HEAP_MB` | `int` | `8192` | Node heap size (MiB) given to scip-typescript. |
| `LOOM_CODE_SCIP_PY_DELTA_ENABLED` | `bool` | `True` | Python compiler-tier deltas: scip-python over an entry with `scip_producer` "python", over the files the window touched (a first run indexes the tree). Needs node and scip-python. |
| `LOOM_CODE_SCIP_PY_DELTA_INTERVAL_SECONDS` | `int` | `600` | Seconds between Python delta passes. |
| `LOOM_CODE_SCIP_PYTHON_PATH` | `str` | `'scip-python'` | The scip-python binary: a name looked up on PATH, or a path. |
| `LOOM_CODE_SCIP_PY_DELTA_TIMEOUT_SECONDS` | `int` | `3600` | Seconds the Python delta harness may run before it is killed. |
| `LOOM_CODE_SCIP_PY_HEAP_MB` | `int` | `16384` | Pyright dies silently at 8 GB |
| `LOOM_CODE_SCIP_GO_DELTA_ENABLED` | `bool` | `True` | Go compiler-tier deltas: scip-go per touched module of an entry with `scip_producer` "go". Needs the Go toolchain and scip-go (`go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7`, the image's version). |
| `LOOM_CODE_SCIP_GO_DELTA_INTERVAL_SECONDS` | `int` | `900` | Seconds between Go delta passes. |
| `LOOM_CODE_SCIP_GO_PATH` | `str` | `'scip-go'` | The scip-go binary, looked up on PATH or in `code_scip_go_bin_dir`. |
| `LOOM_CODE_SCIP_GO_BIN_DIR` | `str` | `''` | Where `go` and scip-go live when not on PATH |
| `LOOM_CODE_SCIP_GO_DELTA_TIMEOUT_SECONDS` | `int` | `7200` | Seconds the Go delta harness may run before it is killed; also the pass deadline. |
| `LOOM_CODE_SCIP_GO_JOBS` | `int` | `2` | Parallel module builds (~4 GB each) |
| `LOOM_CODE_SCIP_GO_MODCACHE` | `str` | `'var/gocache'` | Root of the Go module caches kept across passes: one per entry beneath it (<root>/<org>/<namespace>), never shared between repositories. |
| `LOOM_CODE_SCIP_GO_PRIVATE` | `str` | `''` | GOPRIVATE / GONOSUMDB (a vanity import host) |
| `LOOM_CODE_PR_HISTORY_ENABLED` | `bool` | `False` | Merged-PR history sync (GitHub REST, cursor-resumable). |
| `LOOM_CODE_PR_HISTORY_INTERVAL_SECONDS` | `int` | `900` | Seconds between PR-history sync passes. |

### The set-wide backport report

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_BACKPORT_GAPS_ENABLED` | `bool` | `True` | Nightly pass: runs `code.backport_gap(exact, history)` for every trunk PR of the last `days` labelled for backport and writes one row per release branch into loom.code_backport_gaps. Paced one fix per `pace_s`, at most `prs_per_pass` a night, stalest first, stopping at `budget_s`; settled fixes are skipped. Off = nothing written; the read op serves the table. |
| `LOOM_CODE_BACKPORT_GAPS_INTERVAL_SECONDS` | `float` | `86400.0` | Seconds between passes; the read op reports the table stale past twice this. |
| `LOOM_CODE_BACKPORT_GAPS_DAYS` | `int` | `90` | Days back of backport-labelled trunk PRs a pass considers. |
| `LOOM_CODE_BACKPORT_GAPS_PRS_PER_PASS` | `int` | `300` | Max fixes checked per pass, stalest first. |
| `LOOM_CODE_BACKPORT_GAPS_PACE_S` | `float` | `1.0` | Seconds of pause between two fixes. |
| `LOOM_CODE_BACKPORT_GAPS_BUDGET_S` | `float` | `1800.0` | Seconds after which a pass stops starting fixes. |
| `LOOM_CODE_BACKPORT_GAPS_MAX_PASS_SECONDS` | `float` | `3600.0` | The pass's hard deadline (seconds) under the worker supervisor. |

### Coverage watches

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_COVERAGE_NOTIFY_ENABLED` | `bool` | `True` | Coverage watches: post one code.coverage.ready event on the watcher's task inbox once every index tier covers a watched commit. A pass with no pending watch is one read of an empty table. |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | `int` | `60` | Seconds between passes while watches are pending. |
| `LOOM_CODE_COVERAGE_NOTIFY_IDLE_INTERVAL_SECONDS` | `int` | `120` | Seconds between passes that found no pending watch. |

### Bisect

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_BISECT_HISTORY_DAYS` | `int` | `90` | Days of the repo's own PR history bisect reads back from the window's end when there is no tracker (or no changed files) and no `after`. |
| `LOOM_CODE_BISECT_LOOKBACK_MINORS` | `int` | `6` | Minor releases searched below the failing one when the report names no clean one. |
| `LOOM_CODE_BISECT_WINDOW_DAYS` | `int` | `90` | Merge-time window (days) for a report naming no release. |
| `LOOM_CODE_BISECT_RERANK_TIMEOUT_S` | `float` | `6.0` | The rerank's wall budget (seconds); a timeout keeps the fused order. |
| `LOOM_CODE_BISECT_WIDE_DAYS` | `int` | `180` | The wide read's merge-time window (days) with no release named. |
| `LOOM_CODE_BISECT_DUPCHECK_TIMEOUT_S` | `float` | `4.0` | The dupcheck's budget inside a bisect, from the call's start; it runs beside the resolve, the scan and the fusion reads and is awaited only before the ranking stages. Past it the bisect answers without it (`dupcheck_fix.status: timeout`). |

### PR review comments

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_PR_REVIEWS_ENABLED` | `bool` | `True` | Also fetch each merged PR's inline review comments into loom.code_pr_reviews (one request per PR; skipped without a GitHub token). |

### GitHub issues and PRs mirror

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_GH_ITEMS_ENABLED` | `bool` | `True` | Keep loom.code_gh_items current: every issue and PR updated since the last pass via /issues; the first pass reaches back backfill_days. Skipped anonymously. |
| `LOOM_CODE_GH_ITEMS_BACKFILL_DAYS` | `int` | `730` | Days back the first mirror pass (and `loom code` with no start date) reads. |

### Code.pr_check

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_PR_CHECK_DUPLICATE_THRESHOLD` | `float` | `0.55` | Verdict: `duplicate_of` when a candidate's heuristic score reaches the duplicate line (a closed-unmerged PR caps at related), `related` at the related line, else `none`. A title alone never reaches duplicate. |
| `LOOM_CODE_PR_CHECK_RELATED_THRESHOLD` | `float` | `0.2` | Score at or above which a candidate is `related`. |
| `LOOM_CODE_PR_CHECK_MERGED_DAYS` | `int` | `365` | merged PRs older than this do not vote as duplicates |
| `LOOM_CODE_PR_CHECK_CLOSED_DAYS` | `int` | `180` | closed-without-merge PRs read back this far |
| `LOOM_CODE_PR_CHECK_MASS_FILES` | `int` | `200` | a candidate touching more files than this is a mass change: files do not vote |

### Merged-PR backfill

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_PR_BACKFILL_MONTHS` | `int` | `12` | Months of merged PRs `loom code backfill-prs` (and a cold worker) fetch; matches the churn window in code/app/risk.py. |

### Embedding recovery

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_EMBED_RECOVERY_ENABLED` | `bool` | `False` | Embedding recovery for code_chunks/code_summaries stubs (dim=0). |
| `LOOM_CODE_EMBED_RECOVERY_INTERVAL_SECONDS` | `int` | `30` | Seconds between recovery passes while stubs are pending. |
| `LOOM_CODE_EMBED_RECOVERY_IDLE_INTERVAL_SECONDS` | `int` | `120` | Sleep after a pass that found no pending stub; the normal interval returns once a pass writes a row. |
| `LOOM_CODE_EMBED_RECOVERY_FULL_SCAN_SECONDS` | `int` | `1800` | The pending scan reads above a version watermark; once per this many seconds it scans from version 0 to catch late rows. 0 = always from 0. |
| `LOOM_CODE_EMBED_MAX_ATTEMPTS` | `int` | `8` | Embed attempts per stub before it is poisoned (`embed_failed_at` set) and skipped. |
| `LOOM_CODE_EMBED_RECOVERY_BATCH` | `int` | `100` | Stub keys the recovery scan takes per repo per pass (also texts per embed call); raise it to onboard large repositories faster. |
| `LOOM_CODE_READY_EMBED_COVERAGE` | `float` | `0.95` | Share of embeddable chunks that must carry a vector before a repo is `ready`; below it code.search says "lexical only". Poisoned rows count against it; fixtures unembedded by design do not. |

### Ingest limits and denylist

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_INGEST_MAX_FILE_BYTES` | `int` | `1000000` | Files above this size are recorded (index_error) and not chunked. 0 = no cap. |
| `LOOM_CODE_INGEST_DENYLIST_BUILTIN` | `bool` | `False` | Ingest denylist (code/app/index/denylist.py): the built-in generated/minified/vendored rules are off (they match 0.19% of rows); `exclude` globs (CSV here, a list per repo entry) and the repo-local ignore file are always honoured. |
| `LOOM_CODE_INGEST_EXCLUDE` | `str` | `''` | CSV of path globs never indexed (the scalar entry's `exclude`). |
| `LOOM_CODE_INGEST_IGNORE_FILE` | `str` | `'.loomignore'` | Name of the repo-local ignore file whose patterns are never indexed. |

### Reconciliation and re-chunking

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_RECONCILE_ENABLED` | `bool` | `True` | Content-based reconciliation (code/app/index/reconcile.py): each interval, compare every path's chunk commit with the branch head and re-ingest up to heal_per_pass stale paths. |
| `LOOM_CODE_RECONCILE_INTERVAL_SECONDS` | `int` | `3600` | Seconds between reconciliation runs per namespace. |
| `LOOM_CODE_RECONCILE_HEAL_PER_PASS` | `int` | `200` | Stale paths re-ingested per run. |
| `LOOM_CODE_RECONCILE_MAX_COMMITS` | `int` | `1000` | Max distinct chunk commits a run compares; past it the stale set is a floor (`truncated`). |
| `LOOM_CODE_EXTRACTOR_RECHUNK_ENABLED` | `bool` | `True` | When the recorded parser/chunker/grammar stamp differs from the running process, re-chunk the namespace in paced batches per ingest pass. A first observation is recorded, never acted on. |
| `LOOM_CODE_EXTRACTOR_RECHUNK_PER_PASS` | `int` | `200` | Live paths re-chunked per ingest pass while the stamp differs. |
| `LOOM_CODE_EXTRACTOR_RECHUNK_CONCURRENCY` | `int` | `4` | Files a re-chunk re-derives at once. A re-derive waits mostly on its ClickHouse reads and writes, so raising this shortens a heal roughly in proportion, at that much more concurrent load on the cluster. |

### Dependents, closure repair and call guards

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_INGEST_DEPENDENTS_ENABLED` | `bool` | `True` | After a commit, re-extract edges only on untouched files whose call sites or edges reference symbols it added, removed or tombstoned (or, opt-in, that include a touched header). Capped per commit; overflow drains through reconcile. A replay closed every gap at 20% of the ingest wall. |
| `LOOM_CODE_INGEST_DEPENDENTS_MAX_FILES` | `int` | `200` | Dependent files re-extracted per commit; the overflow queues for reconcile. |
| `LOOM_CODE_INGEST_DEPENDENTS_CONCURRENCY` | `int` | `4` | Dependents re-extracted at once; the batch is bound by ClickHouse read throughput, so more is not faster. |
| `LOOM_CODE_INGEST_DEPENDENTS_PENDING_MAX` | `int` | `5000` | Size cap of that overflow queue; paths past it are dropped and counted. |
| `LOOM_CODE_CLOSURE_REPAIR_ENABLED` | `bool` | `True` | Closure repair: a bare function name the commit added with exactly ONE live definition repo-wide binds its `unresolved` sites at 0.6 without re-parsing; several definitions leave them declined. Capped per commit. |
| `LOOM_CODE_CLOSURE_REPAIR_MAX_NAMES` | `int` | `500` | Added and removed names per commit closure repair considers; the rest are skipped. |
| `LOOM_CODE_CLOSURE_REPAIR_MAX_SITES` | `int` | `2000` | Unresolved call sites read per commit for those names. |

### Graph and symbol statistics

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_GRAPH_STATS_ENABLED` | `bool` | `False` | Weekly graph statistics: PageRank, degrees, test_map rebuild. |
| `LOOM_CODE_GRAPH_STATS_INTERVAL_SECONDS` | `int` | `604800` | Seconds between graph-statistics passes. |
| `LOOM_CODE_GRAPH_STATS_MAX_PASS_SECONDS` | `int` | `3600` | This worker's pass deadline (seconds); a pass past it is cancelled and counted a failure. |
| `LOOM_CODE_GRAPH_STATS_UNIT_RISK_BUDGET_S` | `int` | `2400` | Seconds from pass start the unit-risk leg may schedule rounds; past it the pass succeeds with `unit_risk_partial: true` and the next resumes. Must stay under the deadline above. |
| `LOOM_CODE_FAN_IN_PRIOR_CAP` | `float` | `0.25` | Cap of code.search's fan-in rank prior: score *= 1 + cap * log1p(fan_in) / log1p(max fan_in in the pool); skipped under test intent and on noise paths; inert without the stats. 0 = off. |
| `LOOM_CODE_CALLERS_CANDIDATE_MIN_CONFIDENCE` | `float` | `0.2` | The floor below which a declined site code.callers / code.callees return is counted, not returned; a request may pass `candidate_min_confidence`. |
| `LOOM_CODE_SYMBOL_USES_HUB_FAN_IN` | `int` | `200` | Fan-in at or above which the `uses` block says `skipped: hub` instead of listing sites. 0 = never. |

### Summary cards

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_SUMMARY_ENABLED` | `bool` | `False` | Weekly LLM file/module summary cards (inputs_hash-gated). |
| `LOOM_CODE_SUMMARY_INTERVAL_SECONDS` | `int` | `604800` | Seconds between card passes. |
| `LOOM_CODE_SUMMARY_MAX_SCOPES_PER_PASS` | `int` | `200` | Cards written per pass across every repository, shared out evenly (a share one repository does not use rolls to the next). |
| `LOOM_CODE_SUMMARY_CONCURRENCY` | `int` | `1` | Cards in flight per pass; 1 = sequential. |
| `LOOM_CODE_SUMMARY_CHURN_DAYS` | `int` | `365` | A bounded card pass considers files by merged-PR count per path over these days (source tree), then by include-graph rank. Days of merged PRs the churn order counts. |
| `LOOM_CODE_SUMMARY_CHURN_PREFIXES` | `str` | `''` | CSV path prefixes the churn order may propose (empty = any) and CSV prefixes it never proposes, at any depth (vendored trees). |
| `LOOM_CODE_SUMMARY_CHURN_EXCLUDE` | `str` | `''` | CSV of path prefixes the churn order never proposes, at any depth. |
| `LOOM_CODE_SUMMARY_UPGRADE_FROM_MODELS` | `str` | `''` | CSV of summary models whose cards are rewritten when code_summary_model differs, even for an unchanged file. Empty = an unchanged file keeps its card. |

### Chunking and language lanes

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_CHUNK_MAX_CHARS` | `int` | `6000` | Chunking: whole AST units; functions larger than this split at top-level statement boundaries, each piece re-prefixed with the signature header. |
| `LOOM_CODE_SQL_STATEMENT_RECORDS` | `bool` | `False` | SQL tests as statement records: one `statement` symbol per record, chunks packed by whole statements, facet rows in loom.code_test_facets. Off = line-window chunks for `.sql`. Existing namespaces need `loom code backfill --paths-file` (outside the extractor stamp). |
| `LOOM_CODE_SQL_CHUNK_TARGET_CHARS` | `int` | `2000` | Statement packing target (chars); a larger single statement splits at lines at `code_chunk_max_chars`. |
| `LOOM_CODE_PY_SQL_ISLANDS` | `bool` | `False` | SQL a Python file runs (database-client call arguments and SQL-looking literals) becomes `statement` symbols, facets (confidence 0.7 with a format hole) and table edges. Off = functions only. Needs a `--paths-file` backfill of the Python tree. |
| `LOOM_CODE_SQL_COLUMN_LINEAGE` | `bool` | `False` | Column-level SQL lineage: `<table>.<column>` symbols, `column_read` / `column_write` facets and statement → column edges, for `.sql` and Python SQL islands. Off = table-level lineage. Needs a `--paths-file` backfill. |
| `LOOM_CODE_SHELL_ISLANDS` | `bool` | `False` | A shell test's SQL strings (`$CLICKHOUSE_CLIENT -q`, heredocs, `echo \|`) become `statement` symbols with facets; leftover chunks coalesce up to the target below, never across a function. Needs a `--paths-file` backfill. |
| `LOOM_CODE_SHELL_CHUNK_TARGET_CHARS` | `int` | `2000` | Leftover coalescing target in chars for shell files. |
| `LOOM_CODE_HOST_ISLANDS` | `bool` | `False` | Shell inside CI/build files (Actions `run:`, GitLab `script:`, Dockerfile `RUN`, Makefile recipes, Jenkinsfile `sh`) is parsed for facets, script `includes` edges and SQL; jobs, targets and stages become symbols, and extensionless Dockerfile / Makefile / Jenkinsfile index as config. Needs a `--paths-file` backfill. |

### Chunk descriptions and ingest rounds

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_CONTEXT_DESCRIPTION_ENABLED` | `bool` | `True` | Contextual-embedding one-liner per chunk, inline on the per-merge path; falls back to structural-only on any LLM failure. |
| `LOOM_CODE_CONTEXT_DESCRIPTION_PROMPT` | `'oneliner' \| 'questions'` | `'oneliner'` | Shape of the embedded chunk description: `oneliner` (what the code does) or `questions` (file card + chunk → what it does and the questions it answers). Changing it re-texts new rows only. |
| `LOOM_CODE_DESCRIBE_CONCURRENCY` | `int` | `8` | A file's chunk descriptions in flight at once; 1 = serial. |
| `LOOM_CODE_INGEST_ROUND_MODEL_CONCURRENCY` | `int` | `16` | Model calls one round runs at once. |
| `LOOM_CODE_INGEST_ROUND_WRITE_ROWS` | `int` | `2000` | A round writes one INSERT per table (flushed when a read could see the rows, at the row count below, or at round end). Buffered rows at which a round writes a table early. |

### Bot authors

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_BOT_AUTHORS` | `str` | `''` | Comma-separated GitHub logins of autonomous agents that author pull requests or issues in the indexed repositories, beyond the generic bots (dependabot, github-actions, …). They are never suggested as owners, their names are not tracker entities, and bisect ignores the lines they sign. |

### Search ranking, legs and caches (code.search)

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_TRACE_LOG_ENABLED` | `bool` | `True` | Fire-and-forget code_search_trace/code_search_outcome telemetry. |
| `LOOM_CODE_FUSION_SIGNALS` | `bool` | `True` | Post-fusion rank priors in code.search (path-noise penalty, query↔path stem affinity, file coherence), applied after RRF and before the outcome boost. Kill switch. |
| `LOOM_CODE_SEARCH_QUERY_MAX_TOKENS` | `int` | `48` | Long-question trim: past this many lexemes the question is treated as an issue body (template headings stripped, the rarest 32 tokens gate, the first 512 cleaned words embed). Under the cap nothing changes. 0 = off. |
| `LOOM_CODE_SEARCH_FILE_SATURATION` | `float` | `0.5` | Per-file saturation: the 2nd, 3rd... unit from one path is multiplied by decay^excess before the cut; a file's best unit never decays. 0 = off. Skipped under path_prefix, symbol routes and test intent. |
| `LOOM_CODE_SEARCH_VECTOR_MODELS` | `str` | `''` | CSV of vector-model specs a request may name as `vector_model` (e.g. "prefix:512:text-embedding-3-large"); only the vector leg switches. Empty = the override is refused. |
| `LOOM_CODE_VECTOR_TABLES` | `str` | `''` | JSON {label: {"table": "loom.x", "dim": 256}}: where a label's chunk rows live (the coded vector column is fixed-width). Absent label = loom.code_chunks. Inserts and the vector leg route by it. |
| `LOOM_CODE_SEARCH_VECTOR_SHORTLIST` | `int` | `0` | With a vector_model set and this > 0, the second model's top-N ids are rescored by exact cosine on the served vectors. 0 = its own distances rank. Per-request CodeSearchInput.vector_shortlist. |
| `LOOM_CODE_SEARCH_CARDS_FETCH_MULTIPLIER` | `float` | `10.0` | Over-fetch multiplier of the file-card leg's codes shortlist once loom.code_summaries carries the codec. |
| `LOOM_CODE_SEARCH_VECTOR_MODEL_DEFAULT` | `str` | `''` | Default vector-leg shortlist label (`prefix:<dim>:<served model>`, listed in code_search_vector_models and code_vector_tables), rescored by the served vectors. Needs code_search_vector_shortlist > 0; an empty shortlist falls back to the full scan. Empty = off. |
| `LOOM_CODE_COMPACT_AFTER_BULK_ROWS` | `int` | `10000` | A bulk `loom code` pass that wrote at least this many chunk rows ends by compacting the prefix/twin vector tables past code_compact_min_ratio. 0 = off; loom.code_chunks is compacted only by the CLI. |
| `LOOM_CODE_COMPACT_MIN_RATIO` | `float` | `1.5` | Copies per key (active rows / distinct keys) at which a code vector table is OPTIMIZE ... FINAL'd. |
| `LOOM_CODE_RISK_ANNOTATIONS` | `bool` | `True` | Per-result risk annotations (churn/amp/tested) and one-hop callee context on code.search results; reserves part of the token budget. Kill switch. |
| `LOOM_CODE_UNIT_RISK_MAX_AGE_S` | `int` | `864000` | Age (seconds) past which a risk row is stale and annotated live; must exceed the weekly pass interval. |
| `LOOM_CODE_SEARCH_BODIES_DEFAULT` | `'auto' \| 'none'` | `'auto'` | code.search `bodies` when the request omits it: "auto" fills bodies into the budget; "none" serves handles only. |
| `LOOM_CODE_SEARCH_MEMO_ENABLED` | `bool` | `True` | Memoise served code.search responses per (org, namespace, request shape) in loom.code_search_memo and replay within the TTL (route "+memo"); `fresh=true` bypasses it. |
| `LOOM_CODE_SEARCH_MEMO_TTL_S` | `int` | `1800` | Seconds a memoised response, and a cursor's stored pool, stay replayable. |
| `LOOM_CODE_SEARCH_MEMO_MAX_PAYLOAD_BYTES` | `int` | `200000` | Max bytes of a response the memo stores; a larger one is not memoised. 0 = no cap. |
| `LOOM_CODE_CORPUS_STATS_TTL_S` | `int` | `300` | Seconds the BM25 corpus statistics (term df, chunk count, mean length) are cached per process; 0 = no cache. Bounded by the term cap, oldest out. |
| `LOOM_CODE_CORPUS_STATS_MAX_TERMS` | `int` | `50000` | Max terms whose df the per-process cache holds; the oldest goes first. |
| `LOOM_CODE_SEARCH_CURSOR_ENABLED` | `bool` | `True` | A computed code.search / code.grep stores its ranked pool (no bodies) in loom.code_search_memo and issues `next_cursor`; the next page is a point read. The pool size is capped below. |
| `LOOM_CODE_SEARCH_CURSOR_POOL` | `int` | `100` | Units kept in a cursor's ranked pool; capped at 200. |
| `LOOM_CODE_SEARCH_FANOUT_TIMEOUT_MS` | `int` | `3000` | Wall deadline (ms) per namespace in a multi-namespace search; a late namespace is dropped and named in `fanout.dropped`. |
| `LOOM_CODE_SEARCH_FANOUT_MAX_NAMESPACES` | `int` | `8` | Max namespaces one request fans out to; can only lower the wire cap of 8. |
| `LOOM_CODE_SEARCH_PATTERN_MAX_FILES` | `int` | `1500` | A `pattern` admitting more files than this is refused with `pattern_too_broad` and the count (1,500 = 5% of a large namespace's files). |
| `LOOM_CODE_SEARCH_CARD_LEG` | `bool` | `False` | The card leg: file cards are searched by cosine and the top files' chunks join fusion as a capped RRF list (the history leg's shape). No per-request override. |
| `LOOM_CODE_SEARCH_CARD_LEG_FILES` | `int` | `15` | Files the card leg votes for. |
| `LOOM_CODE_SEARCH_CARD_LEG_UNITS_PER_FILE` | `int` | `2` | Skeleton chunks per voted file (the history leg's shape). |
| `LOOM_CODE_SEARCH_CARD_LEG_MODE` | `'skeleton' \| 'best_chunk'` | `'skeleton'` | What a voted file contributes: `skeleton` (its class skeletons) or `best_chunk` (its source chunks nearest the query by exact cosine). |
| `LOOM_CODE_SEARCH_CARD_LEG_CAP` | `float` | `1.0` | Max share of the best two-leg RRF score a card row contributes. |
| `LOOM_CODE_SEARCH_CARD_LEG_KINDS` | `str` | `'responsibility'` | CSV of prose question kinds the card leg votes on; `any` = all. |
| `LOOM_CODE_SEARCH_RERANK_DEFAULT` | `str` | `''` | Reranker code.search and code.ask use when the request names none (same values as the per-request `rerank`: a builtin or a name in LOOM_RERANK_ENDPOINTS_JSON; rerank="none" turns it off). Empty = no default rerank. |
| `LOOM_CODE_SEARCH_RERANK_KINDS` | `str` | `'responsibility'` | Prose question kinds the default rerank applies to (`any` = all); symbol-shaped or constrained searches are never default-reranked. |
| `LOOM_CODE_SEARCH_RERANK_TOP` | `int` | `20` | How many fused units the default rerank reads. |
| `LOOM_CODE_SEARCH_RERANK_EXCERPT_CHARS` | `int` | `300` | Characters of each candidate shown to the default rerank (path and signature first, then the body head). |
| `LOOM_CODE_SEARCH_CATALOGUE_PENALTY` | `float` | `1.0` | Fusion multiplier for settings-catalogue units (Settings.cpp and kin) on the kinds below; 1.0 = off. |
| `LOOM_CODE_SEARCH_PROSE_PENALTY` | `float` | `0.25` | Fusion multiplier for prose files (documentation, release notes, changelogs) unless the query asks for documentation or the search is constrained to paths; 1.0 = off. |
| `LOOM_CODE_SEARCH_CATALOGUE_KINDS` | `str` | `'responsibility'` | The question kinds the catalogue penalty applies to. |
| `LOOM_CODE_SEARCH_RRF_LEXICAL_WEIGHT_RESPONSIBILITY` | `float` | `1.0` | Lexical list's RRF weight for a responsibility question (vector = 1.0). 1.0 = equal weights. |
| `LOOM_CODE_SEARCH_ABSTAIN_CARD_BELOW` | `float` | `0.0` | A prose question whose nearest file card's cosine is below this abstains `off_domain`; independent of the card leg. 0 = off. |
| `LOOM_CODE_SEARCH_ABSTAIN_CONFIDENCE_BELOW` | `float` | `0.35` | Calibrated confidence below which a result is flagged `low_calibrated_confidence` (still returned). The deployment sets 0.30. |
| `LOOM_CODE_SEARCH_MEDIUM_RISK_FROM` | `float` | `0.4` | The calibrated confidences from which a code.search result reads `retrieval_risk` "medium" and "low" (below the first it reads "high"). Code embeddings score a right hit lower than memory text does: on the conceptual question set every right top 3 scored 0.40–0.63, so the memory plane's 0.55 / 0.75 labelled three in four right answers "high". |
| `LOOM_CODE_SEARCH_LOW_RISK_FROM` | `float` | `0.55` |  |
| `LOOM_CODE_SEARCH_HYDE_KINDS` | `str` | `'responsibility'` | The question kinds the hypothetical answer votes on. |
| `LOOM_CODE_SEARCH_HYDE_CAP` | `float` | `1.0` | The list's cap × the two-leg top (the history rule). |
| `LOOM_CODE_SEARCH_HYDE_MAX_TOKENS` | `int` | `220` | The passage's length budget. |
| `LOOM_CODE_SEARCH_DEADLINE_MS` | `int` | `0` | Default code.search request budget (ms) when the request sets no `deadline_ms`; what it cuts (late legs, rerank) makes the answer `partial`. 0 = none. |
| `LOOM_CODE_SEARCH_LLM_LEG_GRACE_MS` | `int` | `0` | Ms a model-backed leg (HyDE) may run past the last plain leg before it is dropped. 0 = no bound. |
| `LOOM_CODE_SEARCH_LLM_LEGS_DEFAULT` | `'auto' \| 'none'` | `'auto'` | Default `llm_legs`: `auto` runs the configured model-backed legs; `none` skips them. |
| `LOOM_CODE_SEARCH_CONCEPT_EXPAND_CAP` | `float` | `0.5` | Max share of the best two-leg RRF score a concept unit contributes. |
| `LOOM_CODE_SEARCH_CONCEPT_EXPAND_MAX_TERMS` | `int` | `8` | Identifiers + phrases kept from the model's answer. |
| `LOOM_CODE_SEARCH_CONCEPT_EXPAND_KINDS` | `str` | `'responsibility'` | CSV of prose question kinds the concept leg expands; `any` = all. |
| `LOOM_CODE_SEARCH_CONCEPT_EXPAND_MIN_IDF` | `float` | `2.0` | Expansion tokens below this BM25 idf are dropped (2.0 ≈ in more than an eighth of chunks). |
| `LOOM_CODE_SEARCH_PR_LEG` | `'off' \| 'issue' \| 'always'` | `'issue'` | History leg: merged-PR text scored lexically votes for the files each PR touched, as a third RRF list. "off" \| "issue" (issue-shaped queries) \| "always". |
| `LOOM_CODE_SEARCH_PR_LEG_POOL` | `int` | `30` | PRs read per query, files that may vote, chunk units per voted file. |
| `LOOM_CODE_SEARCH_PR_LEG_MAX_FILES` | `int` | `10` | Files that may vote per query. |
| `LOOM_CODE_SEARCH_PR_LEG_UNITS_PER_FILE` | `int` | `2` | Chunk units a voted file contributes. |
| `LOOM_CODE_SEARCH_PR_LEG_MIN_PRS` | `int` | `500` | A namespace with fewer live voting PRs than this skips the leg. |
| `LOOM_CODE_SEARCH_PR_LEG_CAP` | `float` | `0.5` | Per-unit cap on the leg's contribution as a fraction of the top two-leg fusion score. |
| `LOOM_CODE_SEARCH_PR_LEG_PATH_PREFIXES` | `str` | `''` | Only files under these prefixes (CSV) receive votes; "" = every file. |
| `LOOM_CODE_SEARCH_HISTORY_ROUTE` | `'off' \| 'number' \| 'on'` | `'number'` | History intent: a question about a change is answered from the PR plane as a `history` section. "off" \| "number" (explicit issue/PR numbers, point reads) \| "on" (also why/when/who-changed phrasings). |
| `LOOM_CODE_SEARCH_HISTORY_MAX_PRS` | `int` | `3` | PR rows served in the section (no bodies; inside the token budget). |
| `LOOM_CODE_SEARCH_HISTORY_ANCHOR_MAX_FILES` | `int` | `5` | Max PR files of a number intent that anchor the ranking (present files stay, absent ones insert at rank 1); a phrase intent never anchors. |

### Measured coverage

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_COVERAGE_IMPORT_ENABLED` | `bool` | `False` | Import per-test line coverage from an instrumented CI run into the test map (`method='coverage'`) and the per-file executed lines (`code_coverage_files`). Off by default; `loom code coverage-import` runs one import by hand whatever this says. |
| `LOOM_CODE_COVERAGE_SOURCE_URL` | `str` | `''` | ClickHouse HTTP endpoint holding the coverage tables (`checks_coverage_lines`, `checks`); "" = no source, the import is a no-op. |
| `LOOM_CODE_COVERAGE_SOURCE_USER` | `str` | `''` | User and password for the source endpoint. |
| `LOOM_CODE_COVERAGE_SOURCE_PASSWORD` | `str` | *(unset)* |  |
| `LOOM_CODE_COVERAGE_SOURCE_DATABASE` | `str` | `'default'` | Database of the source tables. |
| `LOOM_CODE_COVERAGE_NAMESPACES` | `str` | `''` | Namespaces (CSV) whose repository the source's runs cover; every other namespace is skipped. |
| `LOOM_CODE_COVERAGE_SHARDS` | `int` | `16` | The run's coverage is read in this many slices by file hash, two statements per slice. One slice must fit the source server's per-query memory, and a source with a per-statement-shape hourly quota (a public playground allows 100) must take a whole import plus a retry: a nightly run read in 16 slices is 16 statements of each shape. |
| `LOOM_CODE_COVERAGE_SPECIFIC_MAX_TESTS` | `int` | `200` | A line executed by more tests than this names none of them (shared plumbing every query runs); a test-map row needs a line under the cap. |
| `LOOM_CODE_COVERAGE_SOURCE_TIMEOUT_S` | `float` | `300.0` | Seconds one source query may take before the import stops (no rows are tombstoned by an import that did not finish). |
| `LOOM_CODE_COVERAGE_CHECK_PATTERN` | `str` | `'%per_test_coverage%'` | Check names (a LIKE pattern) whose runs carry per-test coverage. |
| `LOOM_CODE_COVERAGE_RUN_WINDOW_HOURS` | `int` | `6` | Jobs of one run start within this many hours of its newest job. |
| `LOOM_CODE_COVERAGE_IMPORT_INTERVAL_SECONDS` | `float` | `86400.0` | Seconds between import passes, and the pass's hard deadline. |
| `LOOM_CODE_COVERAGE_IMPORT_MAX_PASS_SECONDS` | `float` | `7200.0` |  |

## Context

context.pack, context offload, session briefs and the mentor's hints.

Source: `server/settings/_context.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CONTEXT_OFFLOAD_MAX_BYTES` | `int` | `8000000` | context.offload: largest single parked tool result, in bytes. |
| `LOOM_SESSION_BRIEF_LLM` | `bool` | `True` | session.brief_save. `session_brief_llm` off = every brief comes from the deterministic builder (goal, status, files, errors, rules, pointers; no decisions or dead ends). The model is the deployment's LLM when `session_brief_llm_model` has no key. A save condenses its events into windows of `session_brief_window_chars` (≈6k tokens each) and revises the brief once per window, at most `session_brief_max_windows` calls per save (the last window takes the remainder, condensed harder). |
| `LOOM_SESSION_BRIEF_WINDOW_CHARS` | `int` | `24000` | Condensed size, characters, of one brief window (floor 2000); a window holds about three times this much raw event text before it is condensed. |
| `LOOM_SESSION_BRIEF_MAX_WINDOWS` | `int` | `4` | Most windows (and model revision calls) per brief save; the last window takes the rest. |
| `LOOM_MENTOR_TTL_SECONDS` | `int` | `3600` | Seconds a per-process (org, namespace, agent) mentor profile is cached (a failed read ≤ 300 s). 0 disables the mentor everywhere (no pack section, no call hints, empty mentor.brief). |
| `LOOM_MENTOR_MAX_HINTS_PER_SESSION` | `int` | `6` | At most this many hints per session, each code once. |

## Erasure

Erasure: saga recovery, reclaim, purge and the external backends it cascades to.

Source: `server/settings/_erasure.py`


### Saga recovery and reclaim

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_ERASURE_RECOVERY_ENABLED` | `bool` | `True` | Replay erasure sagas stuck `in_progress` past a heartbeat from `steps_completed`. |
| `LOOM_ERASURE_RECOVERY_INTERVAL_SECONDS` | `int` | `60` | Seconds between passes of the erasure recovery worker. |
| `LOOM_ERASURE_RECLAIM_ENABLED` | `bool` | `True` | Physically remove purged rows: a purge of a wide table only masks its rows (lightweight DELETE), and large settled parts rarely merge, so the bytes stay. The erasure reclaim worker gives the purge table with the most dead bytes one `APPLY DELETED MASK` per pass. False = the masked rows wait for merges. |
| `LOOM_ERASURE_RECLAIM_INTERVAL_SECONDS` | `int` | `3600` | Seconds between passes of the erasure reclaim worker. |
| `LOOM_ERASURE_RECLAIM_MIN_DEAD_FRACTION` | `float` | `0.3` | A table is reclaimed once this share of its rows on disk are purged rows. |
| `LOOM_ERASURE_RECLAIM_MIN_DEAD_BYTES` | `int` | `1073741824` | ...and those rows hold at least this many bytes (default 1 GiB). |
| `LOOM_ERASURE_MUTATION_WAIT_S` | `int` | `3600` | Seconds one erasure step waits for its mutation (submitted with mutations_sync=0, polled in system.mutations); past it the saga fails naming the mutation. |

### External erasure backends

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CLICKSTACK_DATABASE` | `str` | `''` | ClickStack erasure: direct ALTER TABLE DELETE on the OTel tables in the same CH service. `clickstack_database` = HyperDX's DB ("" skips the leg); `clickstack_user_id_keys` = comma-separated Map attribute keys that carry user_id (ORed; depends on the emitting SDK). empty → skip cascade |
| `LOOM_CLICKSTACK_USER_ID_KEYS` | `str` | `'user_id,user.id,enduser.id'` | Comma-separated OTel Map attribute keys holding user_id, ORed in the delete. |
| `LOOM_EXTERNAL_ERASURE_TIMEOUT_S` | `float` | `30.0` | Per-request seconds for external trace/chat backend lookups and deletes during erasure. |

## Extraction

Fact extraction from messages and observed events.

Source: `server/settings/_extraction.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_EXTRACT_VALUE_UPDATES` | `bool` | `False` | Value-update supersession on the extraction write path (memory.app.value_updates): a fresh memory restating an existing one with a different value token marks the older one superseded. Costs one ANN lookup per written row; a per-request `detect_updates` overrides it. `_min_cosine` is the neighbour floor, set below the conflict threshold. |
| `LOOM_MEMORY_EXTRACT_VALUE_UPDATES_MIN_COSINE` | `float` | `0.75` | Least cosine similarity a neighbour needs to be checked as the older value of a fresh memory. |
| `LOOM_MEMORY_EXTRACT_OBSERVATION_DATE_DEFAULT` | `'request_time' \| 'none'` | `'request_time'` | The observation date a write is anchored to when the caller sends none. "request_time" = today's UTC date, so valid_at is never NULL for a live agent; "none" = undated. |
| `LOOM_MEMORY_CAPTURE_STRUCTURED_ANSWERS` | `bool` | `True` | Capture long / structured / enumerated assistant answers whole (not clipped at EXCERPT_LIMIT) so a positional follow-up resolves: at write time the prompt gains a structured-answer block and assistant-answer facts get a higher content_excerpt ceiling. The value column is untouched; takes effect only on (re-)ingest. |
| `LOOM_MEMORY_EXTRACT_COMPACT_PROMPT_MAX_CHARS` | `int` | `1200` | Current messages totalling at most this many characters are extracted with the compact system prompt (every rule, the schema anchors a short statement can match, no structured-answer block); longer ones keep the full prompt. 0 turns it off. |
| `LOOM_MEMORY_EXTRACT_EXISTING_SHORT_LIMIT` | `int` | `8` | Existing memories (fact rows only) an extraction in that short tier sees for update detection (sized on an extraction eval); 0 lets it see the caller's full limit. A longer transcript sees none: shown the namespace's facts, the extractor links later details to them instead of writing them. |
| `LOOM_OBSERVE_EXTRACT_NOTES_MIN_OBSERVATIONS` | `int` | `15` | A session's procedure and hint (observe notes) are mined only once the session holds this many non-trivial observations. 0 = any. |
| `LOOM_MEMORY_EXTRACT_CODING_SWEEP_MIN_USER_TURNS` | `int` | `4` | A coding capture keeps the coverage sweep's density / uncovered auto-gates only with at least this many user turns (on short sessions the sweep's output is mostly junk). 0 = always keep. An explicit namespace / per-call switch wins either way. |
| `LOOM_MEMORY_EXTRACT_VERIFY_TIMEOUT_S` | `float` | `45.0` | Per verifier call; a timeout keeps every fact. |
| `LOOM_MEMORY_EXTRACT_COVERAGE_SWEEP_MIN_CHARS` | `int` | `40` | The coverage sweep: turns the first pass cited nowhere get a second reading of their own (one more call), when the namespace or the call turns it on or the density / uncovered gates below fire. `_min_chars` skips "thanks!"-sized turns; `_max_turns` bounds the call. |
| `LOOM_MEMORY_EXTRACT_COVERAGE_SWEEP_MAX_TURNS` | `int` | `60` | Most uncovered turns one sweep call reads; later ones are left out. 0 = no cap. |
| `LOOM_MEMORY_EXTRACT_COVERAGE_SWEEP_MAX_SENTENCES` | `int` | `60` | Inside a turn the first pass cited, the individual statements no fact covers are swept too (a fact list, a long update), at most this many. |
| `LOOM_MEMORY_EXTRACT_COVERAGE_SWEEP_AUTO_MIN_STATEMENTS` | `int` | `10` | With the namespace and the call silent, the sweep still runs when the ingest is statement-dense — some turn holds at least this many statements (a fact list, a long update). 0 = never. Chatty conversation (one to four sentences a turn) never reaches it. |
| `LOOM_MEMORY_EXTRACT_COVERAGE_SWEEP_AUTO_MIN_UNCOVERED` | `int` | `0` | With the namespace and the call silent and no list-shaped turn, a conversation ingest still gets the sweep when the first pass left at least this many statements without a memory (lexical, LLM-free). 0 = never: on conversation the gate fires on nearly every session, so enabling it is the operator's token trade. |
| `LOOM_MEMORY_EXTRACT_COVERAGE_SWEEP_BATCH_ITEMS` | `int` | `0` | The listed turns and sentences are read in groups of this many items, one concurrent call each (more recall for more tokens). 0 = one call. |
| `LOOM_MEMORY_EXTRACT_SIZE_SEGMENT_MESSAGES` | `int` | `60` | Size segments: a session longer than this many messages is extracted in fixed windows of `_turns` turns (a turn = one user message + the assistant replies that follow). 0 disables. One call per window, concurrently. |
| `LOOM_MEMORY_EXTRACT_SIZE_SEGMENT_TURNS` | `int` | `20` | Turns per size segment window (a window never splits a turn). |
| `LOOM_MEMORY_EXTRACT_SIZE_SEGMENT_MAX` | `int` | `16` | Most size segment windows per session; past it the last window takes the remainder (windows grow past `_turns` turns first). |
| `LOOM_MEMORY_EXTRACT_SIZE_SEGMENT_CHARS` | `int` | `60000` | The same windows past this many characters of message content, whatever the message count; a window closes early once its turns carry this much (an agent transcript with tool results runs ~190K chars a session at the median). 0 disables the character budget (turn budget only). |
| `LOOM_OBSERVE_EXTRACT_BUFFER_CHARS` | `int` | `0` | Buffer threshold: a session whose un-distilled observations reach this many characters is distilled even without an explicit observe.crystallize, like a short-term buffer that flushes when full. 0 disables; the READY tag still works either way. |
| `LOOM_OBSERVE_EXTRACT_BUFFER_MIN_OBSERVATIONS` | `int` | `8` | Observations a session needs before the buffer threshold can fire (at least 1); a per-namespace value wins. |
| `LOOM_OBSERVE_EXTRACT_ENABLED` | `bool` | `False` | Deferred-extraction worker for the observe plane: distills READY rows (observe.record/crystallize) into durable memories through the memory.set_from_messages pipeline. Off by default (makes LLM calls). |
| `LOOM_OBSERVE_EXTRACT_INTERVAL_SECONDS` | `int` | `300` | Seconds between observe-extraction worker passes. |
| `LOOM_OBSERVE_EXTRACT_MAX_EVENTS_PER_SESSION` | `int` | `12` | Most transition events written per distilled session. |
| `LOOM_OBSERVE_SKIP_TOOLS` | `str` | `'mcp__loom__*,mcp__plugin_loom*,loom_memory_*,loom_knowle…` | Tool names (case-insensitive globs, comma-separated) whose observations are stored content-less and already distilled: loom's own MCP tools (observing one copies a memory outside the key the user can delete) and the harness's meta tools, which say nothing about the task. "" disables the skip. |
| `LOOM_EXTRACTION_QUEUE_ENABLED` | `bool` | `True` | Deferred set_from_messages replay. On by default: `defer` is opt-in per call, and an empty queue costs one SELECT per interval. |
| `LOOM_EXTRACTION_DEFER_AGENTS` | `str` | `''` | Agents whose set_from_messages runs deferred without the flag: comma-separated agent names, "*" = every service-account caller, "" = nobody. A call that sets `record_schema` is never deferred by this list (it reads `written` back), and an explicit defer=false still wins. |
| `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS` | `int` | `15` | Seconds between deferred-extraction queue passes. |
| `LOOM_EXTRACTION_QUEUE_MAX_BATCHES_PER_PASS` | `int` | `8` | Most queued batches one pass claims. |
| `LOOM_EXTRACTION_QUEUE_LEASE_SECONDS` | `int` | `600` | Seconds a claimed batch stays leased to one worker before another may claim it. |
| `LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS` | `int` | `3` | Attempts before a failing batch stops returning to `pending` and is marked failed. |
| `LOOM_EXTRACTION_QUEUE_NAMESPACE_CONCURRENCY` | `int` | `4` | Namespaces replayed concurrently per pass; batches inside one namespace stay serial in session-date order. |

## Knowledge

Knowledge documents and conversations: chunking, search floor, thread titles.

Source: `server/settings/_knowledge.py`


### Conversations

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY` | `float` | `0.3` | conversation.search cosine floor; without it the top_k nearest threads return however far. Stricter than memory recall: chat excerpts are noisy. |
| `LOOM_CONVERSATION_TITLER_ENABLED` | `bool` | `True` | LLM-title untitled threads in namespaces with conversation_logging_enabled (the Sessions "Recording" toggle is the off-switch). Skipped with the fake LLM. |
| `LOOM_CONVERSATION_TITLER_INTERVAL_SECONDS` | `int` | `1800` | Seconds between titler passes. |
| `LOOM_CONVERSATION_TITLER_WINDOW_HOURS` | `int` | `168` | Only threads with a turn in the last this-many hours are titled. |
| `LOOM_CONVERSATION_TITLER_MAX_THREADS_PER_PASS` | `int` | `10` | Threads one pass titles at most. |
| `LOOM_CONVERSATION_TITLER_MIN_TURNS` | `int` | `4` | Turns a thread needs before it is titled. |

### Chunking

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_KNOWLEDGE_CHUNK_SIZE` | `int` | `1500` | Sliding-window chunking on knowledge.add/upsert, in characters. chunk_size 0 = one chunk per doc (hurts long-form retrieval); stride < chunk_size overlaps. Smaller chunks mean more embed calls; 1500/1230 stays well under the text-embedding-3 context. characters per chunk; 0 disables |
| `LOOM_KNOWLEDGE_CHUNK_STRIDE` | `int` | `1230` | stride < chunk_size produces overlap |

## Mcp

The MCP transport: tool listing, lean results, session runs.

Source: `server/settings/_mcp.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MCP_LOOM_SEARCH_KNOWLEDGE_MIN_SIMILARITY` | `float` | `0.3` | loom_search drops knowledge hits below this cosine similarity (the conversation leg's floor, applied to the knowledge leg). |
| `LOOM_MCP_LOOM_SEARCH_PROFILE_MIN_SIMILARITY` | `float` | `0.38` | loom_search's profile leg: sections the words missed pass by cosine against `label\nvalue` at this hard floor. 0 = lexical only. |
| `LOOM_MCP_LOOM_SEARCH_PROFILE_SOFT_SIMILARITY` | `float` | `0.3` | The soft floor (a question about the user only). |
| `LOOM_MCP_LOOM_SEARCH_PROFILE_MARGIN` | `float` | `0.08` | The soft tier's lead over the namespace's runner-up. |
| `LOOM_MCP_LOOM_SEARCH_FLOOR_MODE` | `'drop' \| 'readmit' \| 'ids_only'` | `'drop'` | Sub-floor rows in loom_search: "drop" serves only what clears the floor; "readmit" (the memory.search default) pads top_k with below-threshold rows the reader is told to ignore; "ids_only" pads with empty bodies. |
| `LOOM_MCP_LOOM_SEARCH_PRECISION_FLOOR` | `float` | `-1.0` | Precision floor for loom_search memory hits. Negative = auto (midpoint of the embedder's calibrated noise floor and question→fact p25); 0 disables. |
| `LOOM_MCP_LOOM_SEARCH_FOREIGN_MARGIN` | `float` | `0.08` | Extra margin over the precision floor for hits from another namespace. |
| `LOOM_MCP_AUTH_MODE` | `'required' \| 'discovery_only' \| 'open'` | `'required'` | MCP transport auth mode (the endpoint exposes one tool per wire op, so an anonymous prober could otherwise enumerate every tool): "required"        — every JSON-RPC method requires a bearer. "discovery_only"  — initialize/tools/list/ping open; tools/call needs auth. "open"            — no auth on any method. Trusted-network only. Any other value fails at load: a mistyped mode must not boot as some mode. |
| `LOOM_MCP_TOOLSET` | `str` | `'core'` | Which tools `tools/list` shows — VISIBILITY only; every op stays callable by name over MCP and /v1/*. Rosters are the frozensets in server/operations/toolsets.py plus loom_search and loom_activate. "core"   — (default) _MCP_CORE_OPS (+ _MCP_CODE_CORE_OPS when mcp_code_tools is on). "coding" — _MCP_CODING_OPS + _MCP_CODE_CORE_OPS; the plugin selects it per request with `?toolset=coding`. "all"    — every op except _MCP_HIDDEN_OPS and the experimental ones no curated tier lists (they stay callable by name). "atomic" — CRUD + scratchpad + run ledger (_MCP_ATOMIC_OPS), policy-shaped descriptions. An unknown value is "core". |
| `LOOM_MCP_SCHEMA_PROFILE` | `'standard' \| 'full' \| 'compact'` | `'standard'` | tools/list schema profile: "standard" (curated prose + inputSchema), "full" (+ outputSchema) or "compact" (first paragraph, argument tree). Per request: `?schema_profile=`. |
| `LOOM_MCP_ALWAYS_LOAD_TOOLS` | `str` | `'code.search,code.symbol'` | Ops whose tools/list entry carries `_meta: {"anthropic/alwaysLoad": true}`, so a host that defers MCP tools behind tool search lists these up front and an agent calls them on its first turn instead of spending a turn loading them. Comma-separated op names; empty = none. |
| `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` | `int` | `1800` | Seconds without a tool call after which the mcp_session_run_sweep worker closes an MCP session's implicit run (one per token and `Mcp-Session-Id`, begun on the first run-scoped tool call); with no background workers anywhere, idle runs stay open. At least 60: each web process re-checks its cached run once a minute (mcp_runs.CHECK_INTERVAL_S), so a shorter window would close runs a process still treats as open. |
| `LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S` | `float` | `2.0` | A rate-limited tool call waits out its Retry-After once and re-checks before returning RATE_LIMITED (MCP clients don't retry). This is the longest Retry-After, seconds, that one retry waits out; a longer hint is refused at once, and 0 refuses every limited call at once. |
| `LOOM_MCP_CODE_TOOLS` | `bool` | `False` | List code.* tools on the MCP surface (core toolset union). Off = code ops are HTTP/SDK-only. |

## Memory

The memory plane outside search/ask/extraction: writes, conflicts, lifecycle, workers.

Source: `server/settings/_memory.py`


### Prompt safety and search priors

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_PRIOR_CAP` | `float` | `0.08` | Cap on memory.search's metadata priors (confidence, soft tag/category) so they break ties instead of re-ranking. 0.0 = uncapped; a request's `prior_cap` overrides it. |
| `LOOM_PROMPT_FENCE_SECRET` | `str` | *(unset)* | Memory bodies in every synthesis and extraction prompt sit between nonce-carrying data markers (server/memory/domain/prompt_fence.py). Key for the fence nonce, HMAC(secret, prompt): an identical prompt renders identically (the response cache keys on it) while a memory author cannot predict the closing marker. "" = derive from jwt_secret. |
| `LOOM_MEMORY_SCRATCHPAD_MAX_CHARS` | `int` | `2000` | Characters each injected working-memory block renders before it is cut with "(truncated)". |

### Deployment-specific names the fact filters recognise

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_QUALITY_ACTOR_NAMES` | `str` | `''` | Comma-separated names of the people whose sessions this deployment records ("jane,jane doe"). The junk-fact filters read "Jane ran a command …" like "The user ran a command …". Empty = only the generic actors (user, assistant, Claude, the agent). |
| `LOOM_MEMORY_QUALITY_PEER_HOST_PATTERN` | `str` | `''` | Regex (case-insensitive) matching the host name of another agent session on this deployment ("build-[0-9]+"); a fact whose subject is such a peer is coordination chatter, not a durable fact. Empty = no host pattern. |

### Write-path contracts and reinforcement

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_NEGATIVE_DEMOTE_THRESHOLD` | `int` | `0` | Negative memory.outcome signals after which a memory is quarantined (status 'rejected', excluded from search). 0 = off: negatives stay audit-only. |
| `LOOM_MEMORY_USED_REINFORCEMENT_WEIGHT` | `int` | `2` | Recall-equivalents one `used` event (run.complete memories_used, memory.outcome "used") adds to search-side reinforcement; the boost follows use, not exposure. 0 = recorded, not reinforcing. |
| `LOOM_MEMORY_HARM_REINFORCEMENT_WEIGHT` | `int \| None` | `None` | Recall-equivalents one `harm` event (a relied-on row in a run that ended badly) subtracts. Floored at the write-time base: harm cancels earned reinforcement, never ranks below cosine and never deletes. None = the `used` weight; 0 = counted in memory.health, not charged. |
| `LOOM_MEMORY_USED_CITED_REINFORCEMENT_WEIGHT` | `int \| None` | `None` | Recall-equivalents one `used_cited` event (memory.ask crediting the keys its answer cited) adds; `used` stays for labels an agent wrote. None = the `used` weight; 0 = recorded, not ranking. |
| `LOOM_MEMORY_REINFORCEMENT_CLOSER_WINDOW_DAYS` | `int` | `14` | Days a consumer's closer keeps its run-less reads credited. |
| `LOOM_MEMORY_REINFORCEMENT_CLOSER_CACHE_SECONDS` | `int` | `300` | Seconds one (org, consumer) closer answer is cached per worker. |
| `LOOM_MEMORY_LIST_VIEWER_LIMIT` | `int` | `100` | memory.list rows per call for a token below writer on the namespace; it pages with `offset` (wire cap stays 1000). 0 = no viewer cap. |
| `LOOM_MEMORY_SESSION_DIGEST_MAX_INPUT_CHARS` | `int` | `3000` | Transcript characters the session-digest call reads (~4 chars a token); 0 = the built-in 3000. |
| `LOOM_MEMORY_STRUCTURED_ANSWER_EXCERPT_LIMIT` | `int` | `4096` | content_excerpt ceiling for answer-bearing facts (question_topic set) when memory_capture_structured_answers is on, so an enumerated answer is not clipped mid-list; every other fact keeps EXCERPT_LIMIT (1,200 chars). |
| `LOOM_MEMORY_VALUE_ADVISORY_CHARS` | `int` | `30000` | A searchable memory.set value serializing longer than this many chars gets one advisory line (the write still lands): only the first EXCERPT_LIMIT chars are embedded, and past the embedder's 8,191-token cap nothing is. 30,000 chars ≈ 8k tokens. 0 disables. |

### Conflict detection

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CONFLICT_THRESHOLD` | `float` | `0.85` | memory.set flags a write `conflicted` when an active peer in the same (org, namespace, memory_type) sits at cosine ≥ this. Tuned for text-embedding-3-*; 1.0 disables. Per-namespace override: loom.namespaces.conflict_threshold. |
| `LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED` | `bool` | `False` | The conflict resolver: a leader-only worker that judges each write marked for it (a live peer of its type at cosine >= memory_conflict_resolver_min_similarity, or flagged `conflicted`) against its nearest peers in one LLM call, then retires, flags or clears. Unset, it is resolved at load: off when the LLM provider or conflict_resolver_model's provider is `fake`, else on (without the key, writes are marked and judged once it is set); true / false force it. ~$0.005 per judged write on gpt-6.1-sol. |
| `LOOM_MEMORY_CONFLICT_RESOLVER_MIN_SIMILARITY` | `float` | `0.6` | Peers down to this cosine (same memory_type) mark a write for the resolver and join the cluster it is judged in. The cosine `conflicted` flag stays at the conflict threshold. |
| `LOOM_MEMORY_CONFLICT_RESOLVER_MAX_PEERS` | `int` | `8` | Most peers judged with a row in one call, nearest first. |
| `LOOM_MEMORY_CONFLICT_RESOLVER_RETIRE_CONFIDENCE` | `float` | `0.95` | An update at or above this confidence retires the older row (newest_wins only). |
| `LOOM_MEMORY_CONFLICT_RESOLVER_CLEAR_CONFIDENCE` | `float` | `0.9` | Below this confidence a verdict changes nothing. At or above it a compatible verdict clears the pair's flag, and an update this build does not retire or a contradiction flags the pair for review. |
| `LOOM_MEMORY_CONFLICT_RESOLVER_BATCH` | `int` | `40` | Marked rows judged per pass, then rows already `conflicted` re-checked per pass. 48 rows at 8 at once are 6 rounds of ~5 s calls: a 30 s pass, then the 10 s wait, so ~70 rows a minute (55 at 7 s a call, 85 at 4 s). |
| `LOOM_MEMORY_CONFLICT_RESOLVER_RECHECK_BATCH` | `int` | `8` |  |
| `LOOM_MEMORY_CONFLICT_RESOLVER_CONCURRENCY` | `int` | `8` | Rows of a pass judged at once (LLM calls in flight). 1 judges them one at a time; doubling it roughly doubles the rows judged a minute, up to the provider's rate limit. |
| `LOOM_MEMORY_CONFLICT_RESOLVER_INTERVAL_SECONDS` | `float` | `10.0` | Seconds the resolver waits after a pass before the next one. |
| `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES` | `int` | `200` | Entries per bulk call that get the conflict check; later entries are written unchecked and counted. 0 = none are checked. |
| `LOOM_BULK_CONFLICT_CHECK_CONCURRENCY` | `int` | `8` | Bulk-path conflict checks run at once (at least 1). |
| `LOOM_MEMORY_WRITE_CANDIDATE_BATCH` | `int` | `6` | Facts per batched candidate lookup (conflict and value-update peers) on the write path; matches the per-fact concurrency so wall time stays flat. 0 = one statement per fact. |
| `LOOM_DUPLICATE_THRESHOLD` | `float` | `0.97` | Cosine ≥ this with a canonical-form mismatch marks a write 'duplicate' with a duplicate_of pointer instead of 'conflicted'. Per-namespace override: loom.namespaces.duplicate_threshold. |
| `LOOM_BLACKLIST_VERSION_CHECK_INTERVAL_SECONDS` | `float` | `2.0` | Blacklist cache invalidation: in-process invalidate_cache(), a one-row CH version watermark (loom.blacklist_version) polled at most every check interval (bounds cross-replica lag), and a hard TTL backstop. No Redis. |
| `LOOM_BLACKLIST_CACHE_TTL_SECONDS` | `int` | `300` | Seconds after which the blacklist cache reloads regardless of the version watermark. |

### Workers

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_RECALIBRATION_ENABLED` | `bool` | `True` | Periodically re-run the embedder calibration probe so thresholds follow a silently revised embedder. Fake providers skip. |
| `LOOM_RECALIBRATION_INTERVAL_SECONDS` | `int` | `604800` | Seconds between embedder calibration probes. |
| `LOOM_ENTITY_HEAL_INTERVAL_SECONDS` | `int` | `900` | Seconds between the entity extractor's mirror divergence heal, a FINAL-to-FINAL join of loom.memory and loom.memory_embeddings (343 GB/day if run every pass). Divergence arrives a few rows a day. |
| `LOOM_PROJECTION_REPAIR_ENABLED` | `bool` | `True` | Retry best-effort projections (chunks, evidence links, graph side tables) that failed after the primary write. Bounded and idempotent; unsupported ones stay in ops.projection_failures.list. |
| `LOOM_PROJECTION_REPAIR_INTERVAL_SECONDS` | `int` | `300` | Seconds between projection repair passes. |
| `LOOM_PROJECTION_REPAIR_MAX_FAILURES_PER_PASS` | `int` | `25` | Recorded projection failures one pass retries (at least 1). |
| `LOOM_USAGE_FOLD_ENABLED` | `bool` | `True` | Fold loom.memory_usage_totals onto the ranking columns (reinforcement boost, memory.list sort_by=score, auto-promote gate); off, they freeze at write-time values. Cadence is fixed in server/memory/workers/usage_fold.py. |
| `LOOM_ORPHAN_VECTOR_RECONCILE_ENABLED` | `bool` | `True` | Re-emit, with a tombstone, vector rows still live after their loom.memory row was tombstoned or erased, so search stops offering them. Bounded per pass; loom.integrity.orphan_vectors should read 0 between passes. |
| `LOOM_ORPHAN_VECTOR_RECONCILE_INTERVAL_SECONDS` | `int` | `600` | Seconds between orphan vector reconcile passes. |
| `LOOM_ORPHAN_VECTOR_RECONCILE_MAX_KEYS_PER_PASS` | `int` | `500` | Orphaned keys one pass re-emits tombstones for (at least 1). |
| `LOOM_ORPHAN_VECTOR_RECONCILE_GRACE_SECONDS` | `int` | `600` | Seconds before an unmatched embedding counts as orphaned, so a write whose two rows land apart is not taken for a deletion. |

### Procedures

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_PROCEDURE_STRIKE_LIMIT` | `int` | `3` | `procedure.evolve` quarantines a procedure whose trace log ends in this many consecutive failed runs (out of procedure.match and the pack until a run succeeds). 0 = off. |

### Consolidation

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CONSOLIDATION_MERGE_CONCURRENCY` | `int` | `4` | Clusters merged concurrently by memory.consolidate; 1 = sequential. |
| `LOOM_PROFILE_CONSOLIDATION_ENABLED` | `bool` | `False` | Fold a user's durable preference/decision memories into a versioned profile section (memory_type='profile') for profile.render / context.pack; skipped when refreshed within the cooldown. LLM calls; namespace opt-in via consolidation_enabled. |
| `LOOM_PROFILE_CONSOLIDATION_INTERVAL_SECONDS` | `int` | `21600` | Seconds between profile fold passes. |
| `LOOM_PROFILE_CONSOLIDATION_MAX_IDENTITIES_PER_PASS` | `int` | `10` | Users (org, namespace, user_id) one pass folds, most preference/decision rows first. |
| `LOOM_PROFILE_CONSOLIDATION_MIN_MEMORIES` | `int` | `5` | Live preference/decision memories a user needs before a profile is folded. |
| `LOOM_PROFILE_CONSOLIDATION_MEMORY_LIMIT` | `int` | `100` | Memories read into one fold, highest confidence then newest first. |
| `LOOM_PROFILE_CONSOLIDATION_COOLDOWN_SECONDS` | `int` | `86400` | Seconds after a profile section is written before it is folded again. |
| `LOOM_PROFILE_CONSOLIDATION_LABEL` | `str` | `'consolidated_profile'` | `memory_key` of the folded profile row. |
| `LOOM_REFLECTION_DISTILL_ENABLED` | `bool` | `False` | Reflection distillation: label recent sessions that carry no outcome (positive / negative / unclear into loom.session_outcomes_by_user), pair each confidently failed session with its nearest successful one and distil the difference into one hidden `reflection` memory. LLM calls; sessions labelled through run.complete are not re-judged. |
| `LOOM_REFLECTION_DISTILL_INTERVAL_SECONDS` | `int` | `3600` | Seconds between reflection distillation passes. |
| `LOOM_REFLECTION_DISTILL_LOOKBACK_DAYS` | `int` | `14` | Days back a pass looks for unlabelled sessions and for failed sessions to contrast. |
| `LOOM_REFLECTION_DISTILL_MAX_JUDGED_PER_PASS` | `int` | `40` | Sessions one pass sends to the outcome labeller. |
| `LOOM_REFLECTION_DISTILL_MAX_CONTRASTS_PER_PASS` | `int` | `10` | Failed sessions one pass tries to pair and distil. |
| `LOOM_REFLECTION_DISTILL_MIN_CONFIDENCE` | `float` | `0.7` | A judged label below this confidence neither pairs nor is paired with. |
| `LOOM_REFLECTION_DISTILL_MIN_PAIR_SIMILARITY` | `float` | `0.55` | Cosine floor between the failed and the successful summary; below it the sessions were not about the same thing. |
| `LOOM_ENTITY_EXTRACT_ENABLED` | `bool` | `True` | Async LLM entity tagging of memory rows (fills `entities` for search's entity match); off the memory.set hot path. |
| `LOOM_ENTITY_EXTRACT_INTERVAL_SECONDS` | `int` | `60` | Seconds between entity extraction passes that found work. |
| `LOOM_ENTITY_EXTRACT_IDLE_INTERVAL_SECONDS` | `int` | `300` | Sleep after a pass that found no unentitied row. |
| `LOOM_ENTITY_EXTRACT_FULL_SCAN_SECONDS` | `int` | `1800` | Each pass scans only memories of the last recent_seconds; the full candidate scan (without the value column) runs once per full_scan_seconds. full_scan_seconds 0 = full scan every pass. |
| `LOOM_ENTITY_EXTRACT_RECENT_SECONDS` | `int` | `7200` | Window, seconds, of recent memories each pass scans for untagged rows. |
| `LOOM_ENTITY_EXTRACT_FULL_SCAN_KEYS` | `int` | `5000` | Keys the full scan queues for the passes in between (refilled early when the queue runs dry). |
| `LOOM_ENTITY_EXTRACT_BACKLOG_PER_NAMESPACE` | `int` | `500` | Backlog keys per namespace in one refill, oldest first, round-robin across namespaces so one cannot starve the rest. 0 = org-wide newest first. |

### Dedup and expiry

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_BULK_DEDUP_SCOPE` | `'session' \| 'identity'` | `'identity'` | Bulk-path exact dedup. "session" = a byte-identical value is skipped only under the same key in the same session. "identity" = a live row anywhere in (org, namespace, agent, user_id) with the same content_sha256 is the fact; the entry becomes an `evidence` reinforcement of it, named in `identity_duplicates`. |
| `LOOM_MEMORY_EXPIRY_SWEEP_ENABLED` | `bool` | `True` | Apply each expired row's `on_expire` (tombstone \| demote \| anonymize) as a version row. Reads already hide expired rows; the sweep records it. |
| `LOOM_MEMORY_EXPIRY_SWEEP_INTERVAL_SECONDS` | `int` | `300` | Seconds between expiry sweep passes. |
| `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE` | `int` | `500` | Expired rows one sweep pass applies. |
| `LOOM_MEMORY_EXPIRY_DEMOTE_FACTOR` | `float` | `0.5` | `demote` multiplies confidence by this factor, never below the floor. |
| `LOOM_MEMORY_EXPIRY_DEMOTE_FLOOR` | `float` | `0.05` | Lowest confidence a `demote` (expiry or run.complete) leaves on a row. |

### Namespace health, knowledge graph, communities

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_NAMESPACE_HEALTH_TTL_SECONDS` | `int` | `3600` | Seconds a namespace's health summary is cached per process (a failed lookup at most 300 s). 0 disables the summary and its pack section. |
| `LOOM_ENTITY_COMMUNITIES_ENABLED` | `bool` | `False` | Label-propagation communities over the entity co-occurrence graph, per namespace, into loom.entity_communities (read by namespace.taxonomy and graph-mode expansion). A CPU pass over every namespace's edges. |
| `LOOM_ENTITY_COMMUNITIES_INTERVAL_SECONDS` | `int` | `21600` | Seconds between community partition passes. |
| `LOOM_ENTITY_COMMUNITIES_MAX_EDGES` | `int` | `200000` | Entity pairs read per namespace, strongest first; past it the rest are dropped and the namespace is reported truncated. |
| `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS` | `int` | `14` | A pass covers the namespaces with consolidation_enabled plus those whose graph was walked (facts.traverse / related / path, namespace.taxonomy) within this many days; 0 = the opted-in set only. |
| `LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION` | `float` | `0.002` | Leave the top fraction of entities by degree out of the partition, so generic tokens cannot fuse the graph into one community. A graph under 500 entities drops none. 0 = keep every entity. |
| `LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` | `float` | `0.5` | When one community holds more than this share of a namespace's entities, double hub_drop_fraction and re-partition, up to hub_drop_max_fraction (a code corpus parted at 0.016, 2.9%). 0 = fixed fraction. |
| `LOOM_ENTITY_COMMUNITIES_HUB_DROP_MAX_FRACTION` | `float` | `0.032` | Four doublings from the default; a graph still fused here is written as is and counted in the pass outcome. |
| `LOOM_ENTITY_COMMUNITIES_MIN_CO_OCCUR` | `int` | `1` | Co-occurrence count an entity pair needs to count as an edge. |

### Bulk contextualization

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_BULK_CONTEXTUALIZE_ALLOWED` | `bool` | `True` | memory.bulk_set honours contextualize=true (embed a chunk with its context) only when allowed; concurrency bounds the per-entry model calls, max_entries caps one call's spend. |
| `LOOM_MEMORY_BULK_CONTEXTUALIZE_CONCURRENCY` | `int` | `8` | Context model calls one bulk_set runs at once. |
| `LOOM_MEMORY_BULK_CONTEXTUALIZE_MAX_ENTRIES` | `int` | `200` | A bulk_set with more pending entries than this is not contextualized at all. |

### Graph walks (facts.traverse)

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE` | `float` | `0.01` | Relative hub rule: hold a reached node when the edge that reached it is at most this share of the node's degree (via_co_occur / degree). Scale-free. 0 = off; a request's `hub_share` overrides it. |
| `LOOM_FACTS_TRAVERSE_RANK_BY` | `'co_occur' \| 'specificity' \| 'cosine'` | `'cosine'` | What facts.traverse's per-hop trim ranks by: `co_occur` (most co-mentioned pairs), `specificity` (via_co_occur / degree; request option only, it fills hop 1 with singletons) or `cosine` (via_co_occur / sqrt(degree)). |

### Sensitivity and provenance

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_SENSITIVITY_AUTODETECT` | `bool` | `True` | With no explicit `sensitivity`, detect it from the value with regexes: credential shapes -> 'high' (never embedded, key-only reads, audited); email/phone -> 'medium' (embedded, labelled). Off = every row stays ''. |
| `LOOM_MEMORY_SENSITIVE_RETENTION_DAYS` | `int` | `0` | `medium`/`high` rows get expires_at = now + N days on the set and bulk paths (a shorter caller expiry wins) and, if the caller set none, this on_expire. 0 = off. |
| `LOOM_MEMORY_SENSITIVE_ON_EXPIRE` | `'tombstone' \| 'demote' \| 'anonymize'` | `'anonymize'` | `on_expire` given to a sensitive row whose caller set none. |
| `LOOM_SENSITIVITY_SWEEP_ENABLED` | `bool` | `True` | Daily sweep: re-label live rows written `sensitivity = ''` by a path that skipped the detector, as a new version per row; `high` rows' embeddings are tombstoned, never ALTER UPDATE. Covers the trailing window; the backlog is `loom sensitivity-sweep --apply`. |
| `LOOM_SENSITIVITY_SWEEP_INTERVAL_SECONDS` | `int` | `86400` | Seconds between sensitivity sweep passes. |
| `LOOM_SENSITIVITY_SWEEP_WINDOW_DAYS` | `int` | `2` | Days back the sweep re-labels rows created in. |
| `LOOM_SENSITIVITY_SWEEP_MAX_ROWS_PER_PASS` | `int` | `500` | Rows one sweep pass re-labels (at least 1). |
| `LOOM_SENSITIVITY_SWEEP_SHAPES` | `str` | `'credential,email'` | Shapes the sweep applies (comma-separated: credential, email, phone, other). `phone` (a ten-digit run) mostly matches ids, and a `medium` label demotes the row in ranking, so it is left to the operator's `--only-shape phone`. |

### Code-linked memories

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_CODE_LINKS_ENABLED` | `bool` | `True` | Link memories to the code they name (symbols and files of the repository their `repo:` tag or code namespace names) and keep the links current as the code changes; the namespace's `code_change_policy` decides how reads treat a memory whose code is removed or changed. Off stops linking and rechecks; reads still show the stored states but no longer hide a memory, as nothing keeps them current. |
| `LOOM_MEMORY_CODE_LINKS_INTERVAL_SECONDS` | `int` | `60` | Seconds between link passes. |
| `LOOM_MEMORY_CODE_LINKS_BATCH` | `int` | `500` | Memory writes one pass links (at least 1). |
| `LOOM_MEMORY_CODE_LINKS_SETTLE_SECONDS` | `int` | `60` | Seconds the pass stays behind the newest queued memory write, so inserts running at the same moment are visible before the cursor passes them (at least 10). A write visible later than this is passed and stays unlinked until a backfill reads it; a longer wait makes that rarer and links every write that much later. |
| `LOOM_MEMORY_CODE_LINKS_PENDING_DAYS` | `int` | `14` | Days a code-shaped name that names nothing on trunk yet (unmerged code) is retried before it is dropped. |
| `LOOM_MEMORY_CODE_LINKS_REVISE_PER_PASS` | `int` | `20` | Changed claims one pass re-checks with the LLM under the `revise` policy. |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | `bool` | `True` | Tag rows mentioning a protected attribute or common proxy with `attribute:<class>` at write time (gender, religion, health, age, family, income, ethnicity, sexuality, politics). A label, never a gate; the profile fold leaves such rows out. |

### Memory chunks

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_CHUNKS_MAX_PER_CALL` | `int` | `512` | Max ~800-char verbatim chunks one set_from_messages call writes (512 ≈ 400K chars; agent transcripts run ~190K chars a session at the median). Text past the cap is recorded in loom.memory_drops as chunk.split/truncated. |
| `LOOM_MEMORY_SET_CHUNK_MAX_CHUNKS` | `int` | `48` | Max chunks per memory.set value (48 ≈ 34 KB); the tail is recorded in loom.memory_drops as chunk.split/truncated. |

### Fact-leg reads of memory_embeddings

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_EMBEDDINGS_PATCH_PARTS_PROBE_SECONDS` | `int` | `60` | Seconds a process caches its count of patch parts on loom.memory_embeddings (server/core/vectors/patch_parts.py); the fact leg reads quantized codes only while the count is zero. |

## MemoryAsk

memory.ask: evidence assembly and answer synthesis.

Source: `server/settings/_memory_ask.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_ASK_RECENCY_WEIGHT` | `float` | `0.2` | memory.ask only: re-rank the synthesis window by similarity + weight * recency, recency = 1/(1 + age_days/14), so the freshest copy of a fact reaches synthesis. 0.0 = cosine only. |
| `LOOM_MEMORY_ASK_SYNTH_BUDGET_CHEAP_RATIO` | `float` | `0.7` | Fraction of the namespace's 24 h cost cap at which the cascade routes every ask cheap, with no escalation. 0 = off. |
| `LOOM_MEMORY_ASK_SYNTH_BUDGET_RATIO` | `float` | `0.75` | Adaptive synthesis output budget: max(window rule, ratio * evidence tokens), capped at memory_ask_synth_max_tokens_ceiling. The window rule (600 tokens at <= 15 memories, 900 above) is the floor; 0 = the window rule alone. |
| `LOOM_MEMORY_ASK_SYNTH_MAX_TOKENS_CEILING` | `int` | `8000` | Upper bound on the adaptive budget (a cost and p99 guard, not a target); the provider's own maximum still applies. |
| `LOOM_MEMORY_ASK_SYNTH_MAX_TOKENS` | `int` | `0` | Fixed synthesis output-token budget; 0 = adaptive. |
| `LOOM_MEMORY_ASK_SYNTH_CHEAP_MAX_EVIDENCE_TOKENS` | `int` | `2500` | The cheap tier answers only when the synthesis window holds at most this many evidence tokens (chars/4); above it the ask routes strong (cheap losses averaged 5.9k tokens). 0 = off. |
| `LOOM_MEMORY_ASK_SYNTH_ROUTE_PRIOR` | `str` | `''` | Routing prior from the consistency judge's contradicted rate per route. '' = rule-only router; "measured" = synth_policy.MEASURED_ROUTE_PRIOR; or a JSON object route -> rate. A strong-routed ask whose route rate is <= memory_ask_synth_route_prior_max_cheap goes cheap (reason "<rule>:prior_cheap"); hard stops (identifier, breadth, rich evidence, as_of, ungrounded) never move. |
| `LOOM_MEMORY_ASK_SYNTH_ROUTE_PRIOR_MAX_CHEAP` | `float` | `0.1` | Route contradicted rate at or below which the routing prior sends a strong-routed ask cheap. |
| `LOOM_MEMORY_ASK_SYNTH_MID_ROUTES` | `str` | `'uncertain'` | Which strong-routed reasons go to the mid tier: "uncertain", "off", or a comma list of reason tokens. |
| `LOOM_MEMORY_ASK_CONTEXT_MEMORIES` | `int` | `15` | How many top memories the synthesizer sees, independent of the caller's top_k; also sizes the fetch pool and, above 15, the answer-token budget. Raise it only with a strong memory_ask_synth_model, since a weak one dilutes over more items. |
| `LOOM_MEMORY_ASK_DEEP_EVIDENCE_POOL` | `int` | `30` | Deep-evidence pass: a selector picks up to memory_ask_deep_evidence_max_memories ranked candidates, their source turns are read (loom.memory_evidence via loom.memory_belief_sources, scoped rows first) and synthesis reruns with the raw turns. Runs on depth="raw" and temporal questions. This field: how many candidates the selector sees (30 = the whole 2x top_k pool). |
| `LOOM_MEMORY_ASK_DEEP_EVIDENCE_SELECT` | `'llm' \| 'top'` | `'llm'` | Who picks the memories to open: "llm" = one selection call over the pool; "top" = the top-ranked memories, no LLM call. A request's deep_select overrides. |
| `LOOM_MEMORY_ASK_DEEP_EVIDENCE_MAX_MEMORIES` | `int` | `5` | At most this many memories are opened per ask. |
| `LOOM_MEMORY_ASK_DEEP_EVIDENCE_MAX_CHARS_PER_MEMORY` | `int` | `2500` | Source text per opened memory and in total, so a window-linked memory cannot expand into its whole conversation. |
| `LOOM_MEMORY_ASK_DEEP_EVIDENCE_MAX_CHARS` | `int` | `8000` | Total source-text characters deep evidence may open across all memories of one ask. |
| `LOOM_MEMORY_ASK_DEFAULT_ANSWER_CHARS` | `int` | `0` | Default answer length in characters for asks that pass max_answer_chars=0, except breadth questions and structured count/preference/affect answers. 0 = unbounded (synthesis is 88% of ask median latency). |
| `LOOM_MEMORY_ASK_TEMPORAL_DEEP_SELECT` | `'llm' \| 'top'` | `'llm'` | Selector the temporal trigger uses: "llm" (one selection call, about +3 s p50 on temporal asks only) or "top" (no model call). Per-request deep_select overrides. |
| `LOOM_MEMORY_ASK_DEEP_SELECT_TIER` | `'cheap' \| 'routed'` | `'cheap'` | Client for the deep selector: "cheap" = the cascade's cheap client when configured, else the routed client; "routed" = the client that synthesizes the answer. |
| `LOOM_MEMORY_ASK_IDENTIFIER_INJECTION` | `bool` | `True` | When a query names an identifier (PR/issue number, @handle) that recall missed, inject the memories citing it (exact substring) into the synthesis pool. The lexical leg indexes only alpha tokens >= 3 chars, so bare numbers are otherwise unretrievable. Additive. |
| `LOOM_MEMORY_ASK_IDENTIFIER_CHASE_HOPS` | `int` | `3` | After injection, follow identifier-shaped tokens the injected memories name (a value that is a key, a PR naming a commit) for up to this many more exact-substring reads, adding at most memory_ask_identifier_chase_cap rows. 0 = off. Runs only when the question named an identifier. |
| `LOOM_MEMORY_ASK_IDENTIFIER_INJECT_CHUNKS` | `int` | `0` | Identifier injection also reads up to this many verbatim chunks (loom.memory_chunks), ranked by cosine to the question; 0 = facts only. |
| `LOOM_MEMORY_ASK_IDENTIFIER_INJECT_SET_FACTS` | `int` | `24` | A set-shaped question about an identifier reads this many fact rows (and twice as many chunks) from the exact fetch instead of eight. |
| `LOOM_MEMORY_ASK_DECISIVE_CHUNKS` | `int` | `6` | For a bug / error / flaky question, read this many verbatim chunks that cite the identifier and carry fix-or-cause vocabulary (root cause, Closes #, backtrace ...) into the pool ahead of the window. 0 = off. |
| `LOOM_MEMORY_ASK_IDENTIFIER_INJECT_RECENT` | `int` | `4` | For a disposition / premise / classification question, read this many newest facts and chunks citing the identifier into the pool, and tell the synthesizer the later-dated memory is current when memories on one subject disagree. 0 = off. |
| `LOOM_MEMORY_ASK_MENTION_WINDOW_CHUNKS` | `int` | `4` | A question about what was mentioned at a time reads up to this many verbatim chunks whose session date falls in the resolved window (±1 day), cosine-ranked; facts carry the event date, not the mention date. 0 = off. |
| `LOOM_MEMORY_ASK_IDENTIFIER_CHASE_CAP` | `int` | `8` | Fact rows the identifier chase (identifiers named by the rows the question's identifier reached) may add across all its hops. |
| `LOOM_MEMORY_ASK_IDENTIFIER_CHASE_CHUNK_CAP` | `int` | `8` | Verbatim chunks the chase may add, counted apart from memory_ask_identifier_chase_cap. 0 = facts only. |
| `LOOM_MEMORY_ASK_FACET_COMPLETION_MAX` | `int` | `60` | Facet completion: a count question pulls every memory in scope whose key shares the hit keys' attribute facet (user_maya_age -> _age), widening the window to memory_ask_aggregational_window. At most this many rows. |
| `LOOM_MEMORY_ASK_BRIDGE_COMPLETION_TOP_HITS` | `int` | `12` | Bridge completion: the other attributes of the entities the top hits name (key-prefix siblings) join the pool, so a question that names an entity by one attribute can answer with another. The top hits it reads, the rows it adds, the rows per prefix. |
| `LOOM_MEMORY_ASK_BRIDGE_COMPLETION_MAX` | `int` | `48` | Most rows bridge completion adds to the pool. |
| `LOOM_MEMORY_ASK_BRIDGE_COMPLETION_PER_PREFIX` | `int` | `6` | Most rows bridge completion adds per entity key prefix. |
| `LOOM_MEMORY_ASK_BRIDGE_ROW_HOPS` | `int` | `2` | Hops over the rows — the proper-noun objects of the top hits and their bridged siblings bridge again, twice at most, so a multi-hop question is answered from rows when the triple plane has no path. |
| `LOOM_MEMORY_ASK_LITERAL_COMPLETION_MAX` | `int` | `40` | Count questions: the raw turns naming the question's literal (a place, a number) join the pool, at most this many — a fact the extractor dropped is still a turn. |
| `LOOM_MEMORY_ASK_AGGREGATIONAL_WINDOW` | `int` | `40` | The synthesis window a count question widens to when facet or literal completion adds rows (never narrower than the window it had). |
| `LOOM_MEMORY_ASK_DECOMPOSE_MAX_PARTS` | `int` | `4` | Question decomposition (request `decompose`, off when unset): most sub-questions a decomposed question splits into. |
| `LOOM_MEMORY_ASK_DECOMPOSE_PART_TOP_K` | `int` | `8` | Rows retrieved for each decomposed sub-question. |
| `LOOM_MEMORY_ASK_WORLD_KNOWLEDGE` | `'forbid' \| 'allow'` | `'forbid'` | memory.ask general-knowledge bridging: forbid (default) keeps the synthesizer memory-only; allow lets it combine a memory fact with widely known public facts, marking each bridged sentence, and the output's `grounding` says whether it did. Per-request `world_knowledge` overrides. Agents that must never answer beyond the store keep forbid. |
| `LOOM_MEMORY_ASK_RELEVANCE_GATE_MIN_KEEP` | `int` | `3` | The fewest synthesis rows the similarity knee below may leave. |
| `LOOM_MEMORY_ASK_COUNT_WINDOW` | `int` | `30` | Synthesis window for count-shaped questions ("how many X", op=count), whose answer set is scattered across sessions (a missed item sat at rank 18). 0 = no widening. |
| `LOOM_MEMORY_ASK_CODE_ENRICHMENT` | `bool` | `True` | Kill-switch for code-plane enrichment: when a query names code-shaped identifiers (a symbol, a File.cpp, a snake_case setting), fuse the bound code index's file:line, symbol and recent PRs into the grounding pool. Additive and fail-open. The opt-in is the code namespace binding (_resolve_code_target): with no per-request override and no memory_ask_code_namespace, enrichment is skipped, hence default on. |
| `LOOM_MEMORY_ASK_CODE_HOP_SYMBOLS` | `int` | `4` | Symbols the retrieved window names (not only the question) are looked up in the bound code index too, up to this many, one code.symbol definition each, appended after the question's own grounding rows. 0 = off. |
| `LOOM_MEMORY_ASK_PR_GROUNDING` | `bool` | `True` | For each PR the query or grounding references, inject its authoritative code_pr_history record (author, merged_at, title) as a high-trust grounding fact; the synthesizer prefers it over a memory's older claim (_PR_RECORD_NOTE). Gated by the code binding (_resolve_code_target); additive, fail-open, as-of aware. See _inject_pr_facts. A judged arm shows fewer control regressions with it on than off. |
| `LOOM_MEMORY_ASK_PR_TEMPORAL_GATE` | `bool` | `True` | For an as_of query, drop grounding memories that cite a PR opened after the anchor (an under-dated bulk ingest can name a later PR). Open dates come from code_pr_history.opened_at; a no-op without as_of or a bound code index; a PR with an unknown open date is never gated. See _gate_future_prs. |
| `LOOM_MEMORY_ASK_CODE_STALENESS` | `bool` | `True` | Check the code locations a retrieved memory asserts against the live index and flag rotted ones (symbol moved or renamed) so synthesis prefers the current site. Read-time, additive, gated by the code binding; ambiguous symbols are skipped (code_anchor), never false-flagged. |
| `LOOM_MEMORY_ASK_CODE_NAMESPACE` | `str` | `''` | Code namespace enrichment reads when the request sets none (a per-request override wins). '' = enrichment skipped: with many repos and branches indexed as separate namespaces there is no safe global default. Pair with memory_ask_code_org when the org differs. |
| `LOOM_MEMORY_ASK_ORDER_WINDOW_MAX_DAYS` | `int` | `95` | An ordering question with a time cue of up to this many days reads the user's turns from that window with the first synthesis, so a member the extractor left as an aside is listed. |
| `LOOM_MEMORY_ASK_CUE_WINDOW_TURNS_TOP_K` | `int` | `24` | How many user turns dated inside the question's short time window ("a couple of days ago") the not-recorded second reading adds, whatever words they carry. |
| `LOOM_MEMORY_ASK_WHEN_EVIDENCE_TOP_K` | `int` | `6` | Dated raw turns the not-recorded second reading recalls. |
| `LOOM_MEMORY_ASK_CODE_ORG` | `str` | `''` | Org of memory_ask_code_namespace; "" = code_org, then the ask's own org. |
| `LOOM_MEMORY_ASK_CONSISTENCY_CHECK` | `bool` | `False` | After synthesis one judge call (the cheap client when the cascade is on, else the synth client) labels the answer entailed / neutral / contradicted against the memories it cites (consistency, consistency_score, consistency_note). Off: one extra LLM call per synthesized ask. |
| `LOOM_MEMORY_ASK_CONSISTENCY_MODE` | `'inline' \| 'deferred'` | `'deferred'` | Where the judge runs. "inline" awaits it and returns the verdict on the wire. "deferred" answers first, leaves the wire fields empty, and writes the verdict as a memory.ask.consistency ledger row (kind='verdict', joined by request_id) plus the contradicted usage event; a precedent is stored only once the verdict is in. A request's consistency field pins either; benches that read the label pin "inline". |
| `LOOM_MEMORY_ASK_PREMISE_CHECK` | `bool` | `False` | When the window holds a pinned or human-verified memory, one judge call asks whether the question presupposes something it contradicts; if so the synthesizer holds the trusted fact and corrects the premise (premise_conflict / premise_note on the wire). One read per ask; an LLM call only with trusted rows in the window. |
| `LOOM_MEMORY_ASK_SET_WINDOW` | `int` | `40` | Synthesis window for a set question ("what books has she read"), like a count's, since its items sit one per session. 0 = off. MemoryAskInput.set_window overrides. |
| `LOOM_MEMORY_ASK_SET_FULL_SCAN` | `int` | `300` | A count or set question reads up to this many rows (window and search pool) instead of the 40-row pool; 0 = off. Such an ask costs about four times a plain one. |
| `LOOM_MEMORY_ASK_SET_FULL_SCAN_CHUNK_SHARE` | `float` | `0.15` | When the whole-set scan widens the pool, cap its chunk share so it is mostly fact rows. 0 = uncapped. |
| `LOOM_MEMORY_ASK_ENTITY_FOLLOW_MAX` | `int` | `3` | A chain question ("the citizenship of the CEO of X") follows the first hop's answer entity into one more retrieval per bridge entity: at most this many entities, this many rows each. |
| `LOOM_MEMORY_ASK_ENTITY_FOLLOW_TOP_K` | `int` | `6` | Rows retrieved for each followed bridge entity. |
| `LOOM_MEMORY_ASK_KNOWLEDGE_TOP_K` | `int` | `5` | Passages the knowledge.search leg appends to the synthesis window. |
| `LOOM_MEMORY_ASK_EXCLUDE_UNVERIFIED_EXTERNAL` | `bool` | `True` | memory.ask keeps declared-external, unattested rows out of the synthesis pool (they still surface as hits). Off = they are synthesized like any other row. |
| `LOOM_MEMORY_ASK_PRECEDENTS` | `bool` | `False` | Serve the earlier synthesized answer to the same question and shape, with no LLM call, when the synthesis window is byte-identical (same keys, same content_sha256 per key); otherwise synthesize and store. A per-request precedents field overrides. Max age 0 = only the evidence decides. |
| `LOOM_MEMORY_ASK_PRECEDENTS_MAX_AGE_HOURS` | `int` | `0` | Hours a stored precedent stays servable; 0 = no age bound, the evidence alone decides. |
| `LOOM_MEMORY_ASK_PRECEDENTS_NAMESPACES` | `str` | `''` | Comma-separated fnmatch globs the precedent flag applies to; '' = every namespace. Scope it to namespaces whose traffic is real repeats (e.g. "support-*"): on a shared server a paired A/B would answer its second arm from the first. |
| `LOOM_MEMORY_ASK_PRECEDENTS_FAST_MAX_AGE_HOURS` | `int` | `1` | With precedents on, an eligible precedent is served before retrieval when no plane its namespaces read was written since (loom.namespace_write_marks), the blacklist is unchanged and every cited row is live with the same hash. Hours a precedent stays servable on that fast path (a bound on rank drift, not staleness); 0 = no bound. |
| `LOOM_MEMORY_ASK_LLM_CACHE_DEFAULT` | `bool` | `True` | Exact-prompt LLM response cache for every memory.ask synthesis-path call, keyed by sha256(system, user, max_tokens, response_format) per model in loom.llm_response_cache; a miss writes fire-and-forget. The evidence is in the key, so a changed memory misses. The namespace globs (comma-separated fnmatch, '' = all) scope the default; a per-request llm_cache wins (benches measuring synthesis send false). |
| `LOOM_MEMORY_ASK_LLM_CACHE_NAMESPACES` | `str` | `''` | Comma-separated fnmatch namespace globs the LLM cache default applies to; '' = all. |
| `LOOM_MEMORY_ASK_PENDING_EVIDENCE_TURNS` | `int` | `5` | memory.ask pending evidence: the turns of in-flight deferred batches that share words with the question join the synthesis window as raw rows (source="pending"). Most pending turns read into the synthesis window per ask. |
| `LOOM_MEMORY_ASK_PENDING_EVIDENCE_BATCHES` | `int` | `20` | Newest in-flight deferred batches the pending-evidence read scans. |
| `LOOM_MEMORY_ASK_SYNTHESIZE_ROUTE_MAX_DOCS` | `int` | `3` | A summary-shaped question ("summarize the report") routes to knowledge.synthesize over the documents the knowledge search concentrates on, falling through to the memory path when none concentrates. Documents that route may synthesize over at most. |

## MemorySearch

memory.search: legs, fusion, ranking and gates.

Source: `server/settings/_memory_search.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_SEARCH_DEFAULT_NAMESPACE_SCOPE` | `'this' \| 'all_mine'` | `'this'` | Default `namespace_scope` when a request leaves it unset. "this" reads the addressed namespace only; "all_mine" also reads the caller's own namespaces (the MCP loom_search asks for it by name), see docs/concepts/identities-and-namespaces.md#reading-several-namespaces. |
| `LOOM_MEMORY_SEARCH_FANOUT_KINDS` | `str` | `'memory'` | Namespace kinds an all_mine fan-out spans (memory.search, knowledge.search, conversation.search, MCP loom_search): comma list over loom.namespaces.kind; a namespace with no kind row stays; "" = every kind. Code and QA indexes are reached by name or their own tools (measured sweep of 24 namespaces: 6.2 s). |
| `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES` | `int` | `32` | Upper bound on how many of the caller's namespaces an all_mine fan-out reads besides the addressed one; a search names the rest in `namespaces_skipped`. Every leg issues at least one statement per namespace. A guard, not a tuning knob: set well above what one person holds, so it bounds a runaway sweep rather than trimming normal ones. |
| `LOOM_MEMORY_SEARCH_KEY_AXIS` | `'auto' \| 'on' \| 'off'` | `'auto'` | Key-embedding axis on the fact leg: "on" scores least(value_dist, key_dist) and reads both vector columns (measured 399 vs 203 MB/query on a 30k-row namespace); "off" reads the value vector only; "auto" turns it on per namespace when its key-reuse share is at or above memory_search_key_axis_auto_reuse_share (cached for memory_search_key_axis_auto_ttl_seconds; a failed probe reads as off). A per-request `key_axis` wins. |
| `LOOM_MEMORY_SEARCH_KEY_AXIS_AUTO_REUSE_SHARE` | `float` | `0.05` | "auto" key-axis threshold: share of a namespace's keys written more than once. 0.05 sits between product namespaces and chat namespaces. |
| `LOOM_MEMORY_SEARCH_KEY_AXIS_AUTO_TTL_SECONDS` | `int` | `600` | Seconds a namespace's "auto" key-axis probe result is cached before it is re-read. |
| `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | `float` | `0.38` | No-probe fallback for the query-rewrite gate the calibration reports (calibration.get's scalar row): ≈ question→fact P10 for text-embedding-3-small. |
| `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS` | `int` | `1` | Bounds on the recursive fallback cascade (iterative-judge retry), shared through one SearchBudget: recursive rounds and wall-clock ms; whichever trips first stops further fallbacks. The first-pass retrieve always runs. |
| `LOOM_MEMORY_SEARCH_FALLBACK_MAX_WALL_MS` | `int` | `4000` | Wall-clock ms the fallback cascade may spend across all rounds before it stops. |
| `LOOM_MEMORY_SEARCH_FALLBACK_MAX_LLM_CALLS` | `int` | `2` | Finer caps inside a round: LLM calls and vector queries a fallback may spend before it is denied. |
| `LOOM_MEMORY_SEARCH_FALLBACK_MAX_VECTOR_QUERIES` | `int` | `12` | Vector queries one fallback round may issue before further ones are denied. |
| `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD` | `int` | `3` | First-stage pool = top_k × multiplier, per retrieval profile, capped at memory_search_max_pool_top_k (all_mine searches multiply by namespace and sub-query count). |
| `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_RERANK` | `int` | `6` | Rerank gets the widest pool: a reranker only reorders what the first stage surfaced (measured: items it should promote sat at rank 11-30). Affects rerank-enabled searches only. |
| `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_DEEP` | `int` | `5` | A deep-budget search's pool multiplier (the budget class `deep`). |
| `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K` | `int` | `150` | Upper bound on the first-stage pool size; a caller's top_k above it raises the bound to top_k. |
| `LOOM_MEMORY_SEARCH_QUALITY_POOL_MULTIPLIER` | `int` | `3` | Pool widening factor for a quality-filtered search (loom_search, context.pack), applied before junk is dropped. |
| `LOOM_MEMORY_SEARCH_PREFERENCE_FALLBACK` | `float` | `0.15` | Boost for any preference-typed/tagged row on any query; 0.0 makes it intent-gated (only a query naming the attribute earns +0.3). |
| `LOOM_MEMORY_SEARCH_SOURCE_CLASS_BOOST_ELIGIBLE` | `str` | `''` | Comma list of source_class values whose rows may earn the user-derived rank terms (preference, prior group, reinforcement); "" = every row. Leaving out "extracted" keeps an inference from outranking a statement by its priors. |
| `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD` | `float` | `0.3` | Top-hit similarity below which a search abstains (weak abstention): the scalar gate for fake embedders or LOOM_ADAPTIVE_THRESHOLDS=false; a calibrated namespace or embedder derives its own. |
| `LOOM_MEMORY_SEARCH_IDENTIFIER_GROUNDING` | `bool` | `True` | Identifier-grounding abstain: when the query carries a salient identifier (PR/issue number, test id, @handle) and no returned hit contains any of them, abstain with abstain_code "entity_absent" regardless of the top cosine — catches the on-topic but wrong neighbour the cosine gate cannot. |
| `LOOM_MEMORY_SEARCH_IDENTIFIER_GROUNDING_MAX_TOKENS` | `int` | `32` | The grounding leg applies only to queries of at most this many whitespace tokens; longer inputs (a PR body, a path list) carry incidental numbers. 0 = no cap. |
| `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT` | `float` | `0.0` | Recency-intent searches ("latest", "current …") re-rank the fused pool by date: the newest dated hit gains this much, the oldest and undated hits nothing. 0.0 = off. A namespace can set its own in retrieval_defaults. |
| `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD` | `float` | `0.45` | No-probe fallback for the question→fact P25 gate (≈ P25 for text-embedding-3-small): the loom_search precision floor and calibration.get's scalar row read it. |
| `LOOM_MEMORY_SEARCH_DEFAULT_MODE` | `'semantic' \| 'lexical' \| 'hybrid' \| 'bm25_hybrid' \| 'related' \| 'graph' \| 'rrf' \| 'temporal'` | `'semantic'` | search_mode used when the caller sets neither search_mode nor a recipe. The wire default stays "rrf"; planner shape rules still apply on top. |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` | `float` | `0.15` | Weight of the decayed-reinforcement rank term (recalls and positive memory.outcome signals, fading with time since last reinforcement). 0 disables; the term is capped below relevance. |
| `LOOM_MEMORY_SEARCH_SENSITIVITY_DEMOTE` | `float` | `0.1` | Additive penalty on rows tagged `sensitivity:medium\|high` unless the query has contact intent. 0.0 disables. |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS` | `float` | `90.0` | Days after which a use's reinforcement weighs half (the base half-life the use-scaled one below grows from). |
| `LOOM_SEARCH_TRACE_LOG_ENABLED` | `bool` | `False` | Per-query recall telemetry into loom.search_trace_log (query, returned keys+scores, abstain, latency), fire-and-forget. Off by default: query text is logged, so it is an operator opt-in. |
| `LOOM_MEMORY_QUERY_NODES_ENABLED` | `bool` | `False` | Past queries as nodes: one loom.query_nodes row per memory.search (normalised text, embedding, returned keys), gated on the namespace's capture consent; the erasure saga deletes the table. |
| `LOOM_MEMORY_SEARCH_SET_EXPANSION_LEG_TOP_K` | `int` | `2` | Set-expansion instance legs (collective nouns -> "has a sister"): each phrase runs as a narrow sub-query leg of top_k rows; a row joins only above min_similarity, at most max_added per search, never above the main leg's best row. |
| `LOOM_MEMORY_SEARCH_SET_EXPANSION_MIN_SIMILARITY` | `float` | `0.5` | Similarity a set-expansion leg's row must exceed to join the pool. |
| `LOOM_MEMORY_SEARCH_SET_EXPANSION_MAX_ADDED` | `int` | `5` | Most rows the set-expansion legs add to one search. |
| `LOOM_MEMORY_EXPOSURE_LEDGER_ENABLED` | `bool` | `True` | Read-side exposure ledger in loom.memory_exposures: one row per read (token subject, returned keys, query digest never the text, scope) plus an hourly per-actor roll-up. Carries no user content. |
| `LOOM_MEMORY_SEARCH_MIN_SIMILARITY` | `float` | `0.22` | Cosine floor: hits below it are dropped as noise (ANN always returns top_k). The backstop when rerank is bypassed or fails. |
| `LOOM_MEMORY_SEARCH_FLOOR_MODE` | `'readmit' \| 'ids_only' \| 'drop'` | `'readmit'` | Sub-floor handling when the caller sets top_k: "readmit" (fill k, tag below_floor), "ids_only" (fill k with empty bodies), "drop". A request's floor_mode overrides. |
| `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` | `float` | `2.5` | Per-attempt bound (seconds) on the query embedding; after the retries the search answers 503 EMBED_TIMEOUT. 0 = unbounded. |
| `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` | `int` | `1` | Retries after a timed-out first attempt, each with the bound above. |
| `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS` | `int` | `6` | A query of at most this many words embeds each all-lowercase word of 4+ letters in the identifier spelling the caller's rows in the addressed namespace use most ("datalakecatalog" -> "DataLakeCatalog"; never "what" -> "What"). One read of the namespace beside the embed (100-300 ms for 40k rows), reused for a minute; fail-open. 0 = off. |
| `LOOM_MEMORY_SEARCH_EXCERPT_CHARS` | `int` | `200` | Body length (chars) for bodies="excerpt" (the MCP search default). |
| `LOOM_MEMORY_SEARCH_EXCERPT_BODIES_K` | `int` | `15` | Above this top_k a search that pinned no `bodies` serves excerpts, and per-hit explanation / score_components ride only under `explain: true`. Keys, order and scores are unchanged. 0 = never. |
| `LOOM_MEMORY_SEARCH_PRIVATE_FIRST_STOP_SIMILARITY` | `float` | `0.3` | Visibility-ordered private-first fallback (non-scoped tokens): the best private-namespace similarity that skips the public pass. "Private" is namespace visibility, not the caller's role; scoped tokens use their priority order instead. |
| `LOOM_MEMORY_SEARCH_TIER_STOP_SIMILARITY` | `float` | `0.4` | Tier waterfall stop: a per-tier best similarity at or above this stops the search before lower-priority tiers. A control cutoff, not a return filter (memory_search_min_similarity still gates rows); keep it above the noise band (~0.10-0.25) or a middling hit hides a stronger one. |
| `LOOM_MEMORY_SEARCH_KEY_MATCH_MIN_STRENGTH` | `float` | `0.5` | A key matches when it covers at least this share of the query's idf-weighted stems (idf over the namespace's own keys). |
| `LOOM_MEMORY_SEARCH_KEY_MATCH_BOOST_SCALE` | `float` | `0.03` | Boost (cosine units) of a full-strength key match: a tie-break inside the pool's band, not a lever over it (a 0.5 scale lifted wrong rows over gold). |
| `LOOM_MEMORY_SEARCH_ASSERTED_BOOST_SCALE` | `float` | `0.03` | Tie-break boost (cosine units) for a hand-asserted fact row (source_class 'asserted', not a hint:/procedure/summary:/obs: row) against derived rows; never a lever over a stronger match. 0 disables. |
| `LOOM_MEMORY_SEARCH_KEY_MATCH_MODE` | `'always' \| 'gated' \| 'off'` | `'gated'` | When the key-match arm runs: "always" (every search; measured 585-781 ms p50 on live namespaces), "gated" (only when an added row could reach the served window: rrf/rerank pool, pool short of top_k, an under-covered identifier, or a pooled key already matching), "off". |
| `LOOM_MEMORY_SEARCH_PER_KEY_CAP` | `int` | `3` | Rows per memory_key in the final block (0 = no cap), freeing slots taken by one key re-extracted per session (38 of 60 questions on one eval). Skipped for aggregational / temporal_diff shapes. |
| `LOOM_MEMORY_SEARCH_CHUNK_DATE_SOURCE` | `'session' \| 'parent'` | `'session'` | Digest-anchored expansion: a session digest in the pool's top-m summaries adds that session's top-k facts as candidates (+300 ms ask p50); MemorySearchInput.digest_descent overrides. chunk_date_source: "session" dates a chunk by its conversation's digest, "parent" by the session's first fact anchor. |
| `LOOM_MEMORY_SEARCH_DIGEST_DESCENT_TOP_M` | `int` | `3` | Top session digests in the pool that digest descent expands. |
| `LOOM_MEMORY_SEARCH_DIGEST_DESCENT_K` | `int` | `5` | Facts digest descent adds as candidates from each expanded session. |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED` | `'query' \| 'recent' \| 'both'` | `'query'` | graph_seed: query \| recent \| both — under recent/both the entities of the identity's newest memories (recent_memories, at most recent_max) join the graph seeds. diversify: "none" \| "session" — under "session" at most diversify_session_cap hits per source session (0 = max(2, top_k // 3)); per-request `diversify` overrides. |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED_RECENT_MEMORIES` | `int` | `5` | Newest memories of the identity whose entities seed the graph under recent/both. |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED_RECENT_MAX` | `int` | `8` | Most recent-memory entities added to the graph seeds under recent/both. |
| `LOOM_MEMORY_SEARCH_BM25_LIVE_TAIL` | `bool` | `True` | bm25_hybrid merges the once-a-minute BM25 roll-ups with a live aggregate over rows newer than their mark, so df and corpus size are exact. Off = roll-ups alone (up to ~70 s behind a write). |
| `LOOM_MEMORY_SEARCH_PAGE_TTL_S` | `int` | `1800` | Seconds a parked page (a `paging=true` search's tail) stays fetchable before the TTL merge reclaims it. |
| `LOOM_MEMORY_SEARCH_ENTITY_BOOST_TOP_ENTITIES` | `int` | `20` | Entity-boost candidate shape (server/memory/app/search/ann_entity_boosts.py). loom.entity_vectors holds one row per (entity, memory_key) edge, so the lookup ranks distinct entities first, then expands to edges (a flat LIMIT surfaced as few as 1 distinct entity). top_entities = distinct entities kept; max_pairs = hard bound on the edge expansion (a hub can carry hundreds), which bounds work rather than results. |
| `LOOM_MEMORY_SEARCH_ENTITY_BOOST_MAX_PAIRS` | `int` | `400` | Most (entity, memory_key) edges the entity-boost lookup expands to. |
| `LOOM_MEMORY_SEARCH_TRUST_WEIGHT` | `float` | `0.0` | Weight on the provenance term (source_class + verified); 0 = off, a strict no-op in every mode. |
| `LOOM_MEMORY_EMBED_ANSWER_TOPIC` | `bool` | `True` | Prepend the extractor's `question_topic` to the embedded text of assistant-direct-answer facts, so a topic-only follow-up matches them. The stored value is untouched; takes effect on (re-)ingest. |
| `LOOM_MEMORY_SEARCH_ENTITY_SUMMARIES_TOP_N` | `int` | `5` | Pre-built entity summaries fetched per namespace on that leg. |
| `LOOM_MEMORY_SEARCH_TOP_K_CEILING_VIEWER` | `int` | `0` | Per-token top_k ceiling by the token's role on the namespace: viewer and writer each have one, admin and above are uncapped. 0 = no ceiling. The applied ceiling rides the response as `top_k_ceiling` and the ledger as `out_top_k_ceiling`. |
| `LOOM_MEMORY_SEARCH_TOP_K_CEILING_WRITER` | `int` | `0` | top_k ceiling for a writer token on the namespace; 0 = no ceiling. |
| `LOOM_MEMORY_RRF_CODE_SHAPE_LO` | `float` | `0.2` | RRF adaptive lexical weight: code/path-shaped queries (CamelCase, extensions, separators, digit runs) down-weight the lexical leg so fusion leans semantic; prose keeps full weight. shape at/below which w_lex == 1.0 (prose) |
| `LOOM_MEMORY_RRF_CODE_SHAPE_HI` | `float` | `0.3` | shape at/above which w_lex == floor (code) |
| `LOOM_MEMORY_RRF_LEXICAL_WEIGHT_FLOOR` | `float` | `0.1` | never fully zero the lexical leg |
| `LOOM_MEMORY_SEARCH_CHUNK_MIN_SHARE` | `float` | `0.0` | Smallest window share verbatim chunks keep when the pool has them. 0.0 = off. |
| `LOOM_MEMORY_SEARCH_ABSTAIN_TOP_SIM_SCOPE` | `str` | `'first'` | Which cosine the abstain gate reads: "first" = the fused rank-1 row, "window" = the best cosine among returned rows. |
| `LOOM_MEMORY_SEARCH_FACT_CODES_FIRST` | `'auto' \| 'on' \| 'off'` | `'auto'` | Codes-first main fact leg (`vector_search_use_quantized_codes = 1`), safe only while loom.memory_embeddings has no patch part (ClickHouse skips the lazy rescore under patch parts). "auto" follows a 60 s system.parts probe (gauge loom.memory_embeddings.patch_parts); "on" / "off" pin it. The setting ships only where the replica probe finds it. |
| `LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER` | `float` | `3.0` | Over-fetch of the codes pass on the main leg; x1 is too low (it changes the top-10 on a quarter of queries). |
| `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS` | `int` | `0` | `max_threads` pin on the main fact statement; 0 = no pin (threads follow the marks); 4 bounds each statement to four threads. Unpinned costs more CPU and peak memory per statement. |

## Model

Models: which model does each job.

Source: `server/settings/_models.py`


### Defaults

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_LLM_MODEL` | `str` | `''` | The deployment's LLM: memory extraction, the conflict judge, rerank and every job below left "". Empty takes the resolved provider's default model (gpt-6-luna / claude-haiku-4-5 / fake-echo-1). |
| `LOOM_LLM_FALLBACK_MODEL` | `str` | `''` | Model to answer with when a client's provider is still unavailable after its retries (429 / 5xx / transport / timeout) or rejected the request (400 / 413 / 422). Counted in `loom.llm.fallback`. 401 / 403 / 404 are configuration and never fall back. "" = fail the op. |
| `LOOM_EMBEDDING_MODEL` | `str` | `'fake-ngram-3072'` | Embeddings for memories, queries and code chunks. fake-ngram-3072 needs no network or key; production uses text-embedding-3-* (OPENAI_API_KEY). Change it together with `embedding_dim`; a new embedding space needs a full re-embed. |

### Memory

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_ASK_SYNTH_MODEL` | `str` | `''` | memory.ask synthesis (the strong tier when the cascade is on). |
| `LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP` | `str` | `''` | memory.ask cheap tier. "" = off: every ask uses memory_ask_synth_model. When set, server/memory/domain/synth_policy.py routes easy asks cheap and hard ones strong; an empty, uncited or hedging cheap answer escalates to strong once. route_tier / route_reason / synth_model on the output record the choice. |
| `LOOM_MEMORY_ASK_MID_MODEL` | `str` | `''` | memory.ask mid tier. "" = none. With a model set, strong-routed asks whose reason is in memory_ask_synth_mid_routes go mid; an empty, uncited or hedging mid answer escalates to strong once. |
| `LOOM_MEMORY_EXTRACT_VERIFY_MODELS` | `str` | `'claude-opus-5-5'` | The durability check on a coding capture: one batched call per capture per listed model (CSV) judges each fact durable or not and folds same-claim paraphrases and replaced states. A fact is dropped only when every model says drop; folds come from the first. Fails open. "" turns it off. |
| `LOOM_CONFLICT_RESOLVER_MODEL` | `str` | `'gpt-6.1-sol'` | The conflict resolver's judge (memory_conflict_resolver_enabled). Its confidence thresholds were set on this model; a cheaper one is only safe for clears. |
| `LOOM_PROFILE_CONSOLIDATION_MODEL` | `str` | `''` | The profile fold. |
| `LOOM_REFLECTION_DISTILL_MODEL` | `str` | `''` | Reflection distillation: the outcome labeller and the contrast writer. |
| `LOOM_CODE_LINK_REVISE_MODEL` | `str` | `''` | The code-link `revise` judge: does a memory's claim still hold for its code's new text. |
| `LOOM_SESSION_BRIEF_LLM_MODEL` | `str` | `'gpt-6-luna'` | Session briefs; without its provider's key the deployment's LLM writes them. |

### Code

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_CODE_ASK_LLM_MODEL` | `str` | `''` | code.ask synthesis. Answers are memoized fleet-wide, so a stronger model costs once per unique question. |
| `LOOM_CODE_SEARCH_HYDE_MODEL` | `str` | `''` | code.search HyDE: writes a hypothetical indexed passage whose embedding runs a second vector list (see code_search_hyde_*). "" = off. |
| `LOOM_CODE_SEARCH_CONCEPT_EXPAND_MODEL` | `str` | `''` | code.search concept expansion: maps a prose, unconstrained question to likely code identifiers — via this model, or `cards` / `cards:<n>` (symbols of the n nearest file cards) — fused as a capped lexical list. "" = off. |
| `LOOM_CODE_BISECT_LLM_RERANK_MODEL` | `str` | `'gpt-6-luna'` | code.bisect_candidates' one-call rerank of the fused top 20. |
| `LOOM_CODE_SUMMARY_MODEL` | `str` | `''` | File and module summary cards. |

### Tracker

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_DUPCHECK_RERANK_MODEL` | `str` | `'gpt-6-luna'` | tracker.duplicate_check's rerank call. |
| `LOOM_TRACKER_DUPCHECK_JUDGE_MODEL` | `str` | `''` | tracker.duplicate_check's judge call. |

### Observability

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_RETRIEVAL_JUDGE_MODEL` | `str` | `'gpt-6.1-sol'` | The sampled retrieval judge (retrieval_judge_sample_rate). Deliberately a different model from the ones answering: a same-model judge measures agreement, not quality. |

## Namespace

Namespace registration, write marks and bench-namespace GC.

Source: `server/settings/_namespaces.py`


### Namespace write marks

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_NAMESPACE_WRITE_MARKS_FLUSH_INTERVAL_S` | `float` | `5.0` | loom.namespace_write_marks is an in-process max per (org, namespace, plane), flushed as one INSERT this often. |

### Namespace registration and GC

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_NAMESPACE_AUTOREGISTER_ENABLED` | `bool` | `True` | A principal's first write into a namespace with no loom.namespaces row inserts one: owner = token subject, `kind = 'bench'` under a bench prefix, `retention_days = 0` (never a TTL). No ALTER. |
| `LOOM_NAMESPACE_AUTOREGISTER_SKIP_PREFIXES` | `str` | `'namespace.,code.,admin.,ops.,grants.,policy.,blacklist.,…` | Op-name prefixes the auto-register hook ignores: ops on a namespace rather than into it, the code plane (registers `kind = 'code'` itself), and the operator planes. |
| `LOOM_NAMESPACE_BENCH_PREFIXES` | `str` | `'lme-,mb-,td-,locomo-,kcurve-,wpath-,bench-,ma-verbatim-,…` | Comma-separated name prefixes of throwaway harness namespaces: stamped `kind = 'bench'` by `namespace.register` (when no kind is given) and auto-register, and scanned by `namespaces-gc`. |
| `LOOM_NAMESPACE_GC_WORKER_ENABLED` | `bool` | `True` | namespace_gc: `kind = 'bench'` namespaces with live memory and no write for idle_hours are purged by one list-scoped erasure saga per batch, at most sagas_per_pass a pass, kept to loom tables. |
| `LOOM_NAMESPACE_GC_INTERVAL_SECONDS` | `int` | `3600` | Seconds between namespace GC passes. |
| `LOOM_NAMESPACE_GC_IDLE_HOURS` | `int` | `24` | Hours without a write before a bench namespace is purged. |
| `LOOM_NAMESPACE_GC_BATCH` | `int` | `200` | Namespaces one erasure saga purges, oldest write first (at least 1). |
| `LOOM_NAMESPACE_GC_SAGAS_PER_PASS` | `int` | `1` | Erasure sagas one GC pass submits (at least 1). |

## Observability

Telemetry, audit, tracing and the ops surface.

Source: `server/settings/_observability.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_LOG_LEVEL` | `'debug' \| 'info' \| 'warning' \| 'error' \| 'critical'` | `'info'` | Root log level. (Bind host/port are flags of `loom serve`, not settings.) |
| `LOOM_LOG_FORMAT` | `'text' \| 'json'` | `'text'` | "text" (dev) or "json" (production). |
| `LOOM_OPS_OP_MIX_WINDOW_DAYS` | `int` | `7` | ops.health's create/read/update/delete mix over the window: warns when reads per write exceed the bar (polling load) or, after `min_writes`, the update share falls below its bar (append-only bank). |
| `LOOM_OPS_OP_MIX_READS_PER_WRITE_WARN` | `float` | `20.0` | Reads per write above which the `op_mix` check warns (polling load). |
| `LOOM_OPS_OP_MIX_UPDATE_SHARE_WARN` | `float` | `0.01` | Share of writes that are updates below which the check warns (append-only bank). |
| `LOOM_OPS_OP_MIX_MIN_WRITES` | `int` | `500` | Writes in the window before the update-share warning can fire. |
| `LOOM_OPS_PROCEDURE_PLANE_WINDOW_DAYS` | `int` | `7` | ops.health `procedure_plane`: warns when ≥ `min_packs` packs flowed and none showed a procedure while procedures exist, or when success retention over ≥ `min_pairs` pairs is below `retention_warn`. |
| `LOOM_OPS_PROCEDURE_PLANE_MIN_PACKS` | `int` | `50` | Context packs in the window before "no pack showed a procedure" can warn. |
| `LOOM_OPS_PROCEDURE_PLANE_RETENTION_WARN` | `float` | `0.5` | Success retention (fraction) below which the `procedure_plane` check warns. |
| `LOOM_OPS_PROCEDURE_PLANE_MIN_PAIRS` | `int` | `20` | Pairs the retention is measured over before its warning can fire. |
| `LOOM_AUDIT_VERIFIER_ENABLED` | `bool` | `True` | Periodic re-hash of audit rows; a mismatch writes `audit.tamper_detected`. Per-row detection, not a hash chain (see server/observability/audit.py). |
| `LOOM_AUDIT_VERIFIER_INTERVAL_SECONDS` | `int` | `3600` | Seconds between audit verifier passes (row re-hash and checkpoint sealing). |
| `LOOM_AUDIT_CHECKPOINT_WINDOW_SECONDS` | `int` | `3600` | Length, seconds, of one audit window. Each closed window is sealed as a chained checkpoint once `grace_seconds` old, and the last `verify_windows` are recounted every pass: a short recount is a deletion, which per-row hashes cannot see. |
| `LOOM_AUDIT_CHECKPOINT_GRACE_SECONDS` | `int` | `600` | Seconds after a window closes before it is sealed. |
| `LOOM_AUDIT_CHECKPOINT_VERIFY_WINDOWS` | `int` | `24` | Most recent sealed windows recounted every pass (at least 1). |
| `LOOM_AUDIT_CHECKPOINT_MAX_SEAL_PER_PASS` | `int` | `48` | Most windows one pass seals; a backlog is sealed over later passes. |
| `LOOM_OTEL_EXPORTER` | `'' \| 'ch' \| 'otlp'` | `''` | OTel span exporter: "ch" writes to `default.otel_traces` on the same service, "otlp" ships to otel_exporter_otlp_endpoint (headers: comma- separated K=V), "" = auto (otlp when the endpoint is set, else ch). |
| `LOOM_OTEL_CODE_COUNTS_CACHE_S` | `int` | `3600` | Seconds between refreshes of the per-namespace code-plane COUNT gauges (one FINAL scan each of code_files, code_chunks, code_symbols and code_edges, on the lease holder). The counts move once an index run, so an hour is enough; each refresh reads every live code row. 0 = every export. |
| `LOOM_OTEL_CODE_GREP_REPLAY_CACHE_S` | `int` | `3600` | Seconds between recounts of the code.grep wrong-abstain gauge (a `content` scan per namespace with abstains). 0 = gauge off. |
| `LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES` | `int` | `20` | Max namespaces `loom.memory.agent_labels_24h` names per probe (top N by label total); the rest sum under `loom.namespace = "other"`. 0 = no cap. |
| `LOOM_OTEL_INVARIANTS_PROBE_S` | `int` | `3600` | Seconds between the state probe's whole-table readings (embed backlog, integrity invariants), taken on the lease holder only. The invariants read 0 and exist to catch drift, so an hour is enough; each reading is one FINAL pass over memory, memory_embeddings and entity_vectors. |
| `LOOM_OTEL_RETENTION_CENSUS_S` | `int` | `3600` | Seconds between retention censuses (`loom.retention.*`: per-org counts over 7- and 45-day windows; one FINAL pass over loom.memory on the lease holder). 0 = every export. |
| `LOOM_OTEL_CENSUS_SCOPE` | `'leader' \| 'process'` | `'leader'` | Who takes the whole-table censuses (invariants, code-plane counts, retention, agent labels, storage): "leader" = only the supervisor-lease holder, "process" = every process (costly). |
| `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` | `int` | `60` | Metric export cadence, seconds. Spans keep their own 3 s batch delay, which bounds span loss at SIGTERM. |
| `LOOM_OTEL_METRICS_DELTA_TEMPORALITY` | `bool` | `False` | Delta temporality for counters and histograms (idle series write no row). Off: the dashboards (deploy/hyperdx_dashboards.md) read cumulative rows — flip together with them. |
| `LOOM_OTEL_GAUGE_HEARTBEAT_S` | `int` | `0` | Write a gauge point only when its value changed or this many seconds after the last write; 0 = every export. Off because gauge tiles fill gaps with 0 and would draw a sawtooth. |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `str` | `''` | OTLP/HTTP collector base URL (`/v1/traces` is appended); set with `otel_exporter` = "", it selects the otlp exporter. |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS` | `str` | *(unset)* | Comma-separated K=V headers sent to that endpoint, e.g. `Authorization=Bearer <token>`. |
| `LOOM_OTEL_SERVICE_NAME` | `str` | `'loom'` | OTel resource `service.name` and `deployment.environment` stamped on every span, log and metric. |
| `LOOM_OTEL_ENVIRONMENT` | `str` | `'dev'` | The `deployment.environment` value. |
| `LOOM_OTEL_EXCLUDED_URLS` | `str` | `'/v1/health'` | Comma-separated path regexes the FastAPI instrumentation opens no span for (health polls). "" = instrument every path. |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | `float` | `1.0` | Scales every per-op `budget_p99_ms` (tuned for local CH) at the comparison site; set e.g. 20.0 on Cloud so SLO alerts don't fire on baseline RTT. |
| `LOOM_LATENCY_BUDGET_OVERRIDES_BY_CONSUMER` | `str` | `''` | JSON map {consumer: {op \| "*": ms \| "<n>x"}}: an integer is a budget in ms, "3x" multiplies the op's declared budget. Checked after runtime `admin/budget.set` overrides, before per-op ones; malformed = logged once and ignored. '' = none. |
| `LOOM_WORKER_LEDGER_ROWS` | `bool` | `True` | One agent_events row per worker pass that spent something or failed (operation worker.<name>.pass, kind='worker'); loom.worker_status records every pass. |
| `LOOM_OTEL_ENABLED` | `bool` | `True` | Master switch for OTel emission. `init_otel()` also refuses under pytest regardless: server/main.py calls it at import, and a test run must never ship rows into production observability tables. |
| `LOOM_LANGFUSE_URL` | `str` | `''` | Langfuse: with both set, OTel spans are forwarded to Langfuse's OTLP endpoint as a second span processor beside the ClickHouse exporter, retrieval scores are posted, and the erasure saga makes real HTTP DELETE calls (empty = log intent only). The token is `pk-lf-…:sk-lf-…`. |
| `LOOM_LANGFUSE_ADMIN_TOKEN` | `str` | *(unset)* | Langfuse `pk-lf-…:sk-lf-…` key pair for erasure, scores and the OTLP forward. |
| `LOOM_LANGFUSE_SAMPLE_RATE` | `float` | `1.0` | Fraction of TRACES forwarded, sampled by trace id so a trace arrives whole. Span volume is dominated by worker embeddings; turn down in production. |
| `LOOM_LANGFUSE_SPAN_DENYLIST` | `str` | `'ch.query,ch.insert,GET /v1/health,POST /v1/health'` | Span-name prefixes never forwarded: high-volume infrastructure spans with no LLM work (they belong in ClickStack). |
| `LOOM_LANGFUSE_CIRCUIT_FAILURE_THRESHOLD` | `int` | `5` | Score-POST circuit breaker: consecutive failures that open it. |
| `LOOM_LANGFUSE_CIRCUIT_COOLDOWN_S` | `float` | `30.0` | Seconds the open breaker waits before a trial call (floor 1). |
| `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` | `'off' \| 'query' \| 'all'` | `'off'` | How much LLM prompt/completion text is recorded, by what the prompt holds: "off"   — none (default; consent is given, not withdrawn). "query" — only legs whose prompt is the user's own search text (hyde, query_rewrite, enrichment, …). "all"   — also legs carrying retrieved memories or conversation turns (inline_rerank, iterative_judge, extract, session_summary, …). A namespace's `capture_generation_io` overrides it (false ⇒ off, true ⇒ all, NULL ⇒ this). It also gates loom.search_trace_log.query. Token counts, cost, latency and quality scores are never gated. |
| `LOOM_LANGFUSE_GENERATION_IO_MAX_CHARS` | `int` | `2000` | Per-field cap on recorded prompt/completion text; truncation adds an explicit marker. |
| `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` | `float` | `0.0` | Fraction of searches sent to an independent LLM judge, written back as a `retrieval_relevance` score (unlike loom's own confidence scores, it shares no state with retrieval). 0.0 = off: it costs money per search; a few percent (e.g. 0.05) suits production. |

## Provider

Embedding and LLM providers.

Source: `server/settings/_providers.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_EMBEDDING_DIM` | `int` | `3072` | Inherits ModelSettings for `llm_model`, which the provider resolution below reads and fills. Vector width the embedder is asked for and the schema is sized from; the width probed from the embedder at boot wins when they differ. Change it together with `embedding_model`. Unset with a `local:*` model, it is that model's own width. |
| `LOOM_EMBEDDING_PROVIDER` | `'' \| 'openai' \| 'gemini' \| 'compatible' \| 'local' \| 'fake'` | `''` | "" = infer from `embedding_model` (fake-ngram-* → fake, text-embedding-* → openai, gemini* → gemini, local/local:* → local CPU embedder); or pin "openai", "gemini", "local", "fake". Switching providers changes the embedding space: a full re-embed is required. |
| `LOOM_EMBEDDING_BASE_URL` | `str` | `''` | API root for an explicitly selected OpenAI-compatible embedding service, e.g. https://provider.example/v1. Empty is invalid for compatible providers. |
| `LOOM_EMBEDDING_SEND_DIMENSIONS` | `bool` | `False` | Send the optional dimensions request parameter to a compatible endpoint. False uses its native output width; responses must still match embedding_dim. |
| `LOOM_EMBEDDING_CACHE_SHARED` | `bool` | `True` | Shared query-embedding cache in loom.embedding_cache behind the per-process LRU, keyed (model, sha1 of normalised text). Only single-text (query) embeds up to `embedding_cache_shared_max_chars` (0 = no cap) use it. Off = process cache only. |
| `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | `int` | `2000` | Longest query text, characters, that uses the shared cache; 0 = no cap. |
| `LOOM_EMBEDDING_CACHE_SIZE` | `int` | `10000` | The per-process query-embedding LRU: entries held (0 = cache nothing) and seconds each lives. |
| `LOOM_EMBEDDING_CACHE_TTL_S` | `float` | `3600.0` | Seconds one per-process LRU entry lives. |
| `LOOM_ADAPTIVE_THRESHOLDS` | `bool` | `True` | Derive the similarity gates from this embedder's cosine distribution at boot (server/memory/app/embedder_calibration.py). False pins the scalar defaults (tuned for text-embedding-3-small) — re-bench after an embedder change. Fake embedders and probe failures always use the scalars. |
| `LOOM_CALIBRATION_REFRESH_SECONDS` | `float` | `60.0` | Seconds between each replica's re-read of the shared calibration (loom.embedder_calibration), so every replica serves the same gates. |
| `LOOM_LLM_PROVIDER` | `str` | `''` | "openai" \| "anthropic" \| "fake"; "" = infer from LOOM_LLM_MODEL's name (gpt*/o1*/o3*/o4*/chatgpt* → openai, claude* → anthropic, fake* → fake), else from the key present (OPENAI_API_KEY, then ANTHROPIC_API_KEY), else fake. Resolved once at load; an explicit value is never overridden. Run `make bench-quick` before making anthropic a default. |
| `LOOM_REQUIRE_REAL_PROVIDERS` | `bool` | `False` | A fake or key-less provider is a `warn` in /v1/health `problems`; true (production) makes it an `error` and `status: degraded`, which preflight and monitors catch. Never changes the HTTP code (503 is ClickHouse-only). |
| `LOOM_LLM_OPENAI_REASONING_EFFORT` | `str` | `''` | Reasoning effort sent to OpenAI models. "" = the lowest the model accepts (learned from its refusals, see server/llm/traits.py); set to pin one value for every call. |
| `LOOM_LLM_ANTHROPIC_EFFORT` | `str` | `''` | Thinking effort sent to Claude models as `output_config.effort`: "low" \| "medium" \| "high"; "" = send none (the model's own default). A model that does not take it refuses it once and is not sent it again. |
| `LOOM_LLM_HIDDEN_REASONING_RESERVE_MAX` | `int` | `16384` | Most output tokens a call adds over the caller's cap for a model's hidden reasoning. The reserve per model is learned from the reasoning tokens its replies report (twice the most it has spent) and capped here. |
| `LOOM_LLM_REQUEST_TIMEOUT_S` | `float` | `300.0` | Seconds an LLM call waits for its reply before the client retries. Generous on purpose: it only catches a hung connection, so a slow model or a long output never hits it. Calls that must be fast set their own, shorter deadline (HyDE, search budgets). |
| `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` | `str` | `'{"bench": {"llm_cache": true}}'` | JSON {class: {"llm_cache": bool, "strong": bool, "mid": bool}}: `llm_cache` overrides the class's cache default (a per-request pin wins); `strong`/`mid` false bars the cascade from that tier. Unlisted = every tier eligible. |
| `LOOM_RERANK_ENDPOINTS_JSON` | `str` | `'{}'` | JSON {name: url}: the rerank endpoints a request's `rerank`, a namespace's `rerank_endpoint` and LOOM_CODE_SEARCH_RERANK_DEFAULT may name. Names are short identifiers, URLs http(s). Loom posts candidate text only to these. |
| `LOOM_LLM_RERANK_SKIP_ABOVE` | `float` | `0.62` | Skip the rerank when the vector top hit's cosine is ≥ this (strong matches rank reliably; the rerank costs ~500 ms). Fallback when the adaptive gate has no probe. |
| `LOOM_EMBEDDING_WORKER_ENABLED` | `bool` | `True` | Recovery worker: retries failed embeds and reconciles orphan scope='shared' rows (the write path embeds inline). |
| `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS` | `int` | `5` | Seconds between recovery worker passes while there is work. |
| `LOOM_EMBEDDING_WORKER_IDLE_INTERVAL_SECONDS` | `int` | `30` | Sleep after an idle pass (nothing to embed); the 5 s interval resumes once a pass writes a row. |
| `LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` | `int` | `1800` | Orphan reconciliation: the full memory × embedding anti-join runs once per `full_scan_seconds`; other passes check only memories of the last `recent_seconds`. 0 = full join every pass. |
| `LOOM_EMBEDDING_WORKER_ORPHAN_RECENT_SECONDS` | `int` | `900` | Age window, seconds, of the memories a non-full orphan pass checks. |
| `LOOM_LLM_RESPONSE_CACHE_TABLE` | `str` | `'loom.llm_response_cache'` | The table the wrapper reads and writes; an isolated server whose boot migrations are off points this at its own scratch mirror. |

## RateLimit

Rate limits: per-actor and per-traffic-class buckets, shared across web workers.

Source: `server/settings/_ratelimit.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_RATELIMIT_ACTOR_FRACTION` | `float` | `1.0` | Per-actor rate-limit bucket, keyed (namespace, token subject), as a fraction of the namespace limit. 1.0 = no actor bucket; 0.6 leaves a second credential budget while one sweeps. |
| `LOOM_RATELIMIT_SHARED` | `'auto' \| 'on' \| 'off'` | `'auto'` | Share rate buckets across one server's web workers via a memory-mapped file, so a keep-alive client pinned to one worker sees the full cap. auto = when web_workers > 1; on = always; off = per-process buckets divided by replicas × workers. |
| `LOOM_RATELIMIT_SHARED_PATH` | `str` | `''` | Bucket file; "" = one per server in the runtime dir (server/core/runtime_files.py). The file is 0600 and never opened through a symlink. Set it for two servers sharing one cap. |
| `LOOM_RATELIMIT_SHARED_SLOTS` | `int` | `16384` | Table slots (32 bytes each). Every process of a server must agree; a mismatch falls back to per-process buckets and shows in /v1/health. |
| `LOOM_RATELIMIT_CLASS_FRACTIONS` | `str` | `'{"bench": 0.5}'` | JSON {class: fraction}: a traffic class carved into its own bucket of the base cap × fraction, so a bench sweep never spends production's budget. Class = token `traffic_class` claim, else namespace row, else request hint, else interactive. Absent class = 1.0 (base bucket). |
| `LOOM_TENANT_MAX_CONCURRENT_REQUESTS` | `int` | `256` | Requests one org may have in flight at once, deployment-wide. Each process holds its share (divided by replicas × web workers, at least 1); a call over it gets 429 RATE_LIMITED. 0 = no cap. |

## Runtime

Process shape, deployment, event stream and cost budgets.

Source: `server/settings/_runtime.py`

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_BUILD_COMMIT` | `str` | `''` | Deployed git commit, stamped at image build (ARG LOOM_BUILD_COMMIT) and shown in /v1/health. Empty in a source run. |
| `LOOM_EVENTS_STREAM_FLUSH_INTERVAL_S` | `float` | `1.0` | Flush cadence of the loom.events queue (budget violations); also the visibility delay of those rows and the loss window on a process crash. |
| `LOOM_EVENTS_STREAM_FLUSH_BATCH` | `int` | `500` | Queued telemetry rows that trigger a flush before the interval elapses. |
| `LOOM_TRAFFIC_CLASS_REGISTRY_TTL_SECONDS` | `int` | `300` | How long a namespace's registry class is kept in the process cache. |
| `LOOM_DEFAULT_DAILY_COST_CAP_USD` | `float` | `1.0` | Daily LLM spend cap per namespace, USD: extraction workers skip a namespace whose rolling 24 h cost reached it. Overridden per namespace by loom.namespaces.daily_cost_cap_usd; 0.0 disables on both tiers. |
| `LOOM_REPLICA_COUNT` | `int` | `1` | The rate limiter divides every cap by the deployment's replica count so the replicas' sum matches the target (approximate, not cluster-strict). The count is the hosts with a live serving loom.instances row; this is its floor, and the count while those rows cannot be read. |
| `LOOM_SERVES_TRAFFIC` | `bool` | `True` | False for a process no load balancer routes to (a workers-only container): its instance row does not count as a replica. |
| `LOOM_INSTANCE_HEARTBEAT_SECONDS` | `float` | `10.0` | Seconds between this process's loom.instances heartbeats, each of which also compares it with the deployment's published configuration. A row not renewed for three intervals is not live. |
| `LOOM_WEB_WORKERS` | `int` | `1` | uvicorn processes per container (the image's default: server.core.web_workers); the rate limiter counts N workers as N replicas. |
| `LOOM_RUNTIME_DIR` | `str` | `''` | Directory of the files one server's web processes share (the rate-limit table); "" = chosen per process by server/core/runtime_files.py. scripts/serve.sh sets it once, before the workers start, so all of them name the same files. |
| `LOOM_BACKGROUND_WORKERS` | `'auto' \| 'on' \| 'off'` | `'auto'` | Which processes run the WorkerSupervisor: auto = the one process of the deployment holding the `workers_supervisor` lease in ClickHouse (server/workers/leader.py), and none outside the deployment (no LOOM_DEPLOYMENT); on = always; off = never (a pure web replica). |
| `LOOM_SUPERVISOR_LEASE_TTL_S` | `int` | `30` | auto mode: seconds a lease holder that stops renewing keeps the workers before another process takes them over (failover time). At least 24: below that a renewal blocked for its 2 s timeout leaves no retry before the holder must give up (server/workers/leader.py has the timings). |
| `LOOM_WORKERS_PROCESS` | `'child' \| 'inline'` | `'child'` | auto mode: where the lease holder runs the workers. child = a separate process it starts and stops with the lease, so the passes do not share the event loop that serves requests; inline = in the serving process. |
| `LOOM_SHUTDOWN_READY_DELAY_S` | `float` | `15.0` | Seconds a process keeps serving after SIGTERM while /v1/health/ready answers 503, so the load balancer stops routing to it before its port closes. Size it to the LB's probe interval × failure threshold. 0 = stop at once. |
| `LOOM_BOOT_WARMUP_TIMEOUT_S` | `float` | `30.0` | Seconds boot waits for the first load of the tracker registry and the runtime overrides, and the first publish of the dashboard bundle, before it serves; readiness stays false until the first two load. |
| `LOOM_READY_REQUIRES_MIRRORS` | `bool` | `False` | True: /v1/health/ready also waits until the mirror warm-up has queued its clones and while a clone of this host's git mirrors is running or queued (a failed clone does not hold it). Needs LOOM_CODE_MIRROR_WARMUP: without the warm-up nothing is cloned at boot and readiness does not wait for mirrors. Off by default: history ops answer `no_git_mirror` meanwhile. |
| `LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS` | `float` | `10.0` | Seconds between re-reads of loom.runtime_overrides (the admin rate-limit and latency-budget overrides) in every process. |
| `LOOM_DEPLOYMENT` | `bool` | `False` | The deployment's compose files set LOOM_DEPLOYMENT=1; every other process must set LOOM_INSTANCE_LABEL (e.g. `iso:<worktree>:<port>`, `ci:integration`). A process with neither refuses to start, so every ledger row is attributable. |
| `LOOM_BUDGET_VIOLATION_AUDIT_INTERVAL_S` | `float` | `60.0` | At most one `budget.violated` audit row per op per this many seconds (the row carries `suppressed_since_last`). 0 = a row per violation. |
| `LOOM_BUDGET_VIOLATION_AUDIT_BURST` | `int` | `5` | Violations per window that still write their own row before the rest are folded into the next: a short burst stays visible, a storm costs at most this many inserts a minute per (op, org, namespace). |
| `LOOM_PUBLIC_BASE_URL` | `str` | `''` | The address the server hands to clients — the Setup page's config block, `claude mcp add`, a bot's claim_url, OAuth metadata. Empty = the request's own Host and scheme, which only a loopback bind may rely on: a non-loopback bind_host refuses to start without it. |
| `LOOM_BIND_HOST` | `str` | `'127.0.0.1'` | The address Loom is reachable on: `loom serve` and `make server` bind it; in a container (which listens on all its interfaces) it is the address the port is published on. Anything but loopback turns on the startup checks for the dev secret, auth-off and public_base_url. |
| `LOOM_TRUSTED_PROXIES` | `str` | `'127.0.0.1'` | Comma-separated peer addresses or CIDRs whose X-Forwarded-For/Proto headers uvicorn applies; any other peer's are ignored. "*" trusts all. Required behind a load balancer: at the default every client counts as the balancer's address for the sign-in and sign-up limits. |
| `LOOM_ALLOW_INSECURE` | `bool` | `False` | True lets a non-loopback bind start with the dev signing secret or with auth off. Only for a port that is not actually reachable by others. |

## Storage

Storage: the legacy blob directory, per-table retention and warm-tier moves.

Source: `server/settings/_storage.py`


### Host directory

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_BLOB_DIR` | `str` | `'var/blobs'` | Where an earlier build kept uploaded document originals on this host. Nothing writes here now (originals live in loom.document_blobs); the repository-job sandbox still hides it until `loom blobs import` has moved what it holds. A relative path resolves from the server's working directory. |

### Document originals (loom.document_blobs)

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_DOCUMENT_BLOB_GC_ENABLED` | `bool` | `True` | The sweep that deletes originals no live document points at: those of deleted and re-uploaded documents, and of documents that retention (retention_documents_days, a namespace's retention_days) aged out, so the bytes leave with their rows. |
| `LOOM_DOCUMENT_BLOB_GC_INTERVAL_SECONDS` | `int` | `3600` | Seconds between sweeps. |
| `LOOM_DOCUMENT_BLOB_MIN_AGE_SECONDS` | `int` | `3600` | Seconds an original must have gone unwritten before the sweep may remove it. A write stores its original before the document row that names it, so a younger original may belong to a write still in flight. Erasure ignores it and deletes every original of its scope. |

### Retention and tiering

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_RETENTION_AUDIT_LOG_DAYS` | `int` | `0` | Opt-in retention/tiering, applied as idempotent ALTER TABLE MODIFY TTL by server/storage/retention_overrides.py: - retention_*_days: delete rows older than N days. 0 = keep forever. - tiering_*_warm_days: move rows older than W days to tiering_warm_volume_name. 0 = no tiering. With both set, W must be < N or the move is a no-op (logged WARN). |
| `LOOM_RETENTION_AGENT_EVENTS_DAYS` | `int` | `0` | Days `loom.agent_events` rows are kept; 0 = forever. |
| `LOOM_RETENTION_MEMORY_DAYS` | `int` | `0` | Days `loom.memory` rows are kept; 0 = forever. |
| `LOOM_RETENTION_MEMORY_EMBEDDINGS_DAYS` | `int` | `0` | Days `loom.memory_embeddings` rows are kept; 0 = forever. |
| `LOOM_RETENTION_CONVERSATIONS_DAYS` | `int` | `0` | Days `loom.conversations` rows are kept; 0 = forever. |
| `LOOM_RETENTION_CHUNKS_DAYS` | `int` | `0` | Days `loom.chunks` rows are kept; 0 = forever. |
| `LOOM_RETENTION_DOCUMENTS_DAYS` | `int` | `0` | Days `loom.documents` rows are kept; 0 = forever. |
| `LOOM_RETENTION_RECONCILE_ENABLED` | `bool` | `True` | The supervisor re-applies table TTLs from settings + namespace rows on this timer as metadata changes (materialize_ttl_after_modify = 0): rows already past a new TTL leave at their part's next merge or a manual MATERIALIZE TTL. |
| `LOOM_RETENTION_RECONCILE_INTERVAL_SECONDS` | `int` | `300` | Seconds between TTL re-apply passes. |

### Storage tiering

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TIERING_AUDIT_LOG_WARM_DAYS` | `int` | `0` | Per-table warm-tier move age, days (see retention and tiering above). |
| `LOOM_TIERING_AGENT_EVENTS_WARM_DAYS` | `int` | `0` | Days before `loom.agent_events` rows move to the warm volume; 0 = never. |
| `LOOM_TIERING_MEMORY_WARM_DAYS` | `int` | `0` | Days before `loom.memory` rows move to the warm volume; 0 = never. |
| `LOOM_TIERING_MEMORY_EMBEDDINGS_WARM_DAYS` | `int` | `0` | Days before `loom.memory_embeddings` rows move to the warm volume; 0 = never. |
| `LOOM_TIERING_CONVERSATIONS_WARM_DAYS` | `int` | `0` | Days before `loom.conversations` rows move to the warm volume; 0 = never. |
| `LOOM_TIERING_CHUNKS_WARM_DAYS` | `int` | `0` | Days before `loom.chunks` rows move to the warm volume; 0 = never. |
| `LOOM_TIERING_DOCUMENTS_WARM_DAYS` | `int` | `0` | Days before `loom.documents` rows move to the warm volume; 0 = never. |
| `LOOM_TIERING_WARM_VOLUME_NAME` | `str` | `'warm'` | Volume named in tiering TTL clauses; must match a volume in the cluster's storage_policies. |

## Task

The task inbox workers and what run.complete does to a run's rows.

Source: `server/settings/_tasks.py`


### Task inbox

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TASK_LEASE_SWEEP_ENABLED` | `bool` | `True` | Lease sweeper: return expired leases to todo, resume deferred tasks at resume_at, escalate stale triage. One read per pass. |
| `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS` | `int` | `60` | Seconds between passes while there is work. |
| `LOOM_TASK_LEASE_SWEEP_IDLE_INTERVAL_SECONDS` | `int` | `300` | Seconds between passes that found nothing. |
| `LOOM_TASK_WEBHOOK_DELIVER_ENABLED` | `bool` | `True` | Post one Standard Webhooks-signed notice per new event batch to each inbox's webhook_url; the secret comes from the env var task_inboxes.webhook_secret_ref names (`LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`, one of the inbox org's own). |
| `LOOM_TASK_WEBHOOK_DELIVER_INTERVAL_SECONDS` | `int` | `5` | Seconds between passes while notices are going out. |
| `LOOM_TASK_WEBHOOK_DELIVER_IDLE_INTERVAL_SECONDS` | `int` | `30` | Seconds between passes that found no new event. |

### Run completion

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN` | `'keep' \| 'demote' \| 'expire'` | `'keep'` | What run.complete does to the rows the run wrote when the call does not say: keep = nothing; demote = scale confidence by the expiry demote factor now; expire = expires_at now + memory_run_complete_expire_days with on_expire=demote. Pinned, verified, promoted, locked, episodic and reflection rows are always kept, and so is everything an MCP session's implicit run wrote when the transport closes it. |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS` | `int` | `7` | Days until expiry for `memory_run_complete_retain = expire`. |

## Tracker

The tracker plane: records, duplicate checks, bisect.

Source: `server/settings/_tracker.py`


### Tracker record, read live

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_LIVE_TIMEOUT_S` | `float` | `1.5` | Per-statement `max_execution_time` on the remote, seconds; the client's receive timeout is this plus 5 s. |
| `LOOM_TRACKER_LIVE_BUDGET_MS` | `int` | `300` | Caller-side budget per read, ms: past it the read is `tracker_unavailable` and counts against the breaker. |
| `LOOM_TRACKER_LIVE_CACHE_TTL_S` | `float` | `300.0` | Process cache TTL for live rows, seconds (upstream refreshes every 5–10 min); an empty result is kept at most 60 s. |
| `LOOM_TRACKER_LIVE_SERVE` | `bool` | `True` | Kill switch: off = every tracker read answers `tracker_unavailable`. |
| `LOOM_TRACKER_LIVE_BISECT_BUDGET_MS` | `int` | `6000` | Budget of the bisect candidate read, which scans a release range's diffs, ms: a triage step, not an interactive one. |
| `LOOM_TRACKER_LIVE_TEXT_BUDGET_MS` | `int` | `900` | Budget of the text-index reads (similar bugs, threads, changelog, PRs by path, area escalations / incidents by term), ms; above the 300 ms interactive budget. |
| `LOOM_TRACKER_CIRCUIT_FAILURE_THRESHOLD` | `int` | `3` | Breaker per credential: consecutive failures that open it, and how long it stays open before one probe read. |
| `LOOM_TRACKER_CIRCUIT_COOLDOWN_S` | `float` | `120.0` | Seconds an open breaker stays open before one probe read (at least 1 s). |

### Tracker org and code-namespace bindings

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_ORG` | `str` | `''` | The loom org the tracker namespaces belong to; empty (default) = none, so no tracker namespace serves and self-service sources are refused until it is set. |
| `LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON` | `str` | `''` | Which tracker namespace a code namespace reads its facts from: glob keys → namespace, e.g. {"code-myrepo*": "tracker-myrepo"}. Numbers are repository-global, so every branch namespace binds to its repository's one tracker namespace. '' = no QA facts anywhere. |

### Tracker sources and the record contract

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_SOURCES_JSON` | `str` | `''` | Every tracker source and the namespaces that read them: {"sources": {"<name>": {"host": "x.clickhouse.cloud", "port": 8443, "secure": true, "database": "loom_tracker"}}, "namespaces": {"qa-<team>": {"source": "<name>", "repos": ["Org/repo"], "restricted": false}}}. A namespace naming an unknown source does not serve (one error line, never a guess). |
| `LOOM_TRACKER_RESTRICTED_REPOS_JSON` | `str` | `''` | The repositories whose numbers name a restricted namespace's escalations and core incidents, as `Org/repo`: {"escalation": "Org/escalations", "incident": "Org/incidents"}. `escalation_context` / `incident_context` read the record `Org/repo#<number>`; a kind with no repository here reads no record by number. |
| `LOOM_TRACKER_CREDENTIALS_JSON` | `str` | *(unset)* | Credentials per source, kept apart from the map above: {"<name>": {"user": "...", "password": "..."}}. |
| `LOOM_TRACKER_CONTRACT_TTL_S` | `float` | `3600.0` | A source serves only while its schema meets the record contract (docs/features/tracker/record-contract.md), checked through its own credential once per this many seconds (at least 1 s); a failed check retries after 60 s. |

### Self-service tracker sources

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_SELF_SERVICE_ENABLED` | `bool` | `True` | Self-service tracker sources: any signed-in org member registers one (`tracker.source.add`); credential and schema are checked before any write. Off = only the operator's map serves; loom.qa_sources rows are ignored. `_max_sources` caps the registrations. |
| `LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES` | `int` | `10` | At most this many self-service sources an org may register; a further `tracker.source.add` is refused. |
| `LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS` | `bool` | `False` | A registered host must resolve to a public address (no loopback, RFC 1918, link-local, CGNAT 100.64/10 or multicast); True lets a dev stack point at a local ClickHouse. |
| `LOOM_TRACKER_SOURCES_REFRESH_SECONDS` | `float` | `60.0` | How often a process re-reads loom.qa_sources (seconds); a write in this process refreshes at once. |

### Similar items (tracker.similar)

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_SIMILAR_VECTOR_LEG` | `bool` | `True` | Whether tracker.similar's vector leg (over loom.qa_item_embeddings, filled by the `tracker_embed` pass) is in the default leg set; a caller naming `vector` in `legs` runs it regardless. The leg only adds candidates, after the ranked legs (the agreement boost below aside). |
| `LOOM_TRACKER_SIMILAR_AGREEMENT_WEIGHT` | `float` | `1.0` | The agreement boost: a tracker.similar candidate found by a ranked leg AND the vector leg gains the vector leg's RRF contribution times this weight (1.0 = counts like a ranked leg, 0.5 = a tie-breaker) and the ranked set is re-sorted; vector-only candidates stay appended. |
| `LOOM_TRACKER_SIMILAR_AGREEMENT_MAX_RANK` | `int` | `5` | The deepest vector-leg rank that may boost a ranked candidate; 0 = any rank. |
| `LOOM_TRACKER_SIMILAR_CATCH_UP_BUDGET_MS` | `int` | `300` | Ms the vector leg waits for its query-time catch-up (embedding the items changed since the pass's cursor), run beside the cosine scan. Past it the call answers from the table (`query.fresh_pending = true`) and the embedding finishes in the background for the next call. |

### The tracker_embed pass

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_EMBED_ENABLED` | `bool` | `True` | Every interval (seconds), for each configured tracker namespace, the issues and PRs open or closed within `closed_days` and changed since the last pass are embedded (`title + "\n" + left(body, body_chars)`, `tracker_embed_batch` a call) into loom.qa_item_embeddings; an unchanged text (sha256) is skipped, an item that left the window is tombstoned. Off = nothing is written; the leg serves the table plus its query-time catch-up. The interval bounds only that catch-up, never what a query can find. |
| `LOOM_TRACKER_EMBED_INTERVAL_SECONDS` | `float` | `1800.0` | Seconds between `tracker_embed` passes. |
| `LOOM_TRACKER_EMBED_CLOSED_DAYS` | `int` | `365` | The embedding window for PRs, days: a PR closed longer ago is not embedded and one that leaves the window is tombstoned. |
| `LOOM_TRACKER_EMBED_ISSUE_CLOSED_DAYS` | `int` | `3650` | The window for ISSUES, days (3650 = every closed issue the record holds): labelled duplicate targets are often closed long before the draft. PRs keep `tracker_embed_closed_days`. |
| `LOOM_TRACKER_EMBED_BODY_CHARS` | `int` | `8000` | Body characters embedded per item. Must stay under the embedder's 8,191-token input limit (a stack trace runs ~2 chars a token; one oversized text fails the whole batch). A change needs a re-embed (`loom tracker-embed-reset`, then the pass). |
| `LOOM_TRACKER_EMBED_BATCH` | `int` | `200` | Items sent to the embedder in one call (at least 1). |
| `LOOM_TRACKER_EMBED_FETCH_MULTIPLIER` | `float` | `10.0` | The nearest read's codes-shortlist over-fetch on loom.qa_item_embeddings (x10's misses are BFloat16 rounding only). |
| `LOOM_TRACKER_SIMILAR_IDF_TTL_S` | `float` | `3600.0` | Seconds the issue corpus's per-token document frequency (for BM25F) is cached per process; the upstream index cannot count it inside a budget. |

### An issue names the PRs that probably fixed it

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_LINKS_ENABLED` | `bool` | `True` | A daily pass ranks, for every issue opened in the last `issue_days`, the merged trunk PRs of the `window_days` after it (from a week before) by BM25F over title and body, the issue's stack frames in the PR's diff, the closing mention and the component, and writes each issue's top `per_issue` into loom.qa_link_candidates. Off = nothing is written; `tracker.links` serves the table. |
| `LOOM_TRACKER_LINKS_INTERVAL_SECONDS` | `float` | `86400.0` | Seconds between `tracker_links` passes. |
| `LOOM_TRACKER_LINKS_ISSUE_DAYS` | `int` | `90` | Issues opened within this many days are ranked each pass. |
| `LOOM_TRACKER_LINKS_WINDOW_DAYS` | `int` | `90` | Days after an issue opened in which a merged trunk PR is a fix candidate. |
| `LOOM_TRACKER_LINKS_PER_ISSUE` | `int` | `20` | The top candidates written per issue into loom.qa_link_candidates. |
| `LOOM_TRACKER_LINKS_WEIGHT_MENTION` | `float` | `1.5` | Score = bm25_norm (the pair's BM25F as a share of the issue's best) + weight_mention × (1 closing keyword, 0.5 bare mention) + 0.05 × component_match. The stack-frames-in-diff features ride the row unscored. |
| `LOOM_TRACKER_LINKS_MIN_SCORE` | `float` | `1.0` | A consumer (code.backport_gap's issue path, the bisect's fix candidate) takes the top candidate as the fix only above `min_score` AND when a closing keyword names the issue. tracker.links serves every row with its score. |

### Tracker.duplicate_check

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_DUPCHECK_DUPLICATE_THRESHOLD` | `float` | `0.25` | A draft's verdict: `duplicate_of` when the top candidate's calibrated confidence reaches this, `related` when any candidate reaches the related threshold, else `none` with no items. Both are probabilities from a logistic fit on labelled duplicate pairs, so they survive a refit; the scale is compressed. Measured `none` on 99% of negatives at 0.25. |
| `LOOM_TRACKER_DUPCHECK_RELATED_THRESHOLD` | `float` | `0.05` | the lower band's floor |
| `LOOM_TRACKER_DUPCHECK_FIX_PATH_DAYS` | `int` | `365` | The fix-in-flight leg reads PRs from the last this many days. |
| `LOOM_TRACKER_DUPCHECK_FIX_PATH_PRS` | `int` | `20` | At most this many PRs the fix-in-flight leg reads per draft. |
| `LOOM_TRACKER_DUPCHECK_LEG_RETRY_DELAY_MS` | `int` | `50` | A leg whose read failed for a transient reason (a timeout, a connection error, an upstream 5xx) is run once more inside the op budget; one that still fails leaves the answer `epistemic: degraded` and `leg_status` names it with its reason. The pause before that retry, ms (at most 200). |
| `LOOM_TRACKER_DUPCHECK_LEG_RETRY_RESERVE_MS` | `int` | `300` | What the retry leaves of the op budget for the steps after the legs (hydrate, fit, judge), ms. |
| `LOOM_TRACKER_DUPCHECK_LEG_RETRY_MIN_MS` | `int` | `200` | The shortest retry worth starting, ms: with less left the retry is skipped and `leg_status` says `retry_skipped_budget`. |
| `LOOM_TRACKER_DUPCHECK_FANOUT_NAMESPACES` | `int` | `4` | How many tracker namespaces a duplicate check addressed to a code namespace (or its bound tracker namespace) reads when no `namespaces` list is given: every readable tracker record, the bound one first. 1 = the bound one only. |
| `LOOM_TRACKER_DUPCHECK_DUPLICATE_THRESHOLD_MIN` | `float` | `0.15` | Bounds the fitted duplicate threshold is clamped into, so one odd fit cannot make every candidate (or none) a duplicate. |
| `LOOM_TRACKER_DUPCHECK_DUPLICATE_THRESHOLD_MAX` | `float` | `0.5` | Upper bound of the fitted duplicate threshold. |
| `LOOM_TRACKER_DUPCHECK_REFIT_ENABLED` | `bool` | `True` | The fit (coefficients, curve, holdout) is JSON in loom.code_index_state under the tracker namespace, refitted by the tracker_links pass once older than the refit interval by replaying every gold query at its filing time; `fit_negatives` non-duplicates are sampled by hash per fit. Off = the stored fit keeps serving; with none stored the op answers `uncalibrated`. |
| `LOOM_TRACKER_DUPCHECK_REFIT_INTERVAL_S` | `float` | `604800.0` | 7 days |
| `LOOM_TRACKER_DUPCHECK_FIT_NEGATIVES` | `int` | `400` | the negatives sampled per fit |
| `LOOM_TRACKER_DUPCHECK_JUDGE_ENABLED` | `bool` | `True` | The judge stage. On a `related` answer one model call reads the draft against the top `judge_top_k` issue candidates' title + body head (`judge_body_chars`) and may promote a same-defect one to `duplicate` (never demotes; a recorded related_to never promotes). `judge_model` "" = default client. Benched on a labelled duplicate set. |
| `LOOM_TRACKER_DUPCHECK_JUDGE_TOP_K` | `int` | `5` | Issue candidates the judge reads (at least 1). |
| `LOOM_TRACKER_DUPCHECK_JUDGE_BODY_CHARS` | `int` | `1500` | Body characters per candidate the judge reads. |
| `LOOM_TRACKER_DUPCHECK_RERANK_ENABLED` | `bool` | `True` | The rerank stage. One model call reads the draft against a pool of each leg's own head (`_rerank_per_leg` rows a leg) plus the issues the tracker graph links to the leading candidates; its same/related picks lead `nearest`, the verdict stays the fit's. Fails open. |
| `LOOM_TRACKER_DUPCHECK_RERANK_PER_LEG` | `int` | `50` | Rows each leg contributes to the rerank pool; also deepens the rows kept per leg when it exceeds the default head. |
| `LOOM_TRACKER_DUPCHECK_RERANK_POOL_CAP` | `int` | `200` | The candidates one rerank call reads (0 = dup_rerank.POOL_CAP, 40); 200 lets a 50-per-leg pool through a fan-out's namespaces. |
| `LOOM_TRACKER_DUPCHECK_RERANK_NS_CAP` | `int` | `60` | At most this many candidates the rerank pool holds for one namespace's legs (after dedup, the legs taking turns). |
| `LOOM_TRACKER_DUPCHECK_LEAD_MIN` | `float` | `0.9` | A rerank `same` row at or above this is offered as a lead ("nearest reported: … — may be related, check it"), never as the verdict. |
| `LOOM_TRACKER_DUPCHECK_RERANK_BODY_CHARS` | `int` | `200` | Body characters per candidate the rerank call reads. |
| `LOOM_TRACKER_DUPCHECK_RERANK_TIMEOUT_S` | `float` | `4.0` | Seconds the rerank call may take; past it the rerank reports error="timeout" and the verdict stands without it. |

### Bisect without a stack frame

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_BISECT_SCOPE_PATTERNS` | `int` | `6` | When no frame resolves to a file, code.bisect_candidates(issue_number) scans the release window only for PRs whose diff carries one of this many of the issue's rarest patterns (IDF over the issue corpus). 0 turns the leg off. |
| `LOOM_TRACKER_BISECT_COMPONENT_DIRS` | `int` | `8` | The issue's component widens the scan to at most `component_dirs` `src/` directories its PRs touch, each with `min_lift` × the share every trunk PR has (a hub directory says nothing); the aggregate is cached per component for `ttl_s`. |
| `LOOM_TRACKER_BISECT_COMPONENT_MIN_LIFT` | `float` | `1.5` | A component directory counts only when its share of the component's PRs is at least this multiple of its share of every trunk PR. |
| `LOOM_TRACKER_BISECT_COMPONENT_TTL_S` | `float` | `3600.0` | Seconds the per-component directory aggregate is cached. |

### A failing test is read against its own CI history

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_CHECKS_FROM` | `str` | `''` | The FROM clause of the checks reads; '' = none. A tracker names its own (`checks` table, or its source entry's `checks.from`); this is the deployment-wide fallback. Name the table function, not a view over it: a view's filter runs on the QA side, so a hot test's read hits play's 1 GB result cap. ClickHouse's public CI (the `play` user is public) is "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')". |
| `LOOM_TRACKER_CHECKS_REPO` | `str` | `''` | the repository whose CI `tracker_checks_from` is ('' = the namespace's only repository) |
| `LOOM_TRACKER_LIVE_CHECKS_BUDGET_MS` | `int` | `8000` | Budget of the per-test history read, ms: play scans the whole window by its time key, far past the 300 ms interactive budget. |
| `LOOM_TRACKER_TEST_SIGNAL_WINDOW_DAYS` | `int` | `30` | Window of the history read, days; churn window of the ledger leg, days. |
| `LOOM_TRACKER_TEST_SIGNAL_CHURN_DAYS` | `int` | `54` | Churn window of the ledger leg, days (at least 1). |
| `LOOM_TRACKER_TEST_SIGNAL_MIN_RUNS` | `int` | `5` | Verdict thresholds: `min_runs` below which nothing is said; `flip_rate` (status changes between consecutive runs of one PR/check sequence) that reads as flaky; a storm hour holds ≥ K other failing tests across ≥ `cofail_prs` PRs, K = max(`cofail_k`, `cofail_multiple` × the window's median failing tests per hour); `cofail_share` of the test's failing hours must be storm hours for `infra_candidate`; a test failing on ≥ `cofail_prs` PRs in one hour is a shared cause by itself. |
| `LOOM_TRACKER_TEST_SIGNAL_FLIP_RATE` | `float` | `0.02` | Status-change rate between consecutive runs of one PR/check sequence at or above which a test reads as flaky. |
| `LOOM_TRACKER_TEST_SIGNAL_COFAIL_K` | `int` | `100` | Floor of K, the other failing tests an hour needs to be a storm hour. |
| `LOOM_TRACKER_TEST_SIGNAL_COFAIL_MULTIPLE` | `float` | `5.0` | K is at least this multiple of the window's median failing tests per hour. |
| `LOOM_TRACKER_TEST_SIGNAL_COFAIL_PRS` | `int` | `3` | A storm hour spans at least this many PRs; a test failing on this many PRs in one hour is a shared cause by itself. |
| `LOOM_TRACKER_TEST_SIGNAL_COFAIL_SHARE` | `float` | `0.5` | Share of the test's failing hours that must be storm hours for `infra_candidate`. |

### The per-test daily roll-up and the CI prior read from it

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_TEST_DAILY_ENABLED` | `bool` | `True` | The tracker_test_daily worker rolls the CI table into loom.qa_test_daily once per day per test, so tracker.test_signal reads a key range here and the remote only for hours no roll-up covers. Off = the tables stop filling; the op falls back to the live read of the whole window. |
| `LOOM_TRACKER_TEST_DAILY_INTERVAL_SECONDS` | `int` | `1800` | Pass interval, seconds; today (and yesterday inside the settle lag) is re-rolled every pass as a partial day, so the live tail the op reads is at most one interval plus the current hour. |
| `LOOM_TRACKER_TEST_DAILY_BACKFILL_DAYS` | `int` | `30` | How far back a cold worker rolls, in days (the op's largest window is 90). |
| `LOOM_TRACKER_TEST_DAILY_MAX_DAYS_PER_PASS` | `int` | `6` | Days rolled per pass and the pause between remote statements: the shared `play` user is metered at 100 statements of one shape per hour, and the op's live reads share that quota. |
| `LOOM_TRACKER_TEST_DAILY_PACE_S` | `float` | `5.0` | Seconds paused between remote statements of a pass; 0 = no pause. |
| `LOOM_TRACKER_TEST_DAILY_LAG_HOURS` | `float` | `3.0` | Hours after a day ends before its roll is final: a check's row lands when the check finishes, not when it starts. |
| `LOOM_TRACKER_TEST_DAILY_BUDGET_MS` | `int` | `60000` | Budget of one roll-up statement, ms. |

### The static smell leg

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `LOOM_TRACKER_TEST_SIGNAL_SMELL_CHUNKS` | `int` | `6` | The static smell leg reads at most this many chunks of the test's home file(s) from the code plane (flaky-test smell tokens per category, counted). |
