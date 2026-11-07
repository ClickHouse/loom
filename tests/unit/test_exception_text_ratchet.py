"""Code inside `except ... as e` never formats `e` outside a logging call, unless a
file's baseline says so: an exception's text can carry a statement, the server's
address or another row's values, and text kept in a raise, a return, a response
field, a stored column or an outcome reaches a caller. Use `caller_text(e)` and log
the text. The baseline only shrinks; each site it counts was checked by hand: it
quotes the caller's own input (a pattern, a token, a document) or Loom-written text,
or its text stays internal (a string check, a list only logged, a column no op
returns, the operator's output). A logging call's arguments are not counted, nor is
`server/cli`, which prints to the operator who ran it. The scan reads the handler's
own body: a helper or a new exception that is passed the caught one
(`raise Refused(e)`, whose text is then `e`'s) and formats it, an exception taken from
`asyncio.gather(..., return_exceptions=True)`, `task.exception()` or an `__exit__`, an
attribute kept unformatted (`error=e.stderr`), an alias formatted after the handler
ends or bound inside a logging call, `traceback` or `sys` imported under another
name, a serialiser that stringifies it (`json.dumps(..., default=str)`,
`Template.substitute`), and a reviewed site swapped for a new one in the same file
are not caught, so review those by hand. CH-free."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from scripts.gates import ratchet

ROOT = Path(__file__).resolve().parents[2]
_LOGGING = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})
# The names a module's logger goes by (`log = logging.getLogger(...)`), and the module.
_LOGGERS = frozenset({"log", "logger", "_log", "logging"})

# {path: places inside `except ... as e` that format `e` outside a logging call}.
BASELINE = {
    "server/auth/bearer.py": 2,
    "server/auth/jwt.py": 6,
    "server/auth/login.py": 1,
    "server/auth/machines.py": 3,
    "server/auth/oidc.py": 1,
    "server/auth/routes.py": 2,
    "server/auth/scim_routes.py": 1,
    "server/auth/self_signup.py": 1,
    "server/boot.py": 1,
    "server/clients/_http_errors.py": 1,
    "server/clients/github.py": 1,
    "server/code/app/blame.py": 1,
    "server/code/app/grep.py": 1,
    "server/code/app/index/compaction.py": 1,
    "server/code/app/index/compiler_tier.py": 1,
    "server/code/app/index/coverage_import.py": 1,
    "server/code/app/index/egress_relay.py": 1,
    "server/code/app/index/entry_lock.py": 1,
    "server/code/app/index/gitrepo.py": 1,
    "server/code/app/index/ingest.py": 1,
    "server/code/app/index/ingest_pass.py": 7,
    "server/code/app/index/pass_commit.py": 2,
    "server/code/app/index/pass_fetch.py": 9,
    "server/code/app/index/pass_stream.py": 2,
    "server/code/app/index/risk_fit.py": 1,
    "server/code/app/index/sandbox.py": 1,
    "server/code/app/index/scip_full.py": 1,
    "server/code/app/memory_links.py": 1,
    "server/code/app/repo_admin.py": 7,
    "server/code/app/search/handler.py": 1,
    "server/code/workers/backport_gaps.py": 2,
    "server/code/workers/coverage_import.py": 1,
    "server/code/workers/crossref.py": 1,
    "server/code/workers/embed_recovery.py": 1,
    "server/code/workers/graph_stats.py": 4,
    "server/code/workers/pr_history.py": 6,
    "server/code/workers/scip_apply.py": 1,
    "server/code/workers/summary.py": 3,
    "server/core/caller_regex.py": 1,
    "server/core/ch.py": 2,
    "server/core/deployment_config.py": 1,
    "server/core/runtime_files.py": 1,
    "server/core/schema/migrate.py": 5,
    "server/core/schema/table_rebuild.py": 3,
    "server/embedding/_cache.py": 1,
    "server/erasure/api/erasure.py": 1,
    "server/erasure/app/backends.py": 1,
    "server/erasure/app/handlers.py": 1,
    "server/erasure/app/saga.py": 1,
    "server/erasure/domain/scopes.py": 1,
    "server/erasure/workers/recovery.py": 1,
    "server/identity/app/claim_release.py": 1,
    "server/identity/app/collections.py": 1,
    "server/identity/app/history_policy.py": 1,
    "server/identity/app/history_policy_ops.py": 2,
    "server/identity/app/namespace_classification.py": 1,
    "server/insights/app/admin_routes.py": 2,
    "server/insights/app/provider_validation.py": 3,
    "server/insights/store/deploy_delta.py": 1,
    "server/knowledge/app/conversations.py": 1,
    "server/knowledge/app/download_routes.py": 2,
    "server/knowledge/app/extractors.py": 3,
    "server/knowledge/app/ingest_run.py": 3,
    "server/knowledge/app/knowledge_search.py": 2,
    "server/knowledge/app/originals.py": 7,
    "server/knowledge/app/query.py": 1,
    "server/knowledge/app/source_probes.py": 11,
    "server/knowledge/app/source_sync.py": 5,
    "server/knowledge/app/source_sync_notion.py": 2,
    "server/knowledge/app/source_sync_recording.py": 4,
    "server/knowledge/app/sources.py": 5,
    "server/knowledge/app/upload_routes.py": 3,
    "server/knowledge/app/writes.py": 1,
    "server/knowledge/domain/datasets.py": 2,
    "server/knowledge/domain/query.py": 1,
    "server/memory/app/apply.py": 1,
    "server/memory/app/blacklist.py": 1,
    "server/memory/app/code_link_policy.py": 2,
    "server/memory/app/code_link_recheck.py": 1,
    "server/memory/app/code_links.py": 2,
    "server/memory/app/conflict_resolver.py": 2,
    "server/memory/app/consolidate.py": 1,
    "server/memory/app/curation.py": 1,
    "server/memory/app/extract/handler.py": 2,
    "server/memory/app/extract/parse.py": 1,
    "server/memory/app/extract/typed.py": 2,
    "server/memory/app/procedure.py": 1,
    "server/memory/app/procedure_evolve.py": 1,
    "server/memory/app/reembed.py": 1,
    "server/memory/app/reextract.py": 1,
    "server/memory/app/rerank.py": 1,
    "server/memory/app/sensitivity_sweep.py": 1,
    "server/memory/app/session_outcomes.py": 2,
    "server/memory/app/trajectory_notes.py": 5,
    "server/memory/domain/record_constraints.py": 1,
    "server/memory/workers/code_links.py": 1,
    "server/memory/workers/conflict_resolver.py": 3,
    "server/memory/workers/embedding.py": 1,
    "server/memory/workers/entity_extractor.py": 1,
    "server/memory/workers/observe_extract.py": 2,
    "server/memory/workers/orphan_vector_reconcile.py": 4,
    "server/memory/workers/structured_profile.py": 1,
    "server/settings/_env.py": 4,
    "server/settings/_file.py": 1,
    "server/settings/catalog.py": 1,
    "server/storage/retention_overrides.py": 1,
    "server/tasks/store/repo.py": 1,
    "server/tracker/app/contract.py": 1,
    "server/tracker/app/dupcheck_fit.py": 1,
    "server/tracker/app/onboarding.py": 5,
    "server/tracker/app/source.py": 1,
    "server/tracker/app/sources.py": 7,
    "server/tracker/app/test_signal.py": 3,
    "server/tracker/domain/contract.py": 1,
    "server/tracker/domain/mapping.py": 5,
    "server/tracker/domain/sources.py": 1,
    "server/tracker/store/source/base.py": 1,
    "server/tracker/store/source/qa_intel.py": 1,
    "server/tracker/workers/embed.py": 2,
    "server/tracker/workers/links.py": 2,
    "server/tracker/workers/test_daily.py": 2,
    "server/transport/http.py": 1,
}


def _is(node: ast.AST, names: frozenset[str]) -> bool:
    """`node` is the caught exception (`names` holds its name and the handler's aliases
    of it), or an attribute, item, unpacking or walrus of it (`e.stderr`, `e.args[0]`,
    `*e.args`), or picks it (`e.__cause__ or e`, `e if x else ""`, `getattr(e, "msg")`,
    `map(str, e.args)`)."""
    while isinstance(node, ast.Attribute | ast.Subscript | ast.Starred | ast.NamedExpr):
        node = node.value
    if isinstance(node, ast.BoolOp):
        return any(_is(v, names) for v in node.values)
    if isinstance(node, ast.IfExp):
        return _is(node.body, names) or _is(node.orelse, names)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("getattr", "map", "list", "tuple", "sorted"):
            return any(_is(a, names) for a in node.args)
    return isinstance(node, ast.Name) and node.id in names


def _holds(node: ast.AST, names: frozenset[str]) -> bool:
    """`node` is the caught exception or a part of it, or a tuple, list or dict holding
    one."""
    if isinstance(node, ast.Tuple | ast.List):
        return any(_is(i, names) for i in node.elts)
    if isinstance(node, ast.Dict):
        return any(v is not None and _is(v, names) for v in node.values)
    return _is(node, names)


def _formats_the_handled_exception(node: ast.Call) -> bool:
    """`traceback.format_exc()` and its kin (`print_exc(file=...)`,
    `TracebackException.from_exception(e)`, `format_exc()` imported by name),
    `sys.exc_info()` and `sys.exception()`: the exception being handled, whether or not
    the handler named it."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in ("format_exc", "format_exception", "print_exc", "exc_info")
    if not isinstance(func, ast.Attribute):
        return False
    owner = func.value
    if isinstance(owner, ast.Attribute) and owner.attr == "TracebackException":
        return True
    if not isinstance(owner, ast.Name):
        return False
    if owner.id == "traceback":
        return func.attr.startswith(("format_", "print_")) or func.attr == "TracebackException"
    return owner.id == "sys" and func.attr in ("exc_info", "exception")


def _quotes(node: ast.AST, names: frozenset[str]) -> bool:
    """`node` formats the caught exception or a part of it: `{e}`, `str(e)`, `repr(e)`,
    `ascii(e)`, `format(e)`, `e.__str__()`, `... % e` (or a tuple or a dict holding it),
    `"...".format(e)`, `.format_map({...: e})`, `", ".join(e.args)`, or reads the
    exception being handled (`traceback.format_exc()`, `sys.exc_info()`)."""
    if isinstance(node, ast.FormattedValue):
        return _is(node.value, names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _holds(node.right, names)
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in ("str", "repr", "ascii", "format"):
        return bool(node.args) and _is(node.args[0], names)
    if not isinstance(func, ast.Attribute):
        return _formats_the_handled_exception(node)
    if func.attr in ("format", "format_map", "join"):
        args = [*node.args, *(k.value for k in node.keywords)]
        return any(_holds(a, names) for a in args)
    if func.attr in ("__str__", "__repr__", "__format__"):
        return _is(func.value, names)
    return _formats_the_handled_exception(node)


def _aliases(handler: ast.ExceptHandler) -> frozenset[str]:
    """The caught exception's name and every name the handler binds to it or a part of
    it (`err = e`, `cause = e.__cause__`, `err: Exception = e`, `(err := e)`,
    `err, n = e, 1`, `for a in e.args`, `[... for a in e.args]`); empty for a handler
    without `as`."""
    if handler.name is None:
        return frozenset()
    names = {handler.name}
    for node in ast.walk(handler):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.For | ast.comprehension):
            # `for a in e.args`: each item is a part of the exception.
            if _is(node.iter, frozenset(names)):
                names.update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
            continue
        else:
            continue
        if _is(value, frozenset(names)):
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        # `err, n = e, 1`: each name bound to a part of the exception.
        for target in targets:
            if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple):
                pairs = zip(target.elts, value.elts, strict=False)
                names.update(
                    t.id for t, v in pairs if isinstance(t, ast.Name) and _is(v, frozenset(names))
                )
    return frozenset(names)


def _is_logging(node: ast.AST) -> bool:
    """A call on a logger (`log.warning(...)`, `logger.exception(...)`)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOGGING
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in _LOGGERS
    )


def _outside_logging(node: ast.AST) -> Iterator[ast.AST]:
    """`node` and its descendants, nested functions and lambdas included, without
    logging calls."""
    yield node
    for child in ast.iter_child_nodes(node):
        if _is_logging(child):
            continue
        yield from _outside_logging(child)


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("server/cli/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            names = _aliases(handler)
            for stmt in handler.body:
                for node in _outside_logging(stmt):
                    if _quotes(node, names):
                        out[rel] = out.get(rel, 0) + 1
    return out


def test_no_new_code_keeps_the_text_of_the_exception_it_caught() -> None:
    report = ratchet.compare(BASELINE, _counts())
    assert not report.regressions, (
        "code inside `except ... as e` formats `e` outside a logging call "
        f"(use caller_text(e) and log the text): {report.regressions}"
    )
    assert not report.decreases, f"lower BASELINE to match: {report.decreases}"


def test_the_scan_counts_each_shape_and_skips_only_logger_calls() -> None:
    src = """
try:
    pass
except Exception as e:
    a = f"x {e}"
    b = "x %s" % e
    c = "x {}".format(e)
    d = "x %s %s" % (1, e)
    out.append(str(e))
    log.warning("kept in the log: %s", e)
    logger.exception("kept in the log")
    emitter.warning("not a logger %s" % e)
    g = f"x {e.stderr}"
    h = f"%s {1}" % e
    i = "x %(e)s" % {"e": e.args[0]}
    err = e
    j = f"x {err}"
    k = "x {}".format(*e.args)
    m = ", ".join(e.args)
    n = e.__str__()
    o = traceback.format_exc()
    code = e.code
    p = f"x {code}"
    first, n = e.args[0], 1
    hh = f"x {first}"
    ii = ", ".join(str(a) for a in e.args)
    jj = [f"{arg}" for arg in e.args]
    for part in e.args:
        out.append(f"{part}")
    kk = traceback.print_exc(file=buf)
    ll = "".join(traceback.TracebackException.from_exception(e).format())
    mm = format_exc()
    q = format(e)
    r = ascii(e)
    t = "{x}".format_map({"x": e})
    u = FMT % e
    ann: Exception = e
    v = f"x {ann}"
    w = f"x {(walrus := e)}"
    y = f"x {walrus}"
    z = lambda: f"x {e}"
    aa = sys.exc_info()
    bb = f"x {e.__cause__ or e}"
    cc = f"x {e if verbose else ''}"
    dd = f"x {getattr(e, 'msg', '')}"
    ee = ", ".join(map(str, e.args))
    ff = f"x {type(e).__name__}"
    gg = f"x {caller_text(e)}"
"""
    handler = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ExceptHandler))
    names = _aliases(handler)
    assert names == {"e", "err", "ann", "walrus", "code", "first", "a", "arg", "part"}
    found = [n for st in handler.body for n in _outside_logging(st) if _quotes(n, names)]
    assert len(found) == 35


def test_a_handler_without_a_name_is_scanned_for_the_exception_it_handles() -> None:
    src = """
try:
    pass
except Exception:
    a = traceback.format_exc()
    b = f"x {sys.exception()}"
    c = "plain"
"""
    handler = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ExceptHandler))
    names = _aliases(handler)
    assert names == frozenset()
    found = [n for st in handler.body for n in _outside_logging(st) if _quotes(n, names)]
    assert len(found) == 2
