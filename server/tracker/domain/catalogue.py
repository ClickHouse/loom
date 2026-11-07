"""The QA query catalogue: which tracker-source read
answers which question, decided from the question's anchors, not from
free-form SQL.

A tracker record can hold every quality signal of a product — issues, PRs
with diffs, core incidents, support escalations, Slack incident / on-call /
release threads, the cloud instance dump, release-blocker projects, and the
graph that links them. A client (a terminal session, a review agent)
asks loom in prose or hands it an artefact. This module turns that into a
**plan**: the anchors it recognised (numbers with their kind, a version, a
component, a path, a customer, a person, a free-text term, a time window)
and the ordered list of **templates** to run — each a named, bounded,
keyed statement with a fixed shape, an access class (`public`: issues,
PRs, snapshots, releases; `restricted`: incidents, escalations, customers,
Slack, instances), and the citations it yields. The answer always says
what was searched, so a reader can judge relevance and a bot can chain.

Everything here is text → dataclasses; the readers live in
`server/tracker/store/source/`, the executor in `server/tracker/app/router.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.settings import settings

PUBLIC = "public"
RESTRICTED = "restricted"
# The ceiling of a cell's open-issue read (the reader's
# `CELL_LIMIT` in `server/tracker/store/source/items.py`; the largest live cell is 236)
CELL_LIMIT = 300

# --- anchors -------------------------------------------------------------------

_NUM_RE = re.compile(r"(?<![\w.])#?(\d{3,7})(?![\d.]\d)\b")
_INC_RE = re.compile(r"(?i)\bINC[- ]?(\d{3,7})\b")
_VERSION_RE = re.compile(r"(?<![\d.])(\d{2}\.\d{1,2})(?![\d.]\d)")
_PATH_RE = re.compile(r"\b((?:src|tests|programs|base|utils|docs)/[A-Za-z0-9_./-]+)")
_HANDLE_RE = re.compile(r"@\s?([A-Za-z0-9][A-Za-z0-9-]{1,38})")
_QUOTED_RE = re.compile(r"[\"'“‘]([^\"'”’]{3,120})[\"'”’]")
_CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9][a-z]*[A-Z0-9][A-Za-z0-9]*)\b")
_CUSTOMER_RE = re.compile(
    r"(?i)\b(?:customer|org|organisation|organization|account|tenant)\s+(?:named\s+|called\s+)?"
    r"[\"'“]?([A-Z][A-Za-z0-9&_-]*(?:\s+[A-Z][A-Za-z0-9&_-]*){0,3})[\"'”]?"
)
_WINDOW_RE = re.compile(
    r"(?i)\b(?:last|past|previous)\s+(\d{1,3})\s*(day|days|week|weeks|month|months|year|years)\b"
    r"|\b(this week|this month|this year|last week|last month|last year|today|yesterday)\b"
)
_TEST_RE = re.compile(r"\b(\d{4,6}_[a-z][a-z0-9_]{5,}|test_[A-Za-z0-9_]{3,})\b")
# A severity spoken in the question ("open medium bugs in
# MergeTree") narrows the component's open-issue read to that cell of the board
_SEVERITY_RE = re.compile(
    r"(?i)\b(critical|major|medium|minor|low|unrated)(?:[- ]severity)?\b|\bseverity\b"
)

# words that shape the intent; each list is matched case-insensitively as words
_W = {
    "escalation": r"escalation|escalations|escalated|support case|support cases|case\b",
    "incident": (
        r"incident|incidents|crash(?:es|ed)?\b|core[- ]incident|production (?:issue|bug|crash)"
    ),
    "customer": (
        r"customer|customers|tenant|tenants|org\b|orgs\b|account|prioriti[sz]e|who uses"
        r"|who is using|which customers"
    ),
    "risk": r"risk|risky|production risk|blast radius|dangerous|fragile",
    "similar": (
        r"similar|existing issues|already (?:reported|known|exists?)|known (?:issue|bug)"
        r"|issues? (?:mentioning|about|on|for)|any issues?|duplicate"
    ),
    "release": r"release|blocker|blockers|blocking|ready to ship|shippable|readiness|ship\b",
    "changelog": r"changelog|release notes|what shipped|shipped in|what changed|what'?s new",
    "prs": (
        r"\bprs?\b|pull requests?|merged|changes? (?:to|in|touching|under)|changed (?:in|under)"
        r"|touching|commits?\b"
    ),
    "review": r"review(?:ed|s|ing|ers?)?\b",
    "week": r"this week|last week|weekly|my week|team'?s week|what did",
    "gaps": r"untested|test gap|gap ratio|without (?:a )?test|no regression test|test coverage",
    "flaky": r"flaky|flakiness|fails? randomly|unstable test",
    "instances": r"\binstances?\b|\bclusters?\b|services? running|running version|deployed version",
    "slack": r"\bslack\b|\bthreads?\b|\bon-?call\b",
    "backport": r"backport(?:ed|s)?",
    # The bug board — open issues by component and severity
    "board": (
        r"bug board|severity board|(?:kinds?|types?|categories) of (?:open )?(?:bugs?|issues?)"
        r"|(?:bugs?|issues?) by (?:component|severity)|by component and severity"
        r"|(?:bugs?|issues?) breakdown|breakdown of (?:open )?(?:bugs?|issues?)"
    ),
}
_WORDS = {k: re.compile(r"(?i)(?:" + v + r")") for k, v in _W.items()}
_WORDS_ANY = re.compile(r"(?i)(?:" + "|".join(_W.values()) + r")")

_COMPONENT_STOP = frozenset(
    {
        "ClickHouse",
        "GitHub",
        "PullRequest",
        "MergeTreeSettings",
        "SELECT",
        "INSERT",
        "Slack",
        "Loom",
        "QA",
        "PR",
        "PRs",
        "CI",
        "LOGICAL_ERROR",
    }
)
# component names that are ordinary words in a question about anything
_GENERIC_COMPONENT_WORDS = frozenset(
    {
        "core",
        "ops",
        "sql",
        "ci",
        "cli",
        "ddl",
        "ttl",
        "json",
        "settings",
        "functions",
        "formats",
        "logging",
        "documentation",
        "infrastructure",
        "databases",
        "aggregation",
        "compression",
        "replication",
        "distributed",
        "backup",
        "interpreter",
        "protocols",
        "optimizer",
        "mutations",
        "projections",
        "fuzzer",
        "datetime",
        "s3",
        "joins",
        "dictionary",
        "analyzer",
        "smt",
    }
)
_STOP_TERMS = frozenset(
    (
        "the a an and or of to in on for with about from by at as is are was were be "
        "been this that these those what which who whom whose when where why how do does "
        "did any all recent latest last past previous show list find give me my our "
        "their its top open closed new old issue issues bug bugs pr prs pull request "
        "requests incident incidents escalation escalations customer customers release "
        "version component area feature line code week month year day days weeks months "
        "years mention mentioning mentions related exists existing already known similar "
        "have has had there here risk risks production over across channels analytics "
        "provide summarize summary type get run use see can via per not now yet but was "
        "one two set way say ask let big low high many much more most some such than then "
        "too very will would could should may might must did done doing going want need "
        "escalate escalated escalating affected affecting about which report reports "
        "core test tests quarter after before since until during while entries entry notes "
    ).split()
)


@dataclass
class Anchors:
    numbers: list[tuple[int, str]] = field(default_factory=list)  # (number, kind hint)
    versions: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)
    customers: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)  # free-text tokens worth a text-index search
    quoted: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)  # The cell's severities
    days: int = 0  # 0 = the template's default window
    words: set[str] = field(default_factory=set)  # intent words present

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            if k == "words":
                continue
            if v:
                out[k] = sorted(v) if isinstance(v, set) else v
        return out


def _days_of(m: re.Match[str]) -> int:
    if m.group(1):
        n, unit = int(m.group(1)), m.group(2).lower()
        mult = (
            1
            if unit.startswith("day")
            else 7
            if unit.startswith("week")
            else 30
            if unit.startswith("month")
            else 365
        )
        return max(1, min(n * mult, 3650))
    phrase = (m.group(3) or "").lower()
    return {
        "today": 1,
        "yesterday": 2,
        "this week": 7,
        "last week": 14,
        "this month": 30,
        "last month": 60,
        "this year": 365,
        "last year": 730,
    }.get(phrase, 0)


def _severities_of(text: str) -> list[str]:
    """The severity names spoken, lower-case, in rank order, once each."""
    found = {m.group(1).lower() for m in _SEVERITY_RE.finditer(text) if m.group(1)}
    return sorted(found, key=_SEVERITY_ORDER.index)


_SEVERITY_ORDER = ("critical", "major", "medium", "minor", "low", "unrated")


_KIND_WORD_RE = re.compile(
    r"(?i)\b(?:(escalations?|cases?)|(incidents?)|(pull\s+requests?|prs?|merged?|merges)"
    r"|(issues?|bugs?))\b"
)
_KIND_GROUPS = ("escalation", "incident", "pr", "issue")


def _number_kind(text: str, start: int) -> str:
    """The kind a number is spoken of as: the nearest whole kind word before it
    ("problem" is no PR, and in "just in case, is issue 98500 …" it is an issue)."""
    words = list(_KIND_WORD_RE.finditer(text, max(0, start - 40), start))
    return _KIND_GROUPS[words[-1].lastindex - 1] if words and words[-1].lastindex else ""


def extract_anchors(question: str, *, known_components: tuple[str, ...] = ()) -> Anchors:
    q = question or ""
    a = Anchors()
    for m in _INC_RE.finditer(q):
        a.numbers.append((int(m.group(1)), "incident"))
    inc_spans = [m.span() for m in _INC_RE.finditer(q)]
    for m in _NUM_RE.finditer(q):
        if any(s <= m.start() < e for s, e in inc_spans):
            continue
        if _VERSION_RE.match(q, m.start()) or (m.start() > 0 and q[m.start() - 1] == "."):
            continue
        n = int(m.group(1))
        if (n, _number_kind(q, m.start())) not in a.numbers and n >= 100:
            a.numbers.append((n, _number_kind(q, m.start())))
    a.versions = list(dict.fromkeys(_VERSION_RE.findall(q)))
    a.paths = list(dict.fromkeys(p.rstrip(".,;:)") for p in _PATH_RE.findall(q)))
    a.handles = list(dict.fromkeys(_HANDLE_RE.findall(q)))
    a.quoted = list(dict.fromkeys(s.strip() for s in _QUOTED_RE.findall(q)))
    a.tests = list(dict.fromkeys(_TEST_RE.findall(q)))
    for m in _CUSTOMER_RE.finditer(q):
        name = m.group(1).strip(" .")
        if name and name.lower() not in _STOP_TERMS and len(name) >= 2:
            a.customers.append(name)
    a.customers = list(dict.fromkeys(a.customers))
    comps: list[str] = []
    lowered = q.lower()
    for c in known_components:
        # a component that is also an English word ("Core", "Settings", "CI")
        # counts only in its own spelling; a distinctive name in any case
        hay = q if c.lower() in _GENERIC_COMPONENT_WORDS else lowered
        needle = c if c.lower() in _GENERIC_COMPONENT_WORDS else c.lower()
        if c and re.search(r"(?<![A-Za-z0-9_])" + re.escape(needle) + r"(?![A-Za-z0-9_])", hay):
            comps.append(c)
    bots = settings.code_bot_author_names()
    for m in _CAMEL_RE.finditer(q):
        c = m.group(1)
        if (
            c not in _COMPONENT_STOP
            and c.lower() not in bots
            and c not in comps
            and not any(c in p for p in a.paths)
        ):
            comps.append(c)
    a.components = comps[:4]
    wm = _WINDOW_RE.search(q)
    a.days = _days_of(wm) if wm else 0
    a.words = {k for k, rx in _WORDS.items() if rx.search(q)}
    a.severities = _severities_of(q)
    # free-text terms: the remaining specific tokens (identifiers, error words);
    # the routing words, the window phrase and the severities are never search terms
    stripped = _PATH_RE.sub(" ", _QUOTED_RE.sub(" ", q))
    stripped = _WINDOW_RE.sub(" ", stripped)
    stripped = _WORDS_ANY.sub(" ", stripped)
    stripped = _SEVERITY_RE.sub(" ", stripped)
    toks = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", stripped)
    terms: list[str] = []
    for t in toks:
        low = t.lower()
        if low in _STOP_TERMS or t in a.components or t in a.handles:
            continue
        if any(low == c.lower() for c in a.components):
            continue
        if t not in terms:
            terms.append(t)
    a.terms = terms[:8]
    return a


# --- templates -----------------------------------------------------------------


@dataclass(frozen=True)
class Template:
    name: str
    access: str  # public | restricted
    reader: str  # TrackerSource method
    title: str  # section heading
    cites: str  # what a row cites: source_id/url


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in (
        Template(
            "escalation_context",
            RESTRICTED,
            "escalation_context",
            "Escalation, its graph and the instances it names",
            "support case, linked docs",
        ),
        Template(
            "incident_context",
            RESTRICTED,
            "incident_context",
            "Incident, its lifecycle and the PRs that fix it",
            "incident, linked PRs",
        ),
        Template(
            "customer_state",
            RESTRICTED,
            "customer_state",
            "Customer: escalations, instances, incidents",
            "support cases, instances",
        ),
        Template(
            "area_incidents",
            RESTRICTED,
            "incidents_in_area",
            "Core incidents in this area",
            "incidents",
        ),
        Template(
            "area_escalations",
            RESTRICTED,
            "escalations_in_area",
            "Support escalations in this area",
            "support cases",
        ),
        Template(
            "customers_in_area",
            RESTRICTED,
            "customers_in_area",
            "Customers who escalated in this area",
            "support cases grouped by customer",
        ),
        Template(
            "similar_bugs",
            PUBLIC,
            "similar_bugs",
            "Issues (and, restricted, incidents and escalations) that mention this",
            "bugs",
        ),
        Template(
            "area_open_issues",
            PUBLIC,
            "open_issues_by_component",
            "Open issues in this component",
            "issues",
        ),
        Template(
            "component_risk", PUBLIC, "component_risk_latest", "Component risk snapshot", "snapshot"
        ),
        Template(
            "release_blockers",
            PUBLIC,
            "release_blockers_with_fixes",
            "Release blockers and whether their fixes merged",
            "blocker issues, fixing PRs",
        ),
        Template("changelog", PUBLIC, "changelog_entries", "Changelog entries", "PRs"),
        Template(
            "prs_touching", PUBLIC, "prs_touching_prefix", "Merged PRs touching this path", "PRs"
        ),
        Template(
            "person_activity",
            PUBLIC,
            "person_activity",
            "What this person merged, reviewed, opened",
            "PRs, issues",
        ),
        Template(
            "test_gaps",
            PUBLIC,
            "test_gap_components",
            "Components by untested-bug ratio",
            "snapshot",
        ),
        Template(
            "flaky", PUBLIC, "flaky_issues_matching", "Flaky-test issues naming this test", "issues"
        ),
        Template("item", PUBLIC, "items_by_number", "The issue / PR record", "issue or PR"),
        Template(
            "slack_threads",
            RESTRICTED,
            "slack_threads_matching",
            "Slack incident / on-call / release threads",
            "threads",
        ),
        Template(
            "version_exposure",
            RESTRICTED,
            "version_exposure",
            "Cloud instances running this release",
            "instance dump",
        ),
        Template(
            "version_limits",
            RESTRICTED,
            "version_limits",
            "Instances pinned below a version, and why",
            "instance dump",
        ),
        Template(
            "duplicates",
            PUBLIC,
            "duplicates_of",
            "Duplicates and related issues in the graph",
            "issues",
        ),
        # The bug board the dashboard's Patterns mode draws
        Template(
            "severity_board",
            PUBLIC,
            "open_issue_counts_by_severity",
            "Open issues by component and severity",
            "issues, counted per cell",
        ),
    )
}


def template_names(restricted: bool) -> list[str]:
    """The templates a principal may run through a namespace: the public
    ones, plus the restricted class when the namespace is restricted
    (`tracker.binding` hands this list to the dashboard)."""
    return [t.name for t in TEMPLATES.values() if restricted or t.access == PUBLIC]


@dataclass
class Step:
    template: str
    params: dict[str, Any]
    why: str  # the anchor or word that selected it

    def as_dict(self) -> dict[str, Any]:
        t = TEMPLATES[self.template]
        return {
            "template": self.template,
            "access": t.access,
            "params": self.params,
            "why": self.why,
        }


@dataclass
class Plan:
    intent: str
    anchors: Anchors
    steps: list[Step] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "anchors": self.anchors.as_dict(),
            "steps": [s.as_dict() for s in self.steps],
        }


def plan_from_anchors(
    anchors: dict[str, Any],
    *,
    question: str = "",
    known_components: tuple[str, ...] = (),
    default_limit: int = 10,
) -> Plan:
    """A plan from structured anchors a client already holds — a PR's
    components and paths, an issue's number and frames, a customer, a
    version — merged with whatever the optional prose adds.
    The same rules, so an artefact and a sentence plan alike."""
    a = extract_anchors(question, known_components=known_components) if question else Anchors()
    for n in anchors.get("numbers") or []:
        kind = str(anchors.get("number_kind") or "")
        if isinstance(n, int) and n > 0 and (n, kind) not in a.numbers:
            a.numbers.append((n, kind))
    for key in (
        "versions",
        "components",
        "paths",
        "handles",
        "customers",
        "tests",
        "terms",
        "quoted",
    ):
        for v in anchors.get(key) or []:
            if isinstance(v, str) and v.strip() and v.strip() not in getattr(a, key):
                getattr(a, key).append(v.strip())
    spoken = [str(v).strip().lower() for v in (anchors.get("severities") or []) if str(v).strip()]
    if spoken:
        a.severities = sorted(
            {*a.severities, *(v for v in spoken if v in _SEVERITY_ORDER)}, key=_SEVERITY_ORDER.index
        )
    if int(anchors.get("days") or 0) > 0:
        a.days = max(1, min(int(anchors["days"]), 3650))
    for w in anchors.get("words") or []:
        if str(w) in _W:
            a.words.add(str(w))
    p = Plan(intent="", anchors=a)
    ctx = _Ctx(p, a, a.words, default_limit, question, frozenset(known_components))
    for rule in _RULES:
        if rule(ctx):
            break
    return p


def plan(question: str, *, known_components: tuple[str, ...] = (), default_limit: int = 10) -> Plan:
    """The ordered templates for a question. Deterministic: the first rule
    whose anchors are present wins the intent; every step names why it is
    there. No anchor → an empty plan (the caller abstains, never guesses)."""
    a = extract_anchors(question, known_components=known_components)
    p = Plan(intent="", anchors=a)
    ctx = _Ctx(p, a, a.words, default_limit, question, frozenset(known_components))
    for rule in _RULES:
        if rule(ctx):
            break
    return p


@dataclass
class _Ctx:
    p: Plan
    a: Anchors
    w: set[str]
    lim: int
    question: str
    known: frozenset[str] = frozenset()

    def component_filter(self) -> str:
        """The `primary_component` a read may filter on: only a name the
        source's own vocabulary carries. A CamelCase class in the question
        (`MergeTreeData`) is an anchor for text, not a component value."""
        return next((c for c in self.a.components if c in self.known), "")

    def text_terms(self) -> str:
        """The free text a read searches: the terms plus every CamelCase
        anchor that is not a vocabulary component."""
        extra = [c for c in self.a.components if c not in self.known]
        return " ".join([*extra, *self.a.terms[:4]]).strip()

    def area(self) -> dict[str, Any]:
        """The parameters of an area read: a vocabulary component (or none),
        a path prefix, the text, the window."""
        a = self.a
        return {
            "component": self.component_filter(),
            "path_prefix": a.paths[0] if a.paths else "",
            "term": a.quoted[0] if a.quoted else self.text_terms(),
            "days": a.days or 90,
        }

    def add(self, template: str, why: str, **params: Any) -> None:
        self.p.steps.append(Step(template, {**params, "limit": self.lim}, why))

    def add_open_issues(self, component: str) -> None:
        """The component's open issues; with severities spoken, the cell of
        the board: `severities` rides along and the limit is
        the cell ceiling, not the request's — the cell's list is the count."""
        if self.a.severities:
            self.p.steps.append(
                Step(
                    "area_open_issues",
                    {
                        "component": component,
                        "severities": list(self.a.severities),
                        "limit": CELL_LIMIT,
                    },
                    f"component named, severity {', '.join(self.a.severities)}",
                )
            )
        else:
            self.add("area_open_issues", "component named", component=component)


def _rule_numbered_doc(c: _Ctx) -> bool:
    """1. a number spoken of as an escalation or an incident."""
    for n, kind in c.a.numbers:
        if kind == "escalation" or ("escalation" in c.w and not kind):
            c.p.intent = c.p.intent or "escalation_context"
            c.add("escalation_context", f"#{n} named as an escalation", number=n)
        elif kind == "incident" or ("incident" in c.w and not kind):
            c.p.intent = c.p.intent or "incident_context"
            c.add("incident_context", f"#{n} named as an incident", number=n)
    return bool(c.p.steps)


_PROPER_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)\b")


def _rule_customer(c: _Ctx) -> bool:
    """2. a customer."""
    a = c.a
    if (
        not a.customers
        and {"escalation", "instances"} & c.w
        and "customer" not in c.w
        and not (a.numbers or a.components or a.paths or a.handles or a.versions or a.tests)
    ):
        # "Acme escalations and instances": a proper name beside customer-side
        # words and nothing else anchoring — the name is the customer
        for m in _PROPER_RE.finditer(c.question):
            name = m.group(1)
            if name.lower() not in _STOP_TERMS and not _WORDS_ANY.search(name):
                a.customers.append(name)
                break
    if not a.customers:
        return False
    c.p.intent = "customer_state"
    for cust in c.a.customers[:2]:
        c.add("customer_state", f"customer {cust!r}", org_name=cust, days=c.a.days or 365)
    return True


def _rule_release(c: _Ctx) -> bool:
    """3. a release version with release or changelog words."""
    if not (c.a.versions and ("release" in c.w or "changelog" in c.w)):
        return False
    v = c.a.versions[0]
    if "changelog" in c.w:
        c.p.intent = "changelog"
        # the narrowing text: a quote, else the components and terms ("for Parquet")
        term = (
            c.a.quoted[0]
            if c.a.quoted
            else " ".join(
                [*[x for x in c.a.components[:2] if x in c.known], *c.text_terms().split()[:3]]
            )
        )
        c.add("changelog", f"version {v} with changelog words", version=v, term=term)
    else:
        c.p.intent = "release_readiness"
        c.add("release_blockers", f"version {v} with release words", version=v)
    return True


def _rule_person(c: _Ctx) -> bool:
    """4. a person and activity words or a window."""
    if not (c.a.handles and ({"week", "prs", "review", "escalation"} & c.w or c.a.days)):
        return False
    c.p.intent = "person_activity"
    for h in c.a.handles[:2]:
        c.add("person_activity", f"@{h} with activity words", login=h, days=c.a.days or 7)
    return True


def _rule_version_exposure(c: _Ctx) -> bool:
    """3b. a release version with instance / exposure words: who runs it."""
    if not (c.a.versions and ("instances" in c.w or "customer" in c.w)):
        return False
    v = c.a.versions[0]
    c.p.intent = "version_exposure"
    c.add("version_exposure", f"version {v} with instance / customer words", version=v)
    if "backport" in c.w or "release" in c.w:
        c.add("version_limits", "version words: instances capped below it", version=v)
    return True


# `incident channels`, `the oncall channel`, `release-26-3`: a channel prefix
_CHANNEL_WORDS = (
    r"incident|oncall|on-call|release|proj|qa|support|dev|data|control|cloud|product|docs"
)
_CHANNEL_RE = re.compile(
    r"\b(" + _CHANNEL_WORDS + r")[\w-]*\s+channels?\b|\b(" + _CHANNEL_WORDS + r")-[\w-]+"
)


def _rule_slack(c: _Ctx) -> bool:
    """3c. Slack words with a term, component or channel."""
    if "slack" not in c.w or not (c.a.terms or c.a.components or c.a.quoted):
        return False
    c.p.intent = "slack_threads"
    term = c.a.quoted[0] if c.a.quoted else " ".join([*c.a.components[:1], *c.a.terms[:3]])
    m = _CHANNEL_RE.search(c.question.lower())
    channel = (m.group(1) or m.group(2)) if m else ""
    c.add(
        "slack_threads", "slack words with a term", term=term, channel=channel, days=c.a.days or 90
    )
    return True


def _rule_duplicates(c: _Ctx) -> bool:
    """3d. duplicate words with an issue number."""
    plain = [n for n, kind in c.a.numbers if kind in ("", "issue")]
    if not (plain and "similar" in c.w and "duplicate" in c.question.lower()):
        return False
    c.p.intent = "duplicates"
    c.add("duplicates", f"duplicate words with #{plain[0]}", number=plain[0])
    return True


def _rule_gaps(c: _Ctx) -> bool:
    """5. test gaps without an area."""
    if not ("gaps" in c.w and not c.a.components and not c.a.paths):
        return False
    c.p.intent = "test_gaps"
    c.add("test_gaps", "test-gap words without an area", days=c.a.days or 365)
    return True


def _rule_board(c: _Ctx) -> bool:
    """5b. bug-board words: open issues counted per component
    and severity — the whole board, or the vocabulary components named."""
    if "board" not in c.w:
        return False
    c.p.intent = "severity_board"
    known = [x for x in c.a.components if x in c.known][:3]
    why = "bug-board words" + (f" with component {', '.join(known)}" if known else "")
    # no `limit`: the board is every cell (≤ 2000), never the first ten
    c.p.steps.append(Step("severity_board", {"components": known, "days": c.a.days}, why))
    return True


def _rule_flaky(c: _Ctx) -> bool:
    """6. a flaky test."""
    if not (c.a.tests and ("flaky" in c.w or len(c.question.split()) <= 8)):
        return False
    c.p.intent = "flaky"
    c.add("flaky", f"test {c.a.tests[0]}", test_name=c.a.tests[0])
    return True


_BUG_WORDS = frozenset(
    {
        "leak",
        "crash",
        "segfault",
        "deadlock",
        "timeout",
        "exception",
        "error",
        "corruption",
        "hang",
        "oom",
        "regression",
        "bug",
        "sigsegv",
        "sigabrt",
    }
)
_BUG_RE = re.compile(r"(?i)\b(?:" + "|".join(sorted(_BUG_WORDS)) + r")s?\b")


_NARROWING = frozenset({"incident", "escalation", "risk", "customer"})


def _rule_area(c: _Ctx) -> bool:
    """7. an area — component or path: what the words ask for, else the risk picture."""
    a, w = c.a, c.w
    if not (a.components or a.paths):
        if "customer" in w and a.terms and not ({"incident", "risk"} & w):
            # "which customers escalated about Iceberg": the area is a term
            area = c.area()
            c.p.intent = "customers_in_area"
            c.add("customers_in_area", "customer words with a term", **area)
            c.add("area_escalations", "customer words with a term", **area)
            return True
        return False
    area = c.area()
    if "prs" in w and a.paths and not (_NARROWING & w):
        c.p.intent = "prs_touching"
        c.add(
            "prs_touching",
            f"PR words with path {a.paths[0]}",
            path_prefix=a.paths[0],
            days=a.days or 30,
        )
        return True
    similar = dict(
        text=a.quoted[0] if a.quoted else c.text_terms(),
        component=c.component_filter(),
        version=a.versions[0] if a.versions else "",
        days=a.days or 730,
    )
    # "open medium bugs in MergeTree": a severity beside a bare bug word and
    # no text to search is the board's cell, not a similar-bugs search
    bugish = _BUG_RE.search(c.question) is not None and not (a.severities and not similar["text"])
    if ("similar" in w or a.quoted or bugish) and not (_NARROWING & w):
        c.p.intent = "similar_bugs"
        c.add("similar_bugs", "issue words with an area", **similar)
        if c.component_filter():
            c.add_open_issues(c.component_filter())
        return True
    c.p.intent = "customers_in_area" if "customer" in w else "area_risk"
    risk = c.p.intent == "area_risk"
    if "customer" in w:
        c.add("customers_in_area", "customer words with an area", **area)
    if {"incident", "risk"} & w or risk:
        c.add("area_incidents", "incident / risk words with an area", untested="gaps" in w, **area)
    if {"escalation", "risk", "customer"} & w or risk:
        c.add("area_escalations", "escalation / risk words with an area", **area)
    known = [x for x in a.components if x in c.known][:3]
    if risk and known:
        c.add("component_risk", "component named", components=known)
        c.add_open_issues(known[0])
    if "similar" in w or a.quoted:
        c.add("similar_bugs", "issue words with an area", **similar)
    return True


def _rule_free_text(c: _Ctx) -> bool:
    """8. quoted text, or terms that read like a bug report."""
    a, w = c.a, c.w
    bugish = bool(a.terms) and _BUG_RE.search(c.question) is not None
    if not (a.quoted or ("similar" in w and a.terms) or bugish):
        return False
    c.p.intent = "similar_bugs"
    c.add(
        "similar_bugs",
        "quoted text or bug words",
        text=(a.quoted[0] if a.quoted else " ".join(a.terms[:5])),
        component="",
        version=a.versions[0] if a.versions else "",
        days=a.days or 730,
    )
    return True


def _rule_item(c: _Ctx) -> bool:
    """9. bare issue / PR numbers → the record."""
    plain = [n for n, kind in c.a.numbers if kind in ("", "issue", "pr")]
    if not plain:
        return False
    c.p.intent = "item"
    c.add("item", "issue / PR numbers", numbers=plain[:10])
    return True


_RULES = (
    _rule_numbered_doc,
    _rule_customer,
    _rule_version_exposure,
    _rule_release,
    _rule_slack,
    _rule_duplicates,
    _rule_person,
    _rule_gaps,
    _rule_board,
    _rule_flaky,
    _rule_area,
    _rule_free_text,
    _rule_item,
)


__all__ = [
    "PUBLIC",
    "RESTRICTED",
    "TEMPLATES",
    "Anchors",
    "Plan",
    "Step",
    "Template",
    "extract_anchors",
    "plan",
    "template_names",
]
