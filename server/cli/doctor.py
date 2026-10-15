"""`loom doctor` and `loom report`: the pre-boot and live checks, the
problem list /v1/health carries, and the report body."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from server import __version__


def _version() -> str:
    return __version__


# Doctor grades. PASS and WARN exit 0; only FAIL exits 1 —
# a warning is a configuration the operator should look at, not a broken boot.
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _grade(ok: bool) -> str:
    return PASS if ok else FAIL


def dev_jwt_secret_warning() -> str | None:
    """The one sentence `loom mint-token` prints to stderr and `loom doctor`
    grades WARN when the signing secret is the checked-in dev default while
    auth is on. ``None`` when there is nothing to say: a real
    secret, or single-user mode (no bearer is checked, so the secret is moot)."""
    from server.settings import DEV_JWT_SECRET, settings

    if not settings.auth_actually_required():
        return None
    if not settings.jwt_secret_is_dev_default():
        if not settings.jwt_sealing_secret_is_dev_default():
            return None
        return (
            f"LOOM_JWT_SECRET is the dev default ({DEV_JWT_SECRET!r}): tokens are signed "
            "with LOOM_JWT_SECRETS_JSON, but stored credentials are sealed with it. "
            "Set LOOM_JWT_SECRET before storing any."
        )
    return (
        f"LOOM_JWT_SECRET is the dev default ({DEV_JWT_SECRET!r}) with auth on — "
        "anyone who has read this repo can mint tokens this server accepts. "
        "Set LOOM_JWT_SECRET before exposing it."
    )


def _doctor_llm_provider_check() -> tuple[str, str, str]:
    """The `llm_provider` doctor line.

    PASS names the resolved provider/model and the evidence behind it (an
    explicit LOOM_LLM_PROVIDER, the model name, or the key present). WARN when
    the provider is keyed but its key is not in the environment: an inferred
    `openai` from `LOOM_LLM_MODEL=gpt-6-luna` with no OPENAI_API_KEY runs
    extraction on the fake client and yields zero facts. An explicit `fake` is
    a PASS: the operator chose it.
    """
    from server.core.llm_provider import KEY_ENV_BY_PROVIDER
    from server.llm.client import provider_key_missing
    from server.settings import settings

    provider, model = settings.llm_provider, settings.llm_model
    how = (
        f"inferred from {settings.llm_provider_source()}"
        if settings.llm_provider_inferred()
        else "set by LOOM_LLM_PROVIDER"
    )
    if provider_key_missing(provider):
        key_env = KEY_ENV_BY_PROVIDER.get(provider, "its API key")
        return (
            "llm_provider",
            WARN,
            f"{provider} ({how}) but {key_env} is not set — extraction and "
            "memory.ask run on the fake LLM and extract nothing. Set "
            f"{key_env}, or LOOM_LLM_PROVIDER=fake to make the fake explicit.",
        )
    return ("llm_provider", PASS, f"{provider} / {model} ({how})")


# The erasure saga's ClickStack leg matches OTel rows by a Map
# value (`Attributes['loom.namespace'] = …`); a `mapValues(...)` bloom filter
# turns that from a full scan into a granule read. The collector's default
# logs/traces tables carry one, the metrics tables do not, so an erasure can
# read GiBs of metrics to find its own footprint.


def _ch_http_param(value: Any) -> str:
    """A query parameter as the HTTP interface reads it: a scalar is its text,
    an array is ClickHouse's own literal — `['a','b']`, single quotes,
    backslash-escaped — not JSON (a JSON-encoded Array(String) answers 400)."""
    if isinstance(value, (list, tuple)):
        items = ("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in value)
        return "[" + ",".join(items) + "]"
    return str(value)


def _doctor_ch_endpoint(settings: Any) -> tuple[str, tuple[str, str] | None]:
    """The ClickHouse HTTP base URL and basic-auth pair the doctor probes with
    (no pair without a password), resolved as the server's client resolves
    them: an unset `ch_secure` means HTTPS on 8443 only."""
    from server.core.ch_target import ch_secure

    secure = ch_secure(settings)
    base = f"{'https' if secure else 'http'}://{settings.ch_host}:{settings.ch_port}"
    auth = (str(settings.ch_user), str(settings.ch_password)) if settings.ch_password else None
    return base, auth


def _doctor_ch_query(settings: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """One read-only statement over the HTTP interface, JSONEachRow rows; the
    doctor runs before boot and holds no client. It carries the same
    `log_comment` a CHClient query would (`cli.doctor`), as a URL setting."""
    import json as _json

    import httpx

    from server.core.ch import query_settings

    base, auth = _doctor_ch_endpoint(settings)
    query = {k: str(v) for k, v in query_settings().items()}
    query.update({f"param_{k}": _ch_http_param(v) for k, v in params.items()})
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{base}/", params=query, content=sql, auth=auth or httpx.USE_CLIENT_DEFAULT)
    r.raise_for_status()
    return [_json.loads(line) for line in r.text.splitlines() if line.strip()]


def _doctor_otel_erasure_index_check(settings: Any) -> tuple[str, str, str]:
    """The `otel_erasure_index` doctor line: WARN naming every
    OTel metrics table in the ClickStack database without a
    `mapValues(Attributes)` skip index — each erasure full-scans it. PASS
    when the leg is off or every table present carries the index."""
    from server.erasure.store.health import OTEL_INDEX_SQL, OTEL_METRICS_TABLES

    db = str(settings.clickstack_database)
    if not db:
        return ("otel_erasure_index", PASS, "skipped (LOOM_CLICKSTACK_DATABASE unset)")
    try:
        rows = _doctor_ch_query(
            settings,
            OTEL_INDEX_SQL + " FORMAT JSONEachRow",
            {"db": db, "tables": list(OTEL_METRICS_TABLES)},
        )
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("otel_erasure_index", WARN, f"could not read system.data_skipping_indices: {e}")
    missing = sorted(str(r["name"]) for r in rows if not int(r.get("n") or 0))
    if not missing:
        return (
            "otel_erasure_index",
            PASS,
            f"{len(rows)} metrics table(s) carry mapValues(Attributes)",
        )
    return (
        "otel_erasure_index",
        WARN,
        f"{db}.{', '.join(missing)} have no mapValues(Attributes) bloom filter — every "
        "erasure full-scans them (GiBs per saga). Fix: see ClickStack erasure index in "
        "docs/operations/maintenance.md "
        "(ALTER TABLE … ADD INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter, "
        "then MATERIALIZE INDEX).",
    )


_GRADE_BY_SEVERITY = {"error": FAIL, "warn": WARN, "info": PASS}


def _doctor_reader_probe(settings: Any) -> tuple[str, str]:
    """Whether ClickHouse accepts the boot probe's read as LOOM_CH_READER_USER,
    under the caller-shaped caps: `("ok", "")`, `("failing", <error>)`, or
    `("not_probed", <reason>)`. The reason is empty when the user is unset or
    ClickHouse does not answer (`clickhouse_reachable` says so), and names the
    missing table before migrations created loom.memory."""
    import httpx

    from server.core.ch import (
        BEFORE_MIGRATIONS_CODES,
        READER_PROBE_SQL,
        caller_shaped,
        statement_settings,
    )
    from server.core.ch_errors import error_code

    if not settings.ch_reader_user or not settings.ch_host:
        return "not_probed", ""
    base, _ = _doctor_ch_endpoint(settings)
    auth = (str(settings.ch_reader_user), str(settings.ch_password))
    with caller_shaped():
        query = {k: str(v) for k, v in statement_settings(READER_PROBE_SQL).items()}
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(f"{base}/", params=query, content=READER_PROBE_SQL, auth=auth)
    except httpx.HTTPError:
        return "not_probed", ""
    if r.status_code != 200:
        if error_code(r.text) in BEFORE_MIGRATIONS_CODES:
            return "not_probed", "reader user not probed: loom.memory missing"
        return "failing", r.text.strip()
    if r.text.split("\t")[0] != settings.ch_reader_user:
        return "failing", ""
    return "ok", ""


def _doctor_keeper_table(settings: Any, table_sql: str) -> bool | None:
    """Whether the KeeperMap table `table_sql` asks for exists here (run.begin's
    run id claims, /oauth/token's exchange claims, the grant change claims); None
    when ClickHouse does not answer (`clickhouse_reachable` says so)."""
    from server.core.schema.migrate_lease import is_keeper_table

    if not settings.ch_host:
        return None
    try:
        rows = _doctor_ch_query(settings, table_sql + " FORMAT JSONEachRow", {})
    except Exception:  # noqa: BLE001 — a doctor line must never crash the doctor
        return None
    return is_keeper_table(rows[0] if rows else None)


def _doctor_problems() -> list[Any]:
    """The shared problem list as the doctor sees it: settings
    only — no probed dim, breaker, lease or ledger before boot. Empty when
    settings do not load (the `settings_load` check already says so)."""
    try:
        from server.auth.grant_claims import CLAIMS as GRANT_CLAIMS
        from server.auth.oauth_claims import CLAIMS as OAUTH_CLAIMS
        from server.code.app.index.sandbox import layout_refusal, sandbox_available
        from server.core.schema.migrate_lease import RUN_CLAIMS_TABLE_SQL
        from server.observability.problems import HealthState, Problem, collect_problems
        from server.settings import settings

        status, detail = _doctor_reader_probe(settings)
        problems = collect_problems(
            settings,
            HealthState(
                repo_sandbox=sandbox_available(),
                repo_sandbox_refusal=layout_refusal(),
                ch_reader_ok={"ok": True, "failing": False}.get(status),
                ch_reader_error=detail if status == "failing" else "",
                run_claims_atomic=_doctor_keeper_table(settings, RUN_CLAIMS_TABLE_SQL),
                # Only with OAuth on, the one place the answer is used: no query otherwise.
                oauth_claims_atomic=(
                    _doctor_keeper_table(settings, OAUTH_CLAIMS.table_sql)
                    if settings.oauth_enabled
                    else None
                ),
                grant_claims_atomic=_doctor_keeper_table(settings, GRANT_CLAIMS.table_sql),
            ),
        )
        if status == "not_probed" and detail:
            problems.append(
                Problem("ch_reader_user_not_probed", "info", detail, "run `loom migrate`.")
            )
        return problems
    except Exception:  # noqa: BLE001 — a doctor line must never crash the doctor
        return []


def _problem_checks(problems: list[Any]) -> list[tuple[str, str, str]]:
    """Render problems as doctor lines: `problem:<code>`, graded error→FAIL,
    warn→WARN, info→PASS, detail `message Fix: …` — the same list `/v1/health`
    carries, so the two surfaces cannot disagree."""
    return [
        (f"problem:{p.code}", _GRADE_BY_SEVERITY.get(p.severity, WARN), f"{p.message} Fix: {p.fix}")
        for p in problems
    ]


def _run_doctor_checks() -> list[tuple[str, str, str]]:
    """The live checks (Python, settings, JWT, ClickHouse, LLM) followed
    by the shared problem list, as (name, grade, detail)."""
    return _run_live_checks() + _problem_checks(_doctor_problems())


def _run_live_checks() -> list[tuple[str, str, str]]:
    """Run the doctor's live checks and return them as (name, grade, detail)
    with grade one of PASS / WARN / FAIL.

    Shared by `_cmd_doctor` and `_cmd_report`. Each check captures its own
    exception to stay independent — one failure doesn't short-circuit the rest.
    """
    import sys

    checks: list[tuple[str, str, str]] = []  # (name, grade, detail)

    # 1. Python version (matches pyproject's requires-python>=3.12)
    py = sys.version_info
    py_ok = (py.major, py.minor) >= (3, 12)
    checks.append(
        (
            "python_version",
            _grade(py_ok),
            f"{py.major}.{py.minor}.{py.micro}"
            + ("" if py_ok else " — Loom requires Python 3.12 or newer"),
        )
    )

    # 2. Settings load (Pydantic surfaces missing env vars cleanly).
    try:
        from server.settings import settings

        ch_host = settings.ch_host
        jwt_secret_present = bool(settings.jwt_secret)
        settings_ok = bool(ch_host)
    except Exception as e:  # noqa: BLE001 — doctor reports any settings load failure
        checks.append(("settings_load", FAIL, f"{type(e).__name__}: {e}"))
        settings_ok = False
        ch_host = ""
        jwt_secret_present = False
    else:
        checks.append(
            (
                "settings_load",
                _grade(settings_ok),
                "loaded" if settings_ok else "LOOM_CH_HOST is empty",
            )
        )

    # 3. JWT secret. FAIL when empty even if auth_required is off — the secret
    # is needed for `loom mint-token` and the /me/setup flow. WARN when it is
    # the checked-in dev default with auth on: the server
    # boots and works, and anyone with the repo can mint tokens it accepts.
    if not jwt_secret_present:
        jwt_grade, jwt_detail = (
            FAIL,
            "LOOM_JWT_SECRET is empty — `loom mint-token` and /me/setup will fail",
        )
    elif settings_ok and (dev_warning := dev_jwt_secret_warning()):
        jwt_grade, jwt_detail = WARN, dev_warning
    else:
        jwt_grade, jwt_detail = PASS, "set"
    checks.append(("jwt_secret", jwt_grade, jwt_detail))

    # 4. ClickHouse reachability. HTTP probe avoids spinning up asyncio.
    ch_ok = False
    ch_detail = ""
    if not settings_ok or not ch_host:
        ch_detail = "skipped (no LOOM_CH_HOST configured)"
    else:
        base, auth = _doctor_ch_endpoint(settings)
        try:
            import httpx

            # 10s covers serverless cold-start without dragging out a real
            # outage's failure feedback.
            with httpx.Client(timeout=10.0) as c:
                r = c.get(f"{base}/ping", auth=auth)
            ch_ok = r.status_code == 200
            ch_detail = f"{base} → HTTP {r.status_code}" + ("" if ch_ok else " (expected 200)")
        except Exception as e:  # noqa: BLE001 — doctor reports any probe failure
            # The probed host is the first thing an operator needs on a
            # timeout: it tells a wrong `.env` from a blocked path.
            ch_detail = f"{base} → {type(e).__name__}: {e}"
    checks.append(("clickhouse_reachable", _grade(ch_ok), ch_detail))

    # 4b. OTel erasure index: only once ClickHouse answered.
    if settings_ok and ch_ok:
        checks.append(_doctor_otel_erasure_index_check(settings))
    else:
        checks.append(("otel_erasure_index", PASS, "skipped (ClickHouse not reachable)"))

    # 5. LLM provider resolution: what extraction runs on and
    # what decided it; WARN when the provider was inferred (or named) but its
    # key is absent — the runtime then falls back to the echo client.
    if settings_ok:
        checks.append(_doctor_llm_provider_check())
    else:
        checks.append(("llm_provider", PASS, "skipped (settings did not load)"))

    return checks


def _client_checks(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """`loom doctor --client`: the checks from the machine a
    coding agent runs on, with the CLI's own HTTP helpers. A crash in one of
    them is a FAIL line, never a crash of the doctor."""
    try:
        from server.cli._client import get_json, post_op
        from server.cli.doctor_client import client_checks

        return client_checks(args, get_json=get_json, post_json=post_op)
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return [("client", FAIL, f"client checks crashed: {type(e).__name__}: {e}")]


def _client_machine_grades(
    checks: list[tuple[str, str, str]], client: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """Under `--client`, a machine whose server answered
    `/v1/health` clean is a client. Its own direct ClickHouse probe (run only
    because a `.env` loaded here) is then not a failure — the hooks and the
    MCP server go through the server, which reaches ClickHouse itself — so a
    FAIL on `clickhouse_reachable` becomes a WARN that says so. Anything else,
    and a server that did not answer, is left as graded."""
    server = next((c for c in client if c[0] == "client:server"), None)
    if server is None or server[1] != PASS:
        return checks
    note = (
        " — not required on a client machine: the server answers and reaches "
        "ClickHouse itself; the hooks and the MCP server go through it. Ignore, or "
        "run `loom doctor` from outside a checkout to skip the server checks."
    )
    return [
        (name, WARN, detail + note)
        if name == "clickhouse_reachable" and grade == FAIL
        else (name, grade, detail)
        for name, grade, detail in checks
    ]


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Diagnose common pre-boot misconfigurations.

    Reports every check before exiting (not just the first failure), so the
    operator sees the full picture. Exit 0 = nothing failed (warnings are
    listed but do not fail); 1 = at least one FAIL.
    """
    checks = _run_doctor_checks()
    # The client's side of the same picture, when asked.
    client = _client_checks(_args) if getattr(_args, "client", False) else []
    checks = _client_machine_grades(checks, client) + client
    failed = [name for name, grade, _ in checks if grade == FAIL]
    warned = [name for name, grade, _ in checks if grade == WARN]
    if getattr(_args, "json", False):
        # The same problem list `/v1/health` carries, for scripts.
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "checks": [{"name": n, "grade": g, "detail": d} for n, g, d in checks],
                    "problems": [p.as_dict() for p in _doctor_problems()],
                    "client_checks": [{"name": n, "grade": g, "detail": d} for n, g, d in client],
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    width = max(len(name) for name, _, _ in checks)
    for name, grade, detail in checks:
        print(f"  [{grade}]  {name:<{width}}  {detail}")

    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    suffix = f" ({len(warned)} warning(s): {', '.join(warned)})" if warned else ""
    print(f"\nAll {len(checks)} checks passed.{suffix}")
    return 0


def _build_report_body(*, header_only: bool = False) -> str:
    """Compose a GitHub-Issue-ready markdown body: bug-report fields,
    environment block, and auto-attached doctor output.

    No secrets — doctor reports presence booleans + the CH host only.
    """
    import platform as _platform

    loom_version = _version()
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    platform_str = _platform.platform()

    if header_only:
        # --open uses just the header so the URL fits in a browser bar; the
        # operator pastes the fuller `loom report` output into the issue body.
        return (
            "**Environment**\n"
            f"- Loom version: `{loom_version}`\n"
            f"- Python: `{py_version}`\n"
            f"- Platform: `{platform_str}`\n\n"
            "**What you were trying to do**\n_describe here_\n\n"
            "**What you expected**\n_describe here_\n\n"
            "**What actually happened**\n_describe here_\n\n"
            "**Error messages**\n```\npaste here\n```\n\n"
            "<!-- Run `loom report` locally and paste its full output "
            "below for the diagnostic attachment. -->\n"
        )

    checks = _run_doctor_checks()
    width = max(len(name) for name, _, _ in checks)
    doctor_block_lines = []
    for name, grade, detail in checks:
        doctor_block_lines.append(f"  [{grade}]  {name:<{width}}  {detail}")
    doctor_block = "\n".join(doctor_block_lines)

    return (
        "**What you were trying to do**\n_describe here_\n\n"
        "**What you expected to happen**\n_describe here_\n\n"
        "**What actually happened**\n_describe here_\n\n"
        "**Error messages**\n```\npaste here\n```\n\n"
        "---\n\n"
        "**Environment**\n"
        f"- Loom version: `{loom_version}`\n"
        f"- Python: `{py_version}`\n"
        f"- Platform: `{platform_str}`\n\n"
        "**Diagnostic (`loom doctor`)**\n"
        "```\n"
        f"{doctor_block}\n"
        "```\n"
    )


def _cmd_report(args: argparse.Namespace) -> int:
    """Produce a GitHub-Issue-ready bug report with auto-attached diagnostic.

    Default: print the markdown body to stdout. `--open` opens the new-issue
    page in the browser with a short header pre-filled (the full diagnostic
    stays on stdout — long URLs get truncated by some browsers).
    """
    import urllib.parse
    import webbrowser

    body = _build_report_body()
    print(body)

    if args.open:
        header = _build_report_body(header_only=True)
        url = "https://github.com/ClickHouse/loom/issues/new?" + urllib.parse.urlencode(
            {"body": header}
        )
        opened = webbrowser.open(url)
        if opened:
            print(
                "\nOpened the new-issue page in your browser. "
                "Paste the full output above into the body.",
            )
        else:
            print(
                "\nCould not open a browser. Visit:\n  "
                "https://github.com/ClickHouse/loom/issues/new\n"
                "and paste the output above into the body.",
            )
    return 0


_DOCTOR_HELP = (
    "Diagnose pre-boot misconfig (Python version, settings, JWT, CH reachability) "
    "and print the problem list /v1/health carries, each with its fix. With --client, "
    "also examine this machine as a coding-agent client."
)


def _add_doctor_flags(p_doc: argparse.ArgumentParser) -> None:
    """`loom doctor`'s flags (kept out of `main`, which is at its length
    baseline)."""
    p_doc.add_argument(
        "--json",
        action="store_true",
        help="Emit the checks and the problem list as JSON.",
    )
    p_doc.add_argument(
        "--client",
        action="store_true",
        help=(
            "Also examine this machine as a coding-agent client: the "
            "server the hooks would call, the token they would present, whether it is "
            "accepted and can write the intended namespace, the Claude Code / Codex "
            "wiring, and capture jobs that never reached the server."
        ),
    )
    p_doc.add_argument("--server-url", default=None, help="Override the client's server URL.")
    p_doc.add_argument("--token", default=None, help="Override the client's token.")
