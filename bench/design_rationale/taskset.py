"""Load and check a task set directory (the schema is in this package's README).

A task set never lives in the repository except the synthetic `example/`: the real one
is private material read from a directory given on the command line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("A", "B", "C")
# Each detector type and the keys it needs (`path_glob` and `to` are optional).
DETECTOR_TYPES = {
    "file_touched": ("paths",),
    "diff_added_regex": ("pattern",),
    "diff_removed_regex": ("pattern",),
    "setting_default_changed": ("setting", "from"),
    "judge": ("rubric",),
}
C_SUBTYPES = ("supersession", "abstention")


class TaskSetError(ValueError):
    pass


@dataclass
class Document:
    id: str
    source_uri: str
    title: str
    content: str
    # The id of the document this one revises: it is ingested later under the same
    # source_uri, so the earlier text is superseded.
    revision_of: str = ""


@dataclass
class GoldRow:
    memory_key: str
    value: str
    document: str
    section: str
    files: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    memory_type: str = "decision"
    pinned: bool = True
    # Written after the document revisions are ingested (a Revise of the same key).
    after_revision: bool = False
    # Its source section carries no extractable text.
    image_only: bool = False


@dataclass
class TaskSet:
    root: Path
    system: str
    documents: dict[str, Document]
    # The current row per key; `gold_writes` holds every row in write order, a key
    # written again after a revision included.
    gold: dict[str, GoldRow]
    tasks: dict[str, list[dict[str, Any]]]
    gold_writes: list[GoldRow] = field(default_factory=list)

    def all_tasks(self, only: set[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
        return [
            (kind, t)
            for kind in KINDS
            for t in self.tasks.get(kind, [])
            if not only or t["id"] in only
        ]

    def document_of(self, task: dict[str, Any]) -> Document:
        return self.documents[task["document"]]

    def task_paths(self, task: dict[str, Any]) -> list[str]:
        """The files an agent would edit for the task: its gold rows' files, then the
        paths its detectors name. The pre-edit surface is read for each."""
        paths: list[str] = []
        for key in task.get("gold_rows", []):
            paths += self.gold[key].files
        for d in task.get("detector", []):
            paths += d.get("paths", [])
        return list(dict.fromkeys(paths))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TaskSetError(f"missing {path.name} in {path.parent}") from None
    except json.JSONDecodeError as e:
        raise TaskSetError(f"{path.name} is not JSON: {e}") from None


def _section_of(source_resource_id: str, documents: dict[str, Document]) -> tuple[str, str]:
    """(document id, section) of a `knowledge:<ns>/<source_uri>#<section>` pointer. The
    namespace is the author's; the harness serves the document from its own."""
    rest = source_resource_id.removeprefix("knowledge:")
    _ns, _, uri_section = rest.partition("/")
    for d in sorted(documents.values(), key=lambda d: -len(d.source_uri)):
        if uri_section.startswith(d.source_uri) and not d.revision_of:
            return d.id, uri_section[len(d.source_uri) :].removeprefix("#")
    raise TaskSetError(f"source_resource_id {source_resource_id!r} names no document of the set")


def _documents(root: Path) -> dict[str, Document]:
    docs: dict[str, Document] = {}
    for entry in _read_json(root / "documents.json"):
        doc_id = entry["id"]
        path = root / entry["path"]
        if not path.is_file():
            raise TaskSetError(f"document {doc_id}: no file {entry['path']}")
        docs[doc_id] = Document(
            id=doc_id,
            source_uri=entry["source_uri"],
            title=entry.get("title", doc_id),
            content=path.read_text(encoding="utf-8"),
            revision_of=entry.get("revision_of", ""),
        )
    for d in docs.values():
        if d.revision_of:
            base = docs.get(d.revision_of)
            if base is None or base.source_uri != d.source_uri:
                raise TaskSetError(
                    f"document {d.id} revises {d.revision_of!r}: no such document "
                    "with the same source_uri"
                )
    return docs


def _gold(root: Path, documents: dict[str, Document]) -> list[GoldRow]:
    gold: list[GoldRow] = []
    for r in _read_json(root / "gold_rows.json"):
        if "source_resource_id" in r:
            doc_id, section = _section_of(r["source_resource_id"], documents)
        else:
            doc_id, section = r["document"], r.get("section", "")
        if doc_id not in documents:
            raise TaskSetError(f"gold row {r['memory_key']}: unknown document {doc_id!r}")
        gold.append(
            GoldRow(
                memory_key=r["memory_key"],
                value=r["value"],
                document=doc_id,
                section=section,
                files=list(r.get("files", [])),
                tags=list(r.get("tags", [])),
                memory_type=r.get("memory_type", "decision"),
                pinned=bool(r.get("pinned", True)),
                after_revision=bool(r.get("after_revision", False)),
                image_only=bool(r.get("image_only", False)),
            )
        )
    return gold


def _check_detector(tid: str, d: Any) -> None:
    if not isinstance(d, dict) or d.get("type") not in DETECTOR_TYPES:
        raise TaskSetError(f"task {tid}: a detector is an object with a known type")
    missing = [k for k in DETECTOR_TYPES[d["type"]] if k not in d]
    if missing:
        raise TaskSetError(f"task {tid}: a {d['type']} detector without {missing}")
    paths = d.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise TaskSetError(f"task {tid}: a detector's paths is a list of strings")
    for key in ("pattern", "path_glob", "setting", "rubric"):
        if key in d and not isinstance(d[key], str):
            raise TaskSetError(f"task {tid}: a detector's {key} is a string")
    for key in ("from", "to"):
        # Compared as one token after the setting name.
        if key in d and (
            not isinstance(d[key], str | int | float)
            or not str(d[key]).split()
            or len(str(d[key]).split()) != 1
        ):
            raise TaskSetError(f"task {tid}: a detector's {key} is one word or number")


def _check_task(kind: str, t: dict[str, Any], ts: TaskSet) -> None:
    tid = t.get("id", "?")
    if not isinstance(tid, str) or not tid or "/" in tid:
        # Answers are keyed <task>/arm<a>/s<k>.
        raise TaskSetError(f"task id {tid!r}: a non-empty string without '/'")
    if t.get("document") not in ts.documents:
        raise TaskSetError(f"task {tid}: unknown document {t.get('document')!r}")
    if not isinstance(t.get("gold_rows"), list):
        raise TaskSetError(f"task {tid}: gold_rows is a list (empty when nothing is recorded)")
    for key in t["gold_rows"]:
        if key not in ts.gold:
            raise TaskSetError(f"task {tid}: unknown gold row {key!r}")
    need = {
        "A": ("question", "gold_arguments", "rejected_options"),
        "B": ("task", "gold_constraints", "forbidden_options", "detector"),
        "C": ("question", "subtype"),
    }[kind]
    for name in need:
        if name not in t:
            raise TaskSetError(f"task {tid}: {kind} task without {name!r}")
    detectors = t.get("detector", [])
    if not isinstance(detectors, list):
        raise TaskSetError(f"task {tid}: detector is a list")
    for d in detectors:
        _check_detector(tid, d)
    if kind == "C":
        if t["subtype"] not in C_SUBTYPES:
            raise TaskSetError(f"task {tid}: subtype is one of {C_SUBTYPES}")
        if t["subtype"] == "supersession" and not (t.get("stale_claims") and t.get("current")):
            raise TaskSetError(
                f"task {tid}: a supersession task names its current and stale_claims"
            )


def load(root: Path) -> TaskSet:
    root = Path(root)
    documents = _documents(root)
    writes = _gold(root, documents)
    # A row written after the revisions is the current one for its key.
    gold = {r.memory_key: r for r in sorted(writes, key=lambda r: r.after_revision)}
    raw = _read_json(root / "tasks.json")
    tasks = {kind: list(raw.get(kind, [])) for kind in KINDS}
    ts = TaskSet(root, raw.get("system", "the system"), documents, gold, tasks, writes)
    seen: set[str] = set()
    for kind, t in ts.all_tasks():
        _check_task(kind, t, ts)
        if t["id"] in seen:
            raise TaskSetError(f"task id {t['id']} appears twice")
        seen.add(t["id"])
    return ts


def answer_key(tid: str, arm: int, sample: int) -> str:
    """An answer's key: `<task>/arm<a>/s<k>`."""
    return f"{tid}/arm{arm}/s{sample}"


def surface_key(tid: str, arm: int) -> str:
    """A surface's key: `<task>/arm<a>`, shared by the task's samples in that arm."""
    return f"{tid}/arm{arm}"


def parse_key(key: str) -> tuple[str, int]:
    """(task id, arm) of an answer or surface key; a task id may hold "arm" but never
    "/"."""
    tid, _, rest = key.rpartition("/arm")
    return tid, int(rest.split("/", 1)[0])
