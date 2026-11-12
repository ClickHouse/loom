"""The README's Add to Cursor link installs the entry the Cursor page documents."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from server.identity.app.token_clients import client_config

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"https://cursor\.com/en/install-mcp\?[^)\s`]+")


def _link(text: str) -> str:
    urls = LINK.findall(text)
    assert len(urls) == 1, urls
    return urls[0]


def test_readme_and_cursor_page_carry_the_same_link() -> None:
    readme = (ROOT / "README.md").read_text()
    page = (ROOT / "docs/features/integrations/cursor.md").read_text()
    assert _link(readme) == _link(page)


def test_the_link_decodes_to_the_documented_local_entry() -> None:
    page = (ROOT / "docs/features/integrations/cursor.md").read_text()
    query = parse_qs(urlparse(_link(page)).query)
    match = re.search(r"```json\n(\{[^`]*?\})\n```", page)
    assert match, "no JSON entry on the Cursor page"
    documented = json.loads(match.group(1))
    assert query["name"] == ["loom"]
    assert json.loads(base64.b64decode(query["config"][0])) == documented
    # the URL the server's Cursor setup gives (token_clients, kept alike with the dashboard's)
    server_setup = client_config("cursor", "http://localhost:7777/mcp", "-")
    assert documented == {"url": server_setup["config"]["mcpServers"]["loom"]["url"]}
