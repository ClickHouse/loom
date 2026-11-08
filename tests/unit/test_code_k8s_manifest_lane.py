"""Plain Kubernetes manifests — one `type` symbol per
document named `Kind.name`, `reads` edges for the objects a document names,
`includes` for kustomization resources and ArgoCD source paths, `manifest`
and `crd` facets; a templated manifest outside `templates/` is a Go
template."""

from __future__ import annotations

from server.code.domain.extract.lane_units import bind_sites, span_chunks
from server.code.domain.extract.lang_k8s import (
    CHUNK_OPTIONS,
    SITE_RULES,
    facets,
    is_k8s_manifest,
    is_templated_manifest,
    parse_k8s,
    reference_sites,
    resolve_imports,
    wanted_paths,
)
from tests.unit.code_fixtures import assert_linear

MANIFEST = b"""apiVersion: v1
kind: ServiceAccount
metadata:
  name: operator
  namespace: ch
---
apiVersion: v1
kind: Secret
metadata:
  name: stats-exporter
type: Opaque
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: operator
  labels:
    app: operator
spec:
  template:
    spec:
      serviceAccountName: operator
      containers:
        - name: operator
          image: registry/operator:1.0
          env:
            - name: SCRAPER_USER
              valueFrom:
                secretKeyRef:
                  name: stats-exporter
                  key: user
          envFrom:
            - configMapRef:
                name: operator-config
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: operator-data
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: operator
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: operator-role
subjects:
  - kind: ServiceAccount
    name: operator
    namespace: ch
---
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: clickhouseinstallations.clickhouse.altinity.com
spec:
  group: clickhouse.altinity.com
  names:
    kind: ClickHouseInstallation
    plural: clickhouseinstallations
"""

APP = b"""apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: logsvc
  namespace: argocd
spec:
  source:
    repoURL: https://github.com/ClickHouse/deploy-app
    path: helm-charts/logsvc
  destination:
    namespace: logsvc
"""

KUSTOMIZATION = b"""apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
  - ../base/service.yaml
  - https://github.com/example/remote//x
"""


def test_documents_are_symbols_named_kind_dot_name() -> None:
    pf = parse_k8s("clickhouse-operator/deploy/operator.yaml", MANIFEST)
    names = [(s.name, s.kind, s.start_line, s.end_line) for s in pf.symbols]
    assert names == [
        ("ServiceAccount.operator", "type", 1, 5),
        ("Secret.stats-exporter", "type", 7, 11),
        ("Deployment.operator", "type", 13, 38),
        ("ClusterRoleBinding.operator", "type", 40, 51),
        (
            "CustomResourceDefinition.clickhouseinstallations.clickhouse.altinity.com",
            "type",
            53,
            61,
        ),
    ]
    assert (
        pf.symbols[2].qualified_name == "clickhouse-operator.deploy.operator::Deployment.operator"
    )
    assert pf.symbols[2].signature == "Deployment operator"
    fs = {(f.kind, f.name) for f in facets(pf)}
    assert ("manifest", "Deployment") in fs and ("manifest", "Secret") in fs
    assert ("crd", "ClickHouseInstallation") in fs


def test_references_bind_to_the_named_objects() -> None:
    pf = parse_k8s("clickhouse-operator/deploy/operator.yaml", MANIFEST)
    sites = reference_sites(pf)
    assert {(n, k) for _l, n, k in sites} == {
        ("ServiceAccount.operator", "service_account_ref"),
        ("Secret.stats-exporter", "secret_ref"),
        ("ConfigMap.operator-config", "configmap_ref"),
        ("PersistentVolumeClaim.operator-data", "pvc_ref"),
        ("ClusterRole.operator-role", "role_ref"),
    }
    table = {s.name: [(s.id, s.qualified_name, s.kind)] for s in pf.symbols}
    edges = bind_sites(pf, sites, table, SITE_RULES)
    got = {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1], e.confidence)
        for e in edges
    }
    assert got == {
        ("Deployment.operator", "ServiceAccount.operator", 1.0),
        ("Deployment.operator", "Secret.stats-exporter", 1.0),
        ("ClusterRoleBinding.operator", "ServiceAccount.operator", 1.0),
    }, "the ConfigMap, PVC and ClusterRole are declared elsewhere: no same-file edge"
    assert all(e.edge_kind == "reads" for e in edges)


def test_includes_and_detection() -> None:
    app = parse_k8s("argocd/apps/logsvc.yaml", APP)
    assert [s.name for s in app.symbols] == ["Application.logsvc"]
    assert app.includes == ["argo:helm-charts/logsvc"]
    wanted = wanted_paths("argocd/apps/logsvc.yaml", app.includes)
    assert "helm-charts/logsvc/Chart.yaml" in wanted
    assert resolve_imports(
        "argocd/apps/logsvc.yaml", app.includes, {"helm-charts/logsvc/Chart.yaml"}
    ) == ["helm-charts/logsvc/Chart.yaml"]
    kz = parse_k8s("deploy/overlays/prod/kustomization.yaml", KUSTOMIZATION)
    assert kz.includes == ["deployment.yaml", "../base/service.yaml"], (
        "a remote resource names nothing"
    )
    assert wanted_paths("deploy/overlays/prod/kustomization.yaml", kz.includes) == [
        "deploy/overlays/prod/deployment.yaml",
        "deploy/overlays/base/service.yaml",
    ]
    assert is_k8s_manifest("x/a.yaml", MANIFEST) and not is_templated_manifest("x/a.yaml", MANIFEST)
    templated = b"{{ if .Values.on }}\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}\n{{ end }}\n"
    assert is_templated_manifest("experiments/x/app.yaml", templated)
    assert not is_k8s_manifest("experiments/x/app.yaml", templated)
    assert not is_k8s_manifest("ci/pipeline.yaml", b"stages:\n  - build\n")
    assert not is_k8s_manifest("x/values.yaml", b"replicaCount: 1\nimage: x\n")
    chunks = span_chunks(parse_k8s("x/a.yaml", MANIFEST), 6000, **CHUNK_OPTIONS)
    assert [c.kind for c in chunks].count("function") == 5


def test_an_application_of_many_blank_lines_reads_in_linear_time() -> None:
    import time

    def cost(n: int) -> float:
        from server.code.domain.extract.lang_k8s import _includes_of

        t0 = time.perf_counter()
        _includes_of("Application", "spec:\n" + "\n" * n)
        return time.perf_counter() - t0

    assert_linear(cost, 20_000, 80_000, floor=0.05)
