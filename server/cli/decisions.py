"""`loom decisions extract`: mine a knowledge document into decision records
(server/memory/app/decision_extract.py). In-process, with no token: operator only."""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected

if TYPE_CHECKING:
    from server.memory.app.decision_extract import Action, ExtractReport

# Below this a window holds a sentence or two, too little to state a decision and its reason.
# not a setting: the floor of the --window-chars flag, which each run sets
_MIN_WINDOW_CHARS = 500


def _at_least(text: str, low: int) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, at least {low}") from None
    if value < low:
        raise argparse.ArgumentTypeError(f"must be at least {low}")
    return value


def _per_section(text: str) -> int:
    return _at_least(text, 1)


def _window_chars(text: str) -> int:
    return _at_least(text, _MIN_WINDOW_CHARS)


# How each printed action reads, applied and in a dry run; row_changed is never printed (a
# race, listed apart). A test checks every other Action has its line.
_ACTIONS: dict[Action, tuple[str, str]] = {
    "written": ("recorded", "would record"),
    "revised": ("revised", "would revise"),
    "unchanged": ("unchanged", "unchanged"),
    "flagged": ("tagged source-changed", "would tag source-changed"),
    "already_flagged": ("already tagged source-changed", "already tagged source-changed"),
    "not_ours": ("not revised", "would not revise"),
    "outcome_known": ("not revised", "would not revise"),
    "already_recorded": ("skipped", "would skip"),
}


def register_decisions_cli(sub: Any) -> None:
    p = sub.add_parser("decisions", help="Decision records mined from documents.")
    dsub = p.add_subparsers(dest="subcommand", required=True)
    p_ext = dsub.add_parser(
        "extract",
        help="Mine one knowledge document into decision records: one model call per window of its "
        "sections (a heading over only an image and its caption is not sent), two when the "
        "first reply holds no decision or a reason that is not one span of the text, "
        "candidates kept only when their quote, reasoning and rejected options' reasons are "
        "the document's own words, the reasoning says more than the outcome (a ban may give "
        "no reason) and the quote is not in a recap, summary or background section, written "
        "through decision.record as agent loom-decisions. A re-run revises or retires the "
        "extractor's own unconfirmed rows and tags a confirmed (pinned or attested) row "
        "source-changed. Dry run by default (still calls the model); --apply writes. Exits 1 "
        "when it could not read the document (no document, no chunks, no usable model, an "
        "unknown --repo or a namespace it may not use, --into without --repo, a --repo with no "
        "remote to tag rows with, the dashboard's repositories unread, its own rows "
        "unreadable or left in another namespace, no window read), 2 when a write, or a read "
        "it needed (a window included), was refused or failed (a row changed since the run "
        "read it is reported, not counted).",
    )
    p_ext.add_argument("--org", required=True)
    p_ext.add_argument("--namespace", required=True, help="The namespace holding the document")
    p_ext.add_argument("--source-uri", required=True, help="The document's source_uri")
    p_ext.add_argument(
        "--repo",
        default="",
        help="The repository (owner/name or a remote URL) whose trunk code index resolves "
        "the document's code names",
    )
    p_ext.add_argument(
        "--into",
        choices=["repo", "source"],
        default=None,
        help="With --repo, where the rows go: the repository's trunk code namespace (repo; "
        "the default for a document in a code namespace), or the document's namespace, "
        "tagged with the repository (source; the default otherwise), served before an edit "
        "to a session whose own namespace is the document's. A repository reader "
        "who cannot read the document's namespace sees a repo row's quote but is refused "
        "its source link.",
    )
    p_ext.add_argument(
        "--max-per-section",
        type=_per_section,
        default=3,
        help="Decisions kept per section, at least 1 (default 3)",
    )
    p_ext.add_argument(
        "--window-chars",
        type=_window_chars,
        default=6000,
        help=f"Characters of the document per model call, at least {_MIN_WINDOW_CHARS} "
        "(default 6000)",
    )
    add_apply_flags(p_ext, writes="write the decision records")
    p_ext.set_defaults(func=_cmd_extract)


def _cmd_extract(args: argparse.Namespace) -> int:
    from server.memory.app.decision_extract import extract_document

    async def _run(_ch: Any) -> int:
        report = await extract_document(
            args.org,
            args.namespace,
            args.source_uri,
            dry_run=bool(args.dry_run),
            repo=args.repo,
            into=args.into,
            max_per_section=args.max_per_section,
            window_chars=args.window_chars,
        )
        print_report(report)
        return exit_code(report)

    return run_connected(_run)


def exit_code(report: ExtractReport) -> int:
    """1 when the run could not read the document (no document, no chunks, no usable model, an
    unknown --repo or a namespace it may not use, --into without --repo, a --repo with no remote
    to tag rows with, the dashboard's repositories unread, the extractor's rows unreadable or
    left in another namespace, no window read), 2 when a write, or a read it needed (a window
    the model could not answer readably included), was refused or failed (in a dry run too),
    else 0. A row deleted or rewritten since the run read it is reported, not counted: the
    next run settles it."""
    if report.error:
        return 1
    return 2 if report.refused or any(d.refused for d in report.dropped) else 0


def print_report(report: ExtractReport) -> None:
    """The run as a human reads it before pinning: every record with its code-link
    signals, and its body in a dry run or when a human must act on it; then what was
    dropped and why."""
    if report.error:
        print(f"error: {report.error}", file=sys.stderr)
        if not report.dropped:
            return
    print(
        f"{report.source_uri}: {report.windows} window(s), {len(report.records)} decision(s)"
        + (" (dry run)" if report.dry_run else "")
    )
    for rec in report.records:
        verb = _ACTIONS[rec.action][1 if report.dry_run else 0]
        print(f"- {verb} {rec.memory_key}" + (f": {rec.note}" if rec.note else ""))
        for line in rec.signals or ["no code names"]:
            print(f"    signal: {line}")
        if report.dry_run:
            for name in rec.unresolved:
                print(f"    unresolved: {name}")
        if report.dry_run or rec.action in (
            "flagged",
            "already_flagged",
            "not_ours",
            "outcome_known",
        ):
            print("    decision.record " + json.dumps(rec.body, ensure_ascii=False))
    for d in report.dropped:
        quote = f": {d.quote[:120]!r}" if d.quote else ""
        print(f"- dropped [{d.section or '(untitled)'}] {d.reason}{quote}")
    for r in report.retried:
        print(f"- read again [{r.section or '(untitled)'}] {r.reason}")
    for section in report.image_only:
        print(f"- image-only, rationale not extracted: {section or '(untitled)'}")
    retire = "would delete" if report.dry_run else "deleted"
    for key in report.retired:
        print(f"- {retire} {key}: its reasoning is no longer in the document")
    for pair in report.replaced:
        print(f"- {retire} {pair}: its reasoning is recorded under the new key")
    tag = "would tag" if report.dry_run else "tagged"
    for key in report.linked:
        print(f"- {tag} {key} with the repository")
    for line in report.refused + report.races:
        print(f"- not changed {line}")
    in_records = {r.memory_key for r in report.records}
    for key in report.flagged:
        if key not in in_records:
            tag = "would tag" if report.dry_run else "tagged"
            print(
                f"- {tag} {key} source-changed: confirmed or with a recorded outcome, "
                "its reasoning left the document"
            )
