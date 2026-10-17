"""SQL <-> C++ crossref for each C++ entry whose `git_head` moved since its last
run: `code_test_map` rows linking test queries to the files that register the
functions and settings they use. Reads the parser tier's chunks only, so it needs
no checkout and no indexer binary."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

logger = logging.getLogger("loom.workers.code_crossref")

WATERMARK = "crossref_commit"


@dataclass
class CodeCrossrefResult:
    repos_checked: int = 0
    passes_run: int = 0
    rows_written: int = 0
    errors: list[str] = field(default_factory=list)


async def run_pass() -> CodeCrossrefResult:
    from server.code.app.index.producer_inference import worker_owns
    from server.code.domain.extract.crossref import run_crossref_pass
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo

    result = CodeCrossrefResult()
    with record_pass("code_crossref") as rec:
        repo = CodeRepo()
        for cfg in configured_repos():
            if not await worker_owns(repo, cfg, "clang"):
                continue
            result.repos_checked += 1
            pass_context.set_namespace(cfg.namespace)
            pass_context.check_stop()
            try:
                head = await repo.get_state(cfg.org, cfg.namespace, "git_head")
                if not head or head == await repo.get_state(cfg.org, cfg.namespace, WATERMARK):
                    continue
                res = await run_crossref_pass(repo, cfg.org, cfg.namespace)
                await repo.set_state(cfg.org, cfg.namespace, WATERMARK, head)
                result.passes_run += 1
                result.rows_written += res.rows_written
            except Exception as e:
                logger.exception("code_crossref: %s failed", cfg.namespace)
                result.errors.append(f"{cfg.namespace}: {type(e).__name__}: {e}")
                pass_context.note_failure(e, cfg.namespace)
        log_pass_errors(logger, "code_crossref", result.errors)
        rec.outcome = {
            "repos_checked": result.repos_checked,
            "passes_run": result.passes_run,
            "rows_written": result.rows_written,
            "errors": len(result.errors),
        }
    return result
