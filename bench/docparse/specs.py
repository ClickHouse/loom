"""Document specs: the structure every rendering of a corpus file is made from,
so its gold labels are the spec itself.

A spec is a dict: title, kind ("doc" or "deck"), and elements. A doc's elements:
  ("h", level, text), ("p", text), ("ul", items), ("ol", items),
  ("code", text), ("table", rows, style), ("break",)
where an item is text or (text, [sub items]) and rows[0] is the header.
A deck's elements are slides: {"title", "bullets", "table", "notes", "labels", "image"}.
"""

from __future__ import annotations

import html
import random
import re
from html.parser import HTMLParser
from pathlib import Path

SUBJECTS = [
    "The ingest worker",
    "The compaction job",
    "Each replica",
    "The scheduler",
    "The query router",
    "The retention sweeper",
    "The token service",
    "The export pipeline",
    "Every shard leader",
    "The audit writer",
    "The cache layer",
    "The migration runner",
    "The webhook relay",
    "The billing aggregator",
    "The search indexer",
    "The alert evaluator",
    "The backup agent",
    "The rate limiter",
    "The schema registry",
    "The feature flag service",
]
VERBS = [
    "retries",
    "batches",
    "rejects",
    "deduplicates",
    "replicates",
    "throttles",
    "persists",
    "compacts",
    "validates",
    "partitions",
    "rotates",
    "streams",
    "buffers",
    "signs",
    "archives",
]
OBJECTS = [
    "incoming events",
    "stale partitions",
    "expired sessions",
    "malformed payloads",
    "pending writes",
    "cold segments",
    "audit records",
    "tenant quotas",
    "replication logs",
    "dead letters",
    "hourly rollups",
    "access tokens",
    "delta snapshots",
    "index granules",
    "outbound webhooks",
]
TAILS = [
    "before the nightly window closes",
    "when the queue depth passes its threshold",
    "so that a restart loses no acknowledged work",
    "under the tenant's own quota",
    "after the leader confirms the lease",
    "without holding the global lock",
    "unless an operator pauses the rollout",
    "in the order the producer wrote them",
    "while the previous build still serves reads",
    "once every replica reports healthy",
    "so a failed region drains within minutes",
    "with a jittered exponential backoff",
]
NAMES = [
    "orion",
    "lattice",
    "kestrel",
    "basalt",
    "meridian",
    "quill",
    "cobalt",
    "tundra",
    "harbor",
    "saffron",
    "vector",
    "juniper",
    "atlas",
    "ember",
    "nimbus",
    "garnet",
]
SECTIONS = [
    "Background",
    "Goals",
    "Non-goals",
    "Proposed design",
    "Data model",
    "API changes",
    "Failure handling",
    "Rollout plan",
    "Observability",
    "Security considerations",
    "Alternatives considered",
    "Open questions",
    "Capacity estimates",
    "Migration",
    "Testing strategy",
    "Operational runbook",
    "Dependencies",
    "Timeline",
]
SUBSECTIONS = [
    "Write path",
    "Read path",
    "Storage layout",
    "Backfill",
    "Limits",
    "Error budget",
    "Dashboards",
    "Paging policy",
    "Key rotation",
    "Compatibility",
    "Cutover",
    "Cleanup",
]


class Words:
    """A seeded source of distinct, plausible design-doc text."""

    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)
        self.n = 0

    def sentence(self) -> str:
        self.n += 1
        r = self.r
        num = r.choice([r.randint(2, 90), r.randint(100, 9000)])
        unit = r.choice(
            ["seconds", "minutes", "requests per second", "megabytes", "percent", "hours"]
        )
        name = r.choice(NAMES)
        forms = [
            f"{r.choice(SUBJECTS)} {r.choice(VERBS)} {r.choice(OBJECTS)} {r.choice(TAILS)}.",
            f"In the {name} cluster, {r.choice(SUBJECTS).lower()} {r.choice(VERBS)} {r.choice(OBJECTS)} every {num} {unit}.",
            f"We measured {num} {unit} for {r.choice(OBJECTS)} on the {name} staging tier.",
            f"{r.choice(SUBJECTS)} never {r.choice(VERBS).rstrip('s')}s {r.choice(OBJECTS)} {r.choice(TAILS)}.",
            f"Option {name} keeps {r.choice(OBJECTS)} for {num} {unit} and {r.choice(VERBS)} the rest.",
        ]
        return r.choice(forms)

    def paragraph(self, lo: int = 2, hi: int = 5) -> str:
        return " ".join(self.sentence() for _ in range(self.r.randint(lo, hi)))

    def phrase(self) -> str:
        r = self.r
        return f"{r.choice(VERBS).capitalize()} {r.choice(OBJECTS)} {r.choice(TAILS)}"

    def items(self, n: int, nested: bool = False) -> list:
        out: list = []
        for i in range(n):
            if nested and i == 1:
                out.append((self.phrase(), [self.phrase() for _ in range(2)]))
            else:
                out.append(self.phrase())
        return out

    def code(self) -> str:
        r = self.r
        kind = r.choice(["sql", "yaml", "shell"])
        name = r.choice(NAMES)
        if kind == "sql":
            return (
                f"CREATE TABLE {name}_events\n(\n    ts DateTime64(3),\n    tenant LowCardinality(String),\n"
                f"    payload String\n)\nENGINE = MergeTree\nORDER BY (tenant, ts)\nTTL ts + INTERVAL {r.randint(7, 90)} DAY;"
            )
        if kind == "yaml":
            return (
                f"{name}:\n  replicas: {r.randint(2, 9)}\n  queue_depth_limit: {r.randint(100, 5000)}\n"
                f"  retry:\n    max_attempts: {r.randint(3, 12)}\n    backoff_ms: {r.randint(50, 900)}"
            )
        return (
            f"kubectl -n {name} rollout status deploy/{name}-worker\n"
            f"kubectl -n {name} scale deploy/{name}-worker --replicas={r.randint(2, 12)}"
        )

    def table(self, rows: int, kind: str | None = None) -> list[list[str]]:
        r = self.r
        kind = kind or r.choice(["config", "metrics", "compare", "owners"])
        if kind == "config":
            head = ["Setting", "Default", "Description"]
            body = [
                [
                    f"{r.choice(NAMES)}_{r.choice(['timeout_s', 'max_rows', 'batch_size', 'ttl_days', 'replicas'])}_{i}",
                    str(r.randint(1, 5000)),
                    self.phrase(),
                ]
                for i in range(rows)
            ]
        elif kind == "metrics":
            head = ["Service", "p50 ms", "p99 ms", "Error rate"]
            body = [
                [
                    f"{r.choice(NAMES)}-{i}",
                    str(r.randint(2, 90)),
                    str(r.randint(100, 2500)),
                    f"{r.randint(0, 99) / 100:.2f}%",
                ]
                for i in range(rows)
            ]
        elif kind == "compare":
            head = ["Option", "Latency", "Cost", "Risk"]
            body = [
                [
                    f"Option {r.choice(NAMES)} {i}",
                    f"{r.randint(5, 400)} ms",
                    f"${r.randint(1, 90)}k / month",
                    r.choice(["low", "medium", "high"]),
                ]
                for i in range(rows)
            ]
        else:
            head = ["Component", "Owner", "On-call", "Tier"]
            body = [
                [
                    f"{r.choice(NAMES)}-{r.choice(['api', 'worker', 'store', 'proxy'])}-{i}",
                    r.choice(["platform", "storage", "identity", "billing", "search"]),
                    f"rota-{r.randint(1, 9)}",
                    str(r.randint(0, 3)),
                ]
                for i in range(rows)
            ]
        return [head, *body]


def design_doc(
    seed: int,
    *,
    sections: int = 6,
    numbered: bool = False,
    tables: int = 1,
    table_rows: int = 5,
    table_style: str = "grid",
    code: bool = True,
) -> dict:
    w = Words(seed)
    r = w.r
    name = r.choice(NAMES)
    title = (
        f"{name.capitalize()} {r.choice(['design doc', 'RFC', 'specification', 'ADR', 'runbook'])}"
    )
    els: list = [("h", 1, title), ("p", w.paragraph(2, 3))]
    names = r.sample(SECTIONS, sections)
    table_at = set(r.sample(range(sections), min(tables, sections)))
    for i, sec in enumerate(names, start=1):
        els.append(("h", 2, f"{i}. {sec}" if numbered else sec))
        els.append(("p", w.paragraph()))
        if r.random() < 0.5:
            sub = r.choice(SUBSECTIONS)
            els.append(("h", 3, f"{i}.1 {sub}" if numbered else sub))
            els.append(("p", w.paragraph(1, 3)))
        if r.random() < 0.5:
            els.append((r.choice(["ul", "ol"]), w.items(r.randint(2, 4), nested=r.random() < 0.4)))
        if code and r.random() < 0.3:
            els.append(("code", w.code()))
        if i - 1 in table_at:
            els.append(("p", w.sentence()))
            els.append(("table", w.table(table_rows), table_style))
        els.append(("p", w.paragraph(1, 3)))
    return {"title": title, "kind": "doc", "elements": els}


def deck(
    seed: int,
    *,
    slides: int = 6,
    notes: bool = True,
    table: bool = True,
    diagram: bool = True,
    image: bool = False,
) -> dict:
    w = Words(seed)
    r = w.r
    name = r.choice(NAMES)
    out: list[dict] = [
        {
            "title": f"{name.capitalize()} {r.choice(['architecture review', 'quarterly update', 'launch plan', 'incident review'])}",
            "bullets": [
                f"{r.choice(['Platform', 'Storage', 'Identity', 'Search'])} team, {r.choice(['first', 'second', 'third', 'fourth'])} quarter"
            ],
        }
    ]
    kinds = (
        ["bullets"] * max(1, slides - 4)
        + (["table"] if table else [])
        + (["diagram"] if diagram else [])
        + (["image"] if image else [])
        + ["bullets"]
    )
    for k in kinds[: slides - 1]:
        s: dict = {"title": r.choice(SECTIONS + SUBSECTIONS) + f" ({r.choice(NAMES)})"}
        if k == "bullets":
            s["bullets"] = [w.phrase() for _ in range(r.randint(2, 4))]
        elif k == "table":
            s["table"] = w.table(r.randint(3, 4), r.choice(["metrics", "compare"]))
        elif k == "diagram":
            s["labels"] = [
                f"{r.choice(NAMES)} {r.choice(['gateway', 'queue', 'store', 'worker', 'cache', 'router'])}"
                for _ in range(4)
            ]
        else:
            s["image"] = [
                f"{r.choice(NAMES)} {r.choice(['ingest', 'compact', 'serve'])} {r.randint(1, 9)}"
                for _ in range(3)
            ]
        if notes and r.random() < 0.7:
            s["notes"] = w.sentence()
        out.append(s)
    return {"title": out[0]["title"], "kind": "deck", "elements": out}


# ---- public-domain PEPs (peps.python.org: "placed in the public domain or under CC0-1.0")


class _PepParser(HTMLParser):
    BLOCK = {
        "h1",
        "h2",
        "h3",
        "h4",
        "p",
        "li",
        "pre",
        "td",
        "th",
        "tr",
        "table",
        "ul",
        "ol",
        "dl",
        "details",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inside = False
        self.depth_skip = 0
        self.stack: list[str] = []
        self.els: list = []
        self.buf: list[str] = []
        self.list_stack: list[list] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.in_pre = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and a.get("id") == "pep-content":
            self.inside = True
            return
        if not self.inside:
            return
        if (
            self.depth_skip
            or tag in ("dl", "details")
            or (tag == "section" and a.get("id") in ("contents", "copyright", "footnotes"))
        ):
            self.depth_skip += 1 if tag in ("dl", "details", "section") else 0
            return
        if tag in ("ul", "ol"):
            self._flush_text()
            self.list_stack.append([])
        elif tag == "li":
            self._flush_text()
        elif tag == "table":
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag == "pre":
            self.in_pre = True
            self.buf = []
        elif tag in ("h1", "h2", "h3", "h4", "p", "td", "th"):
            self._flush_text()
        elif tag == "br":
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if self.depth_skip:
            if tag in ("dl", "details", "section"):
                self.depth_skip -= 1
            return
        text = " ".join("".join(self.buf).split())
        if tag in ("h1", "h2", "h3", "h4"):
            text = text.replace("¶", "").strip()
            if text:
                self.els.append(("h", int(tag[1]), text))
            self.buf = []
        elif tag == "p":
            if self.row is not None or self.list_stack:
                self.buf.append(" ")
                return
            if text:
                self.els.append(("p", text))
            self.buf = []
        elif tag == "li":
            if text and self.list_stack:
                self.list_stack[-1].append(text)
            self.buf = []
        elif tag in ("ul", "ol"):
            items = self.list_stack.pop() if self.list_stack else []
            if self.list_stack and items:
                parent = self.list_stack[-1]
                if parent and isinstance(parent[-1], str):
                    parent[-1] = (parent[-1], items)
                else:
                    parent.extend(items)
            elif items:
                self.els.append((tag, items))
        elif tag == "pre":
            self.in_pre = False
            code = "".join(self.buf).strip("\n")
            if code.strip():
                self.els.append(("code", code))
            self.buf = []
        elif tag in ("td", "th"):
            if self.row is not None:
                self.row.append(text)
            self.buf = []
        elif tag == "tr":
            if self.table is not None and self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table":
            if self.table and len(self.table) > 1 and len({len(r) for r in self.table}) == 1:
                self.els.append(("table", self.table, "grid"))
            self.table = None

    def _flush_text(self) -> None:
        """Drop text between blocks that no block holds (rare in a PEP)."""
        if not self.in_pre:
            self.buf = []

    def handle_data(self, data):
        if self.inside and not self.depth_skip:
            self.buf.append(data)


def pep(path: Path, max_elements: int) -> dict:
    p = _PepParser()
    p.feed(path.read_text())
    els = [e for e in p.els if not (e[0] == "p" and len(e[1]) < 3)]
    # cut at an element boundary, never mid-table, and keep the first heading the title
    els = els[:max_elements]
    while els and els[-1][0] == "h":
        els.pop()
    title = els[0][2] if els and els[0][0] == "h" else path.stem
    return {"title": title, "kind": "doc", "elements": els}


def items_text(items: list) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for it in items:
        if isinstance(it, tuple):
            out.append((1, it[0]))
            out.extend((2, s) for s in it[1])
        else:
            out.append((1, it))
    return out


def esc(s: str) -> str:
    return html.escape(s, quote=False)
