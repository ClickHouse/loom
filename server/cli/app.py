"""The `loom` parser tree and dispatch: `main` registers every command
family's subcommands in `loom --help` order, parses, and runs the chosen
command under the CLI's principal and traffic class.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from server.cli._apply import add_apply_flags, dry_run_hint
from server.cli.access import _add_mint_token_parser, _add_revoke_token_parser, _cmd_claim_release
from server.cli.doctor import _DOCTOR_HELP, _add_doctor_flags, _cmd_doctor, _cmd_report, _version
from server.cli.hygiene import _add_namespace_hygiene_parsers, _add_namespaces_adopt_parser
from server.cli.import_brain import _add_brain_import_parser
from server.cli.import_clickgap import _add_clickgap_import_parser
from server.cli.import_recall import _add_recall_import_parser
from server.cli.install import _INSTALL_HELP, _add_install_flags, _cmd_install
from server.cli.login import SDK_COMMANDS, add_login_parsers, run_sdk_command
from server.cli.maintenance import (
    _add_activity_trivia_parser,
    _add_memory_repair_parsers,
    _add_reembed_parser,
    _add_reextract_parser,
)
from server.cli.ops import (
    _add_recalibrate_parser,
    _add_sensitivity_sweep_parser,
    _cmd_integrity_repair,
    _cmd_lexical_stats_rebuild,
    _cmd_rechunk,
)
from server.cli.reports import (
    _add_audit_namespace_parser,
    _add_deploy_delta_parser,
    _add_entity_export_parser,
    _add_mentor_parser,
    _cmd_export_actions,
)
from server.cli.serve import _add_serve_and_migrate_parsers
from server.cli.shell import _add_shell_client_parsers
from server.cli.tracker import (
    register_tracker_contract_check,
    register_tracker_dupcheck_fit,
    register_tracker_embed_reset,
    register_tracker_probe,
)
from server.core.env_file import env_file_path
from server.wiring import wire


def _cmd_version(_args: argparse.Namespace) -> int:
    print(_version())
    return 0


def _load_dotenv_like_the_container() -> None:
    """Put every `.env` key in os.environ, as compose's env_file does for the
    container. Pydantic reads only LOOM_* keys; the provider keys read
    straight from os.environ (OPENAI_API_KEY, COHERE_API_KEY, ...) would
    otherwise be unset on the host, and an embedding command would run on
    the fake embedder and write unusable vectors. The shell's own exports
    win. The file is the one `Settings` reads: LOOM_ENV_FILE, else `.env` in
    the working directory (not python-dotenv's upward search from this
    module); empty for none."""
    env_file = env_file_path()
    if env_file is None:
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
    except Exception:  # noqa: BLE001 — best effort; pydantic still reads LOOM_*
        pass


# the QA operator CLIs, registered by `main`
_TRACKER_REGISTRARS = (
    register_tracker_probe,
    register_tracker_contract_check,
    register_tracker_dupcheck_fit,
    register_tracker_embed_reset,
)


def _dispatch(args: argparse.Namespace) -> int:
    """Run the parsed command. A one-shot command runs bound to its own
    context: every write names its principal (the CLI has no auth context,
    so `writer_actor()` would otherwise read '' on every import and backfill),
    the reads run as `background` unless the command names a class of its own
    (`--traffic-class`), and every ClickHouse query is tagged
    `cli.<cmd>[.<sub>]`. Each binding is undone afterwards so an in-process
    caller (a test) is left as it was.

    `serve` runs unbound: it is a server, not a command. Its requests and
    worker passes bind their own class and tag, and a process-wide
    `process:cli:serve` actor would outrank the fallback of every tokenless
    request and every worker write (`writer_actor` reads the pipeline actor
    first)."""
    if getattr(args, "cmd", "") == "serve":
        return int(args.func(args))

    from server.core.ch import query_tag_var
    from server.llm import customer
    from server.memory.domain.provenance import cli_actor, pipeline_actor
    from server.observability.traffic_class import (
        bind_background,
        normalize_class,
        reset_traffic_class,
    )

    token = None
    if not normalize_class(getattr(args, "traffic_class", "")):
        token = bind_background(f"cli:{getattr(args, 'cmd', '') or ''}")
    path = (getattr(args, d, "") for d in ("cmd", "code_cmd", "subcommand"))
    tag_token = query_tag_var.set(".".join(["cli", *filter(None, path)]))
    try:
        # A command for one org (`--org`, or `LOOM_CODE_ORG` for `loom code`) bills
        # its model calls there; one that walks several orgs names each as it goes.
        org = getattr(args, "org", "") or ""
        if not org and getattr(args, "cmd", "") == "code":
            from server.settings import settings

            org = settings.code_org
        # Its namespace too, when it names one: whose agent the identity headers name.
        namespace = str(getattr(args, "namespace", "") or "")
        with (
            pipeline_actor(cli_actor(getattr(args, "cmd", ""))),
            customer.cli_command(),
            customer.for_org(str(org), namespace=namespace),
        ):
            rc = int(args.func(args))
        if hint := dry_run_hint(args, rc):
            # stderr: a command's --json output stays the only thing on stdout
            print(hint, file=sys.stderr)
        return rc
    finally:
        query_tag_var.reset(tag_token)
        if token is not None:
            reset_traffic_class(token)


# Subcommands registered by their own function (kept out of `main` for the
# function-length gate).
_SPLIT_PARSERS: tuple[Any, ...] = (
    _add_reextract_parser,
    _add_deploy_delta_parser,
    _add_entity_export_parser,
    _add_audit_namespace_parser,
    _add_mentor_parser,
)


def _add_module_parsers(sub: Any) -> None:
    """Subcommands registered by their own modules: code, decisions, okf, up, ch-setup,
    participant, deployment, settings, secrets, signing-key, provider-lock and
    export-analytics."""
    from server.cli.code import register_code_cli

    register_code_cli(sub)

    from server.cli.decisions import register_decisions_cli

    register_decisions_cli(sub)

    from server.cli.okf import register_okf_cli

    register_okf_cli(sub)

    from server.cli.up import register_up_cli

    register_up_cli(sub)

    from server.cli.ch_setup import register_ch_setup_cli

    register_ch_setup_cli(sub)

    from server.cli.participant import register_participant_cli

    register_participant_cli(sub)

    from server.cli.deployment import register_deployment_cli

    register_deployment_cli(sub)

    from server.cli.settings_cmd import register_settings_cli

    register_settings_cli(sub)

    from server.cli.secrets import register_secrets_cli

    register_secrets_cli(sub)

    from server.cli.signing_keys import register_signing_key_cli

    register_signing_key_cli(sub)

    from server.cli.provider_lock import register_provider_lock_cli

    register_provider_lock_cli(sub)

    from server.cli.analytics_export import register_analytics_export_cli

    register_analytics_export_cli(sub)


def _add_claim_release_parser(sub: Any) -> None:
    """The `claim-release` subcommand."""
    p_release = sub.add_parser(
        "claim-release",
        help="Release a self-signup claim: revoke its grants, clear the owner, deactivate it. "
        "The name stays reserved.",
    )
    p_release.add_argument("--org", required=True)
    p_release.add_argument("--namespace", required=True)
    p_release.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    p_release.set_defaults(func=_cmd_claim_release)


def _add_doctor_report_parsers(sub: Any) -> None:
    """The `doctor` and `report` subcommands."""
    p_doc = sub.add_parser("doctor", help=_DOCTOR_HELP)
    _add_doctor_flags(p_doc)
    p_doc.set_defaults(func=_cmd_doctor)

    p_rep = sub.add_parser(
        "report",
        help="Print the bug form's Environment field: versions and the `loom doctor` checks.",
    )
    p_rep.add_argument(
        "--open",
        action="store_true",
        help="Also open the bug form in the default browser with the versions pre-filled.",
    )
    p_rep.set_defaults(func=_cmd_report)


def _add_integrity_and_stats_parsers(sub: Any) -> None:
    """The `integrity-repair`, `rechunk`, `export-actions` and
    `lexical-stats-rebuild` subcommands."""
    p_irp = sub.add_parser(
        "integrity-repair",
        help="Read the row-level integrity gauges (dim-0 rows holding "
        "a vector, tombstoned_at/status drift, active rows past valid_to) and, with "
        "--apply, re-emit the dim-0 rows with their true dimension so search can "
        "return them again. Drift and valid_to are listed, not repaired.",
    )
    p_irp.add_argument("--org", default="", help="Limit the repair to one org")
    p_irp.add_argument("--namespace", default="", help="Limit the repair to one namespace")
    p_irp.add_argument("--limit", type=int, default=5000, help="Rows re-emitted per run")
    p_irp.add_argument("--show", type=int, default=20, help="Namespaces to print")
    add_apply_flags(p_irp, writes="re-emit the dim-0 rows")
    p_irp.set_defaults(func=_cmd_integrity_repair)

    p_rechunk = sub.add_parser(
        "rechunk",
        help="Re-chunk and re-embed a namespace's knowledge documents from stored "
        "content through the current chunker, so every document carries section "
        "metadata and document frequency. Idempotent; one embedding per new chunk. "
        "Dry run by default (counts the documents and chunks); --apply re-chunks.",
    )
    p_rechunk.add_argument("--org", required=True)
    p_rechunk.add_argument("--namespace", required=True)
    p_rechunk.add_argument("--source-uri", default="", help="Only this document")
    add_apply_flags(p_rechunk, writes="re-chunk and re-embed the documents")
    p_rechunk.set_defaults(func=_cmd_rechunk)

    p_actions = sub.add_parser(
        "export-actions",
        help=(
            "Export the action schema derived from the operation registry — "
            "an MCP tools/list manifest with loom extras (json) or the Cedar "
            "schema (cedar) a policy gateway governs the surface with."
        ),
    )
    p_actions.add_argument("--format", choices=("json", "cedar"), default="json")
    p_actions.add_argument("-o", "--output", default="-", help="file path, or - for stdout")
    p_actions.set_defaults(func=_cmd_export_actions)

    p_lex = sub.add_parser(
        "lexical-stats-rebuild",
        help="Recompute BM25 document frequency for a namespace from its live rows "
        "(the term-frequency views only see inserts; supersession and tombstones "
        "leave stale vocabulary behind).",
    )
    p_lex.add_argument("--org", required=True)
    p_lex.add_argument("--namespace", required=True)
    p_lex.add_argument("--no-knowledge", dest="knowledge", action="store_false")
    p_lex.add_argument("--no-memory", dest="memory", action="store_false")
    p_lex.set_defaults(func=_cmd_lexical_stats_rebuild)


def _add_version_install_demo_init_parsers(sub: Any) -> None:
    """The `version`, `install`, `demo` and `init` subcommands."""
    p_ver = sub.add_parser("version", help="Print installed Loom version.")
    p_ver.set_defaults(func=_cmd_version)

    p_install = sub.add_parser("install", help=_INSTALL_HELP)
    _add_install_flags(p_install)
    p_install.set_defaults(func=_cmd_install)

    from server.cli.demo import register_demo_cli
    from server.cli.init import register_init_cli

    register_demo_cli(sub)
    register_init_cli(sub)


def _add_sso_parsers(sub: Any) -> None:
    from server.cli.bots import register_bot_cli
    from server.cli.sso import register_sso_cli

    register_sso_cli(sub)
    register_bot_cli(sub)


# Every subcommand `main` registers, in `loom --help` order (the order the
# parsers are added is the order argparse lists them).
_SUBCOMMAND_BUILDERS: tuple[Any, ...] = (
    _add_module_parsers,
    _add_reembed_parser,
    *_SPLIT_PARSERS,
    _add_serve_and_migrate_parsers,
    _add_memory_repair_parsers,
    _add_activity_trivia_parser,
    _add_recalibrate_parser,
    _add_clickgap_import_parser,
    _add_recall_import_parser,
    _add_brain_import_parser,
    _add_claim_release_parser,
    _add_mint_token_parser,
    _add_revoke_token_parser,
    _add_sso_parsers,
    _add_doctor_report_parsers,
    _add_namespace_hygiene_parsers,
    *_TRACKER_REGISTRARS,
    _add_namespaces_adopt_parser,
    _add_sensitivity_sweep_parser,
    _add_integrity_and_stats_parsers,
    _add_version_install_demo_init_parsers,
    _add_shell_client_parsers,
    add_login_parsers,
)


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] in SDK_COMMANDS:
        # Before the checkout's .env, which describes the server and would
        # otherwise override the user's own session (LOOM_API_KEY, LOOM_BASE_URL).
        return run_sdk_command(raw)
    _load_dotenv_like_the_container()
    parser = argparse.ArgumentParser(
        prog="loom",
        description="Loom — vendor-neutral agent memory plane on ClickHouse.",
    )
    parser.add_argument("--version", action="version", version=f"loom {_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for _add_parsers in _SUBCOMMAND_BUILDERS:
        _add_parsers(sub)
    args = parser.parse_args(argv)
    wire()
    return _dispatch(args)
