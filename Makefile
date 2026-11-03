.PHONY: bootstrap dev dev-cloud down down-all migrate migrate-status migrate-dry schema-dump schema-check bench-matrix server frontend-build frontend-dev init onboard test test-unit test-integration test-mixed-version test-unit-cov ingest bench bench-quick bench-algorithm-gate bench-quality-gate bench-code-intel-dry bench-code-portability-dry bench-code-intel bench-code-legs bench-clickgap-replay lint format format-check typecheck mypy-gate install clean bench-clickgap-golden

# Interpreter for the python-invoking targets: the local .venv, which CI also
# builds from uv.lock (`uv sync --locked`).
PYTHON ?= .venv/bin/python

# Bench scripts run as `python bench/x.py`, which puts bench/ (not the repo
# root) first on sys.path, so `import bench.…` / `import server…` resolve
# through whatever else is on the path: with another checkout's venv, whose
# editable install adds that checkout, a worktree's bench would silently run
# that checkout's code. The repo root on PYTHONPATH comes first instead.
BENCH_PYTHON = PYTHONPATH=$(CURDIR)$(if $(PYTHONPATH),:$(PYTHONPATH)) $(PYTHON)

# Create the two gitignored files a fresh clone lacks but the dev stack needs:
# .env (compose env_file) and the ClickHouse named-collections config (a bind
# mount), and give an .env without one an extractor token. Idempotent — only
# fills what's missing, so it's safe as a dev prereq.
bootstrap:
	@PYTHONPATH="$(CURDIR)" python3 -c 'from pathlib import Path; from server.cli.up import bootstrap; [print("✓ " + line) for line in bootstrap(Path.cwd(), ".env")]'

# Local evaluation stack — Loom and its document extractor built from this
# checkout + the bundled ClickHouse (compose.yaml with the
# dev/docker-compose.dev.yml override). CH is Loom's only direct dependency; Langfuse and HyperDX are optional integrations
# that each have their own deployment story (see docs/operations/deployment.md).
# --build so a code change is actually reflected: plain `up` reuses the cached
# image and silently runs stale code. Docker layer caching keeps a no-change
# `make dev` fast. (For a tight backend loop, `make server` runs from source
# with --reload and skips the image entirely.)
# --env-file: compose reads the .env beside the first -f file, the root one
# here; the flag says so explicitly, and the cloud stack below (first file
# under dev/) needs it. --profile bundled starts the ClickHouse
# container whatever COMPOSE_PROFILES the .env names.
dev: bootstrap
	LOOM_BUILD_COMMIT="$$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
		docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
	@echo "✓ Loom:        http://localhost:7777/"
	@ch_port=$$(docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled port clickhouse 8123 2>/dev/null | cut -d: -f2); \
	echo "✓ ClickHouse:  http://localhost:$${ch_port:-8123} (user=loom, pwd=loomdev)"
	@echo "  Migrations applied automatically by the Loom server on boot (FastAPI lifespan)."

# Loom built from this checkout against the ClickHouse in .env (LOOM_CH_*),
# with the web tier and the background workers in two containers. Run
# `loom ch-setup` against that ClickHouse once first.
dev-cloud: bootstrap
	LOOM_BUILD_COMMIT="$$(git rev-parse --short HEAD 2>/dev/null || echo unknown)" \
		docker compose --env-file .env -f dev/docker-compose.cloud.yml up -d --build
	@echo "✓ Loom:        http://localhost:7777/  (upstreams from .env)"

# bootstrap: --env-file .env below is a hard error if .env doesn't exist yet
# (unlike compose's own silent-if-absent default lookup), so these targets
# need the same prerequisite as dev/dev-cloud.
down: bootstrap
	docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled down
	docker compose --env-file .env -f dev/docker-compose.cloud.yml down 2>/dev/null || true

down-all: bootstrap
	docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled down -v
	docker compose --env-file .env -f dev/docker-compose.cloud.yml down -v 2>/dev/null || true

# Apply pending schema migrations idempotently.
# Reads LOOM_CH_* from the environment (same shape .env uses).
# Records each applied filename in loom._migrations so re-runs skip cleanly.
migrate:
	@$(PYTHON) -m server.core.schema.migrate

migrate-status:
	@$(PYTHON) -m server.core.schema.migrate --status

migrate-dry:
	@$(PYTHON) -m server.core.schema.migrate --dry-run

# Regenerate schemas/schema.sql from a fresh ClickHouse (the version CI pins)
# with every migration applied. A failed dump leaves the file as it was.
schema-dump:
	@$(PYTHON) -m scripts.ops.dump_schema

# Exit 1, with the head of the diff, when schemas/schema.sql differs from a
# dump of the connected server. Writes nothing. CI runs it after a replay.
schema-check:
	@$(PYTHON) -m scripts.ops.dump_schema --check

# Run the retrieval bench matrix (search_mode × rerank × pool widths).
# Hits the dev server by default; override with BASE_URL=...
bench-matrix:
	@$(BENCH_PYTHON) bench/matrix.py \
		--base-url $${BASE_URL:-http://localhost:7777} \
		--limit $${BENCH_N:-30} \
		--out bench/MATRIX_RESULTS.md

# Run the Control Plane locally
# LOOM_INSTANCE_LABEL: a process outside the compose
# deployment refuses to boot unlabelled; this is the label its ledger rows
# and spans carry. Override: `LOOM_INSTANCE_LABEL=dev:me:7777 make server`.
# Binds LOOM_BIND_HOST (loopback by default); forwarded headers are honoured
# only from LOOM_TRUSTED_PROXIES.
# A dev server is outside any deployment, where `auto` runs no workers: it runs its
# own (`on`) unless LOOM_BACKGROUND_WORKERS says otherwise.
server:
	LOOM_INSTANCE_LABEL="$${LOOM_INSTANCE_LABEL:-dev:make-server:7777}" \
	LOOM_BACKGROUND_WORKERS="$${LOOM_BACKGROUND_WORKERS:-on}" \
	LOOM_BIND_HOST="$${LOOM_BIND_HOST:-127.0.0.1}" \
		uvicorn server.main:app --host "$${LOOM_BIND_HOST:-127.0.0.1}" --port 7777 --reload --reload-dir server \
		--loop "$${LOOM_UVICORN_LOOP:-asyncio}" --forwarded-allow-ips "$${LOOM_TRUSTED_PROXIES:-127.0.0.1}" --timeout-graceful-shutdown 10

# Build the React/Vite dashboard. Output lands in frontend/dist/
# which the server picks up automatically. Required before `make server`
# if you want the dashboard; without it, dashboard pages return 503.
frontend-build:
	cd frontend && npm install && npm run build

# Dev mode for the frontend — Vite dev server on :5173, proxies /v1 and /mcp
# to the Loom server on :7777. Run `make server` separately.
frontend-dev:
	cd frontend && npm install && npm run dev

# Onboarding: provision service account + .env + starter script.
# Usage:  make init NAMESPACE=eng-tooling AGENT=my-agent
init:
	@test -n "$(NAMESPACE)" || (echo "NAMESPACE=<ns> required" && exit 1)
	@test -n "$(AGENT)" || (echo "AGENT=<agent> required" && exit 1)
	$(PYTHON) dev/loom_init.py --namespace $(NAMESPACE) --agent $(AGENT) \
		--base-url $${LOOM_BASE_URL:-http://localhost:7777}

# Tenant onboarding: loom_init + raised rate limits + smoke test.
# Usage:  make onboard NAMESPACE=eng-tooling AGENT=my-agent
#         make onboard NAMESPACE=… AGENT=… FORCE=1   # overwrite an existing .env
# Without FORCE=1 dev/onboard_tenant.py refuses to clobber a .env it finds.
onboard:
	@test -n "$(NAMESPACE)" || (echo "NAMESPACE=<ns> required" && exit 1)
	@test -n "$(AGENT)" || (echo "AGENT=<agent> required" && exit 1)
	$(PYTHON) dev/onboard_tenant.py --namespace $(NAMESPACE) --agent $(AGENT) \
		--base-url $${LOOM_BASE_URL:-http://localhost:7777} $(if $(FORCE),--force,)

# Tests
test: test-integration

test-unit:
	$(PYTHON) -m pytest tests/unit -q
	$(PYTHON) -m pytest bench -q

test-integration:
	$(PYTHON) -m pytest tests/integration -v

# Builds N-1 (BASE, default the merge base with origin/main) and N side by side
# on one throwaway ClickHouse (docker); skips without docker (REQUIRE=1 fails
# instead).
test-mixed-version:
	$(PYTHON) scripts/gates/mixed_version.py $(if $(BASE),--base $(BASE),) $(if $(REQUIRE),--require,)

# Unit suite + coverage (mirrors the CI `unit` job). Fails under the floor.
test-unit-cov:
	$(PYTHON) -m pytest tests/unit -q \
		--cov=server --cov=sdk/python/loom \
		--cov-report=term-missing --cov-report=xml \
		--cov-fail-under=40

ingest:
	@if [ -z "$(FILE)" ]; then echo "usage: make ingest FILE=path/to/doc.md [TAGS=foo,bar]"; exit 2; fi
	$(PYTHON) dev/loom_ingest.py $(FILE) $(if $(TAGS),--tags $(TAGS))

# Benchmark — drives concurrent load against a single op, reports p50/p95/p99.
# Usage:  make bench OP=memory.set C=10 D=30
#         make bench OP=memory.search C=5 D=60
bench:
	$(PYTHON) scripts/ops/benchmark.py \
		--op $(or $(OP),memory.set) \
		--concurrency $(or $(C),5) \
		--duration $(or $(D),10)

# Quality regression gate — 30-item LongMemEval slice + answer-judge.
# Run before changing LOOM_LLM_MODEL or LOOM_EMBEDDING_MODEL defaults.
# Requires LOOM_API_KEY + OPENAI_API_KEY in env. Output: per-category
# recall@30 + judge accuracy.
#
# Dataset default is the real-haystack -S split
# (bench/datasets/longmemeval_s_cleaned.json, ~47 sessions/item), so a
# 30-item run ingests ~1,400 sessions — one extraction LLM call each —
# i.e. roughly 25x the cost and wall-clock of the evidence-only oracle
# split (~2 sessions/item). The oracle split stays selectable for cheap
# smoke runs:
#   $(BENCH_PYTHON) bench/longmemeval.py \
#       --dataset bench/datasets/longmemeval_oracle.json ...
# Oracle-split numbers have near-zero retrieval pressure and are not
# comparable to -S figures in either direction.
#
# The instrument is pinned: reader=gpt-4o-mini, judge=gpt-4o-2024-08-06
# (the official LongMemEval snapshot). Neither follows LOOM_LLM_MODEL —
# that's the server-side variable this gate exists to vary. Numbers graded
# by an unpinned judge or on the oracle split are not comparable; baseline on
# -S under the pinned judge before comparing absolute numbers.
#
# Override scope:
#   make bench-quick LIMIT=100                       # tighter confidence
#   make bench-quick BENCH_JUDGE_MODEL=gpt-4o-mini   # cheap judge (non-comparable)
#   make bench-quick BENCH_READER_MODEL=gpt-4o       # stronger reader ablation
bench-quick:
	@$(BENCH_PYTHON) bench/longmemeval.py \
		--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
		--limit $(or $(LIMIT),30) \
		--shuffle --answer-judge --concurrency 1 \
		--out /tmp/bench_quick_$$(date +%s).csv

# Fast algorithm regression gate. Requires a running Loom control plane.
# Covers prompt extraction and graph triple extraction on tiny fixtures so
# retrieval/extraction changes have a cheap smoke test before full benches.
bench-algorithm-gate:
	@$(BENCH_PYTHON) bench/extraction_eval.py \
		--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
		--dataset bench/fixtures/extraction_regressions.json \
		--out /tmp/loom_extraction_gate_$$(date +%s).csv
	@$(BENCH_PYTHON) bench/graph_eval.py \
		--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
		--dataset bench/fixtures/graph_regressions.json \
		--out /tmp/loom_graph_gate_$$(date +%s).csv

# Retrieval-quality release gate. Fails (exit 1) when recall falls under the
# recorded baseline, so a ranking regression can block a release instead of being
# noticed later on a dashboard.
#
# NOT wired into CI, deliberately. The CI integration job runs with
# LOOM_EMBEDDING_MODEL=fake-ngram-1024 and LOOM_LLM_PROVIDER=fake, so recall there
# measures nothing about real retrieval — a green gate would be worse than no gate.
# Enforcing it in CI needs a paid provider secret in the repo, which is a decision
# for whoever owns the account, not something to smuggle in via a workflow file.
#
# Run against a control plane with real providers. The threshold is the recorded
# baseline; re-baseline after any embedder change (see bench/README.md), because a
# new embedder moves the whole distance distribution.
bench-quality-gate:
	@$(BENCH_PYTHON) bench/quality.py \
		--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
		--top-k 10 \
		--min-recall $(or $(MIN_RECALL),0.80) \
		--out /tmp/loom_quality_gate_$$(date +%s).csv

# Code-intelligence gates (real-provider deployment; NEVER CI — the CI
# embedder is fake, and a green gate on fake embeddings is worse than none).
bench-code-intel-dry:
	@$(BENCH_PYTHON) bench/code_intel_qa.py --dry

# Every public-repository/language fixture must remain scoreable. This is the
# offline portability gate; run the same datasets without --dry against their
# indexed namespaces before changing retrieval defaults.
bench-code-portability-dry:
	@for dataset in code_intel_qa_multilang code_intel_qa_sqlsh code_intel_oss code_intel_qa_langfuse; do \
		$(BENCH_PYTHON) bench/code_intel_qa.py --dry --dataset bench/fixtures/$$dataset.jsonl || exit $$?; \
	done

bench-code-intel:
	@$(BENCH_PYTHON) bench/code_intel_qa.py \
		--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
		--min-symbol-exact $(or $(MIN_SYMBOL_EXACT),0.95) \
		--min-callers-f1 $(or $(MIN_CALLERS_F1),0.85) \
		--min-recall5 $(or $(MIN_RECALL5),0.75) \
		--min-abstain-precision $(or $(MIN_ABSTAIN_PRECISION),0.8) \
		--min-abstain-recall $(or $(MIN_ABSTAIN_RECALL),0.8) \
		--max-mean-tokens $(or $(MAX_MEAN_TOKENS),4000)

# Leg ablation: the hybrid arm against lexical-only,
# lexical-only + query expansion, and vector-only, each a paired B arm; the
# bench prints per-arm via distribution and p50. Deltas under 3 points on
# the 36-question conceptual slice are noise (bench/README.md).
bench-code-legs:
	@rc=0; for arm in '{"legs":"lexical"}' '{"legs":"lexical","expand":true}' '{"legs":"vector"}'; do \
		$(BENCH_PYTHON) bench/code_intel_qa.py \
			--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
			--compare-extra "$$arm" --track-misses || rc=1; \
	done; exit $$rc

bench-clickgap-replay:
	@$(BENCH_PYTHON) bench/clickgap_code_replay.py \
		--data $(or $(REPLAY_DATA),/tmp/bench/clickgap_prs.jsonl) \
		--base-url $(or $(LOOM_BASE_URL),http://localhost:7777) \
		--min-pack-hit $(or $(MIN_PACK_HIT),0.8) \
		--min-symbol-coverage $(or $(MIN_SYMBOL_COVERAGE),0.7) \
		--max-mean-tokens $(or $(MAX_MEAN_TOKENS),4000)

# Hygiene: the CI lint job's checks, minus formatting and types (format-check,
# typecheck). Every generated file with a --check (SDK tables, Codex plugin
# copies, the configuration and docs/reference tables, the reach manifest) must match its
# source; regenerate with the script named in the failure.
lint:
	$(PYTHON) -m ruff check server extractor sdk/python tests scripts .claude
	$(PYTHON) scripts/gates/function_length_gate.py
	$(PYTHON) scripts/gates/check_no_secrets.py
	$(PYTHON) scripts/codegen/sdk_py_gen.py --check
	$(PYTHON) scripts/codegen/sdk_py_client_gen.py --check
	$(PYTHON) scripts/codegen/sdk_py_admin_gen.py --check
	$(PYTHON) scripts/codegen/sdk_ts_gen.py --check
	$(PYTHON) scripts/codegen/agent_plugin_sync.py --check
	$(PYTHON) scripts/codegen/settings_reference.py --check
	$(PYTHON) scripts/codegen/settings_schema.py --check
	$(PYTHON) scripts/codegen/settings_example.py --check
	$(PYTHON) scripts/codegen/docs_reference.py --check
	$(PYTHON) scripts/codegen/op_reach_census.py --check
	$(BIOME) lint frontend/src

# Formatting: ruff (Python), Biome (JS/TS/JSON/CSS), Prettier (Markdown/YAML),
# Taplo (TOML), shfmt (shell). Versions are pinned so a local run and CI agree.
# Each tool's config names what keeps its bytes: test fixtures, generated
# files, migrations, extractor modules (their bytes are the code-index version
# stamp) and prompt Markdown.
BIOME    ?= npx --yes @biomejs/biome@2.5.14
PRETTIER ?= npx --yes prettier@3.9.9
TAPLO    ?= npx --yes @taplo/cli@0.7.0
SHFMT    ?= shfmt
SHELL_SOURCES = git ls-files '*.sh' ':!tests/fixtures'

format:
	@command -v $(SHFMT) >/dev/null 2>&1 || { echo "missing shfmt; install it (macOS: brew install shfmt, Linux: see https://github.com/mvdan/sh)" >&2; exit 127; }
	$(PYTHON) -m ruff format .
	$(BIOME) format --write .
	$(PRETTIER) --write --log-level warn "**/*.{md,yml,yaml}"
	$(TAPLO) format
	$(SHELL_SOURCES) | xargs $(SHFMT) -w

format-check:
	@command -v $(SHFMT) >/dev/null 2>&1 || { echo "missing shfmt; install it (macOS: brew install shfmt, Linux: see https://github.com/mvdan/sh)" >&2; exit 127; }
	$(PYTHON) -m ruff format --check .
	$(BIOME) format .
	$(PRETTIER) --check --log-level warn "**/*.{md,yml,yaml}"
	$(TAPLO) format --check
	$(SHELL_SOURCES) | xargs $(SHFMT) -d

typecheck: mypy-gate
	$(PYTHON) -m mypy --strict sdk/python/loom

# mypy ratchet gate (the CI `mypy-gate` step). Fails only on NEW type errors
# past the committed baseline, scripts/gates/mypy_baseline.json; `--update` lowers it.
mypy-gate:
	$(PYTHON) scripts/gates/mypy_gate.py

install:
	pip install -e ".[dev]"

clean: down-all
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

# The 30 golden PR fixtures through loom: G1 vs
# code.verify_citations at captured_sha and merge sha, affected_locations vs
# code.impact, G4 vs code.coverage_claim, the nine PUSHBACK fixtures as a
# memory.search negative gate on clickgap-public (read-only, auto_touch:false).
# Needs a token naming code-clickhouse, tracker-clickhouse and clickgap-public.
# The fixtures are private: GOLDEN_DIR is a checkout's tests/golden.
#   make bench-clickgap-golden GOLDEN_DIR=/path/clickgap/tests/golden LOOM_BASE_URL=http://127.0.0.1:7973 LOOM_API_KEY_FILE=/path/token
bench-clickgap-golden:
	@test -n "$(GOLDEN_DIR)" || { echo "GOLDEN_DIR is required: a checkout's tests/golden" >&2; exit 2; }
	@$(BENCH_PYTHON) bench/clickgap_golden_replay.py \
		--fixtures $(GOLDEN_DIR) \
		--base $(or $(LOOM_BASE_URL),http://localhost:7777) \
		$(if $(LOOM_API_KEY_FILE),--token-file $(LOOM_API_KEY_FILE),) \
		$(if $(GOLDEN_OUT),--out $(GOLDEN_OUT),) $(if $(GOLDEN_MD),--md $(GOLDEN_MD),)
