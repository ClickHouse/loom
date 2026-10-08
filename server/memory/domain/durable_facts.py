"""An extracted fact is kept only when it is durable.

`activity_facts` drops tool-activity narration and restatements of what a
session's own Loom tools carried. On a labelled eval of real SessionEnd
extractions, over half the stored rows were still junk after that. The junk
that survives is not narration of a tool call; it is:

- **status snapshots** that are stale within hours and already held by git,
  CI or the deploy log ("Round 10 is live on c0cf54838", "Release 51 is all
  green", "PR #100 was merged as 463e5359d", "CI … is still running");
- **peer-session chatter** — what another agent session said, claimed or
  scheduled ("build-7 claimed the task", "User received a message
  from another session regarding a deploy at 21:00Z");
- **housekeeping actions** the session performed (removed caches, freed
  disk, restarted containers, left N GB of images);
- **index and pipeline trivia** (watermarks, clone tips, PR counts);
- **vague meta** that names no content ("User's findings include insights",
  "Goal acknowledged", "Nothing else is pending");
- **the assistant's own plans in the first person** ("Doing that now as the
  next plugin revision", "Once #100 merges, I rebase #101");
- **in-session duplicates** — the extractor states one fact several times
  with different keys (three wordings of "User wants to build an optimize
  skill").

Each rule is anchored on the SHAPE of the sentence, never on a topic word, so
a real fact about the same subject survives: "Release 69 added inp_ms on every
ui.vitals row" (what a change does) is kept, "Release 51 is all green" (its CI
state at the time) is not; "The disk was growing by 2.1 GB every 20 seconds due to
a runaway writer" (a root cause) is kept, "User freed 27 GB" (an action) is
not. Deterministic; no model call. What needs judgement — a garbled
paraphrase, a value the session later superseded under another key — is the
durability verifier's (`durability_verifier.py`).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from server.memory.domain.session_echoes import _fact_text
from server.settings import settings

_I = re.IGNORECASE

# --- status snapshots -------------------------------------------------------
_STATUS = [
    re.compile(p, _I)
    for p in (
        # "live" as the claim itself ("is live on c0cf54838", "is live.") —
        # not "is now live with three tabs", which describes the change.
        r"\b(is|are) (now )?live( on [0-9a-f]{7,}| at \S+)?\s*[.!]?$",
        r"\b(is|are) (now )?live on [0-9a-f]{7,}\b",
        r"\b(went|goes|go) live\b",
        r"\bis serving build\b",
        r"\b(is|are) (all )?green( locally)?\b",
        r"\bgreen locally\b",
        r"\b(in|on) CI( with a watcher)?\b.*\b(running|pending|queued|watcher)\b",
        r"\bCI\b.{0,40}\b(is |are )?(still )?(running|pending|passed|green|red|failed|queued)\b",
        r"\b(jobs?|checks?) (have )?(passed|failed)\b",
        r"\bmerged as [0-9a-f]{7,40}\b",
        r"\b(is|was) merged \([0-9a-f]{7,40}\)",
        r"\bon main at commit [0-9a-f]{7,40}\b",
        r"\bhead is now [0-9a-f]{7,40}\b",
        r"\b(round|cut|phase) \d+ is (now )?(closed|done|merged|live|green)\b",
        r"\bnot (retrievable|available|deployed) yet\b",
        r"\bnothing (else )?(is|of mine is) (deployed|pending)\b",
        r"\b(is|are) (still )?(running|in progress|pending)\b(?!.*\b(because|due to|since)\b)",
        r"\b(restart|cut|swap|deploy|redeploy)s? (is |was )?(scheduled|due|at) \d{1,2}:\d{2}",
        r"\b(at|by) \d{1,2}:\d{2}(:\d{2})?Z\b.*\b(restart|cut|swap|merged|deploy|completed)\b",
        r"\b(restart|cut|swap|merged|deploy|completed)\b.*\b(at|by) \d{1,2}:\d{2}(:\d{2})?Z\b",
        r"\bpost-check\b.*\b(clean|passed|successful)\b",
        # a clock time on a deploy word anywhere: "the 06:14Z cut went out",
        # "the final restart is scheduled for 19:55:00Z", "the 13:27Z swap is clean"
        r"\b\d{1,2}:\d{2}(:\d{2})?Z\b.{0,40}\b(cut|swap|restart|deploy(-delta)?|redeploy)\b",
        r"\b(cut|swap|restart|deploy(-delta)?|redeploy)\b.{0,40}\b\d{1,2}:\d{2}(:\d{2})?Z\b",
        r"\b(final |next )?(restart|cut|swap|deploy)s? (includes|carries|has no|removes)\b",
        # work in flight: "is waiting for", "is grading", "is working on building"
        r"\b(is|are) (still )?(waiting for|grading|benchmarking|re-?running)\b",
        # run-ledger trivia
        r"\bthis run (wrote|read|recalled)\b",
        r"\b(recalled|observed) cost (was|is)\b",
        r"\brecorded with a count of \d+\b",
        r"\bis still (open|a draft|in draft)\b",
        r"\bbuilt and pushed as (a )?draft\b",
    )
]

# --- another agent session as the subject -----------------------------------
_PEER = [
    re.compile(p, _I)
    for p in (
        r"\breceived (a|another) (message|notice|cross-session)",
        r"\bcross-session (delivery )?(notice|message)\b",
        r"\b(another|the other) claude session\b",
    )
]


@lru_cache(maxsize=4)
def _peer_host_patterns(host: str) -> tuple[re.Pattern[str], ...]:
    """`host` is `settings.memory_quality_peer_host_pattern`: a peer session's host
    as the subject of a fact ("build-7 claimed the lease")."""
    if not host:
        return ()
    return (
        re.compile(
            rf"^\W*(user (noted|acknowledged|confirmed|agreed|received)( that)? )?(?:{host})\b", _I
        ),
        re.compile(
            rf"\b(?:{host}) (claimed|confirmed|released|reported|merged|apologi[sz]ed|"
            r"restarts?|completed|operates|fixed|had no objection)\b",
            _I,
        ),
    )


# --- housekeeping actions ---------------------------------------------------
_HOUSEKEEPING = re.compile(
    r"^\W*(the )?user (has )?(cleaned( up)?|removed|freed|pruned|deleted|left|restarted|reverted|"
    r"stopped|killed|re-?ran|reran|"
    r"confirmed that the .{0,40} (was|were) (restarted|stopped|removed))\b",
    _I,
)
_COMMENT_EDIT = re.compile(
    r"^\W*(the )?user (has )?(appended|added) (a |the )?(comment|line)\b",
    _I,
)
_FREED = re.compile(r"\bfreed \d[\d.,]* ?(g|m|t)i?b\b|\bfree space from\b", _I)

# --- index / pipeline trivia -------------------------------------------------
_TRIVIA = re.compile(
    r"\b(watermark|clone tip|pipeline clone|coverage by language|prs? in history|"
    r"recently ingested commits)\b",
    _I,
)

# --- vague meta: names no content --------------------------------------------
_VAGUE = [
    re.compile(p, _I)
    for p in (
        r"^\W*(the )?user'?s? (findings|measurements|constraints|files) include "
        r"(various|insights|specific)\b",
        r"^\W*(the )?user referenced specific\b",
        r"^\W*(the )?user acknowledged the goal\b",
        r"^\W*goal acknowledged\b",
        r"^\W*(the )?user (requested|asked) to (deploy|merge|restart|complete)\b",
        r"^\W*(the )?user confirmed that everything is done\b",
        r"\b(was|were) recorded in memory\b",
        r"^\W*(the )?user (updated|refreshed) the .{0,40} page with\b",
        r"^\W*(search|think of|we need to think)\b",
        # an event reported with no content: "The incident regarding the disk growth was
        # closed", "Two incidents were reported"
        r"^\W*(the |an? )?(incident|hypothesis|issue|finding|problem) (regarding|about|on|"
        r"with|behind|over) .{0,120}\b(was|were) (closed|reported|resolved|opened|filed)\s*[.!]?$",
        r"^\W*(two|three|several|\d+) (incidents|issues|findings|problems) (were|was) "
        r"(reported|closed|filed)\b",
        r"^\W*(the )?user provided an update\b",
        r"^\W*the agent relied on\b",
    )
]

# --- the session's own tool actions, credited to someone ----
# Checked last, so an earlier rule keeps naming the class it names. One-shot
# Claude Code and Codex sessions store the session's own Loom calls as facts
# about the user.
_SESSION_ACTION = [
    re.compile(p, _I)
    for p in (
        # a Loom tool's own answer, and a cut tool-output fragment
        r"^\W*(the )?user'?s? (\w+ )?loom_[a-z_]+ calls?\b.{0,60}\b(returned|succeeded|failed|"
        r"came back|reported|showed|listed)\b",
        r"^\W*(the )?user'?s? (two|three|four|\d+) loom calls? (returned|succeeded|failed)\b",
        r"\[truncated\]",
        # what the session's own lookups turned up, and the Loom objects a
        # session created while testing: "User found five hits
        # in memories …", "User noted that the context pack has almost nothing
        # about …", "User created two more tasks after the initial task."
        r"^\W*(the )?user found\b",
        r"^\W*(the )?user noted that (the |its |their )?(context pack|search|tool|index|mentor|"
        r"result|primer|hook|namespace summary|memory list)\b",
        r"^\W*(the )?user (created|posted|added|opened) (a|an|one|two|three|four|\d+|more|another|"
        r"the) (more |new )?(tasks?|conversations?|docs?|documents?|keys?|memories|notes?|"
        r"blocks?|inbox(es)?|turns?)\b",
        # the user credited with the session's own tool actions: "User set the
        # preference 'test/x' to …", "User locked the preference …", "User called
        # tracker test_signal …", "User deferred the task …", "User appended 'step 5b'
        # to loom working memory".
        r"^\W*(the )?user (set|updated|locked|unlocked|pinned|unpinned|retrieved|fetched|"
        r"appended|replaced|deleted|removed|deferred|marked|listed|queried) (the |a |an |"
        r"(one|two|both|all|each) of (the )?(two |three |\d+ )?|the other |its )?"
        r"(preference|key|value|task|doc|document|block|conversation|inbox|"
        r"note|memory|memories|row|entry|additional|extra|'|\")",
        r"^\W*(the )?user (called|ran|invoked) (the )?(loom|tracker|code|memory|task|inbox)\b",
        r"^\W*(the )?user (appended|wrote|replaced) .{0,80}\b(to|in|with) (the )?loom "
        r"working memory\b",
        r"^\W*(the )?user made a trivial edit\b",
        # where a session found (or did not find) a credential file: a probe's
        # state, and a pointer to a secret.
        r"\b(token|credentials?|api[ _-]?key|secret|password)s? (file )?(is|are|was|were) "
        r"(absent|present|missing|found|not found|stored)\b",
        r"^\W*(the )?user (appended|replaced|set|cleared|updated) (the )?loom working memory\b",
        r"^\W*the final (state|states|status(es)?) of\b",
        r"^\W*the hook message\b",
        # a hook's own message received, and the memory tool's count of keys
        # under a prefix.
        r"^\W*(the )?user (received|got|saw|was shown) (a|an|the) (\w+ )?"
        r"(hook|loom|shunt|gate|mentor) (message|notice|hint|denial|warning)\b",
        r"^\W*(the )?user has (no|one|two|three|four|\d+) (remaining |more |other )?"
        r"(keys?|memories|blocks?|docs?|documents?|tasks?|entries) (left )?"
        r"(under|in|at|with prefix)\b",
        # the memory tool's own state for one key, and a tracker verdict read back: "the
        # key test/x is locked and cannot be deleted", "is still 'X' (locked)", "…
        # (verdict none, calibrated)".
        r"\b(key|value|preference|memory|entry)( for)? [`'\"]?[\w.-]+/[\w./-]+[`'\"]? (is|was) "
        r"(still )?(locked|pinned|tombstoned|deleted|read-only)\b",
        # the memory tool's answer about one key, read back — "User's memory history for
        # key test/x includes …".
        r"^\W*(the )?user'?s? (memory history|history|memory list|version history|working memory|"
        r"task list|inbox list) (for|of) (the )?(key |block |section |inbox )?"
        r"[`'\"]?[\w.-]+(/[\w./-]+)?[`'\"]? (includes?|has|had|shows?|showed|contains?|lists?)\b",
        # a code tool's answer credited to the user: "User noted that Loom
        # found one production caller …", "User identified that `handle_x`
        # validates the draft …", "… falls in the Context section, lines 5–37".
        r"^\W*(the )?user noted that (the )?loom (found|returned|reported|showed|listed|resolved|"
        r"located|matched)\b",
        r"^\W*(the )?user (identified|determined|confirmed|learned|saw|discovered|verified) that "
        r"`[\w.:]+`",
        r"\b(match|hit|result|line|section)s? (falls?|fell|sits?|lies|lay) (in|within|inside)\b"
        r".{0,80}\blines? \d+\s*[–-]\s*\d+\b",
        r"\bis locked (in read[- ]only mode )?and cannot be (updated|deleted|changed|written)\b",
        r"\bis still [`'\"].{0,120}[`'\"] \((locked|pinned)\)",
        r"\(verdict (none|likely|possible|duplicate|unknown)(, calibrated)?\)",
        r"\bversion( being| is| was|:)? `?\d{15,}\b",  # a write's version id, read back
        # a Loom tool's answer credited to the user or narrated by the
        # assistant, and a code location the index answers
        r"\breturned by (the )?(context pack|search|tool|index|mentor|primer|loom[_ ]\w+)\b",
        r"^\W*(the )?user'?s? loom_[a-z_]+\b.{0,40}\b(found|returned|reported|showed|listed|"
        r"says|states|results? (included|include|show|showed))\b",
        # the same answer with a reporting verb the list above lacks: "User's
        # loom_code_search results indicate that …", "User's loom_code_context_pack
        # indicates a new tracker op starts in …", "the loom_code_outline results show
        # …".
        r"^\W*(the )?(user'?s? )?loom_[a-z_]+( (tool|call|results?|answer|output|response))?"
        r"( for [^,.;]{1,80})? (indicates?|indicated|highlights?|highlighted|suggests?|suggested|"
        r"shows?|showed|points? to|pointed to|names?|named|mentions?|mentioned|contains?|"
        r"contained|confirms?|confirmed|reveals?|revealed)\b",
        r"^\W*(the )?user noted that (the )?loom (test gate|hook|shunt|mentor|primer|index)\b",
        r"^\W*(the )?user (outlined|grepped|searched|scanned) [`'\"]?[\w.-]*/",
        r"^\W*(the )?assistant (explained|deleted|ran|created|updated|read|listed|showed|"
        r"described|summari[sz]ed|removed|called|fetched|checked|noted that it)\b",
        r"\b(is|are|lives|live|sits) (in|at) `?(server|src|lib|tests?|integrations|frontend|sdk)/"
        r"[\w./-]+\.(py|ts|tsx|js|mjs|go|rs|cpp|h|hpp)\b",
    )
]

# --- the memory tool's own state, and code lookups the index answers -------
# "User's default namespace has 2,578 memories", "the mentor section contains
# a hint", "User found the handler at server/memory/app/x.py:75-112", "17 call
# sites": what a Loom or code tool returned, restated. The index/namespace
# answers these fresh; a memory copy is stale at the next write or merge.
_TOOL_STATE = [
    re.compile(p, _I)
    for p in (
        r"\b(default )?namespace (\S+ )?(is|has|had) (\d|`|had)",
        r"\bnamespace has (had )?\d",
        r"\b(is|are) in \d+ namespaces\b",
        r"\b(top entities|distinct agents|recent (writes|recalls)|last activity)\b",
        r"\b(context pack|namespace health check|health check)\b.{0,60}"
        r"\b(returned|found|contains?|came back)\b",
        r"\b(working memory|mentor|memories|procedures) section contains\b",
        r"\b(\d+ versions across \d+ memory ids)\b",
        r"\bloom rated the retrieval risk\b",
    )
]
_CODE_LOOKUP = [
    re.compile(p, _I)
    for p in (
        r"\b(implemented|defined|located|lives|is) (at|in) `?[\w./-]+\.\w{1,5}:\d+",
        r"\bat `?[\w./-]+\.\w{1,5}:\d+(-\d+)?`?\s*\.?$",
        r"\bfound \d+ (call sites|callers|hits|matches|results)\b",
        r"\b(reviewed|read) the outline of\b",
        r"\bthe first function in\b",
    )
]
# "X doesn't exist", "the list came back empty": what a lookup found this
# minute, not a fact about the world.
_NOT_FOUND_NOW = re.compile(
    r"\b(does not|doesn't|do not|don't) exist\b|\breturns? no entries\b|\bcame back empty\b|"
    r"\bnothing to delete\b",
    _I,
)
# A command's or a search's result listing: "the top 3 results from …",
# "User received 3 open issues from the command".
_RESULT_LISTING = re.compile(
    r"\btop \d+ results\b|\breceived \d+ (open |closed )?(issues|results|rows|prs)\b",
    _I,
)

# --- the assistant's own plans, first person ---------------------------------
_FIRST_PERSON_PLAN = re.compile(
    r"^\W*(i('ll| will| am| rebase| merge| open| take| run)|doing that now|once #?\d+ merges, i)\b"
    r"|\bnothing else of mine\b",
    _I,
)


# --- what a tool call returned, restated as a fact --------- Examples: "The pinned
# value won.", "The update was refused due to the lock being read_only.", "It returned
# three sections and was cut off …", "Loom has nothing about the tracker rename
# itself.", "The index is at commit fcf7bb837, 3 commits behind main." The subject is
# the tool, its result or the index; the tool answers it fresh next time.
_TOOL_OUTCOME = [
    re.compile(p, _I)
    for p in (
        r"^\W*(the )?(pinned|locked|stored|new|old|first|second) (value|write) "
        r"(won|lost|was kept|stuck|stayed)\b",
        r"^\W*(the )?(update|write|delete|deletion|set|call|request|edit)s? (was|were) "
        r"(refused|rejected|blocked|denied)\b",
        r"\brefused with (a )?\d{3}\b|\b\d{3} locked\b|\bunlocking (it )?needs\b",
        r"^\W*(the )?extraction ran on\b",
        r"^\W*(it|this|that|the (tool|call|search|lookup|query|result|response|pack|op))"
        r"\b.{0,30}\b(returned|came back|was cut off|accepts only|takes no|found no)\b",
        r"^\W*what came back\b",
        r"^\W*(the )?user found no (reports?|records?|issues?|matches|results?|duplicates?)\b",
        r"^\W*loom (has|had|holds|held|returned|found|knows) (nothing|no)\b",
        r"^\W*(this|the) (tool|op|operation|call) (takes|accepts|has|needs) (no|only)\b",
        r"\b(is|are) not (a )?tools? in the plugin\b|^\W*neither\b.{0,80}\bis a tool\b",
        r"^\W*the (code )?index (is|was) at (commit )?\b|\b\d+ commits? behind (main|master)\b",
        r"^\W*the (first|top|last) (hit|result|match) for\b",
        r"^\W*the last (\w+ )?(prs?|commits?) to touch\b",
        r"\bretrieval_risk\b",
        r"^\W*the (latest|last) related commit\b",
        r"^\W*the other \d+ (are|were)\b",
        r"\bwhen running a (bash |shell )?command\b|\bcommand '[^']+' was not found\b",
        r"\b\d+ tests? (passing|passed|failing|failed)\b|\btests? passed with \d+\b",
        r"\bencountered an? \w*error while\b",
        r"\bconversation turns were not saved\b",
        r"\b(feedback|message|note) from the \w+ hook\b|\bthe \w+ hook (said|stated|blocked|"
        r"reported|refused)\b",
        r"^\W*(the )?user added a comment\b",
        r"^\W*the (\w+ )?hook('s)? message (was|said)\b|^\W*loom blocked (the|a)\b",
        r"^\W*the (first|second|third|fourth|fifth|next|last) (issue|result|hit|match|pr) "
        r"(found|returned|listed)?\s*(is|was)\b",
        r"^\W*(the )?user (completed|claimed|accepted|closed|pulled) the task '",
        r"\b(the )?context pack (found|returned) (nothing|only)\b",
    )
]
# A code location with a line range: "`QaIntelUnavailable` (36-37) is …",
# "… in `sdk/python/loom/agent.py:214-221`." — the code index answers it.
_CODE_SPAN = [
    re.compile(p, _I)
    for p in (
        r"`[^`\n]{1,80}`\s*\(\d+(-\d+)?\)",
        r"\b(in|at) `?[\w./-]+\.\w{1,5}:\d+(-\d+)?`?\s*\.?$",
    )
]


def capture_outcome_class(fact: dict[str, Any]) -> str | None:
    """ "tool_outcome" / "code_lookup" for the tool-outcome shapes alone — what the
    read side (`fact_quality`) applies to rows a coding capture already
    stored."""
    text = _fact_text(fact).strip()
    for r in _TOOL_OUTCOME:
        if r.search(text):
            return "tool_outcome"
    for r in _CODE_SPAN:
        if r.search(text):
            return "code_lookup"
    return None


def session_action_class(fact: dict[str, Any]) -> str | None:
    """The session's own tool actions and answers, credited to
    the user or narrated by the assistant — or None."""
    text = _fact_text(fact).strip()
    return "session_action" if any(r.search(text) for r in _SESSION_ACTION) else None


def non_durable_class(fact: dict[str, Any]) -> str | None:
    """The rule a fact trips, or None when it may be durable."""
    text = _fact_text(fact).strip()
    if not text:
        return "empty"
    for r in _STATUS:
        if r.search(text):
            return "status"
    for r in (*_PEER, *_peer_host_patterns(settings.memory_quality_peer_host_pattern)):
        if r.search(text):
            return "peer"
    if _HOUSEKEEPING.search(text) or _COMMENT_EDIT.search(text) or _FREED.search(text):
        return "housekeeping"
    if _TRIVIA.search(text):
        return "trivia"
    for r in _VAGUE:
        if r.search(text):
            return "vague"
    if _FIRST_PERSON_PLAN.search(text):
        return "first_person_plan"
    for r in _TOOL_STATE:
        if r.search(text):
            return "tool_state"
    for r in _CODE_LOOKUP:
        if r.search(text):
            return "code_lookup"
    if _NOT_FOUND_NOW.search(text):
        return "not_found_now"
    if _RESULT_LISTING.search(text):
        return "result_listing"
    return session_action_class(fact) or capture_outcome_class(fact)


# --- a trajectory replay: the agent acting -----------------
# The observe replay extracts from turns that are each ONE tool action, and
# its instructions make the model name the agent as the subject. So a replay
# fact whose subject is an actor is an action by construction — "Claude-Code
# ran a Bash command in the paths …", "Codex created a pull request …",
# "Codex tests passed with 121 tests …", "Alex wrote the heatmap.js service
# file." — and the `activity_facts` verb list catches only about two thirds of
# them. It is kept only when the verb states what was learned ("Alex noted
# that a trustworthy bisect ends bimodal …").
_REPLAY_ACTORS = (
    r"user|the user|assistant|the assistant|agent|the agent|operator|i|we|"
    r"claude(?:[ -]code)?|codex|cursor"
)
# Not a possessive: "User's favorite editor is Neovim" names the person.
_REPLAY_KNOWN_ACTOR_RE = re.compile(rf"^\W*(?:{_REPLAY_ACTORS})\b(?!'s\b|’s\b)", _I)
# A capitalised name followed by an action in the past tense.
_REPLAY_NAMED_ACTOR_RE = re.compile(
    r"^\W*[A-Z][a-z]+(?:[ -][A-Z]?[a-z]+)?\s+(?:also\s+|then\s+|successfully\s+|just\s+)?"
    r"(?:[a-z]+ed|ran|re-?ran|read|wrote|re-?wrote|took|sent|set|made|built|rebuilt|began|"
    r"put|left|kept|got|gave|saw|did|undid|broke|split|shut|cut|hid|led|met|won|lost|found|"
    r"will|(?:is|was|are|were) [a-z]+ing)\b"
)
_REPLAY_KNOWLEDGE_RE = re.compile(
    r"^\W*\S+(?:[ -]\S+)?\s+(?:also\s+)?(?:noted|learned|learnt|concluded|determined|"
    r"discovered|decided|realized|realised|prefers?|wants?|(?:found|established|"
    r"identified|observed) that)\b",
    _I,
)


def replay_action_class(fact: dict[str, Any], agent: str = "", *, named: bool = True) -> str | None:
    """ "replay_action" when a trajectory-replay fact's subject is an actor
    (the session's agent, a coding client, the user or — with `named` — a
    capitalised name) doing something, None otherwise. A knowledge verb keeps
    the fact."""
    text = _fact_text(fact).strip()
    if not text or _REPLAY_KNOWLEDGE_RE.search(text):
        return None
    name = (agent or "").strip()
    if name and name not in ("*", "-") and re.match(rf"^\W*{re.escape(name)}\b(?!'s\b)", text, _I):
        return "replay_action"
    if _REPLAY_KNOWN_ACTOR_RE.search(text):
        return "replay_action"
    if named and _REPLAY_NAMED_ACTOR_RE.search(text):
        return "replay_action"
    return None


_TOK = re.compile(r"[a-z0-9][a-z0-9._/:#+-]*")
_DUP_STOP = frozenset(
    "a an the and or of to in on at for from by with as is are was were be been it its this that "
    "user user's users has have had now also".split()
)


def _dup_words(text: str) -> set[str]:
    return {w for w in _TOK.findall(text.lower()) if w not in _DUP_STOP}


# Two facts from one extraction are the same fact when one's content words
# are nearly all inside the other's. The longer (more specific) one is kept.
_DUP_CONTAIN = 0.85
_DUP_MIN_WORDS = 4


def collapse_duplicates(
    facts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped): within one extraction, a fact whose content words are
    ≥85% contained in a longer fact's is a restatement of it; the longer, more
    specific one is kept. Each dropped fact names it in `_duplicate_of`."""
    words = [_dup_words(_fact_text(f)) for f in facts]
    order = sorted(range(len(facts)), key=lambda i: -len(words[i]))
    kept_idx: list[int] = []
    dup_of: dict[int, int] = {}
    for i in order:
        w = words[i]
        if len(w) < _DUP_MIN_WORDS:
            kept_idx.append(i)
            continue
        host = next(
            (
                j
                for j in kept_idx
                if len(words[j]) >= len(w) and len(w & words[j]) >= _DUP_CONTAIN * len(w)
            ),
            None,
        )
        if host is None:
            kept_idx.append(i)
        else:
            dup_of[i] = host
    kept = [f for i, f in enumerate(facts) if i not in dup_of]
    dropped = [
        {**f, "_duplicate_of": facts[dup_of[i]].get("key")}
        for i, f in enumerate(facts)
        if i in dup_of
    ]
    return kept, dropped


def partition(
    facts: list[dict[str, Any]], *, replay_agent: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped) — dropped trip a non-durable rule or restate a kept
    fact of the same extraction. Each dropped fact carries `_non_durable`.
    `replay_agent` (not None) marks a trajectory replay: an actor-led fact
    is an action there (`replay_action_class`)."""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for f in facts:
        cls = non_durable_class(f)
        if cls is None and replay_agent is not None:
            cls = replay_action_class(f, replay_agent)
        if cls:
            dropped.append({**f, "_non_durable": cls})
        else:
            kept.append(f)
    kept, dups = collapse_duplicates(kept)
    dropped.extend({**f, "_non_durable": "duplicate"} for f in dups)
    return kept, dropped
