# Set up Loom with your coding agent

This page is for a coding agent, Claude Code or Codex, asked to install Loom on the
machine it runs on and to prove that the install works. It names what to run, what not to
do, what each check must see, and the report to give. Every step is written to be run as
is; the steps a person has to take are marked.

Each release attaches this page as plain text, with its own version and commit filled
in:

```text
https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt
https://github.com/ClickHouse/loom/releases/download/v<version>/agent-setup.txt
```

## Pins

<!-- pins -->

This copy names no release, so its commands do not run as they are. Follow the
`agent-setup.txt` of the release you were asked to install, where every `<version>` and
`<commit>` below is filled in; with no release named, follow the latest one's:
`https://github.com/ClickHouse/loom/releases/latest/download/agent-setup.txt`.

<!-- /pins -->

## Rules

- Do not use `sudo`, and do not install or change system packages. Docker with the
  compose plugin must already run for your user, and `curl`, `tar`, `python3`, Node.js 18
  or newer (the plugin's hooks run on it) and your own CLI (`claude` or `codex`) must be on
  `PATH`; if one is missing, stop and report it.
- Set no provider key and ask for none. The stack boots and every check below passes
  without one.
- Before step 1, stop and report it if any `LOOM_*` variable is set in your environment
  (the installer, the plugin and the SDKs all read them: a port, a bind address, another
  server), if `~/.loom` exists, or if your CLI already knows a `loom`: a plugin
  (`claude plugin list`, `codex plugin list`), a marketplace
  (`claude plugin marketplace list`, `codex plugin marketplace list`) or an MCP server
  (`claude mcp list`, `codex mcp list`). Whether to change any of them is the person's
  call. With none of them, the stack answers on `localhost:7777` only and the plugin
  connects to it.
- Never weaken a check to make it pass: do not edit the check script, skip a step, change
  what a check expects, or report `PASS` for a step that did not run. A `FAIL` line is a
  correct report.

## 1. Install the release

```sh
mkdir -p ~/loom-agent-setup
curl -fsSL "https://github.com/ClickHouse/loom/releases/download/v<version>/install.sh" \
  -o ~/loom-agent-setup/loom-install.sh && sh ~/loom-agent-setup/loom-install.sh \
  && rm ~/loom-agent-setup/loom-install.sh
```

The installer verifies the bundle's SHA-256, installs under `~/.loom`, starts the stack
and waits for the server's `/v1/health`. If it fails, the report is its `install` line
alone; do not go on. The [quickstart](quickstart.md#1-boot-the-stack) says what the
installer does in full.

## 2. Install the plugin

The plugin comes from the same release's source, so it matches the server:

```sh
curl -fsSL "https://github.com/ClickHouse/loom/archive/refs/tags/v<version>.tar.gz" \
  | tar -xz -C ~/loom-agent-setup
```

Keep `~/loom-agent-setup`: it is the marketplace's source. Then, for Claude Code:

```sh
claude plugin marketplace add ~/loom-agent-setup/loom-<version>
claude plugin install loom@loom
```

or for Codex:

```sh
codex plugin marketplace add ~/loom-agent-setup/loom-<version>
codex plugin add loom@loom
```

The installed stack is single-user, so the plugin connects to `http://localhost:7777`
with no token. **The person** now starts a new session of the agent (Codex: an interactive
`codex` run, where they also approve the plugin's hooks), gives it this text again and
says "continue at step 3". Steps 3 and 4 run in that session.

## 3. Run the check

Save this script as `loom-setup-check.py` and run `python3 loom-setup-check.py`. It uses
only the Python standard library and talks to Loom over HTTP. It writes into the namespace
`agent-setup-check`, tags what it writes with its own run, searches only that run's rows,
and deletes them when it is done.

```python title="loom-setup-check.py"
import json
import os
import pathlib
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

PIN_VERSION = "<version>"
PIN_COMMIT = "<commit>"
# Loom's own tests point the check elsewhere; an agent sets neither.
BASE = os.environ.get("SETUP_CHECK_URL", "http://localhost:7777")
WHO = {"org": os.environ.get("SETUP_CHECK_ORG", "dev"), "namespace": "agent-setup-check"}
AGENT = "agent-setup"
SESSION = "setup-1"
RUN = f"{random.randrange(100_000):05d}"
TAGS = [f"setup-check:{RUN}"]
FACT = f"The setup check orchard has {RUN} pear trees."
KEY = f"setup/check/orchard-{RUN}"
DOC = f"setup-check://irrigation-{RUN}"
NOTE = f"Irrigation valve {RUN} opens at five in the morning, every second day."
LOOPBACK = ("localhost", "127.0.0.1", "::1")
checks = []


def call(op, **body):
    req = urllib.request.Request(
        f"{BASE}/v1/{op}",
        data=json.dumps({**WHO, **body}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{op} answered {e.code}: {e.read()[:200].decode(errors='replace')}")


def check(name, probe):
    try:
        grade, detail = probe()
    except Exception as e:  # any failure is this check's FAIL line
        grade, detail = "FAIL", f"{type(e).__name__}: {e}"
    checks.append((name, grade, detail))
    return grade == "PASS"


def hit_problem(hits, field, wanted, text_field):
    """Why the first hit is not the row this run wrote, or None when it is."""
    if not hits["results"]:
        return "no hits"
    top = hits["results"][0]
    if top[field] != wanted:
        return f"the first hit is {top[field]}, not {wanted}"
    if RUN not in top[text_field]:
        return f"{wanted} came back without its text"
    return None


def bind_host():
    """LOOM_BIND_HOST from the installed stack's .env, or "" when it sets none."""
    env = pathlib.Path.home() / ".loom" / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    values = [line.split("=", 1)[1].strip() for line in lines if line.startswith("LOOM_BIND_HOST=")]
    return values[-1] if values else ""


def server():
    if PIN_VERSION.startswith("<"):
        return "FAIL", "this copy names no release: use the release's agent-setup.txt"
    if urllib.parse.urlsplit(BASE).hostname not in LOOPBACK:
        return "FAIL", f"{BASE} is not a loopback address"
    host = bind_host()
    if host not in ("", *LOOPBACK):
        return "FAIL", f"~/.loom/.env publishes Loom on {host}"
    with urllib.request.urlopen(f"{BASE}/v1/health", timeout=30) as resp:
        health = json.load(resp)
    version, commit = health.get("version"), health.get("build_commit")
    # A release candidate's server reports the final version it leads to.
    final = PIN_VERSION.split("-")[0]
    if (version, commit) != (final, PIN_COMMIT):
        return "FAIL", f"serves {version} at {commit}; the pins name {final} at {PIN_COMMIT}"
    return "PASS", f"{BASE} serves {version} at {commit[:12]}"


def memory():
    call("memory.set", agent=AGENT, memory_key=KEY, value=FACT, tags=TAGS, session_id=SESSION)
    # A new session reads with no session_id: it sees what the last one wrote.
    hits = call(
        "memory.search", query="how many pear trees does the setup check orchard have", tags=TAGS
    )
    problem = hit_problem(hits, "memory_key", KEY, "content_excerpt")
    if problem:
        return "FAIL", problem
    similarity = float(hits["results"][0]["similarity"])
    return "PASS", f"a new session finds {KEY} (similarity {similarity:.2f})"


def knowledge():
    call("knowledge.add", agent=AGENT, source_uri=DOC, title="Irrigation", content=NOTE, tags=TAGS)
    hits = call("knowledge.search", query="when does the irrigation valve open", tags=TAGS)
    problem = hit_problem(hits, "source_uri", DOC, "content")
    if problem:
        return "FAIL", problem
    similarity = float(hits["results"][0]["similarity"])
    return "PASS", f"knowledge.search finds {DOC} (similarity {similarity:.2f})"


def cleanup():
    gone = call("memory.delete", agent=AGENT, memory_key=KEY, session_id=SESSION)["found"]
    dropped = call("knowledge.delete", source_uri=DOC)["deleted"]
    if not (gone and dropped):
        return "FAIL", f"memory deleted: {gone}, document deleted: {dropped}"
    return "PASS", "deleted the memory and the document it wrote"


# Nothing is written to a server the pins or the loopback rule refuse.
if check("server", server):
    check("memory", memory)
    check("knowledge", knowledge)
    check("cleanup", cleanup)
width = max(len(name) for name, _, _ in checks)
for name, grade, detail in checks:
    print(f"  [{grade}]  {name:<{width}}  {detail}")
failed = [name for name, grade, _ in checks if grade == "FAIL"]
if failed:
    print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
    sys.exit(1)
print(f"\nAll {len(checks)} checks passed.")
```

What each check must see:

| Check       | Passes when                                                                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `server`    | The URL and `~/.loom/.env`'s `LOOM_BIND_HOST` are loopback, and `/v1/health` names the pinned version (a release candidate's final) and commit.               |
| `memory`    | A fact written in one session is the first hit of a search that names no session, as the next session's would be, with its own text and a numeric similarity. |
| `knowledge` | A document added with `knowledge.add` is the first hit of `knowledge.search`, with its own text and a numeric similarity.                                     |
| `cleanup`   | The memory and the document the script wrote are deleted.                                                                                                     |

When `server` fails, the script writes nothing and runs no other check.

## 4. Check the agent's connection

Call your `loom_search` tool once, with the query `agent setup check`, and run
`claude plugin list` (or `codex plugin list`). The line is
`[PASS]  agent  loom_search answered` when the tool answers without an error and the list
shows `loom@loom` enabled, otherwise
`[FAIL]  agent  <the tool's error, or the plugin's status>`.

## Report

Give the script's lines, then the `agent` line, then one summary that counts the `agent`
line too, in `loom doctor`'s format:

```text
  [PASS]  server     http://localhost:7777 serves 1.2.3 at 0123456789ab
  [PASS]  memory     a new session finds setup/check/orchard-04211 (similarity 0.91)
  [PASS]  knowledge  knowledge.search finds setup-check://irrigation-04211 (similarity 0.77)
  [PASS]  cleanup    deleted the memory and the document it wrote
  [PASS]  agent      loom_search answered

All 5 checks passed.
```

On a failure the summary reads `<n> check(s) failed: <names>`, and the first `FAIL` line
is the one to fix. A step that stops the run is one line and its summary:

- a rule that stops you before step 1: `[FAIL]  setup  <what was missing or found>`;
- a failed install: `[FAIL]  install  <the command's last line>`;
- a failed step 2: `[FAIL]  plugin  <the failed command's last line>`;

each followed by `1 check(s) failed: <its name>`.
