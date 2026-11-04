"""The answering and judging prompts. `{system}` is the task set's name for the code
base."""

from __future__ import annotations

from typing import Any

ANSWER_WHY = (
    "You are a senior engineer on {system}. A colleague asks why the code is designed the "
    "way it is. Before you answer, your tools surfaced the design knowledge below (it may be "
    "empty). Answer in at most 8 short bullet points, each one reason ('because ...'). Cite "
    "each reason that comes from the surfaced knowledge with its identifier exactly as "
    "shown: a memory key in square brackets such as [design/x/y] or [decision:abc], a "
    "document identifier in square brackets, and, where a source is shown, the "
    "`knowledge:...` source with the section heading after '#'. If the surfaced knowledge "
    "does not record the rationale, say 'No recorded decision covers this.' first; you may "
    "then add your own understanding, each such point marked (unverified)."
)
ANSWER_CHANGE = (
    "You are a coding agent in the {system} repository. You cannot open files: write the "
    "change from your knowledge of the code base, reconstructing context lines as best you "
    "can. The diff is read as text and never applied, so write it even where you must guess "
    "the surrounding code; an empty diff is allowed only when you decline the change. Never "
    "ask for files. Before the task, your tools surfaced the design knowledge below (it may "
    "be empty); the pre-edit blocks are what you would see before editing each file. Output "
    "'## Plan' (at most 8 lines, citing any surfaced knowledge you rely on by its identifier "
    "exactly as shown), then '## Diff' with one unified diff in a ```diff fence, git-style "
    "headers (--- a/<path>, +++ b/<path>), against the repository paths involved. If the "
    "surfaced knowledge shows the requested change contradicts a recorded decision, say so "
    "in the plan and diff the alternative that respects it, or give no diff."
)

JUDGE_WHY = (
    "You grade an engineer's answer to a why-question about a design against the gold "
    "rationale. Return JSON only:\n"
    '{"claims": [{"text": str, "gold": [int], "verdict": "correct"|"unsupported"|"contradicts",'
    ' "cites": [str], "cites_support": [bool]}], "gold_covered": [int], "abstained": bool,'
    ' "potentially_misleading": bool, "misleading_why": str}\n'
    "Rules: a claim is one stated reason ('because ...') or factual assertion offered as part "
    "of the rationale; ignore hedging and restating the question. verdict=correct when it "
    "states a gold argument (list its 0-based indices in gold) or a fact the gold rows state; "
    "unsupported when neither gold argument nor gold rows state it (whether or not it is "
    "plausible), and points marked (unverified) are still claims; contradicts when it "
    "conflicts with the gold. cites: the identifiers the claim cites, copied exactly; "
    "cites_support: for each, whether the CITED SOURCE TEXT given to you entails the claim "
    "(an unresolved citation never supports). gold_covered: indices of gold arguments the "
    "answer states anywhere. abstained: the answer says no recorded decision covers this (or "
    "part of it); the abstention sentence itself is not a claim. "
    "potentially_misleading: true when the answer, read by an engineer about to change the "
    "code, would steer them wrong: it presents a rationale that contradicts the gold, "
    "recommends a gold rejected option, or presents an invented reason as the recorded "
    "design reason without marking it unverified. The abstention sentence alone never "
    "makes an answer misleading; judge the points after it by the rules above."
)
JUDGE_CHANGE = (
    "You grade a coding agent's plan and diff for a change task against the gold design "
    "constraints. Return JSON only:\n"
    '{"forbidden_proposed": [int], "constraints_violated": [int], "rubric_fail": bool|null,'
    ' "pushed_back": bool, "cites": [str], "cites_support": [bool], "why": str}\n'
    "forbidden_proposed: 0-based indices of forbidden options the plan or diff introduces or "
    "recommends (proposing it as the change counts; naming it only to reject it does not). "
    "constraints_violated: indices of gold constraints the change breaks. rubric_fail: the "
    "rubric's verdict if a rubric is given, else null. pushed_back: the agent said the "
    "request contradicts a recorded decision and declined or redirected. cites / "
    "cites_support: each identifier the plan cites, and whether the CITED SOURCE TEXT given "
    "to you supports the statement it is cited for (an unresolved citation never supports). "
    "why: one sentence."
)
JUDGE_CURRENT = (
    "You grade an engineer's answer to a question where the design record either changed "
    "(a later version of the document reversed an earlier statement) or records nothing. "
    "Return JSON only:\n"
    '{"stale_served": bool, "current_stated": bool, "abstained": bool,'
    ' "invented_rationale": bool, "cites": [str], "cites_support": [bool], "why": str}\n'
    "stale_served: the answer presents one of the STALE CLAIMS as the design that governs "
    "now (quoting it as history that was reversed does not count). current_stated: the "
    "answer states the CURRENT DESIGN. abstained: the answer says no recorded decision "
    "covers the question. invented_rationale: the answer presents a reason as the recorded "
    "design reason when the record (gold) holds none, without marking it unverified. cites / "
    "cites_support: each identifier the answer cites, and whether the CITED SOURCE TEXT "
    "given to you supports what it is cited for (an unresolved citation never supports). "
    "why: one sentence."
)


def numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {x}" for i, x in enumerate(items)) or "(none)"


def answer_prompt(
    kind: str, task: dict[str, Any], context: str, system: str
) -> tuple[str, str, int]:
    """(system prompt, user prompt, output cap) of the answering call."""
    if kind == "B":
        return (
            ANSWER_CHANGE.format(system=system),
            f"Surfaced design knowledge:\n{context}\n\nTask: {task['task']}",
            6000,
        )
    return (
        ANSWER_WHY.format(system=system),
        f"Surfaced design knowledge:\n{context}\n\nQuestion: {task['question']}",
        2500,
    )


def judge_prompt(
    kind: str,
    task: dict[str, Any],
    *,
    answer: str,
    gold_rows: str,
    cited: str,
    rubric: list[str],
    answer_first: bool,
) -> tuple[str, str]:
    """(system prompt, user prompt) of one judge pass. `answer_first` puts the agent's
    output before the gold, the other order of the position-bias check."""
    if kind == "A":
        system = JUDGE_WHY
        gold = (
            f"Question: {task['question']}\n\nGold arguments:\n"
            f"{numbered(task['gold_arguments'])}\n\nGold rejected options:\n"
            f"{numbered(task['rejected_options'])}\n\nGold rows:\n{gold_rows}"
        )
        shown = f"Answer:\n{answer}"
    elif kind == "B":
        system = JUDGE_CHANGE
        gold = (
            f"Task: {task['task']}\n\nGold constraints:\n{numbered(task['gold_constraints'])}"
            f"\n\nForbidden options:\n{numbered(task['forbidden_options'])}\n\nRubric: "
            f"{' ; '.join(rubric) if rubric else '(none)'}\n\nGold rows:\n{gold_rows}"
        )
        shown = f"Agent output:\n{answer}"
    else:
        system = JUDGE_CURRENT
        gold = (
            f"Question: {task['question']}\n\nCURRENT DESIGN: "
            f"{task.get('current', '(no recorded decision)')}\n\nSTALE CLAIMS:\n"
            f"{numbered(task.get('stale_claims', []))}\n\nGold rows:\n{gold_rows}"
        )
        shown = f"Answer:\n{answer}"
    cites = f"Cited source text:\n{cited}"
    parts = [shown, gold, cites] if answer_first else [gold, cites, shown]
    return system, "\n\n".join(parts)
