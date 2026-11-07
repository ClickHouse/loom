"""Multi-repo configuration for the code plane.

The schema and ops are repo-generic (everything is namespace-keyed); this
module makes the WORKERS generic too. Configure
either:

  * the scalar settings (LOOM_CODE_ORG/NAMESPACE/REPO/GIT_*) — the
    single-repo shape; an empty LOOM_CODE_REPO (the default) configures no scalar
    entry, so a fresh install indexes nothing until a repository is configured; or
  * LOOM_CODE_REPOS_JSON — a JSON array of repo configs; when set it
    REPLACES the scalar config entirely:

      [{"org": "acme", "namespace": "code-widgets",
        "repo": "acme/widgets",
        "git_dir": "var/code_repos/widgets.git",
        "git_remote": "https://github.com/acme/widgets.git",
        "git_branch": "main",
        "path_prefixes": "src/,lib/",
        "exclude": ["vendor/", "*.generated.go"],
        "include_contrib": false, "ignore_file": ".loomignore",
        "github_token": "", "org_readable": true,
        "git_extra_branches": "release/*",
        "classification": "standard"}]

Missing per-repo fields fall back to the scalar settings, so a second repo only declares
what differs; an entry that names no `org` or `repo` while the scalar setting is empty
too is skipped (a configuration error, never a guess). `classification: "restricted"`
marks a private repository's index: it forces `org_readable` off, registers the
directory row restricted, and every read of the namespace needs a token that names it
plus a live direct grant. Every worker iterates ``configured_repos()`` per pass; a
failure on one repo never blocks the others (each is wrapped by the caller).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from server.settings import settings

logger = logging.getLogger("loom.code_index.repos")


@dataclass(frozen=True)
class RepoConfig:
    org: str
    namespace: str
    repo: str  # owner/name (GitHub)
    git_dir: str
    git_remote: str
    git_branch: str
    path_prefixes: tuple[str, ...]
    github_token: str
    # Every authenticated org principal may read this entry (see
    # settings.code_org_readable). Directory row is registered public too.
    org_readable: bool = False
    # Working tree the compiler tier (scip-clang / scip-python) indexes for
    # THIS branch. One per entry: two branches cannot share a checkout, but
    # they can and should share the bare clone (`git_dir`).
    scip_checkout_dir: str = ""
    # Which compiler-tier producer indexes this entry's checkout: "clang", "typescript",
    # "go" or "python". One per entry, like the checkout. '' reads it from the tree's
    # build manifests (producer_inference).
    scip_producer: str = ""
    # Further compiler tiers over the same tree, each in its own checkout
    # (`checkout_for`) so two producers never move one working tree under each other:
    # a Python repository with a TypeScript frontend runs both.
    scip_extra_producers: tuple[str, ...] = ()
    # The scip-typescript projects when the root is not one (no root tsconfig or
    # workspace): directories, each with its own tsconfig or indexed through one the
    # harness writes for its JavaScript. '' = the harness's own inference at the root.
    scip_ts_projects: tuple[str, ...] = ()
    # Namespace whose chunk embeddings may be reused when the exact embedded
    # text already exists there (same org, same model). A release branch
    # shares >90% of its files with master, so pointing it here turns most
    # of a backfill's embedding spend into a lookup.
    embed_reuse_from: str = ""
    # Ingest denylist overrides (code/app/index/denylist.py): globs
    # never indexed for this entry; whether `contrib/` is indexed when the
    # built-in rules are on; the repo-local ignore file honoured at the
    # indexed commit ('' disables).
    exclude: tuple[str, ...] = ()
    include_contrib: bool = False
    # Refuse files whose CONTENT says they are generated (Go's `// Code generated … DO
    # NOT EDIT.` header, the `@generated` marker). Off unless the entry says so: a
    # monorepo of protobuf stubs, mocks and ORM clients turns it on; the C++
    # repositories carry two such files.
    exclude_generated: bool = False
    # `contrib/` prefixes indexed despite `include_contrib` being off
    # (`["contrib/weave"]`), so a vendored dependency's definitions bind the caller's
    # call sites inside the one namespace.
    include_contrib_paths: tuple[str, ...] = ()
    # Namespaces this repository's code calls into (a vendored submodule indexed as its
    # own entry); a definition missing here is looked for there, under the caller's own
    # authorization.
    depends_on: tuple[str, ...] = ()
    ignore_file: str = ".loomignore"
    # Globs of the remote branches the shared mirror carries besides `git_branch`
    # (settings.code_git_extra_branches); fetched once per mirror per ingest pass, never
    # pruned. '' = the configured branch only.
    git_extra_branches: str = ""
    # The shared mirror carries `refs/pull/N/head` for the PRs code_gh_items holds open
    # (settings.code_git_fetch_pr_heads); per entry `git_fetch_pr_heads` overrides. One
    # fetch per mirror per ingest pass.
    git_fetch_pr_heads: bool = True
    # "standard" | "restricted". Restricted entries are never org-readable and their
    # directory row is written restricted; the authorization gate consults this field
    # with no I/O, so the private repository stays restricted even if its directory row
    # is lost.
    classification: str = "standard"
    # The repository is public on its host, so its index is joined at once, read-only,
    # by any org principal (join_policy = open and a discoverable directory row). False
    # (the default) registers the row `approval`; a restricted entry is never public.
    public: bool = False
    # "config" = LOOM_CODE_REPOS_JSON / scalar settings, "registry" = a row of
    # loom.code_repos a person added. The ingest worker clones and builds a registry
    # entry's first index itself.
    source: str = "config"
    # Where the classification / join policy came from — "config" (the entry) or
    # "registry" (a maintainer's access row in loom.code_repos, which wins over the
    # entry).
    access_source: str = "config"

    @property
    def label(self) -> str:
        return f"{self.org}/{self.namespace}"

    @property
    def provider(self) -> str:
        from server.code.domain.index.remote import parse_remote

        for value in (self.git_remote, self.repo):
            if not value:
                continue
            try:
                return parse_remote(value).provider
            except ValueError:
                pass
        return "git"

    @property
    def profile(self) -> str:
        from server.code.domain.index.profiles import profile_for_repo

        return profile_for_repo(self.repo).name


def _prefixes(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# The prefix that keeps every path (`str.startswith("")`); `()` would keep none where a
# consumer tests `startswith(prefixes)`.
WHOLE_TREE: tuple[str, ...] = ("",)


# Where a bare clone lives when the configuration names none.
CLONE_ROOT = "var/code_repos"


def derived_git_remote(repo: str) -> str:
    """`owner/name` → its GitHub remote; '' for no repository."""
    slug = repo.strip().strip("/")
    return f"https://github.com/{slug}.git" if slug else ""


def derived_git_dir(repo: str, root: str = CLONE_ROOT, *, fallback: str = "") -> str:
    """`owner/name` → the bare clone `<root>/<name>.git`; '' when neither the repository
    nor `fallback` names one."""
    name = repo.strip().strip("/").rsplit("/", 1)[-1] or fallback
    return os.path.join(root, f"{name}.git") if name else ""


def _scalar_repo() -> RepoConfig:
    """The scalar settings as one entry. The git dir and remote, when unset, are derived
    from `code_repo`; with no repository they stay empty (`configured_repos()` then
    lists no scalar entry)."""
    repo = str(settings.code_repo).strip()
    return RepoConfig(
        org=settings.code_org,
        namespace=settings.code_namespace,
        repo=repo,
        git_dir=settings.code_git_dir or derived_git_dir(repo),
        git_remote=settings.code_git_remote or derived_git_remote(repo),
        git_branch=settings.code_git_branch,
        path_prefixes=_prefixes(settings.code_path_prefixes) or WHOLE_TREE,
        github_token=settings.code_github_token,
        org_readable=bool(settings.code_org_readable),
        public=bool(settings.code_repo_public),
        scip_checkout_dir=settings.code_scip_checkout_dir,
        git_extra_branches=str(settings.code_git_extra_branches),
        git_fetch_pr_heads=bool(settings.code_git_fetch_pr_heads),
        exclude=_prefixes(settings.code_ingest_exclude),
        ignore_file=str(settings.code_ingest_ignore_file),
    )


def default_git_dir() -> str:
    """The scalar entry's bare clone (configured or derived from `code_repo`); '' when
    no scalar repository is configured. The fallback of a read whose namespace has no
    entry."""
    return _scalar_repo().git_dir


def checkout_for(cfg: RepoConfig, producer: str) -> str:
    """The working tree `producer` indexes for this entry: the entry's checkout for the
    producer it names, a sibling `<checkout>.<producer>` for any other, so two tiers
    never move one tree. An entry that names none keeps its checkout for scip-clang,
    which needs it to be a configured build tree. The dot keeps a sibling from being
    another entry's `checkout-<namespace>`: an entry's namespace has no dots."""
    base = cfg.scip_checkout_dir or settings.code_scip_checkout_dir
    if producer == (cfg.scip_producer or "clang"):
        return base
    return f"{base.rstrip('/')}.{producer}"


def _default_checkout_dir(base: RepoConfig, ns: str) -> str:
    """The scalar checkout for the scalar namespace; a sibling directory
    named after the namespace for every other entry."""
    if ns == base.namespace:
        return base.scip_checkout_dir
    parent = os.path.dirname(base.scip_checkout_dir.rstrip("/")) or "."
    return os.path.join(parent, f"checkout-{ns}")


# A configuration error is a STATE, not an event. Every code worker calls
# configured_repos() once a pass, and the observability probe calls it once an export,
# so logging every occurrence repeats the same ERROR line hundreds of times an hour — a
# log stream that says nothing new and a dashboard that cannot tell "still broken" from
# "broken again". Each distinct condition is logged once per process; the set of
# conditions currently holding is read by the `loom.code.repos_config_errors` gauge
# (server/observability/metrics_gauges.py).
_config_errors: dict[str, str] = {}
_config_errors_logged: set[str] = set()
_config_evaluated = False


def _publish_config_errors(errors: dict[str, str]) -> None:
    global _config_evaluated
    for key, msg in errors.items():
        if key in _config_errors_logged:
            logger.debug("%s (still holding)", msg)
            continue
        _config_errors_logged.add(key)
        logger.error(
            "%s — logged once per process; the loom.code.repos_config_errors "
            "gauge carries the state",
            msg,
        )
    _config_errors.clear()
    _config_errors.update(errors)
    _config_evaluated = True


def config_errors_for(namespace: str) -> list[str]:
    """Why this process could not use the entry for `namespace`, as of its last
    `configured_repos()` call."""
    out = []
    for key, msg in _config_errors.items():
        kind, _, ns = key.partition(":")  # per-entry keys end in the namespace
        if ns == namespace and kind != "entry":  # `entry:<i>` is an index, not a name
            out.append(msg)
    return out


def config_error_count() -> int | None:
    """Entries of LOOM_CODE_REPOS_JSON this process could not use as of its
    last `configured_repos()` call; None before the first call (no
    observation, not 0)."""
    return len(_config_errors) if _config_evaluated else None


# The self-service registry's entries as last read from loom.code_repos
# (`repo_registry.refresh_db_repos` replaces the snapshot); empty until the first read.
_registry_repos: list[RepoConfig] = []


def registry_repos() -> list[RepoConfig]:
    """The registry snapshot (no I/O); empty while self-service is off."""
    return list(_registry_repos) if settings.code_self_service_enabled else []


_registry_namespaces: frozenset[tuple[str, str]] = frozenset()


def registry_namespaces() -> frozenset[tuple[str, str]]:
    """(org, namespace) of every registry entry (no I/O); empty while self-service is off."""
    return _registry_namespaces if settings.code_self_service_enabled else frozenset()


def set_registry_repos(entries: list[RepoConfig]) -> None:
    """Replace the registry snapshot with a fresh read."""
    global _registry_repos, _registry_namespaces
    _registry_repos = list(entries)
    _registry_namespaces = frozenset((c.org, c.namespace) for c in _registry_repos)


def configured_repos() -> list[RepoConfig]:
    """The repo list every code worker iterates: the operator's entries, then the
    self-service registry's (the operator wins on a namespace collision). Invalid JSON
    degrades to the scalar single-repo config with a loud log (a typo must not silently
    stop all indexing) — loud once, then a gauge."""
    return _with_registry(_configured_from_env())


def _with_registry(env_repos: list[RepoConfig]) -> list[RepoConfig]:
    """The operator's entries first, then the registry's. A registry row for an entry's
    own namespace is a maintainer's ACCESS row — its classification and join policy win
    over the entry's (the entry keeps every other field); the row is what
    `code.repo.set_access` writes, so the deployment's default stays where it is and the
    maintainer's decision survives every boot."""
    import dataclasses

    rows = {(c.org, c.namespace): c for c in registry_repos()}
    out: list[RepoConfig] = []
    for c in env_repos:
        access = rows.get((c.org, c.namespace))
        if access is None:
            out.append(c)
            continue
        restricted = access.classification == "restricted"
        out.append(
            dataclasses.replace(
                c,
                classification=access.classification,
                public=access.public and not restricted,
                org_readable=c.org_readable and not restricted,
                access_source="registry",
            )
        )
    seen = {(c.org, c.namespace) for c in env_repos}
    extra = [c for c in registry_repos() if (c.org, c.namespace) not in seen]
    return out + extra


def _scalar_entries(errors: dict[str, str]) -> list[RepoConfig]:
    """The scalar entry, or none: an empty `code_repo` configures no repository, and a
    repository without its org or namespace is a configuration error, not an entry."""
    base = _scalar_repo()
    if not base.repo:
        return []
    missing = [
        k
        for k, v in (("LOOM_CODE_ORG", base.org), ("LOOM_CODE_NAMESPACE", base.namespace))
        if not v
    ]
    if missing:
        errors["scalar"] = (
            f"LOOM_CODE_REPO={base.repo} is set but {' and '.join(missing)} "
            "is empty — the scalar repo entry is not configured"
        )
        return []
    return [base]


def _configured_from_env() -> list[RepoConfig]:
    raw = settings.code_repos_json.strip()
    errors: dict[str, str] = {}
    if not raw:
        scalar = _scalar_entries(errors)
        _publish_config_errors(errors)
        return scalar
    try:
        entries = json.loads(raw)
    except Exception:  # noqa: BLE001 — a malformed value falls back below, never fails the config read
        entries = None
    if not (isinstance(entries, list) and entries):
        errors["json"] = (
            "LOOM_CODE_REPOS_JSON is not a JSON array — falling back to the "
            "scalar single-repo config"
        )
        scalar = _scalar_entries(errors)
        _publish_config_errors(errors)
        return scalar
    base = _scalar_repo()
    repos: list[RepoConfig] = []
    seen: set[str] = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or not e.get("namespace"):
            errors[f"entry:{i}"] = f"code repo entry missing 'namespace', skipped: {e!r}"
            continue
        ns = str(e["namespace"])
        if "." in ns or "/" in ns:
            # a tier's working tree is `<checkout>.<producer>`
            errors[f"namespace:{ns}"] = (
                f"code repo {ns} skipped: a namespace may not contain '.' or '/'"
            )
            continue
        if ns in seen:
            errors[f"duplicate:{ns}"] = f"duplicate code repo namespace {ns} skipped"
            continue
        seen.add(ns)
        org = str(e.get("org") or base.org)
        repo_name = str(e.get("repo") or base.repo)
        if not org or not repo_name:
            field = "org" if not org else "repo"
            errors[f"{field}:{ns}"] = (
                f"code repo {ns} names no '{field}' and LOOM_CODE_{field.upper()} is empty, skipped"
            )
            continue
        classification = str(e.get("classification") or "standard").strip().lower()
        if classification not in ("standard", "restricted"):
            errors[f"classification:{ns}"] = (
                f"code repo {ns}: classification {classification!r} is not "
                "standard|restricted — treated as restricted (fail closed)"
            )
            classification = "restricted"
        org_readable = bool(e["org_readable"]) if "org_readable" in e else base.org_readable
        if classification == "restricted" and org_readable:
            # A restricted entry cannot be org-readable. The config said both: the
            # narrower wins, and the contradiction is a state.
            if "org_readable" in e:
                errors[f"restricted_org_readable:{ns}"] = (
                    f"code repo {ns}: classification=restricted overrides org_readable=true"
                )
            org_readable = False
        git_dir, git_remote = _entry_clone_fields(e, ns, repo_name, base)
        repos.append(
            RepoConfig(
                org=org,
                namespace=ns,
                repo=repo_name,
                git_dir=git_dir,
                git_remote=git_remote,
                git_branch=str(e.get("git_branch") or base.git_branch),
                # An entry that names no prefixes indexes its whole tree (the denylist
                # governs); the scalar's prefixes belong to the scalar entry alone
                path_prefixes=_prefixes(str(e.get("path_prefixes") or "")) or WHOLE_TREE,
                github_token=str(e.get("github_token") or base.github_token),
                org_readable=org_readable,
                classification=classification,
                public=(bool(e["public"]) if "public" in e else base.public)
                and classification != "restricted",
                scip_checkout_dir=str(
                    e.get("scip_checkout_dir") or _default_checkout_dir(base, ns)
                ),
                scip_producer=str(e.get("scip_producer") or base.scip_producer),
                scip_extra_producers=_globs(e.get("scip_extra_producers")),
                scip_ts_projects=_globs(e.get("scip_ts_projects")),
                embed_reuse_from=str(e.get("embed_reuse_from") or ""),
                git_extra_branches=(
                    str(e["git_extra_branches"])
                    if "git_extra_branches" in e
                    else base.git_extra_branches
                ),
                git_fetch_pr_heads=(
                    bool(e["git_fetch_pr_heads"])
                    if "git_fetch_pr_heads" in e
                    else base.git_fetch_pr_heads
                ),
                exclude=_globs(e.get("exclude")) or base.exclude,
                include_contrib=bool(e.get("include_contrib", False)),
                exclude_generated=bool(e.get("exclude_generated", False)),
                include_contrib_paths=_globs(e.get("include_contrib_paths")),
                depends_on=tuple(
                    str(x).strip() for x in (e.get("depends_on") or []) if str(x).strip()
                ),
                ignore_file=(str(e["ignore_file"]) if "ignore_file" in e else base.ignore_file),
            )
        )
    if not repos:
        repos = _scalar_entries(errors)
    _publish_config_errors(errors)
    return repos


def _entry_clone_fields(
    e: dict[str, Any],
    ns: str,
    repo_name: str,
    base: RepoConfig,
) -> tuple[str, str]:
    """An entry's clone fields never come from another repository. An entry that names
    the scalar's repository (a release branch) inherits its `git_dir` / `git_remote`.
    An entry that names a DIFFERENT repository gets, for each field it does not
    declare, a value derived from `repo`: the remote
    `https://github.com/<owner>/<name>.git` and a bare clone of its own, `<name>.git`
    beside the scalar's (under `CLONE_ROOT` when the scalar has none) — never the
    scalar's clone, which the inherited value would have fetched the wrong project
    into. Returns (git_dir, git_remote)."""
    same_repo = bool(base.repo) and repo_name.strip().lower() == base.repo.strip().lower()
    git_remote = str(e.get("git_remote") or "")
    git_dir = str(e.get("git_dir") or "")
    if same_repo:
        return git_dir or base.git_dir, git_remote or base.git_remote
    if not git_remote:
        git_remote = derived_git_remote(repo_name)
    if not git_dir:
        root = (os.path.dirname(base.git_dir.rstrip("/")) or ".") if base.git_dir else CLONE_ROOT
        git_dir = derived_git_dir(repo_name, root, fallback=ns)
    return git_dir, git_remote


def _globs(raw: Any) -> tuple[str, ...]:
    """`exclude` accepts a JSON list or a comma-separated string."""
    if raw is None:
        return ()
    if isinstance(raw, str):
        return _prefixes(raw)
    if isinstance(raw, list | tuple):
        return tuple(str(p).strip() for p in raw if str(p).strip())
    return ()


def repo_for_namespace(org: str, namespace: str) -> RepoConfig | None:
    for cfg in configured_repos():
        if cfg.org == org and cfg.namespace == namespace:
            return cfg
    return None


# The branch names a repository's trunk entry carries.
TRUNK_BRANCHES: tuple[str, ...] = ("master", "main")


def trunk_entry(cfg: RepoConfig, entries: Iterable[RepoConfig]) -> RepoConfig | None:
    """The trunk entry (`master` / `main`) on `cfg`'s mirror (same org, same bare clone),
    for a release-branch entry; None when `cfg` is itself a trunk or no trunk of its
    repository is configured. Two trunks on one mirror: the first by namespace."""
    if cfg.git_branch in TRUNK_BRANCHES:
        return None
    trunks = [
        c
        for c in entries
        if c.org == cfg.org
        and c.git_dir == cfg.git_dir
        and c.namespace != cfg.namespace
        and c.git_branch in TRUNK_BRANCHES
    ]
    return min(trunks, key=lambda c: c.namespace, default=None)


def _trunk_rank(branch: str) -> tuple[int, Any]:
    """The Repositories page's branch order (`branchOrder`,
    frontend/src/lib/repositoryCard.ts): master/main, then release branches
    newest first, then the rest by name."""
    import re as _re

    if branch in TRUNK_BRANCHES:
        return (0, ())
    if branch[:1].isdigit():
        parts = tuple(-int(p) for p in _re.findall(r"\d+", branch))
        return (1, parts)
    return (2, branch)


def docs_namespaces(entries: list[RepoConfig] | None = None) -> dict[tuple[str, str], str]:
    """(org, repo) → the one namespace the repository's documents live in — its trunk
    branch's (the first in the page's branch order), so every branch of the repository
    reads the same docs and they carry the repository's own access rules."""
    best: dict[tuple[str, str], RepoConfig] = {}
    for c in configured_repos() if entries is None else entries:
        key = (c.org, c.repo)
        rank = (_trunk_rank(str(c.git_branch or "")), c.namespace)
        cur = best.get(key)
        if cur is None or rank < (_trunk_rank(str(cur.git_branch or "")), cur.namespace):
            best[key] = c
    return {k: c.namespace for k, c in best.items()}
