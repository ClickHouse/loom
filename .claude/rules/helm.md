---
paths:
  - "deploy/helm/**"
---

# Kubernetes chart

- The chart runs N identical web pods over one ClickHouse service, rolling one pod at a time; the operator guide is [kubernetes](../../docs/operations/kubernetes.md).
- The embedding settings (`embedding.*`) and `LOOM_JWT_SECRET` cannot roll: the chart hashes them (`loom.mustMatch` in `templates/_helpers.tpl`) and refuses an upgrade that changes them unless `mustMatchChange.publish=true`, which runs the publish Job. With `secrets.existingSecret` (the EKS overlay) only the Secret's name is hashed, so rotating its contents is not caught. A new setting that must be the same on every pod joins that list.
- The settings the chart sets by name come from `loom.config` in `templates/_helpers.tpl`; any other goes through `env`. Renaming or removing a setting that `loom.config` names changes it there in the same PR.
- Both overlays (`values-eks.yaml`, `values-kind.yaml`) must lint and render through `kubeconform -strict`, with every optional object switched on, as the CI job does.
- Pods run non-root on a read-only root filesystem, so anything that writes needs a mounted volume.
