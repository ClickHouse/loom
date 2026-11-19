"""Data-only receipts for bounded explicit prompt refreshes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from typing import Any

from scripts.i18n import translate as t

PATH = "i18n/.pending/refresh.json"
OWNER = "scripts/i18n/translate.py"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def epoch() -> tuple[str, str]:
    prompt = json.dumps(
        [t.SYSTEM, [(lg.code, lg.english, lg.style, lg.banner) for lg in t.LANGUAGES]]
    )
    return digest(prompt), t.os.environ.get("LOOM_I18N_MODEL", t.DEFAULT_MODEL)


def parse(text: str) -> dict[str, Any]:
    if len(text.encode()) > 1024 * 1024:
        raise ValueError("oversized generated refresh manifest")
    data = json.loads(text)
    if (
        not isinstance(data, dict)
        or set(data) != {"owner", "schema", "prompt", "model", "languages", "completed", "complete"}
        or data["owner"] != OWNER
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or not isinstance(data["prompt"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", data["prompt"])
        or not isinstance(data["model"], str)
        or data["model"] not in t.PRICES
        or not isinstance(data["complete"], bool)
        or not isinstance(data["languages"], list)
        or not data["languages"]
        or any(not isinstance(code, str) for code in data["languages"])
        or data["languages"] != sorted(set(data["languages"]))
        or not set(data["languages"]) <= {lg.code for lg in t.LANGUAGES}
        or not isinstance(data["completed"], dict)
    ):
        raise ValueError("invalid generated refresh manifest")
    for path, proof in data["completed"].items():
        parts = path.split("/", 2)
        if (
            len(parts) != 3
            or parts[0] != "i18n"
            or parts[1] not in data["languages"]
            or not t.lifecycle.source_path(parts[2])
            or path != t.target_path(parts[1], parts[2])
            or not isinstance(proof, dict)
            or set(proof) != {"blob", "sha256", "base"}
            or not isinstance(proof["base"], str)
            or (proof["base"] != "" and not re.fullmatch(r"[0-9a-f]{64}", proof["base"]))
            or any(
                not isinstance(proof[key], str) or not re.fullmatch(pattern, proof[key])
                for key, pattern in (("blob", r"[0-9a-f]{40}"), ("sha256", r"[0-9a-f]{64}"))
            )
        ):
            raise ValueError("invalid generated refresh completion")
    return data


def read() -> dict[str, Any] | None:
    path = t.ROOT / PATH
    if not path.exists() and not path.is_symlink():
        return None
    if not t.lifecycle.regular(PATH) or path.stat().st_size > 1024 * 1024:
        raise ValueError("refusing nonregular or oversized refresh manifest")
    return parse(path.read_text())


def save(data: dict[str, Any]) -> None:
    read()  # Refuse an existing file that the generator does not own.
    path = t.ROOT / PATH
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("refusing symlink refresh destination")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = stream.name
        try:
            stream.write(json.dumps(parse(json.dumps(data)), sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def plan(every: bool = False, codes: list[str] | None = None) -> dict[str, Any] | None:
    old = read()
    if old and merged(old):
        old = None
    if not old and not every:
        return None
    prompt, model = epoch()
    languages = set(old["languages"] if old else [])
    if every:
        languages.update(codes or [lg.code for lg in t.LANGUAGES])
    current = old and (old["prompt"], old["model"]) == (prompt, model)
    return {
        "owner": OWNER,
        "schema": 1,
        "prompt": prompt,
        "model": model,
        "languages": sorted(languages),
        "completed": old["completed"] if current else {},
        "complete": old["complete"] if current else False,
    }


def published_hash(path: str) -> str:
    return digest((t.ROOT / path).read_text()) if t.lifecycle.regular(path) else ""


def base_matches(path: str, data: dict[str, Any]) -> bool:
    proof = data["completed"].get(path)
    return bool(proof and published_hash(path) in {proof["base"], proof["sha256"]})


def proof_matches(
    path: str,
    text: str,
    data: dict[str, Any],
    *,
    replacement: bool = True,
) -> bool:
    proof = data["completed"].get(path)
    source = path.split("/", 2)[2]
    return bool(
        proof
        and source in t.sources()
        and proof["blob"] == t.blob(source)
        and proof["sha256"] == digest(text)
        and (not replacement or base_matches(path, data))
        and not t.lifecycle.candidate_problems(path, text)
    )


def candidate_exists(path: str, data: dict[str, Any]) -> bool:
    return any(
        t.lifecycle.regular(candidate)
        and proof_matches(path, (t.ROOT / candidate).read_text(), data, replacement=False)
        for candidate in (path, t.lifecycle.checkpoint_path(path))
    )


def done(path: str, data: dict[str, Any]) -> bool:
    return base_matches(path, data) and candidate_exists(path, data)


def blocked(path: str, data: dict[str, Any] | None) -> bool:
    return bool(data and not base_matches(path, data) and candidate_exists(path, data))


def work(
    data: dict[str, Any] | None,
    *,
    include_blocked: bool = False,
) -> list[tuple[str, t.Language]]:
    if data is None:
        return []
    return [
        (source, lg)
        for source in t.sources()
        for lg in t.LANGUAGES
        if lg.code in data["languages"]
        and not done(t.target_path(lg.code, source), data)
        and (include_blocked or not blocked(t.target_path(lg.code, source), data))
    ]


def record(source: str, lang: t.Language, base: str) -> None:
    data = plan()
    if data is None or lang.code not in data["languages"]:
        return
    path = t.target_path(lang.code, source)
    checkpoint = t.lifecycle.checkpoint_path(path)
    if not t.lifecycle.regular(checkpoint):
        raise ValueError(f"{path}: refresh did not produce a verified checkpoint")
    text = (t.ROOT / checkpoint).read_text()
    if t.lifecycle.candidate_problems(path, text):
        raise ValueError(f"{path}: refresh checkpoint failed validation")
    data["completed"][path] = {"blob": t.blob(source), "sha256": digest(text), "base": base}
    data["complete"] = not work(data, include_blocked=True)
    save(data)


def rebase_readme_proof_data(data: dict[str, Any], changed: dict[str, tuple[str, str]]) -> bool:
    """Follow only verified generator-owned README metadata changes."""
    updated = False
    for path, (before, after) in changed.items():
        published = t.lifecycle.publication_path(path)
        proof = data["completed"].get(published)
        old_stamp, new_stamp = t.STAMP.match(before), t.STAMP.match(after)
        if not proof or not old_stamp or not new_stamp or proof["blob"] != old_stamp[2]:
            continue
        if proof["sha256"] == digest(before):
            proof.update(blob=new_stamp[2], sha256=digest(after))
            updated = True
        if path == published and proof["base"] == digest(before):
            proof["base"] = digest(after)
            updated = True
    return updated


def rebase_readme_proofs(changed: dict[str, tuple[str, str]]) -> None:
    data = read()
    if data is not None and rebase_readme_proof_data(data, changed):
        save(data)


def rebase_imported_readme_proofs(text: str, changed: dict[str, tuple[str, str]]) -> str:
    data = parse(text)
    if not rebase_readme_proof_data(data, changed):
        return text
    return json.dumps(parse(json.dumps(data)), sort_keys=True) + "\n"


def merged(data: dict[str, Any]) -> bool:
    if not data["complete"] or (data["prompt"], data["model"]) != epoch():
        return False
    expected = {
        t.target_path(lg.code, source)
        for source in t.sources()
        for lg in t.LANGUAGES
        if lg.code in data["languages"]
    }
    if not expected <= data["completed"].keys():
        return False
    for path, proof in data["completed"].items():
        if path.split("/", 2)[2] not in t.sources():
            continue
        for candidate in (path, t.lifecycle.checkpoint_path(path)):
            original = subprocess.run(
                ["git", "show", f"HEAD:{candidate}"], cwd=t.ROOT, capture_output=True, text=True
            )
            if original.returncode == 0 and digest(original.stdout) == proof["sha256"]:
                break
        else:
            return False
    return True


def clear_merged() -> None:
    data = read()
    if data and merged(data):
        (t.ROOT / PATH).unlink()


def import_receipt(text: str, blobs: dict[str, str]) -> set[str]:
    incoming = parse(text)
    current = (incoming["prompt"], incoming["model"]) == epoch()
    kept = {}
    for path, proof in incoming["completed"].items():
        source = path.split("/", 2)[2]
        if not current or source not in t.sources() or proof["blob"] != t.blob(source):
            continue
        if not any(
            candidate in blobs
            and proof_matches(path, blobs[candidate], incoming, replacement=False)
            for candidate in (path, t.lifecycle.checkpoint_path(path))
        ):
            raise ValueError(f"{path}: refresh receipt has no matching verified output")
        kept[path] = proof
    local = plan()
    prompt, model = epoch()
    data = {
        "owner": OWNER,
        "schema": 1,
        "prompt": prompt,
        "model": model,
        "languages": sorted(set(incoming["languages"]) | set(local["languages"] if local else [])),
        "completed": {**(local["completed"] if local else {}), **kept},
        "complete": False,
    }
    save(data)
    clear_merged()
    return set(kept) if read() else set()


def finish() -> None:
    data = plan()
    if data is not None:
        complete = not work(data, include_blocked=True)
        if data["complete"] != complete:
            data["complete"] = complete
            save(data)
