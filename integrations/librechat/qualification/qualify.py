"""Qualify LibreChat as a client of Loom's assistant toolset.

Runs the labeled cases in cases.json twice: through LibreChat's agents API (a model
calling Loom's MCP tools with each person's own token) and through the same reads over
HTTP with no model, with the arguments the assistant toolset forces. The inventory,
inspect and tune cases are the reads the dashboard's Assistant page makes; the document
cases ask over a namespace of documents. Each answer is scored against its label for
correctness, citation validity and abstention, a document case also for the rank of its
labeled section among the search hits, and each is timed end to end and to the first
streamed token.

It spends provider money (one model answer per case) and needs a local Loom server
with auth on, a LibreChat started from ../docker-compose.yml, and LOOM_JWT_SECRET to
mint the fixture's tokens. It is not a test and never runs in CI.

    python integrations/librechat/qualification/qualify.py seed
    python integrations/librechat/qualification/qualify.py setup
    python integrations/librechat/qualification/qualify.py revoke
    python integrations/librechat/qualification/qualify.py run
    python integrations/librechat/qualification/qualify.py rescore

`setup-bridge`, in place of `setup`, runs the same cases through the assistant as Loom
serves it: each person's own token asks `POST /v1/assistant/chat/completions`, Loom
delegates the answer's token from it and runs the agent it seeded in the LibreChat
beside it. It grants each person `viewer` on their namespace, as a deployment's people
hold it, since an answer's token is delegated from a live grant.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
STATE = HERE / ".state.json"  # tokens, LibreChat ids and keys of one local run; gitignored
MCP_SERVER = "loom"

# Anthropic list price per million tokens, for the cost line of the report.
PRICE_PER_MTOK = {"claude-haiku-4-5-20251001": (1.0, 5.0), "claude-haiku-4-5": (1.0, 5.0)}

ABSTAIN_PATTERNS = (
    r"\bno (stored |recorded )?(memor|record|information|evidence|entr)",
    r"\bnot (found|observed|stored|recorded|in (loom|the namespace|the stored))",
    r"\b(could ?n[o']t|can ?not|unable to|did ?n[o']t) find\b",
    r"\bdoes ?n[o']t (contain|hold|have)\b",
    r"\bnothing (about|on|in)\b",
    r"\bno (matching|relevant)\b",
)
# How an answer says a document does not hold the fact: read for document cases only;
# memory cases use ABSTAIN_PATTERNS alone.
DOC_ABSTAIN_PATTERNS = (
    r"\bnot in the documents\b",
    r"\b(does|do) ?n[o']t (specify|state|say|mention)\b",
    r"\bnot (specified|stated|mentioned)\b",
)
REFUSAL_PATTERNS = (
    r"\b(access|permission|authori[sz]|forbidden|denied|refus|not allowed|no access)",
    r"\b(401|403|unauthenticated|revoked|invalid token|expired)\b",
    r"\b(can ?not|unable to|could ?n[o']t) (access|read|reach|connect)",
)
KEY_RE = re.compile(r"\b[a-z][a-z0-9-]*/[a-z][a-z0-9-]*/[a-z0-9][a-z0-9-]*\b")
# A document citation as INSTRUCTIONS asks for it: `[source_uri § A > B, revision N]` (the
# deployed agent writes `[doc:source_uri § ...]`), the
# section and the revision optional. A source_uri here holds a `/`, so `[1]` is no citation;
# a heading may hold a comma; a revision that is no number (`revision unknown`) names none.
DOC_CITE_RE = re.compile(
    r"\[(?:doc:)?(?P<uri>[^\[\]\s§,]*/[^\[\]\s§,]*)\s*(?:§\s*(?P<section>[^\[\]]*?))?\s*"
    r"(?:,\s*rev(?:ision)?\b\.?\s*(?P<revision>[^\[\],]*?))?\s*\]"
)


def load(name: str) -> Any:
    return json.loads((HERE / name).read_text())


def state() -> dict[str, Any]:
    if not STATE.exists():
        sys.exit(f"{STATE} is missing: run seed first")
    return json.loads(STATE.read_text())


def save_state(data: dict[str, Any]) -> None:
    # The file holds tokens and passwords: it is never readable by others, not even briefly.
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)


def mint(
    loom_bin: str,
    *,
    subject: str,
    org: str,
    namespace: str,
    role: str,
    person: bool = False,
    extra_namespaces: list[str] | None = None,
) -> dict[str, Any]:
    namespaces = [namespace, *(extra_namespaces or [])]
    out = subprocess.run(
        [loom_bin, "mint-token", "--subject", subject, "--org", org, "--namespaces", *namespaces]
        + ["--roles", role, "--ttl-days", "1", "--json"]
        + (["--user-token"] if person else []),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


def loom_call(
    base: str, token: str, op: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any], float]:
    started = time.perf_counter()
    r = httpx.post(
        f"{base}/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60
    )
    elapsed = time.perf_counter() - started
    try:
        data = r.json()
    except ValueError:
        data = {"text": r.text}
    return r.status_code, data, elapsed


# ---------------------------------------------------------------- seed


def write(base: str, token: str, org: str, ns: str, writer: str, key: str, text: str) -> int:
    body = {"org": org, "namespace": ns, "agent": writer, "memory_key": key, "value": text}
    code, out, _ = loom_call(base, token, "memory.set", body)
    if code != 200:
        sys.exit(f"memory.set {key}: {code} {out}")
    return int(out["version"])


def write_document(
    base: str, token: str, org: str, ns: str, writer: str, doc: dict[str, Any]
) -> None:
    """One revision of a fixture document, through the text path: a second write of the
    same source_uri publishes its next revision."""
    body = {
        "org": org,
        "namespace": ns,
        "agent": writer,
        "source_uri": doc["source_uri"],
        "title": doc["title"],
        "content": doc["content"],
    }
    code, out, _ = loom_call(base, token, "knowledge.upsert", body)
    if code != 200:
        sys.exit(f"knowledge.upsert {doc['source_uri']}: {code} {out}")


def cmd_seed(args: argparse.Namespace) -> None:
    fixture = load("fixture.json")
    st: dict[str, Any] = {"loom_url": args.loom_url, "tokens": {}, "versions": {}}
    for tenant in fixture["tenants"]:
        org, ns = tenant["org"], tenant["namespace"]
        documents = tenant.get("documents", {})
        tokens = {
            writer: mint(args.loom_bin, subject=writer, org=org, namespace=ns, role="writer")[
                "token"
            ]
            for writer in [*tenant["writers"], *documents]
        }
        edits = [(w, k, t) for w, rows in tenant["writers"].items() for k, t in rows]
        for writer, key, text in edits + [tuple(r) for r in tenant["revisions"]]:
            version = write(args.loom_url, tokens[writer], org, ns, writer, key, text)
            st["versions"][f"{ns}:{key}"] = version
        docs = [(w, d) for w, rows in documents.items() for d in rows]
        for writer, doc in docs + [(d["writer"], d) for d in tenant.get("document_revisions", [])]:
            write_document(args.loom_url, tokens[writer], org, ns, writer, doc)
        # The scorer's own viewer token: the citation checks read each cited key and
        # document with it.
        st["tokens"][f"scorer:{ns}"] = mint(
            args.loom_bin, subject=f"scorer-{org}", org=org, namespace=ns, role="viewer"
        )["token"]
    for person in fixture["people"]:
        minted = mint(
            args.loom_bin,
            subject=person["name"],
            org=person["org"],
            namespace=person["namespace"],
            role="viewer",
            person=True,
        )
        st["tokens"][person["name"]] = minted["token"]
        st["tokens"][f"{person['name']}:jti"] = minted["jti"]
    save_state(st)
    tenants = fixture["tenants"]
    print(
        f"seeded {sum(len(r) for t in tenants for r in t['writers'].values())} memories and "
        f"{sum(len(r) for t in tenants for r in t.get('documents', {}).values())} documents; "
        f"state in {STATE}"
    )


def cmd_revoke(args: argparse.Namespace) -> None:
    """Revoke the tokens of the people the fixture marks revoked, after setup stored them."""
    st = state()
    for person in load("fixture.json")["people"]:
        if not person["revoked"]:
            continue
        name = person["name"]
        subprocess.run(
            [args.loom_bin, "revoke-token", "--jti", st["tokens"][f"{name}:jti"]]
            + ["--reason", "qualify"],
            check=True,
            capture_output=True,
        )
        print(f"revoked {name}")
    # A server rereads the revocation list every LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS.
    print(f"waiting {args.settle:.0f} s for the revocation to reach the server")
    time.sleep(args.settle)


# ---------------------------------------------------------------- LibreChat setup

# LibreChat's JWT routes refuse a request without a browser user agent (uaParser).
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"

INSTRUCTIONS = """You answer questions about the Loom namespace {namespace} in org {org}
with the Loom tools. Pass org "{org}" and namespace "{namespace}" on every call unless the
question names another. Cite each stored fact by its memory_key and version. Cite each
document passage as [source_uri § section], with the section breadcrumb a search hit gives
(leave it out when the hit has none), for example [guides/setup.md § Setup > Install]. Add
the revision, as in [guides/setup.md § Setup > Install, revision 3], only when a tool
returned that document's revision number; never write a revision no tool returned.
When the tools return nothing relevant, say it is not stored in Loom. When a tool call
fails, say that it failed and quote the error. Never answer from your own knowledge."""


def lc_login(lc: httpx.Client, email: str, password: str) -> str:
    r = lc.post("/api/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return r.json()["token"]


def cmd_setup(args: argparse.Namespace) -> None:
    """Register each person in LibreChat, store their own Loom token as their MCP credential,
    and create their agent and an agents-API key."""
    st = state()
    fixture = load("fixture.json")
    st.setdefault("librechat", {})
    lc = httpx.Client(base_url=args.librechat_url, headers={"User-Agent": BROWSER_UA}, timeout=120)
    for person in fixture["people"]:
        name = person["name"]
        email, password = f"{name}@example.com", secrets.token_hex(12)
        r = lc.post(
            "/api/auth/register",
            json={
                "name": name,
                "email": email,
                "username": name,
                "password": password,
                "confirm_password": password,
            },
        )
        if r.status_code != 200:
            sys.exit(f"register {name}: {r.status_code} {r.text[:200]}")
        auth = {"Authorization": f"Bearer {lc_login(lc, email, password)}"}
        # The person's own Loom token, kept by LibreChat as this user's customUserVars value.
        r = lc.post(
            "/api/user/plugins",
            headers=auth,
            json={
                "pluginKey": f"mcp_{MCP_SERVER}",
                "action": "install",
                "auth": {"LOOM_TOKEN": st["tokens"][name]},
            },
        )
        r.raise_for_status()
        lc.post(f"/api/mcp/{MCP_SERVER}/reinitialize", headers=auth).raise_for_status()
        tools = lc.get("/api/mcp/tools", headers=auth).json()["servers"][MCP_SERVER]["tools"]
        r = lc.post(
            "/api/agents",
            headers=auth,
            json={
                "name": "Loom assistant",
                "provider": "anthropic",
                "model": args.model,
                "model_parameters": {"thinking": False},
                "instructions": INSTRUCTIONS.format(
                    org=person["org"], namespace=person["namespace"]
                ),
                "tools": [t["pluginKey"] for t in tools],
            },
        )
        r.raise_for_status()
        agent_id = r.json()["id"]
        r = lc.post("/api/api-keys", headers=auth, json={"name": "qualify"})
        r.raise_for_status()
        st["librechat"][name] = {
            "email": email,
            "password": password,
            "agent_id": agent_id,
            "api_key": r.json()["key"],
            "tools": len(tools),
        }
        print(f"{name}: agent {agent_id} with {len(tools)} Loom tools")
    st["librechat_url"] = args.librechat_url
    st["model"] = args.model
    save_state(st)


def cmd_setup_bridge(args: argparse.Namespace) -> None:
    """Grant each person `viewer` on their namespace, as a deployment's people hold it:
    the bridge delegates each answer's token from the person's, which needs a live
    grant. The cases then go to Loom's assistant route with each person's own token."""
    st = state()
    people = load("fixture.json")["people"]
    for org in sorted({p["org"] for p in people}):
        namespaces = sorted({p["namespace"] for p in people if p["org"] == org})
        admin = mint(
            args.loom_bin,
            subject=f"qualify-admin-{org}",
            org=org,
            namespace=namespaces[0],
            role="admin",
            extra_namespaces=namespaces[1:],
        )["token"]
        for person in (p for p in people if p["org"] == org):
            body = {
                "org": org,
                "namespace": person["namespace"],
                "subject": person["name"],
                "role": "viewer",
            }
            code, out, _ = loom_call(st["loom_url"], admin, "grants.create", body)
            if code != 200:
                sys.exit(f"grants.create {person['name']}: {code} {out}")
    st["bridge"] = True
    st["model"] = args.model
    save_state(st)
    print(f"granted {len(people)} people; the cases go through {st['loom_url']}")


# ---------------------------------------------------------------- one answer


def ask_librechat(
    st: dict[str, Any], person: str, question: str, *, cancel_after_first: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": question}],
    }
    if st.get("bridge"):
        # Loom's assistant route, as the person: Loom runs its seeded agent.
        ns = next(p["namespace"] for p in load("fixture.json")["people"] if p["name"] == person)
        url = f"{st['loom_url']}/v1/assistant/chat/completions"
        auth = {"Authorization": f"Bearer {st['tokens'][person]}", "X-Loom-Namespace": ns}
    else:
        lc = st["librechat"][person]
        body["model"] = lc["agent_id"]
        url = f"{st['librechat_url']}/api/agents/v1/chat/completions"
        auth = {"Authorization": f"Bearer {lc['api_key']}"}
    started = time.perf_counter()
    first_delta = first_text = None
    text: list[str] = []
    usage: dict[str, Any] = {}
    error = ""
    # A compressed stream reaches the client only once the answer is complete.
    with httpx.stream(
        "POST", url, json=body, headers={**auth, "Accept-Encoding": "identity"}, timeout=180
    ) as r:
        if r.status_code != 200:
            error = f"HTTP {r.status_code}: {r.read().decode()[:300]}"
        else:
            for line in r.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[6:])
                if chunk.get("error"):
                    error = json.dumps(chunk["error"])[:300]
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    now = time.perf_counter() - started
                    if (delta.get("content") or delta.get("reasoning")) and first_delta is None:
                        first_delta = now
                    if delta.get("content"):
                        if first_text is None:
                            first_text = now
                        text.append(delta["content"])
                if cancel_after_first and first_delta is not None:
                    break
    return {
        "answer": "".join(text),
        "error": error,
        "latency_s": time.perf_counter() - started,
        "ttft_s": first_delta,
        "first_text_s": first_text,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def ch_auth() -> tuple[str, str] | None:
    """ClickHouse credentials from the environment, never from argv or a URL."""
    user = os.environ.get("QUALIFY_CH_USER")
    return (user, os.environ.get("QUALIFY_CH_PASSWORD", "")) if user else None


def loom_calls(ch_url: str, subject: str, since: float, until: float) -> list[dict[str, Any]]:
    """The MCP calls Loom ledgered for this person between two instants, in Loom's database
    (`QUALIFY_CH_DATABASE`, the server's `LOOM_CH_DATABASE`; `loom` by default)."""
    sql = (
        "SELECT operation, success, error_code, consumer FROM {db:Identifier}.agent_events "
        "WHERE subject = {s:String} AND transport = 'mcp' "
        "AND ts >= fromUnixTimestamp64Milli({a:Int64}) "
        "AND ts <= fromUnixTimestamp64Milli({b:Int64}) "
        "ORDER BY ts FORMAT JSONEachRow"
    )
    params = {
        "param_db": os.environ.get("QUALIFY_CH_DATABASE", "loom"),
        "param_s": subject,
        "param_a": int(since * 1000),
        "param_b": int(until * 1000),
    }
    r = httpx.post(ch_url, params={**params, "query": sql}, auth=ch_auth(), timeout=30)
    r.raise_for_status()
    return [json.loads(line) for line in r.text.splitlines() if line]


# ---------------------------------------------------------------- scoring


def matches(patterns: tuple[str, ...] | list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def check_citations(
    st: dict[str, Any], person: dict[str, Any], text: str
) -> tuple[list[str], list[str]]:
    """Every memory key the answer names, and those that do not exist in the person's namespace
    or name a version other than the row's current one."""
    ns = person["namespace"]
    scorer = st["tokens"][f"scorer:{ns}"]
    # memory.get over HTTP reads one writer's row; the fixture says who wrote each key.
    writers = {
        key: writer
        for tenant in load("fixture.json")["tenants"]
        if tenant["namespace"] == ns
        for writer, rows in tenant["writers"].items()
        for key, _ in rows
    }
    cited: list[str] = []
    invalid: list[str] = []
    current: dict[str, int | None] = {}  # each key's current version, read once
    found = list(KEY_RE.finditer(text))
    for i, m in enumerate(found):
        key = m.group(0)
        if key not in current:
            cited.append(key)
            body = {
                "org": person["org"],
                "namespace": ns,
                "memory_key": key,
                "agent": writers.get(key, "-"),
            }
            code, out, _ = loom_call(st["loom_url"], scorer, "memory.get", body)
            ok = code == 200 and out.get("found", True) and out.get("version")
            current[key] = int(out["version"]) if ok else None
            if current[key] is None:
                invalid.append(key)
        if current[key] is None:
            continue
        # Every mention is checked: a version belongs to the key before it, up to the next key.
        end = min(m.end() + 80, found[i + 1].start() if i + 1 < len(found) else len(text))
        # A bare version, or the deployed agent's `[memory:<key>@v<version>]`.
        near = re.search(r"(?:\b|@v)(\d{16,20})\b", text[m.end() : end])
        if near and int(near.group(1)) != current[key]:
            stale = f"{key}@{near.group(1)}"
            if stale not in invalid:
                invalid.append(stale)
    return cited, invalid


def parse_doc_citations(text: str) -> list[dict[str, Any]]:
    """The document citations an answer writes, in order: each a source_uri, a section
    breadcrumb ('' when none is named) and a revision (None when none is named)."""
    out: list[dict[str, Any]] = []
    for m in DOC_CITE_RE.finditer(text):
        revision = (m.group("revision") or "").strip()
        out.append(
            {
                "source_uri": m.group("uri"),
                "section": (m.group("section") or "").strip(),
                "revision": int(revision) if revision.isdigit() else None,
            }
        )
    return out


def section_key(section: str) -> tuple[str, ...]:
    """A breadcrumb compared case- and spacing-blind: `A > B` and `a >b` are one section."""
    return tuple(" ".join(p.split()).casefold() for p in section.split(">") if p.strip())


def doc_citation_label(c: dict[str, Any]) -> str:
    label = c["source_uri"] + (f" § {c['section']}" if c["section"] else "")
    return label + (f" @r{c['revision']}" if c["revision"] is not None else "")


def retry_after(out: dict[str, Any]) -> float:
    """The seconds a 429 asks to wait: `detail.retry_after_s` from the rate limiter and
    the admission slots; 1 for a body without one (a detail that is a string, a plain-text
    answer from a proxy)."""
    detail = out.get("detail")
    return float(detail.get("retry_after_s", 1)) if isinstance(detail, dict) else 1.0


def scorer_read(st: dict[str, Any], token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    """One read the scorer makes, waited out when rate-limited: a check that could not read
    the document says nothing about the answer, so any other failure stops the run."""
    for _ in range(10):
        code, out, _ = loom_call(st["loom_url"], token, op, body)
        if code != 429:
            break
        time.sleep(retry_after(out))
    if code != 200:
        sys.exit(f"scorer {op} {body.get('source_uri')}: {code} {out}")
    return out


def check_doc_citations(
    st: dict[str, Any], person: dict[str, Any], case: dict[str, Any], text: str
) -> tuple[list[str], list[str], bool]:
    """Every document citation the answer writes, those that are not valid, and whether the
    labeled section is among them. Valid: the document is live in the person's namespace,
    a named section is one of its headings, a named revision is its live one, and the
    cited text (the section with its subsections, else the whole document) holds one of
    the case's labeled values. Read with the scorer's viewer token, no model."""
    body = {"org": person["org"], "namespace": person["namespace"]}
    scorer = st["tokens"][f"scorer:{person['namespace']}"]
    cited: list[str] = []
    invalid: list[str] = []
    expected = False
    label = case.get("cite_doc")
    for c in parse_doc_citations(text):
        name = doc_citation_label(c)
        if name in cited:
            continue
        cited.append(name)
        doc = scorer_read(st, scorer, "knowledge.get", {**body, "source_uri": c["source_uri"]})
        if not doc.get("found"):
            invalid.append(f"{name}: no such document")
            continue
        if c["revision"] is not None and c["revision"] != doc.get("revision"):
            invalid.append(f"{name}: live revision is {doc.get('revision')}")
            continue
        span = doc.get("content") or ""
        if c["section"]:
            outline = scorer_read(
                st, scorer, "knowledge.outline", {**body, "source_uri": c["source_uri"]}
            )
            # Every heading, a parent with no text of its own under it included.
            paths = {
                section_key(" > ".join(e["path"][:depth])): e["path"][:depth]
                for e in outline.get("sections", [])
                for depth in range(1, len(e["path"]) + 1)
            }
            path = paths.get(section_key(c["section"]))
            if path is None:
                invalid.append(f"{name}: no such section")
                continue
            read = scorer_read(
                st,
                scorer,
                "knowledge.outline",
                {**body, "source_uri": c["source_uri"], "section_path": path},
            )
            span = read.get("section_content") or ""
        if case.get("expect") and not any(
            re.search(p, span, re.IGNORECASE) for p in case["expect"]
        ):
            invalid.append(f"{name}: does not state the answer")
            continue
        if label and c["source_uri"] == label["source_uri"]:
            expected = expected or section_key(c["section"]) == section_key(label["section"])
    return cited, invalid, expected


def score(case: dict[str, Any], text: str) -> dict[str, Any]:
    """Score a written answer: the labeled values present, no forbidden text."""
    kind = case["kind"]
    abstained = matches(ABSTAIN_PATTERNS, text) or (
        is_document_case(case) and matches(DOC_ABSTAIN_PATTERNS, text)
    )
    if kind == "abstain":
        correct = abstained
    elif kind == "refusal":
        correct = matches(REFUSAL_PATTERNS, text)
    else:
        correct = all(re.search(p, text, re.IGNORECASE) for p in case["expect"])
    correct = correct and not matches(case.get("forbid", []), text)
    return {"correct": correct, "abstained": abstained}


# ---------------------------------------------------------------- run


# The arguments the assistant toolset forces on the reads the cases make, whatever the model
# sends (ASSISTANT_FORCED_ARGS in server/operations/toolsets.py): the direct path reads the
# same way.
ASSISTANT_FORCED_ARGS: dict[str, dict[str, object]] = {
    "memory.search": {"auto_touch": False, "explain": False},
}


def baseline(st: dict[str, Any], case: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
    """The same case through one read over HTTP, no model."""
    spec = case["baseline"]
    body = {
        "org": person["org"],
        "namespace": person["namespace"],
        **spec["args"],
        **ASSISTANT_FORCED_ARGS.get(spec["op"], {}),
    }
    code, out, elapsed = loom_call(st["loom_url"], st["tokens"][person["name"]], spec["op"], body)
    return {**native_score(case, code, out), "latency_s": elapsed, "ttft_s": elapsed}


def native_score(case: dict[str, Any], code: int, out: dict[str, Any]) -> dict[str, Any]:
    """Score the op's structured output. It has no prose to read, so abstention is the op's
    own `abstain` flag and a count is compared as a number, not matched as text."""
    spec = case["baseline"]
    cited: list[str] = []
    abstained = False
    if code != 200:
        text = f"{code} {out.get('detail', out)}"
        correct = case["kind"] == "refusal"
    elif spec["op"] == "memory.search":
        hits = out.get("results") or []
        abstained = bool(out.get("abstain")) or not hits
        if abstained:
            text = "abstained"
        else:
            text = f"{hits[0].get('content_excerpt', '')} [{hits[0]['memory_key']}]"
            cited = [hits[0]["memory_key"]]
        correct = abstained if case["kind"] == "abstain" else score(case, text)["correct"]
        correct = correct and case["kind"] != "refusal"
    elif spec["op"] == "knowledge.search":
        return native_document_score(case, out)
    elif "findings" in spec:
        # A recommendation's direct path: the audit names each labeled finding on rows.
        found = {f["code"] for f in out.get("findings", []) if f.get("count")}
        text = json.dumps(sorted(found))
        correct = set(spec["findings"]) <= found
    else:
        value: Any = out
        for step in spec.get("path", []):
            value = value[step]
        if spec.get("count"):
            value = len(value)
        text = json.dumps(value)
        if "min_chars" in spec:
            # A row the guide calls a document: past ~2 KB of text.
            correct = len(str(value)) >= spec["min_chars"]
        else:
            correct = value == spec["equals"]
    return {
        "correct": correct,
        "abstained": abstained,
        "answer": text[:300],
        "tool_calls": 1,
        "cited": cited,
        "invalid_citations": [],
        "cited_expected": case.get("cite") in cited if case.get("cite") else None,
    }


def native_document_score(case: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """A knowledge.search answer: the top hit as the answer and its citation, and the rank
    of the labeled section among the top `k` hits (None when it is not among them). The
    op returns the nearest passages and has no abstain signal, so it abstains only when
    nothing is found at all."""
    hits = out.get("results") or []
    ranked = [(h["source_uri"], section_key(h.get("section", ""))) for h in hits]
    label = case.get("cite_doc")
    rank = None
    if label:
        want = (label["source_uri"], section_key(label["section"]))
        rank = ranked.index(want) + 1 if want in ranked else None
    abstained = not hits
    if abstained:
        text, cited = "abstained", []
        correct = case["kind"] == "abstain"
    else:
        top = hits[0]
        cited = [top["source_uri"] + (f" § {top['section']}" if top.get("section") else "")]
        text = f"{top.get('content', '')} [{cited[0]}]"
        correct = case["kind"] != "abstain" and score(case, top.get("content", ""))["correct"]
    return {
        "correct": correct,
        "abstained": abstained,
        "answer": text[:300],
        "tool_calls": 1,
        "cited": cited,
        "invalid_citations": [],
        "cited_expected": rank == 1 if label else None,
        "rank": rank,
        "k": case["baseline"]["args"].get("top_k", 5),
    }


def cmd_run(args: argparse.Namespace) -> None:
    st = state()
    people = {p["name"]: p for p in load("fixture.json")["people"]}
    if args.native_only:
        run_native_only(st, people, args)
        return
    rows = []
    for case in load("cases.json"):
        if args.only and case["id"] not in args.only:
            continue
        person = people[case["person"]]
        native = baseline(st, case, person)
        t0 = time.time()
        got = ask_librechat(st, case["person"], case["question"])
        t1 = time.time() + 2
        time.sleep(2)  # the ledger write is asynchronous
        calls = loom_calls(args.ch_url, case["person"], t0, t1)
        text = got["answer"] or got["error"]
        result = score(case, text)
        rows.append(
            {
                "id": case["id"],
                "kind": case["kind"],
                "documents": is_document_case(case),
                "librechat": {
                    **got,
                    **result,
                    "tool_calls": len(calls),
                    "tool_ops": [
                        c["operation"] + ("" if c["success"] else f"!{c['error_code']}")
                        for c in calls
                    ],
                    "consumers": sorted({c["consumer"] for c in calls}),
                    **answer_citations(st, case, person, text),
                },
                "native": native,
            }
        )
        lc = rows[-1]["librechat"]
        print(
            f"{case['id']:<28} librechat {'ok ' if lc['correct'] else 'BAD'} "
            f"{lc['latency_s']:5.1f}s tools={lc['tool_calls']} | "
            f"native {'ok ' if native['correct'] else 'BAD'} {native['latency_s']:.2f}s"
        )
    if args.cancel_probe:
        rows.append(cancel_probe(st, args))
    Path(args.out).write_text(json.dumps({"model": st.get("model"), "rows": rows}, indent=2))
    print(report(rows, st.get("model", "")))


def answer_citations(
    st: dict[str, Any], case: dict[str, Any], person: dict[str, Any], text: str
) -> dict[str, Any]:
    """The memory keys and document passages an answer cites, the invalid ones, and whether
    the labeled key or section is among them (None when the case labels neither)."""
    cited, invalid = check_citations(st, person, text)
    expected = case.get("cite") in cited if case.get("cite") else None
    if is_document_case(case):
        doc_cited, doc_invalid, doc_expected = check_doc_citations(st, person, case, text)
        cited, invalid = cited + doc_cited, invalid + doc_invalid
        expected = doc_expected if case.get("cite_doc") else None
    return {"cited": cited, "invalid_citations": invalid, "cited_expected": expected}


def is_document_case(case: dict[str, Any]) -> bool:
    """A question over the namespace's documents: its direct path is knowledge.search."""
    return case["baseline"]["op"] == "knowledge.search"


def run_native_only(st: dict[str, Any], people: dict[str, Any], args: argparse.Namespace) -> None:
    """Answer every case on the direct path again, with no model and no LibreChat, and
    replace the native results of a saved run with them."""
    out = Path(args.out)
    if not out.exists():
        sys.exit(f"{out} is missing: --native-only replaces the native side of a saved run")
    saved = json.loads(out.read_text())
    rows = {r["id"]: r for r in saved["rows"]}
    for case in load("cases.json"):
        if case["id"] in rows and (not args.only or case["id"] in args.only):
            rows[case["id"]]["native"] = baseline(st, case, people[case["person"]])
            native = rows[case["id"]]["native"]
            print(
                f"{case['id']:<28} native {'ok ' if native['correct'] else 'BAD'} "
                f"{native['latency_s']:.2f}s"
            )
    out.write_text(json.dumps(saved, indent=2))
    print(report(saved["rows"], saved.get("model", "")))


def cmd_rescore(args: argparse.Namespace) -> None:
    """Score a saved run's LibreChat answers again against the current labels, with no
    model. Timings and the native results are kept as recorded; with --citations, the
    citations are checked again against the seeded Loom server."""
    saved = json.loads(Path(args.out).read_text())
    cases = {c["id"]: c for c in load("cases.json")}
    people = {p["name"]: p for p in load("fixture.json")["people"]}
    st = state() if args.citations else {}
    for row in saved["rows"]:
        if row["kind"] != "cancel":
            lc = row["librechat"]
            case = cases[row["id"]]
            text = lc["answer"] or lc["error"]
            lc.update(score(case, text))
            if args.citations:
                lc.update(answer_citations(st, case, people[case["person"]], text))
    Path(args.out).write_text(json.dumps(saved, indent=2))
    print(report(saved["rows"], saved.get("model", "")))


def cancel_probe(st: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """Disconnect after the first streamed token and count the Loom calls that still arrive."""
    t0 = time.time()
    got = ask_librechat(
        st, "alice", "List every memory in the namespace with its version.", cancel_after_first=True
    )
    disconnected = time.time()
    time.sleep(args.cancel_wait)
    calls = loom_calls(args.ch_url, "alice", t0, time.time())
    after = loom_calls(args.ch_url, "alice", disconnected, time.time())
    print(f"cancel probe: {len(calls)} Loom calls in total, {len(after)} after the disconnect")
    return {
        "id": "cancel-probe",
        "kind": "cancel",
        "ttft_s": got["ttft_s"],
        "loom_calls": len(calls),
        "loom_calls_after_disconnect": len(after),
    }


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))]


def fmt_s(seconds: float | None, digits: int = 2, unit: str = "") -> str:
    return "-" if seconds is None else f"{seconds:.{digits}f}{unit}"


def report(rows: list[dict[str, Any]], model: str) -> str:
    cases = [r for r in rows if r["kind"] != "cancel"]
    lines = [
        "| case | kind | LibreChat correct | cited (invalid) | tool calls | latency s | TTFT s "
        "| native correct | native s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in cases:
        lc, nv = r["librechat"], r["native"]
        ttft = fmt_s(lc["ttft_s"], 1)
        lines.append(
            f"| {r['id']} | {r['kind']} | {'yes' if lc['correct'] else 'no'} | {len(lc['cited'])} "
            f"({len(lc['invalid_citations'])}) | {lc['tool_calls']} | {lc['latency_s']:.1f} | "
            f"{ttft} | "
            f"{'yes' if nv['correct'] else 'no'} | {nv['latency_s']:.2f} |"
        )
    # The document cases are reported on their own lines (document_report).
    others = [r for r in cases if not r.get("documents")]
    if others:
        for side in ("librechat", "native"):
            got = [r[side] for r in others]
            lat = [g["latency_s"] for g in got]
            ttft = [g["ttft_s"] for g in got if g["ttft_s"] is not None]
            cited = sum(len(g["cited"]) for g in got)
            invalid = sum(len(g["invalid_citations"]) for g in got)
            # The direct path cites the search hit itself, so its citations are not checked.
            validity = "-" if side == "native" else f"{cited - invalid}/{cited} valid"
            expected = [g["cited_expected"] for g in got if g["cited_expected"] is not None]
            abst = [r for r in others if r["kind"] == "abstain"]
            false_abst = [
                r
                for r in others
                if r["kind"] not in ("abstain", "refusal") and r[side]["abstained"]
            ]
            lines.append(
                f"\n{side}: correct {sum(g['correct'] for g in got)}/{len(got)}; "
                f"citations {validity}, "
                f"expected key cited {sum(expected)}/{len(expected)}; "
                f"abstained {sum(r[side]['correct'] for r in abst)}/{len(abst)} where it should, "
                f"{len(false_abst)} false abstentions; "
                f"latency p50 {pct(lat, 0.5):.2f}s p95 {pct(lat, 0.95):.2f}s; "
                f"TTFT p50 {pct(ttft, 0.5):.2f}s p95 {pct(ttft, 0.95):.2f}s; "
                f"tool calls {sum(g['tool_calls'] for g in got)}"
            )
    lines.extend(document_report(cases))
    price_in, price_out = PRICE_PER_MTOK.get(model, (float("nan"), float("nan")))
    costs = [
        (
            r["librechat"]["prompt_tokens"] * price_in
            + r["librechat"]["completion_tokens"] * price_out
        )
        / 1e6
        for r in cases
    ]
    prompt = sum(r["librechat"]["prompt_tokens"] for r in cases)
    completion = sum(r["librechat"]["completion_tokens"] for r in cases)
    lines.append(
        f"\nmodel {model}: {prompt} prompt + {completion} completion tokens, "
        f"${sum(costs):.3f} at list price counting every prompt token; per answer "
        f"mean ${sum(costs) / max(len(costs), 1):.3f}, max ${max(costs, default=0):.3f}"
    )
    for r in rows:
        if r["kind"] == "cancel":
            lines.append(
                f"cancel probe: first token {fmt_s(r['ttft_s'], unit=' s')}, "
                f"{r['loom_calls']} Loom calls, "
                f"{r['loom_calls_after_disconnect']} after the client disconnected"
            )
    return "\n".join(lines)


def document_report(cases: list[dict[str, Any]]) -> list[str]:
    """The document cases on their own: recall of the labeled section among the direct
    path's hits, citation validity, the labeled section cited, and abstention."""
    docs = [r for r in cases if r.get("documents")]
    if not docs:
        return []
    answerable = [r for r in docs if r["kind"] != "abstain"]
    unanswerable = [r for r in docs if r["kind"] == "abstain"]
    ranks = [r["native"].get("rank") for r in answerable]
    k = max((r["native"].get("k", 0) for r in answerable), default=0)
    lines = [
        f"\ndocuments: {len(answerable)} answerable and {len(unanswerable)} unanswerable cases; "
        f"labeled section at rank 1 {sum(rank == 1 for rank in ranks)}/{len(ranks)}, "
        f"within the top {k} {sum(rank is not None for rank in ranks)}/{len(ranks)} "
        "(the direct path's knowledge.search hits)"
    ]
    for side in ("librechat", "native"):
        got = [r[side] for r in docs]
        cited = sum(len(g["cited"]) for g in got)
        invalid = sum(len(g["invalid_citations"]) for g in got)
        validity = "-" if side == "native" else f"{cited - invalid}/{cited} valid"
        if side == "librechat":
            revisions = sum(" @r" in c for g in got for c in g["cited"])
            validity += f", {revisions} naming a revision"
        expected = [r[side]["cited_expected"] for r in answerable]
        false_abst = sum(r[side]["abstained"] for r in answerable)
        lat = [g["latency_s"] for g in got]
        lines.append(
            f"{side} documents: correct {sum(r[side]['correct'] for r in answerable)}/"
            f"{len(answerable)}; citations {validity}, labeled section cited "
            f"{sum(bool(e) for e in expected)}/{len(expected)}; abstained "
            f"{sum(r[side]['correct'] for r in unanswerable)}/{len(unanswerable)} where it "
            f"should, {false_abst} false abstentions; latency p50 {pct(lat, 0.5):.2f}s "
            f"p95 {pct(lat, 0.95):.2f}s; tool calls {sum(g['tool_calls'] for g in got)}"
        )
    return lines


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--loom-url",
        default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"),
        help="the Loom server; seed stores it and the later steps use the stored one",
    )
    p.add_argument(
        "--loom-bin",
        default="loom",
        help="the loom CLI that mints and revokes the fixture's tokens",
    )
    p.add_argument("--librechat-url", default="http://localhost:3080")
    p.add_argument("--model", default="claude-haiku-4-5-20251001")
    p.add_argument(
        "--ch-url",
        default=os.environ.get("QUALIFY_CH_URL", "http://localhost:8123/"),
        help="the ClickHouse behind the Loom server, to count each answer's tool calls; "
        "credentials come from QUALIFY_CH_USER and QUALIFY_CH_PASSWORD, the database from "
        "QUALIFY_CH_DATABASE (default loom)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="write the fixture's memories and documents and mint its tokens")
    sub.add_parser(
        "setup", help="register the fixture's people in LibreChat with their own Loom tokens"
    )
    sub.add_parser(
        "setup-bridge",
        help="grant the fixture's people their namespaces; the cases then go through Loom's "
        "assistant route",
    )
    revoke = sub.add_parser("revoke", help="revoke the tokens the fixture marks revoked")
    revoke.add_argument(
        "--settle",
        type=float,
        default=30.0,
        help="seconds to wait afterwards: the server's LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS",
    )
    run = sub.add_parser(
        "run", help="answer every case through LibreChat and through the Loom reads"
    )
    run.add_argument("--out", default=str(HERE / "results.json"))
    run.add_argument("--only", nargs="*", help="case ids to run")
    run.add_argument("--cancel-probe", action="store_true", help="also disconnect one answer early")
    run.add_argument("--cancel-wait", type=float, default=20.0)
    run.add_argument(
        "--native-only",
        action="store_true",
        help="answer on the direct path only and replace the native side of the saved --out",
    )
    rescore = sub.add_parser("rescore", help="score a saved run again against cases.json")
    rescore.add_argument("--out", default=str(HERE / "results.json"))
    rescore.add_argument(
        "--citations",
        action="store_true",
        help="also check every citation again against the seeded Loom server",
    )
    args = p.parse_args()
    commands = {
        "seed": cmd_seed,
        "setup": cmd_setup,
        "setup-bridge": cmd_setup_bridge,
        "revoke": cmd_revoke,
        "run": cmd_run,
        "rescore": cmd_rescore,
    }
    commands[args.cmd](args)


if __name__ == "__main__":
    main()
