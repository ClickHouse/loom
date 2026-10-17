---
paths:
  - "deploy/helm/**"
---

# Kubernetes chart

- The chart runs N identical web pods over one ClickHouse service, rolling one pod at a time, plus an optional workers Deployment (`workers.enabled`: a leader and a warm standby; off, one web pod leads and runs the workers). The operator guide is [kubernetes](../../docs/operations/kubernetes.md).
- The embedding settings (`embedding.*`) and `LOOM_JWT_SECRET` cannot roll. The chart records a hash of `embedding.*` on the Deployment (`loom.mustMatch` in `templates/_helpers.tpl`), compares `LOOM_JWT_SECRET` with its own Secret (`templates/deployment.yaml`), and refuses an upgrade that changes either unless `mustMatchChange.publish` is a token the live pods don't carry yet; that upgrade runs the publish Job. With `secrets.existingSecret` (the EKS overlay) the root secret is not checked at all.
- A new setting that must be the same on every pod goes in the server's `MUST_MATCH` (`server/core/deployment_config.py`) and in three places in `_helpers.tpl`: the `loom.mustMatch` hash and the `env` and `extraEnv` refuse lists in `loom.config`.
- The settings the chart sets by name come from `loom.config` in `templates/_helpers.tpl`; any other goes through `env`. Renaming or removing a setting that `loom.config` names changes it there in the same PR, and so does a change to a `loom` command the hook Jobs run (`templates/hooks.yaml`: `secrets reseal`, `signing-key record-root`, `deployment publish`).
- Both overlays (`values-eks.yaml`, `values-kind.yaml`) must lint and render through `kubeconform -strict`, with every optional object switched on, as the CI job does.
- Pods run non-root on a read-only root filesystem, so anything that writes needs a mounted volume.
