"""`loom import-brain`: a markdown agent "brain" directory → Loom."""

from __future__ import annotations

import argparse
import logging
from typing import Any

from server.cli.import_recall import _make_writer, _OpHandlers

log = logging.getLogger(__name__)

# --- import-brain: migrate a markdown agent "brain" into loom -----------------
# MEMORY.md (always-injected HOT layer) -> profile.set sections; memory/contexts/
# *.md (read-on-demand WARM layer) -> knowledge.add docs. Completes the migration
# story alongside import-recall (the external vector-recall sqlite DEEP layer).


def _brain_slug(heading: str) -> str:
    s = "".join(c.lower() if c.isalnum() else "-" for c in heading.strip())
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:64] or "section"


def _brain_sections(md_text: str) -> list[tuple[str, str]]:
    """Split a MEMORY.md into (label, value) HOT sections by level-2 (##)
    headings. Text before the first ## becomes the 'overview' section. Empty
    sections are dropped."""
    out: list[tuple[str, str]] = []
    cur_label = "overview"
    cur: list[str] = []

    def _flush() -> None:
        if not cur:
            return
        has_heading = cur[0].startswith("## ")
        # Drop a section that is only its heading (no content beyond the title).
        content = "\n".join(cur[1:] if has_heading else cur).strip()
        if content:
            out.append((cur_label, "\n".join(cur).strip()))

    for line in md_text.splitlines():
        if line.startswith("## ") and not line.startswith("###"):
            _flush()
            cur_label = _brain_slug(line[3:])
            cur = [line]
        else:
            cur.append(line)
    _flush()
    return out


def _brain_context_docs(contexts_dir: str) -> list[tuple[str, str, str]]:
    """Each memory/contexts/*.md -> (source_uri, title, content). Missing dir
    yields []."""
    import os

    out: list[tuple[str, str, str]] = []
    if not os.path.isdir(contexts_dir):
        return out
    for name in sorted(os.listdir(contexts_dir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(contexts_dir, name)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read().strip()
        except OSError:
            continue
        if content:
            out.append((f"contexts/{name}", name[:-3], content))
    return out


def _brain_inproc_ops() -> _OpHandlers:
    from server.knowledge.api.models_knowledge import KnowledgeAddInput
    from server.knowledge.app.writes import handle_knowledge_add
    from server.memory.api.models_profile import ProfileSetInput
    from server.memory.app.profile import handle_profile_set

    return {
        "profile.set": lambda p: handle_profile_set(ProfileSetInput(**p)),
        "knowledge.add": lambda p: handle_knowledge_add(KnowledgeAddInput(**p)),
    }


async def _brain_make_writer(args: argparse.Namespace) -> tuple[Any, Any]:
    """import-brain's writer: profile.set + knowledge.add (a whole document
    per call, so the HTTP timeout is longer)."""
    return await _make_writer(args, _brain_inproc_ops, timeout_s=60.0)


def _cmd_import_brain(args: argparse.Namespace) -> int:
    import asyncio
    import os

    brain_dir = args.dir
    memory_md = os.path.join(brain_dir, "MEMORY.md")
    contexts_dir = os.path.join(brain_dir, "memory", "contexts")

    sections: list[tuple[str, str]] = []
    if os.path.isfile(memory_md):
        with open(memory_md, encoding="utf-8", errors="replace") as fh:
            sections = _brain_sections(fh.read())
    else:
        log.warning("import-brain: no MEMORY.md in %s — skipping HOT layer", brain_dir)
    docs = _brain_context_docs(contexts_dir)

    print(
        f"import-brain → {args.org}/{args.namespace}: "
        f"{len(sections)} HOT section(s) → profile, {len(docs)} WARM doc(s) → knowledge"
    )
    if args.dry_run:
        for label, val in sections:
            print(f"  + profile.set {label} ({len(val)} chars)")
        for uri, _title, content in docs:
            print(f"  + knowledge.add {uri} ({len(content)} chars)")
        print("(dry-run; no writes)")
        return 0

    async def _run() -> int:
        writer, closer = await _brain_make_writer(args)
        ok = 0
        fail = 0
        try:
            for label, val in sections:
                try:
                    await writer(
                        "profile.set",
                        {
                            "org": args.org,
                            "namespace": args.namespace,
                            "agent": args.agent,
                            "label": label,
                            "value": val,
                            "char_limit": max(2000, len(val) + 16),
                        },
                    )
                    ok += 1
                except Exception as e:  # noqa: BLE001 — one section must not sink the rest
                    fail += 1
                    log.warning("import-brain: profile.set %s failed: %s", label, e)
            for uri, title, content in docs:
                try:
                    await writer(
                        "knowledge.add",
                        {
                            "org": args.org,
                            "namespace": args.namespace,
                            "source_uri": uri,
                            "content": content,
                            "title": title,
                        },
                    )
                    ok += 1
                except Exception as e:  # noqa: BLE001 — one document must not sink the rest
                    fail += 1
                    log.warning("import-brain: knowledge.add %s failed: %s", uri, e)
        finally:
            await closer()
        print(f"import-brain: wrote {ok} item(s), {fail} failed")
        return 1 if fail and ok == 0 else 0

    return asyncio.run(_run())


def _add_brain_import_parser(sub: Any) -> None:
    """The `import-brain` subcommand."""
    p_brain = sub.add_parser(
        "import-brain",
        help="Import a markdown agent 'brain' directory into Loom: MEMORY.md "
        "(always-injected HOT layer) → profile.set sections; memory/contexts/"
        "*.md (read-on-demand WARM layer) → knowledge.add docs. Completes the "
        "migration alongside import-recall (the deep vector layer). Missing "
        "files are skipped, never crash.",
    )
    p_brain.add_argument(
        "--dir",
        required=True,
        help="Path to the workspace/brain dir (contains MEMORY.md "
        "and optionally memory/contexts/*.md)",
    )
    p_brain.add_argument("--org", required=True)
    p_brain.add_argument("--namespace", required=True)
    p_brain.add_argument(
        "--agent",
        default="default",
        help="Loom agent id for the profile (HOT) sections (default: %(default)r)",
    )
    p_brain.add_argument(
        "--base-url",
        default=None,
        help="POST loom ops to a running server instead of "
        "writing in-process against the configured CH.",
    )
    p_brain.add_argument(
        "--token",
        default=None,
        help="JWT for --base-url mode (else $LOOM_API_KEY, ~/.loom/config.json or ~/.loom/token).",
    )
    p_brain.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + plan + print the section/doc summary, no writes.",
    )
    p_brain.set_defaults(func=_cmd_import_brain)
