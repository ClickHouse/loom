"""A tracker's own conventions, read from its source entry and contract
report: where its CI-checks history lives and which paths are source code.
"""

from __future__ import annotations

import re

from server.settings import settings
from server.tracker.store.source import contract_reports


def checks_config(org: str, namespace: str) -> tuple[str, str]:
    """(the FROM of the CI-checks reads, the repository whose
    CI it is) for a tracker namespace — the source entry's `checks`
    (`{"from", "repo"}`) first; else the tracker's own `checks` table when its
    report provides one (a view, or a mapped table); else the deployment's
    LOOM_TRACKER_CHECKS_FROM / _REPO. The repository defaults to the
    namespace's only repository. ('', '') = no CI history for this tracker."""
    from server.tracker.domain import sources
    from server.tracker.domain.namespaces import tracker_namespace

    cfg = tracker_namespace(org, namespace)
    if cfg is None:
        return "", ""
    ep = sources.endpoint(cfg.credential)
    frm = ep.checks_from if ep is not None else ""
    repo = ep.checks_repo if ep is not None else ""
    if not frm:
        rep = contract_reports.cached(cfg.credential)
        if (
            rep is not None
            and rep.provides("checks")
            and (rep.mode == "mapping" or "checks" in rep.optional_present)
        ):
            frm = f"{cfg.database}.checks"
    frm = frm or str(settings.tracker_checks_from)
    repo = repo or str(settings.tracker_checks_repo)
    if not repo and len(cfg.repos) == 1:
        repo = cfg.repos[0]
    return frm.strip(), repo.strip()


def checks_source_label(org: str, namespace: str) -> str:
    """The checks FROM clause as callers read it and the rollup ledger stores
    it: a `remote` call's user and password masked."""
    from server.tracker.store.source.qa_intel import redact_table_function

    return redact_table_function(checks_config(org, namespace)[0])


# Which paths are source code is the tracker's convention
# (`bisect.code_prefixes` in its source entry; ClickHouse's is ["src/"]), not
# a constant. Without one, any path outside the usual non-code trees counts.
_NON_CODE_RE = r"^(docs?|tests?|test_|\.github|\.gitlab|ci|examples?|benchmarks?)(/|$)"
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,80}$")


def code_prefixes_for(credential: str) -> list[str]:
    """The tracker's code-path prefixes (validated path text), or []."""
    from server.tracker.domain import sources

    ep = sources.endpoint(credential)
    raw = list(ep.code_prefixes) if ep is not None else []
    return [p for p in raw if _PREFIX_RE.match(p)][:20]


def code_path_sql(var: str, prefixes: list[str]) -> str:
    """A ClickHouse predicate: the path in `var` is source code."""
    from server.tracker.domain.mapping import quote_literal

    if prefixes:
        listed = ", ".join(quote_literal(p) for p in prefixes)
        return f"arrayExists(p -> startsWith({var}, p), [{listed}])"
    return f"NOT match({var}, {quote_literal(_NON_CODE_RE)})"
