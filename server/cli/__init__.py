"""`loom` CLI — operator entry point. `loom --help` lists every command.

  loom up            # boot the local stack, wait for health, print how to connect
  loom serve         # boot the control plane (uvicorn)
  loom migrate       # apply schema migrations against the configured CH
  loom mint-token    # issue a JWT locally (operator-side; needs LOOM_JWT_SECRET)
  loom revoke-token  # revoke one token by jti or subject (writes loom.revoked_tokens)
  loom install       # write the MCP client config (Claude Desktop / Cursor / …)
  loom demo          # ingest this repo's AGENTS.md and ask it a question
  loom doctor        # diagnose common pre-boot misconfigurations

Ships in the server package (bundled in the Docker image), not on PyPI:
the published `clickhouse-loom` distribution is the SDK only.

The console entrypoint is `loom = server.cli:main`; `python -m server.cli`
runs the same `main` through `__main__`. `app` builds the parser tree and
dispatches; each command family lives in its own module and registers its
own subcommands:

  access         mint-token, revoke-token, claim-release
  ch_setup       ch-setup
  code           the `loom code …` tree (code-intelligence ingest and upkeep)
  code_*         its subcommand bodies: run, backfill, prs, scip, maintenance
  demo           demo
  deployment     deployment
  doctor         doctor, report
  doctor_client  the `doctor --client` checks of this machine as a client
  hygiene        drops, namespaces-gc, namespaces-adopt, erasure-re-verify
  import_*       import-clickgap, import-recall, import-brain (one module each)
  init           init
  install        install
  maintenance    reconcile-status, period-digest, demote-activity-narration,
                 reextract, reembed
  okf            export-okf, import-okf, okf-lint
  ops            recalibrate, sensitivity, integrity, rechunk, lexical stats
  participant    participant
  provider_lock  provider-lock
  reports        export-actions, entity-export, audit-namespace, deploy-delta, mentor
  restore        reconcile
  secrets        secrets
  serve          serve, migrate
  shell          config, memory-search, ingest-transcript, set-host
  signing_keys   signing-key
  sql_access     sql-access
  tracker        tracker-probe, tracker-contract-check, tracker-dupcheck-fit,
                 tracker-embed-reset
  up             up

Imports run one way: `app` imports the family modules; a family module never
imports `app`.
"""

from server.cli.app import main

__all__ = ["main"]
