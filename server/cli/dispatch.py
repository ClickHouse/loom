"""How a parsed `loom` command runs: the process's seams wired, then the command in
its own context. `app.main` parses and enters here; so does a process that runs a share
of a command's work (a `loom code backfill --workers` process), with the command's own
parsed arguments, so it runs under every binding the command does. `app` and the
family modules both import this module; it imports neither."""

from __future__ import annotations

import argparse
import sys

from server.cli._apply import dry_run_hint
from server.wiring import wire


def run_parsed(args: argparse.Namespace) -> int:
    """Run a parsed command as `loom` runs it."""
    wire()
    return _dispatch(args)


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
    from server.core.pg import AuthorityMismatch
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
            try:
                rc = int(args.func(args))
            except AuthorityMismatch as e:
                print(f"  ✗ {e}", file=sys.stderr)
                return 2
        if hint := dry_run_hint(args, rc):
            # stderr: a command's --json output stays the only thing on stdout
            print(hint, file=sys.stderr)
        return rc
    finally:
        query_tag_var.reset(tag_token)
        if token is not None:
            reset_traffic_class(token)
