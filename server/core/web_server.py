"""`python -m server.core.web_server`: the web tier scripts/serve.sh starts. It runs
uvicorn with the options serve.sh passes, under the same names as uvicorn's CLI, and
with the TLS context of `server.core.tls`, for which the CLI has no option.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping

# The uvicorn CLI options serve.sh passes as arguments. The CLI read every other option
# from its UVICORN_<NAME> variable; uvicorn.run reads none.
PASSED = frozenset(
    {
        "host",
        "port",
        "workers",
        "loop",
        "timeout_graceful_shutdown",
        "timeout_keep_alive",
        "proxy_headers",
        "forwarded_allow_ips",
    }
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server.core.web_server",
        description="Serve server.main:app under uvicorn, over TLS when LOOM_TLS_CERT_FILE is set.",
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--workers", type=int, required=True)
    p.add_argument("--loop", required=True)
    p.add_argument("--timeout-graceful-shutdown", type=int, required=True)
    p.add_argument("--timeout-keep-alive", type=int, required=True)
    p.add_argument("--forwarded-allow-ips", required=True)
    return p


def unread_uvicorn_variables(environ: Mapping[str, str]) -> list[str]:
    """The variables in `environ` that uvicorn's CLI read for an option serve.sh does not
    pass, named by the CLI's own rule (its prefix and the option's name). This launcher
    would drop each one."""
    import click
    from uvicorn.main import main as cli

    prefix = cli.context_settings["auto_envvar_prefix"]
    names = {
        f"{prefix}_{param.name.upper()}"
        for param in cli.params
        if isinstance(param, click.Option) and param.name and param.name not in PASSED
    }
    return sorted(names & environ.keys())


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    import uvicorn

    from server.core import tls

    if unread := unread_uvicorn_variables(os.environ):
        logging.getLogger(__name__).error(
            "loom: the web tier does not read %s; remove them", " ".join(unread)
        )
        return 2

    try:
        options = tls.uvicorn_options()
    except tls.TLSRefused as e:
        # Before uvicorn configures logging, Python's last-resort handler prints this
        # to stderr.
        logging.getLogger(__name__).error("loom: %s", e)
        return 2
    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        loop=args.loop,
        timeout_graceful_shutdown=args.timeout_graceful_shutdown,
        timeout_keep_alive=args.timeout_keep_alive,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
        **options,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
