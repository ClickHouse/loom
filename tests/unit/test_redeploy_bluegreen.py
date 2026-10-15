"""The blue/green redeploy as far as a unit test can see it
without docker: the script parses, keeps the preflight, names the two
compose files and the proxy config, writes the upstream file atomically, and
the proxy config only ever includes the upstream file (never a colour by
name); the colour compose shares the rate-limit table and the supervisor
lock across colours and publishes the colour port on loopback only."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "redeploy.sh"
COLOUR = ROOT / "dev" / "docker-compose.colour.yml"
PROXY = ROOT / "dev" / "docker-compose.proxy.yml"
NGINX = ROOT / "deploy" / "proxy" / "nginx.conf"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_redeploy_script_parses() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_redeploy_keeps_the_preflight_and_drives_the_colour_pair() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "dev/redeploy_preflight.sh" in text and "--allow-same" in text
    assert "docker-compose.colour.yml" in text and "docker-compose.proxy.yml" in text
    assert "nginx -s reload" in text and "nginx -t" in text
    # readiness = ok AND no pending migrations, checked on the colour's own port
    assert '.status=="ok"' in text and "(.schema.pending|length)==0" in text
    # atomic upstream rewrite: temp file then rename
    assert re.search(r'>\s*"\$UPSTREAM\.tmp"\s*\n\s*mv -f "\$UPSTREAM\.tmp" "\$UPSTREAM"', text)
    # the old colour is stopped gracefully and kept; its image is tagged
    assert "docker stop -t 45" in text and "loom:prev" in text
    assert "--rollback" in text and "--status" in text
    # make dev-cloud (stop-then-start on one port) is not the path
    assert "make dev-cloud" not in text.split("# ---- pull", 1)[1]


def test_nginx_config_includes_the_upstream_file_and_names_no_colour() -> None:
    text = NGINX.read_text(encoding="utf-8")
    assert "include /etc/nginx/upstream/upstream.conf;" in text
    assert "proxy_pass http://loom;" in text
    assert "listen 7777" in text
    assert "proxy_next_upstream off;" in text  # never replay a POST on the other colour
    directives = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert "loom-blue" not in directives and "loom-green" not in directives
    example = (ROOT / "deploy" / "proxy" / "upstream.conf.example").read_text(encoding="utf-8")
    assert re.search(r"server loom-(blue|green):778[12] resolve;", example)


def test_the_proxy_follows_a_colour_that_restarts_on_a_new_address() -> None:
    """nginx resolved the colour once at reload; a container restart moved it to
    a new IP and every request timed out until the next reload."""
    assert "resolver 127.0.0.11 " in NGINX.read_text(encoding="utf-8")
    (line,) = [
        ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines() if "upstream loom {" in ln
    ]
    assert "zone loom " in line and "server loom-%s:%s resolve;" in line


def test_colour_compose_shares_the_table_and_lock_and_binds_loopback() -> None:
    text = COLOUR.read_text(encoding="utf-8")
    assert "LOOM_RATELIMIT_SHARED_PATH: /app/var/shared/ratelimit.buckets" in text
    assert "LOOM_SUPERVISOR_LOCK_PATH: /app/var/shared/supervisor.lock" in text
    assert 'PORT: "${LOOM_COLOUR_PORT' in text
    assert '"127.0.0.1:${LOOM_COLOUR_PORT}:${LOOM_COLOUR_PORT}"' in text
    assert 'container_name: "loom-${LOOM_COLOUR}"' in text
    assert "stop_grace_period: 45s" in text
    # volumes are named, not project-scoped, so both projects share them
    assert (
        "name: loom-shared" in text and "name: loom-blob-data" in text and "name: loom-net" in text
    )


def test_proxy_compose_holds_7777_and_mounts_the_upstream_directory() -> None:
    text = PROXY.read_text(encoding="utf-8")
    assert '"7777:7777"' in text and "container_name: loom-proxy" in text
    assert "../var/proxy:/etc/nginx/upstream:ro" in text
    assert "../deploy/proxy/entry.conf:/etc/nginx/nginx.conf:ro" in text
    assert "../deploy/proxy:/etc/nginx/loom:ro" in text


def test_nginx_entry_file_only_includes_the_directory_mounted_config() -> None:
    entry = (ROOT / "deploy" / "proxy" / "entry.conf").read_text(encoding="utf-8")
    directives = [ln for ln in entry.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert directives == ["include /etc/nginx/loom/nginx.conf;"]


def test_nginx_forwards_the_terminators_scheme_and_never_overwrites_it() -> None:
    """Behind a TLS-terminating proxy the hop to nginx is http; the
    client-facing scheme is what X-Forwarded-Proto must carry."""
    text = NGINX.read_text(encoding="utf-8")
    assert "proxy_set_header X-Forwarded-Proto $scheme;" not in text
    assert "proxy_set_header X-Forwarded-Proto $loom_forwarded_proto;" in text
    assert "map $http_x_forwarded_proto $loom_forwarded_proto" in text


def test_nginx_replaces_client_sent_address_headers() -> None:
    """uvicorn trusts this proxy's X-Forwarded-For (LOOM_TRUSTED_PROXIES) and reads its last
    entry as the client, so the proxy must send only the
    peer address it saw, never a chain the caller wrote."""
    text = NGINX.read_text(encoding="utf-8")
    assert "$proxy_add_x_forwarded_for" not in text
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in text
    assert "proxy_set_header X-Real-IP $remote_addr;" in text


@pytest.mark.parametrize(("tested", "reloaded"), [(0, True), (1, False)])
def test_a_failed_config_test_stops_the_swap(tested: int, reloaded: bool) -> None:
    """`swap_to … || exit 1` turns `set -e` off inside it, so the check is explicit."""
    script = f"""
set -euo pipefail
docker() {{ if [ "$4" = -t ]; then return {tested}; fi; echo reloaded; }}
eval "$(sed -n '/^reload_proxy() {{/,/^}}/p' {SCRIPT})"
swap() {{ reload_proxy || return 1; echo swapped; }}
swap || echo stopped
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out.split() == (["reloaded", "swapped"] if reloaded else ["stopped"])
