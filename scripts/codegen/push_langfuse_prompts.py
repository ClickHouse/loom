#!/usr/bin/env python3
"""Register loom's system prompts in Langfuse and write the version lockfile.

The prompts stay Python constants; `server/observability/genai.py` links a
generation to a Langfuse prompt version by looking up `sha256(text)[:12]` of
the prompt it sent in the committed lockfile, with no runtime call.

    python scripts/codegen/push_langfuse_prompts.py          # plan against the lockfile, offline
    python scripts/codegen/push_langfuse_prompts.py --write  # push, then write the lockfile

`--write` needs LOOM_LANGFUSE_URL and LOOM_LANGFUSE_ADMIN_TOKEN
(`pk-lf-...:sk-lf-...`); there is no default host. A prompt whose text is
already the latest version in Langfuse is not pushed again, so a re-run is a
no-op. The lockfile is keyed by content hash: an edited, unpushed prompt is
left unlinked rather than linked to the wrong version.
`tests/unit/test_generation_content_and_prompts.py` fails while the lockfile
misses a registered prompt.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import write_atomic  # noqa: E402

# Inside the package: the Dockerfile copies `server` and `sdk` only, so a
# lockfile outside them is absent from the image.
LOCKFILE = ROOT / "server" / "observability" / "langfuse_prompts.lock.json"


def registry() -> dict[str, str]:
    """`langfuse_prompt_name -> system prompt text`.

    Imported lazily and only by this script, never by the server: the hot path
    resolves prompts by content hash and must not import these modules (several
    of which would create cycles) to do it.

    Prompts assembled at call time by `_build_synthesis_prompt`-style helpers are
    absent by nature. They still get `loom.prompt.sha` on their generations, so
    they remain groupable in ClickStack; they just have no Langfuse link until
    someone lifts them into a constant.
    """
    from server.code.app import answers_entry
    from server.code.app.index import describe
    from server.code.workers import summary as code_summary
    from server.knowledge.workers import conversation_titler
    from server.memory.app import extract as extract_svc
    from server.memory.app import period_digest as _period_digest
    from server.memory.app import rerank
    from server.memory.app import session_summary as sess
    from server.memory.domain import extraction_prompts as extract
    from server.memory.domain import search_prompts
    from server.memory.workers import entity_extractor, profile_consolidation
    from server.observability import retrieval_judge as judge

    # `wrap` is applied by the CALLER before the string reaches `complete()`, so
    # it must be applied here too: a lockfile of the bare constants would match
    # no generation at runtime. Identity is the text actually
    # sent, and for the wrapped ones that text includes the preamble.
    wrap = extract.with_strict_examples_preamble

    return {
        # search cascade
        "loom-hyde": search_prompts._HYDE_PROMPT,
        "loom-derived-intent": wrap(search_prompts._DERIVED_INTENT_PROMPT),
        "loom-iterative-judge": wrap(search_prompts._ITERATIVE_JUDGE_PROMPT),
        "loom-triple-extract": wrap(search_prompts._LLM_TRIPLE_PROMPT),
        "loom-verb-synonyms": wrap(search_prompts._VERB_SYNONYM_PROMPT),
        # The builtin:openai reranker's prompt, sent verbatim.
        "loom-builtin-rerank": rerank._RERANK_SYSTEM_PROMPT,
        # extraction
        "loom-extraction": extract._EXTRACTION_SYSTEM_PROMPT,
        "loom-judge-facts": wrap(extract._JUDGE_SYSTEM_PROMPT),
        # relation backfill, sent bare
        "loom-extract-relations": extract_svc._RELATION_BACKFILL_SYSTEM_PROMPT,
        # summarisation / workers
        "loom-session-summary": sess._SESSION_SUMMARY_SYSTEM,
        "loom-period-digest": _period_digest._PERIOD_DIGEST_SYSTEM,
        "loom-conversation-title": conversation_titler._TITLE_SYSTEM,
        "loom-profile-fold": profile_consolidation._FOLD_SYSTEM,
        "loom-entity-extract": wrap(entity_extractor._EXTRACT_PROMPT),
        # The judge that grades retrieval. Its own prompt is versioned too — a
        # changed judge changes every score it produces, which is exactly the kind
        # of shift you must be able to attribute.
        "loom-retrieval-judge": judge._JUDGE_SYSTEM,
        # code plane: answer synthesis, identifier proposal, per-file
        # description at ingest, per-file summary worker. Registering them
        # is what lets a code.ask quality shift be attributed to a prompt
        # version instead of a model or index change.
        "loom-code-answer": answers_entry._SYNTH_SYSTEM,
        "loom-code-query-expand": answers_entry._EXPAND_SYSTEM,
        "loom-code-describe": describe._DESCRIBE_SYSTEM,
        "loom-code-summary": code_summary._SYSTEM,
    }


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


class LangfusePrompts:
    """The slice of Langfuse's public prompt API this script uses."""

    def __init__(self, host: str, token: str) -> None:
        if ":" not in token:
            raise SystemExit("set LOOM_LANGFUSE_ADMIN_TOKEN='pk-lf-...:sk-lf-...'")
        self.host = host.rstrip("/")
        self._auth = "Basic " + base64.b64encode(token.encode()).decode()

    @classmethod
    def from_env(cls) -> LangfusePrompts:
        host = os.environ.get("LOOM_LANGFUSE_URL", "").strip()
        if not host:
            raise SystemExit("set LOOM_LANGFUSE_URL to the Langfuse host to push to")
        return cls(host, os.environ.get("LOOM_LANGFUSE_ADMIN_TOKEN", ""))

    def _call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        req = urllib.request.Request(
            self.host + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": self._auth, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300].decode(errors="replace")

    def existing_version(self, name: str, text: str) -> int | None:
        """The version whose content already equals `text`, if any.

        Checked so re-running is a no-op. Without it every run would mint a new
        version of every prompt and the lockfile would churn on every deploy,
        making "which version was live" unanswerable.
        """
        st, body = self._call("GET", f"/api/public/v2/prompts/{urllib.parse.quote(name)}")
        if st >= 300 or not isinstance(body, dict):
            return None
        # The single-prompt endpoint returns the production/latest version. Compare
        # its content; if it differs, a new version is needed.
        v = body.get("version")
        if str(body.get("prompt") or "") == text and isinstance(v, int):
            return v
        return None

    def create(self, name: str, text: str, sha: str) -> int:
        """Register `text` as a new production version; raises on a refusal."""
        st, body = self._call(
            "POST",
            "/api/public/v2/prompts",
            {
                "name": name,
                "type": "text",
                "prompt": text,
                "labels": ["production"],
                "commitMessage": f"loom sha {sha}",
            },
        )
        if st >= 300 or not isinstance(body, dict):
            raise RuntimeError(f"push failed: {st} {body}")
        version = body.get("version")
        if not isinstance(version, int):
            raise RuntimeError(f"no integer version returned: {body}")
        return version


def read_lockfile() -> dict[str, dict[str, Any]]:
    """The committed `sha -> {name, version}` map; empty when there is none."""
    if not LOCKFILE.is_file():
        return {}
    prompts = json.loads(LOCKFILE.read_text(encoding="utf-8")).get("prompts")
    return prompts if isinstance(prompts, dict) else {}


def plan(prompts: list[tuple[str, str]], lock: dict[str, dict[str, Any]]) -> list[str]:
    """One line per prompt saying whether the lockfile already links its
    current text, plus the lockfile entries no current prompt produces."""
    lines: list[str] = []
    current: set[str] = set()
    for name, text in prompts:
        sha = sha_of(text)
        current.add(sha)
        entry = lock.get(sha)
        if entry and entry.get("name") == name:
            lines.append(f"  = {name:<28} sha={sha} locked at v{entry.get('version')}")
        else:
            lines.append(f"  + {name:<28} sha={sha} not in the lockfile: --write registers it")
    for sha in sorted(set(lock) - current):
        lines.append(f"  - {lock[sha].get('name', '?'):<28} sha={sha} stale: would drop")
    return lines


def write_lockfile(lock: dict[str, dict[str, Any]]) -> bool:
    document = {
        "_about": (
            "sha256(system_prompt)[:12] -> the Langfuse prompt name and "
            "INTEGER version it was registered as. Written by "
            "scripts/codegen/push_langfuse_prompts.py; read by "
            "server/observability/genai.py to link generations to prompt "
            "versions with no runtime call to Langfuse."
        ),
        "_warning": (
            "langfuse.prompt.version MUST be an int and MUST be sent with "
            "langfuse.prompt.name. Probed against the live API: a string "
            "version, or a name with no version, causes Langfuse to DROP "
            "the whole observation silently."
        ),
        "prompts": lock,
    }
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    return write_atomic(LOCKFILE, text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help="push to LOOM_LANGFUSE_URL and write the lockfile; without it, print the plan only",
    )
    args = ap.parse_args(argv)

    prompts = sorted(registry().items())
    if not args.write:
        lines = plan(prompts, read_lockfile())
        print("\n".join(lines))
        todo = sum(not line.startswith("  =") for line in lines)
        print(
            f"\nplan only: {len(prompts)} prompts, {todo} lockfile change(s). --write applies it."
        )
        return 0

    langfuse = LangfusePrompts.from_env()
    print(f"pushing {len(prompts)} prompts to {langfuse.host}")
    lock: dict[str, dict[str, Any]] = {}
    created = unchanged = failed = 0
    for name, text in prompts:
        sha = sha_of(text)
        version = langfuse.existing_version(name, text)
        if version is not None:
            unchanged += 1
            print(f"  = {name:<28} sha={sha} already at v{version}")
        else:
            try:
                version = langfuse.create(name, text, sha)
            except RuntimeError as e:
                failed += 1
                print(f"  ! {name:<28} {e}", file=sys.stderr)
                continue
            created += 1
            print(f"  + {name:<28} sha={sha} -> v{version}")
        lock[sha] = {"name": name, "version": version}

    verb = "wrote" if write_lockfile(lock) else "unchanged:"
    print(
        f"\n{verb} {LOCKFILE.relative_to(ROOT)} — {len(lock)} entries "
        f"({created} new, {unchanged} unchanged, {failed} failed)"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
