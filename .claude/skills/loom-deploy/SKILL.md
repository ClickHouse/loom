---
name: loom-deploy
description: Operator procedure for a blue/green Loom deployment with redeploy.sh — preflight, swap, post-swap checks, rollback. Use only when an operator explicitly asks to deploy, roll back or check which build is serving.
disable-model-invocation: true
argument-hint: "[--status | --rollback]"
---

# Deploying Loom

A deploy changes what every user of the deployment is served. Run it only on an explicit request, from the deployment's own checkout. `.claude/settings.json` denies an agent the state-changing commands (`./redeploy.sh`, `--force`, `--rollback`, `docker` on the deployment's containers): print each one for the operator to run, and read its output back.

## 1. Preflight

```bash
./redeploy.sh --status                         # which colour serves, which build, what is idle
git status --short                              # tracked edits ship with the build: each one is a question
git fetch -q origin main && git log --oneline HEAD..origin/main   # what this deploy brings
LOOM_COLOUR=blue LOOM_COLOUR_PORT=7781 docker compose --env-file .env -f dev/docker-compose.colour.yml config -q   # a broken .env edit fails here, not mid-swap
```

- Another deploy in flight (an idle colour starting, a held lease, someone else's announced cut): wait. Two swaps at once can leave the proxy on a colour that is about to stop.
- Read what the incoming commits change: new migrations (additive, applied at boot by the new colour while the old one serves), new settings that need `.env` values, a re-index the PR body announced, host cron scripts a move relocated.
- Never start throwaway containers with the deployment's names (`loom-blue`, `loom-green`, `loom-proxy`, `loom-server`, `loom-workers`, `loom-*-workers`); a cleanup of the throwaway then stops production.

## 2. Swap

```bash
./redeploy.sh            # builds and boots the idle colour, waits for status=ok, readiness.ready and schema.pending=[], swaps the proxy, stops the old colour
```

The script refuses to swap when the new colour is unhealthy or not ready, a provider is degraded, or the embedding width does not match; read its last `!!!` line rather than forcing. Both colours serve on the new schema for a moment, which is safe only because every change follows the [rolling-upgrade rules](../../../docs/architecture/rolling-upgrades.md). The workers lease passes between colours; if the script exits before the new colour's workers take over, it restarts the old colour's.

## 3. After the swap

```bash
curl -s localhost:7777/v1/health | jq '{status, build_commit, ready: .readiness.ready, schema: .schema.pending, providers_degraded}'
docker logs --since 5m "loom-$(./redeploy.sh --status | awk '/\(serving\)/ {print $1}')" 2>&1 | grep -iE 'error|traceback' | tail
```

- `build_commit` is the commit you meant to deploy. The script fetched `origin/main` and rebased onto it (`--autostash`), refused a dirty tree that was behind, and asked `[y/N]` before pulling commits that touch the deploy files; a dirty tree it was not behind ships as it is.
- Reinstall host cron scripts when the deploy moved what they call (`deploy/cron/`).
- Watch error rate and p95 for the first minutes; a ClickHouse replica restarting at the same moment slows inserts without a code cause.

## 4. Rollback

```bash
./redeploy.sh --rollback
```

The previous colour restarts on its previous image and takes the traffic; the current colour keeps running until the operator confirms the rollback and stops it with the command the script prints (`docker stop -t 75 loom-<colour>`). No schema step, because migrations are additive ([upgrades](../../../docs/operations/upgrades.md#rollback)). Rows the new build wrote stay.

## Reference

[Deployment](../../../docs/operations/deployment.md#zero-downtime-pair), [upgrades](../../../docs/operations/upgrades.md), [troubleshooting](../../../docs/operations/troubleshooting.md).
