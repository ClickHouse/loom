"""The README's Add to VS Code links install the entry the VS Code page documents."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from server.identity.app.token_clients import client_config

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"https://(?:insiders\.)?vscode\.dev/redirect\?url=[^)\s`]+")


def _entries(text: str) -> list[dict[str, object]]:
    out = []
    for link in LINK.findall(text):
        parsed = urlparse(link)
        target = parse_qs(parsed.query)["url"][0]
        scheme, _, payload = target.partition(":mcp/install?")
        insiders = (parsed.hostname or "").startswith("insiders.")
        assert scheme == ("vscode-insiders" if insiders else "vscode"), link
        out.append(json.loads(unquote(payload)))
    return out


def test_readme_and_vscode_page_carry_the_same_links() -> None:
    readme = LINK.findall((ROOT / "README.md").read_text())
    page = LINK.findall((ROOT / "docs/features/integrations/vscode.md").read_text())
    assert len(readme) == 2 and readme == page


def test_the_links_install_the_documented_entry_the_server_gives_vs_code() -> None:
    page = (ROOT / "docs/features/integrations/vscode.md").read_text()
    match = re.search(r"```json\n(\{[^`]*?\})\n```", page)
    assert match, "no JSON entry on the VS Code page"
    documented = json.loads(match.group(1))
    assert all(entry == documented for entry in _entries(page))
    server = client_config("vscode", "http://localhost:7777/mcp", "-")
    server_entry = server["config"]["servers"]["loom"]
    assert documented == {"name": "loom", "type": server_entry["type"], "url": server_entry["url"]}
    add_mcp = re.search(r"code --add-mcp '(\{[^']*\})'", page)
    assert add_mcp, "no code --add-mcp command on the VS Code page"
    assert json.loads(add_mcp.group(1)) == documented
