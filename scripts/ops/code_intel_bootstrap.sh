#!/usr/bin/env bash
# Bootstrap the code-intelligence plane on a deployment: the runbook
# (docs/operations/code-index-operations.md) as one supervised, resumable sequence.
# Run ON the loom host, from the repository root. Every phase is idempotent;
# rerunning skips work already done (watermarks and dim=0 stubs are the
# resume state).
#
# Usage:
#   LOOM_BASE_URL=... LOOM_ADMIN_TOKEN=... scripts/ops/code_intel_bootstrap.sh [--dry-run]
#
# Reads CH/embedding/LLM config from the environment/.env like the server.
# PYTHON picks the interpreter (default: python). The preflight (settings,
# git HEAD, the server's health) runs in a dry run too; it only reads. The
# admin token reaches curl on stdin and the bench through LOOM_API_KEY, never
# an argv or a DRY line.
set -euo pipefail

case "${1:-}" in
  "") DRY=0 ;;
  --dry-run) DRY=1 ;;
  *)
    echo "unknown argument: $1 (usage: $0 [--dry-run])" >&2
    exit 2
    ;;
esac
PYTHON=${PYTHON:-python}
say() { printf '\n== %s ==\n' "$*"; }
run() {
  if [[ $DRY == 0 ]]; then
    "$@"
  elif [[ $1 == bench ]]; then
    shift
    echo "DRY: LOOM_API_KEY=<admin token> ${BENCH[*]} $*"
  else
    echo "DRY: $*"
  fi
}
setting() { "$PYTHON" -c "from server.settings import settings; print(settings.$1)"; }

: "${LOOM_BASE_URL:?set LOOM_BASE_URL (the loom URL)}"
: "${LOOM_ADMIN_TOKEN:?set LOOM_ADMIN_TOKEN (ADMIN role, code namespace)}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY (real embeddings; a fake-embedder backfill is useless)}"

say "preflight"
"$PYTHON" -c "from server.settings import settings as s; assert s.embedding_model.startswith('text-embedding'), f'embedding_model is {s.embedding_model} — refusing to backfill with a fake embedder'"
echo "repository HEAD $(git -C . rev-parse --short HEAD)"
curl -fsS -o /dev/null "$LOOM_BASE_URL/v1/health" || {
  echo "$LOOM_BASE_URL/v1/health does not answer 200; nothing ran" >&2
  exit 1
}

say "1/6 init (bare clone + state seed)"
run "$PYTHON" -m server.cli code init

say "2/6 backfill index (files+symbols+chunks; resumable)"
run "$PYTHON" -m server.cli code backfill --phase index

say "3/6 backfill embeddings (TPM-bound)"
run "$PYTHON" -m server.cli code backfill --phase embed

say "4/6 PR history + graph stats (best-effort — enrichment, not retrieval)"
run "$PYTHON" -m server.cli code backfill-prs --months 24 \
  || echo "WARN: PR history skipped (set LOOM_CODE_GITHUB_TOKEN and rerun — recent_prs enrichment stays empty until then)"
echo "enable LOOM_CODE_GRAPH_STATS_ENABLED + LOOM_CODE_SUMMARY_ENABLED for weekly passes"

# Explicit (org, namespace) on every request: a namespace-scoped admin
# token has NO global roles, and a body without a namespace makes the
# transport check roles globally — 403 on every call.
CODE_ORG=$(setting code_org)
CODE_NS=$(setting code_namespace)
echo "target $LOOM_BASE_URL code namespace $CODE_ORG/$CODE_NS"
BENCH=("$PYTHON" bench/code_intel_qa.py --base-url "$LOOM_BASE_URL"
  --org "$CODE_ORG" --namespace "$CODE_NS" --max-mean-tokens 4000)
bench() { LOOM_API_KEY="$LOOM_ADMIN_TOKEN" "${BENCH[@]}" "$@"; }
calibrate() {
  printf 'header = "Authorization: Bearer %s"\n' "$LOOM_ADMIN_TOKEN" \
    | curl -fsS -K - -X POST "$LOOM_BASE_URL/v1/code.calibrate" \
      -H 'Content-Type: application/json' \
      -d "{\"org\":\"$CODE_ORG\",\"namespace\":\"$CODE_NS\"}"
}

say "5/6 calibrate"
run calibrate

# Rollout gates: do NOT flip the MCP listing on a failure.
say "6/6 bench gate: C++ questions"
run bench \
  --min-symbol-exact 0.95 --min-callers-f1 0.85 --min-recall5 0.75 --min-abstain-precision 0.8

# Floors one notch under the accepted baseline (symbol 0.875, callers 1.0,
# r@5 0.75, measured with the deployment-default reranker).
say "6/6 bench gate: python / shell / sql / ci lanes"
run bench --dataset bench/fixtures/code_intel_qa_multilang.jsonl \
  --min-symbol-exact 0.75 --min-callers-f1 0.85 --min-recall5 0.60

# Gates at the code.search baseline (test_set hit@5 0.868, symbol exact@1
# 0.50, no abstains); code.tests_for is measured against it.
say "6/6 bench gate: SQL / shell test corpus"
run bench --dataset bench/fixtures/code_intel_qa_sqlsh.jsonl \
  --min-symbol-exact 0.50 --min-test-hit5 0.85 --min-abstain-recall 0

if [[ $DRY == 1 ]]; then
  say "DRY RUN — nothing above ran; the manual flips after a real pass:"
else
  say "PASSED — manual flips remain (deliberately not automated):"
fi
cat <<'STEPS'
  1. C/C++ compiler tier: build/copy the scip-clang binary (recipe in
     scripts/scip_harness/README.md) and run `loom code scip-full` once.
     The worker is already enabled and begins after those prerequisites exist.
  2. Steady state: LOOM_CODE_INGEST_ENABLED=true (or trigger-only via CI curl),
     LOOM_CODE_EMBED_RECOVERY_ENABLED=true, LOOM_CODE_SCIP_APPLY_ENABLED=true.
  3. Expose to agents: LOOM_MCP_CODE_TOOLS=true and restart.
  4. Repoint the ClickStack placeholder webhook at a real channel.
  5. Optional: seed the cache — `loom code warm <team-questions.txt>`.
STEPS
