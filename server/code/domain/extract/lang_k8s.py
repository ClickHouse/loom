"""Plain Kubernetes manifests: documents are symbols, references are edges.

The Go-template lane reads a Helm manifest *template* (YAML under `templates/` holding a
`{{ … }}` action) as a Go template and writes one `type` symbol per document. A
platform repository also holds the rendered kind: ArgoCD `Application`s,
`CustomResourceDefinition`s, RBAC, `PrometheusRule`s, `ConfigMap`s and `Deployment`s
written as plain YAML with `apiVersion` and no action. Read as `config` line windows,
"where is the Application for billing" and "which Deployments mount the
`db-credentials` Secret" are left to grep.

  * a document with column-0 `apiVersion:` and `kind:` is a `type` symbol
    named `<Kind>.<metadata.name>` (`Application.billing`,
    `Secret.db-credentials`), qualified under the file's dotted path;
    a `CustomResourceDefinition` adds a `crd` facet named by
    `spec.names.kind`, and every document a `manifest` facet named by its
    Kind, so `code.tests_for {kind: manifest, name: PrometheusRule}` lists
    the files that declare one;
  * references are `reads` edges from the document to the named object:
    `configMapRef` / `configMapKeyRef` / `configMap:` → `ConfigMap.<name>`,
    `secretRef` / `secretKeyRef` / `secretName:` → `Secret.<name>`,
    `serviceAccountName:` and RBAC `subjects[]` of kind ServiceAccount →
    `ServiceAccount.<name>`, `roleRef` → `Role.<name>` / `ClusterRole.<name>`,
    `claimName:` → `PersistentVolumeClaim.<name>`, `serviceName:` →
    `Service.<name>` — bound by `lane_units.bind_sites` (same file 1.0,
    repository-unique 0.6);
  * a `Kustomization`'s `resources:` and an ArgoCD `Application`'s
    `spec.source.path` are `includes` to the repository files they name
    (a chart's `Chart.yaml`, a kustomization, the listed manifests),
    settled against the namespace's live paths.

A YAML manifest that carries a template action *outside* `templates/` (a values-driven
ArgoCD app) is a Go template and takes the Go-template lane
(`is_templated_manifest`). Chunks come from the document spans. Outside the extractor
stamp with the other lanes.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.lane_units import SiteRule
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.treesitter import ParsedFile, Symbol

LANG_K8S = "k8s"
KIND_DOCUMENT = "type"
FACET_MANIFEST = "manifest"
FACET_CRD = "crd"

SITE_CONFIGMAP = "configmap_ref"
SITE_SECRET = "secret_ref"
SITE_SERVICE_ACCOUNT = "service_account_ref"
SITE_ROLE = "role_ref"
SITE_PVC = "pvc_ref"
SITE_SERVICE = "service_ref"

SITE_RULES: dict[str, SiteRule] = {
    k: SiteRule("reads", frozenset({KIND_DOCUMENT}))
    for k in (
        SITE_CONFIGMAP,
        SITE_SECRET,
        SITE_SERVICE_ACCOUNT,
        SITE_ROLE,
        SITE_PVC,
        SITE_SERVICE,
    )
}

CHUNK_OPTIONS: dict[str, Any] = {
    "comment_leaders": ("#",),
    "comment_blocks": (),
    "header_prefixes": (),
}

_MANIFEST_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml"})
_API_RE = re.compile(r"(?m)^apiVersion:\s*\S")
_KIND_RE = re.compile(r"(?m)^kind:\s*([A-Za-z][\w.-]*)\s*$")
_NAME_RE = re.compile(r"^  name:\s*+(.*\S)\s*$")
_CRD_KIND_RE = re.compile(r"(?m)^[^\S\n]+kind:\s*([A-Za-z][\w]*)\s*$")
_REF_BLOCK_RE = re.compile(
    r"^(\s*)(?:-\s*)?(configMapRef|configMapKeyRef|configMap|secretRef|secretKeyRef|roleRef|"
    r"persistentVolumeClaim)\s*:\s*$"
)
_INLINE_REF_RE = re.compile(
    r"^\s*(?:-\s*)?(serviceAccountName|secretName|claimName|serviceName)\s*:\s*[\"']?([^\"'\s#]+)"
)
_NAME_LINE_RE = re.compile(r"^(\s*)(?:-\s*)?name\s*:\s*[\"']?([^\"'\s#]+)")
_KIND_LINE_RE = re.compile(r"^(\s*)(?:-\s*)?kind\s*:\s*([A-Za-z]+)")
_SUBJECTS_RE = re.compile(r"^(\s*)subjects\s*:\s*$")
_RESOURCES_RE = re.compile(r"^(\s*)resources\s*:\s*$")
_LIST_ITEM_RE = re.compile(r"^(\s*)-\s*[\"']?([^\"'\s#]+)")
_SOURCE_PATH_RE = re.compile(r"(?m)^[^\S\n]+path:\s*[\"']?([^\"'\s#]+)")
_TEMPLATE_ACTION = b"{{"
_MAX_DOCS = 400

_BLOCK_TARGET: dict[str, tuple[str, str]] = {
    "configMapRef": ("ConfigMap", SITE_CONFIGMAP),
    "configMapKeyRef": ("ConfigMap", SITE_CONFIGMAP),
    "configMap": ("ConfigMap", SITE_CONFIGMAP),
    "secretRef": ("Secret", SITE_SECRET),
    "secretKeyRef": ("Secret", SITE_SECRET),
    "persistentVolumeClaim": ("PersistentVolumeClaim", SITE_PVC),
}
_INLINE_TARGET: dict[str, tuple[str, str]] = {
    "serviceAccountName": ("ServiceAccount", SITE_SERVICE_ACCOUNT),
    "secretName": ("Secret", SITE_SECRET),
    "claimName": ("PersistentVolumeClaim", SITE_PVC),
    "serviceName": ("Service", SITE_SERVICE),
}


@dataclass
class K8sUnits:
    symbols: list[Symbol] = field(default_factory=list)
    sites: list[tuple[int, str, str]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _looks_like_manifest(source: bytes) -> bool:
    head = source[:200_000].decode("utf-8", "replace")
    return bool(_API_RE.search(head)) and bool(_KIND_RE.search(head))


def is_k8s_manifest(path: str, source: bytes) -> bool:
    """A YAML file with column-0 `apiVersion:` and `kind:` and no template
    action — the rendered manifest, ours to read."""
    if _ext(path) not in _MANIFEST_EXTENSIONS:
        return False
    return _TEMPLATE_ACTION not in source[:200_000] and _looks_like_manifest(source)


def is_templated_manifest(path: str, source: bytes) -> bool:
    """A manifest with a template action anywhere in the tree (not only under
    `templates/`, the Helm condition) — a Go template's."""
    if _ext(path) not in _MANIFEST_EXTENSIONS:
        return False
    head = source[:200_000]
    return _TEMPLATE_ACTION in head and b"}}" in head and _looks_like_manifest(head)


def _documents(text: str) -> list[tuple[int, int, str]]:
    """(start line, end line, body) per `---`-separated document."""
    lines = text.split("\n")
    starts = [0] + [i + 1 for i, ln in enumerate(lines) if ln.rstrip() == "---"]
    out: list[tuple[int, int, str]] = []
    for idx, s in enumerate(starts[:_MAX_DOCS]):
        e = (starts[idx + 1] - 2) if idx + 1 < len(starts) else len(lines) - 1
        while e > s and not lines[e].strip():
            e -= 1
        if e < s:
            continue
        out.append((s + 1, e + 1, "\n".join(lines[s : e + 1])))
    return out


def _metadata_name(body: str) -> str:
    in_meta = False
    for ln in body.split("\n"):
        if ln.startswith("metadata:"):
            in_meta = True
            continue
        if in_meta:
            if ln[:1] not in (" ", "\t"):
                in_meta = False
                continue
            m = _NAME_RE.match(ln)
            if m:
                return m.group(1).strip("\"'")
    return ""


def _crd_kind(body: str) -> str:
    """`spec.names.kind` of a CustomResourceDefinition: the first indented
    `kind:` after a `names:` line."""
    seen_names = False
    for ln in body.split("\n"):
        if re.match(r"^\s+names:\s*$", ln):
            seen_names = True
            continue
        if seen_names:
            m = _CRD_KIND_RE.match(ln)
            if m:
                return m.group(1)
            if ln.strip() and not ln.startswith(" "):
                seen_names = False
    return ""


def _sites_of(body: str, first_line: int) -> list[tuple[int, str, str]]:
    """The object references of one document, as (line, `Kind.name`, site)."""
    out: list[tuple[int, str, str]] = []
    state = _RefState()
    for offset, ln in enumerate(body.split("\n")):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        line_no = first_line + offset
        m = _INLINE_REF_RE.match(ln)
        if m:
            kind, site = _INLINE_TARGET[m.group(1)]
            out.append((line_no, f"{kind}.{m.group(2)}", site))
            continue
        if state.block(ln, line_no, out):
            continue
        state.subjects(ln, line_no, out)
    return out


class _RefState:
    """The line scanner's memory: the reference block whose `name:` is still
    to come (`secretKeyRef:` … `name: x`), and the RBAC `subjects:` list."""

    def __init__(self) -> None:
        self.pending: tuple[str, str, int] | None = None  # (target kind, site, indent)
        self.role_kind = ""
        self.in_subjects: int | None = None
        self.subject_kind = ""

    def block(self, ln: str, line_no: int, out: list[tuple[int, str, str]]) -> bool:
        """A `<ref>:` opener, or the `kind:` / `name:` lines under one.
        True when the line was consumed."""
        indent = len(ln) - len(ln.lstrip(" "))
        m = _REF_BLOCK_RE.match(ln)
        if m:
            key = m.group(2)
            if key == "roleRef":
                self.pending, self.role_kind = ("", SITE_ROLE, indent), ""
            else:
                kind, site = _BLOCK_TARGET[key]
                self.pending = (kind, site, indent)
            return True
        if self.pending is None:
            return False
        if indent <= self.pending[2]:
            self.pending = None
            return False
        km = _KIND_LINE_RE.match(ln)
        if km and self.pending[1] == SITE_ROLE:
            self.role_kind = km.group(2)
            return True
        nm = _NAME_LINE_RE.match(ln)
        if nm:
            kind = self.pending[0] or self.role_kind or "ClusterRole"
            out.append((line_no, f"{kind}.{nm.group(2)}", self.pending[1]))
            if self.pending[1] != SITE_ROLE or self.role_kind:
                self.pending = None
            return True
        return False

    def subjects(self, ln: str, line_no: int, out: list[tuple[int, str, str]]) -> None:
        """RBAC `subjects:` entries of kind ServiceAccount."""
        indent = len(ln) - len(ln.lstrip(" "))
        if _SUBJECTS_RE.match(ln):
            self.in_subjects, self.subject_kind = indent, ""
            return
        if self.in_subjects is None:
            return
        if indent <= self.in_subjects and not ln.lstrip().startswith("-"):
            self.in_subjects = None
            return
        km = _KIND_LINE_RE.match(ln)
        if km:
            self.subject_kind = km.group(2)
            return
        nm = _NAME_LINE_RE.match(ln)
        if nm and self.subject_kind == "ServiceAccount":
            out.append((line_no, f"ServiceAccount.{nm.group(2)}", SITE_SERVICE_ACCOUNT))
            self.subject_kind = ""


def _includes_of(kind: str, body: str) -> list[str]:
    """A Kustomization's `resources:` entries, an Application's source path."""
    specs: list[str] = []
    if kind == "Kustomization":
        in_res: int | None = None
        for ln in body.split("\n"):
            indent = len(ln) - len(ln.lstrip(" "))
            rm = _RESOURCES_RE.match(ln)
            if rm:
                in_res = indent
                continue
            if in_res is not None:
                if ln.strip() and indent <= in_res:
                    in_res = None
                    continue
                im = _LIST_ITEM_RE.match(ln)
                if im and not im.group(2).startswith(("http://", "https://", "git@")):
                    specs.append(im.group(2))
    elif kind == "Application":
        # `[^\S\n]`: `^\s*` would scan every blank line below each line start
        m = re.search(r"(?ms)^[^\S\n]*source:\s*\n(.*?)(?=^\S|\Z)", body)
        if m:
            pm = _SOURCE_PATH_RE.search(m.group(1))
            if pm and not pm.group(1).startswith(("http", "oci://")):
                specs.append(f"argo:{pm.group(1).strip('/')}")
    return specs


def k8s_units(path: str, text: str) -> K8sUnits:
    units = K8sUnits()
    mod = module_scope(path)
    for start, end, body in _documents(text):
        km = _KIND_RE.search(body)
        if km is None or not _API_RE.search(body):
            continue
        kind = km.group(1)
        name = _metadata_name(body)
        sym_name = f"{kind}.{name}" if name else kind
        units.symbols.append(
            Symbol(
                qualified_name=f"{mod}::{sym_name}",
                name=sym_name,
                kind=KIND_DOCUMENT,
                signature=f"{kind} {name}".strip(),
                path=path,
                start_line=start,
                end_line=end,
            )
        )
        units.facets.append(Facet(FACET_MANIFEST, kind, start, start, 1.0))
        if kind == "CustomResourceDefinition":
            crd = _crd_kind(body)
            if crd:
                units.facets.append(Facet(FACET_CRD, crd, start, start, 1.0))
        units.sites.extend(_sites_of(body, start))
        units.imports.extend(_includes_of(kind, body))
    return units


def parse_k8s(path: str, source: bytes) -> ParsedFile:
    units = k8s_units(path, source.decode("utf-8", "replace"))
    pf = ParsedFile(path=path, lang=LANG_K8S, source=source)
    pf.symbols = units.symbols
    pf.includes = list(dict.fromkeys(units.imports))
    return pf


def reference_sites(pf: ParsedFile) -> list[tuple[int, str, str]]:
    return k8s_units(pf.path, pf.source.decode("utf-8", "replace")).sites


def facets(pf: ParsedFile) -> list[Facet]:
    return k8s_units(pf.path, pf.source.decode("utf-8", "replace")).facets


_CHART_FILES: tuple[str, ...] = (
    "Chart.yaml",
    "kustomization.yaml",
    "kustomization.yml",
    "values.yaml",
)


def wanted_paths(from_path: str, specs: list[str]) -> list[str]:
    """Repository paths a kustomization entry or an Application source path
    may name: the entry itself (relative to the file) or, for a directory,
    its chart / kustomization files."""
    out: list[str] = []
    here = posixpath.dirname(from_path)
    for spec in specs:
        if spec.startswith("argo:"):
            d = spec[len("argo:") :]
            out.extend(posixpath.join(d, f) if d else f for f in _CHART_FILES)
            continue
        target = posixpath.normpath(posixpath.join(here, spec)).strip("/")
        if posixpath.splitext(target)[1].lower() in (".yaml", ".yml", ".json"):
            out.append(target)
        else:
            out.extend(posixpath.join(target, f) for f in _CHART_FILES)
    return list(dict.fromkeys(out))


def resolve_imports(from_path: str, specs: list[str], present: set[str]) -> list[str]:
    return [c for c in wanted_paths(from_path, specs) if c in present and c != from_path]


__all__ = [
    "CHUNK_OPTIONS",
    "FACET_CRD",
    "FACET_MANIFEST",
    "LANG_K8S",
    "SITE_RULES",
    "K8sUnits",
    "facets",
    "is_k8s_manifest",
    "is_templated_manifest",
    "k8s_units",
    "parse_k8s",
    "reference_sites",
    "resolve_imports",
    "wanted_paths",
]
