"""Daily measured-coverage import (`server.code.app.index.coverage_import`).

One pass imports the newest complete run of the configured coverage source
into every configured repository whose namespace `code_coverage_namespaces`
names. A namespace's failure is logged and the next one runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from server.code.app.index.coverage_import import CoverageImportResult, import_from_settings
from server.workers import pass_context

log = logging.getLogger("loom.workers.code_coverage_import")


@dataclass
class CoverageImportPass:
    results: list[CoverageImportResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def run_pass() -> CoverageImportPass:
    from server.code.domain.index.repos import configured_repos
    from server.settings import settings

    wanted = settings.code_coverage_namespace_set()
    out = CoverageImportPass()
    repos = [cfg for cfg in configured_repos() if cfg.namespace in wanted]
    if not repos:
        pass_context.mark_idle()
        return out
    for cfg in repos:
        try:
            out.results.append(await import_from_settings(cfg.org, cfg.namespace))
        except Exception as e:  # one namespace's import must not stop the next
            out.errors.append(f"{cfg.namespace}: {type(e).__name__}: {e}")
            log.exception("code_coverage_import: %s failed", cfg.namespace)
    return out
