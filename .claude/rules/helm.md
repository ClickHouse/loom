---
paths:
  - "deploy/helm/**"
---

# Kubernetes chart

- The chart runs N identical web pods over one ClickHouse service, rolling one pod at a time, plus an optional workers Deployment (`workers.enabled`: one leader, plus a warm standby with `workers.replicaCount: 2`; off, one web pod leads and runs the workers). The operator guide is [kubernetes](../../docs/operations/kubernetes.md).
- The embedding settings (`embedding.*`) and `LOOM_JWT_SECRET` cannot roll. The chart records a hash of `embedding.*` on the Deployment (`loom.mustMatch` in `templates/_helpers.tpl`), compares `LOOM_JWT_SECRET` with its own Secret (`templates/deployment.yaml`), and refuses a `helm upgrade` that changes either unless `mustMatchChange.publish` is a token the live pods don't carry yet; that upgrade runs the publish Job. The guard reads the release with `lookup`, so `helm template` and Argo CD skip it; with `secrets.existingSecret` (the EKS overlay) the root secret is not checked at all.
- Besides the root secret, every pod must share the embedding space: the five fields of `embedding_space.FIELDS` (`server/core/embedding_space.py`; `deployment_config.MUST_MATCH` is that tuple). Changing that set is a design change: it re-keys rebuild recipes and calibration, and changes the `loom.mustMatch` hash, so the next upgrade of every release needs `mustMatchChange.publish`. It also touches `embedding.*` in `values.yaml` and, in `_helpers.tpl`, `loom.config`, `loom.mustMatch` and `loom.guardedNames`, the one list the `env`, `extraEnv`, `secrets.extra` and `settingsFile` refusals share.
- The chart sets some settings by name, mostly in `loom.config` and `loom.container` (`templates/_helpers.tpl`), the secrets in `secret.yaml` and `hooks.yaml`; any other goes through `env` or `settingsFile`. A renamed or removed setting follows the `settings` rule.
- The hook Jobs (`templates/hooks.yaml`) run `loom secrets reseal [--dry-run]`, `loom signing-key record-root` and `loom deployment publish --yes|--check`; a change to one of those commands or flags changes the chart in the same PR.
- Every overlay (`values-eks.yaml`, `values-kind.yaml`, and `values-clickhouse-cloud.yaml` over the EKS one) must lint and render through `kubeconform -strict`, with every optional object switched on, as the CI job does.
- With `postgres.host` set, every pod that gets `loom.config` also gets `loom.pgEnv` and the `loom.pgVolume` mount; `scripts/gates/helm_postgres.py` checks the render.
- Pods run non-root on a read-only root filesystem, so anything that writes needs a mounted volume.
