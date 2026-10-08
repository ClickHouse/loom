"""Every worked example in the extraction prompt cites its source message.

The extractor imitates the examples: when only a few example facts carry
`source_message_indices`, the model mostly omits the field. The examples are
the schema anchor, so this test keeps the field on every one of them and in
range of that example's input messages.
"""

from __future__ import annotations

import json
import re

from server.memory.domain.extraction_prompts import _EXTRACTION_SYSTEM_PROMPT

_MSG_RE = re.compile(r"^\s{2}\[(user|assistant|system)\]\s*(.*)$")


def _examples() -> list[tuple[int, dict]]:
    """Yield (message_count, parsed_output) per worked example."""

    lines = _EXTRACTION_SYSTEM_PROMPT.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith("EXAMPLES (schema anchors"))
    out: list[tuple[int, dict]] = []
    i = start
    while i < len(lines):
        if not lines[i].startswith("Input messages:"):
            i += 1
            continue
        n_msgs = 0
        j = i + 1
        while lines[j].strip() != "Output:":
            if _MSG_RE.match(lines[j]):
                n_msgs += 1
            j += 1
        k = j + 1
        buf: list[str] = []
        while k < len(lines) and not lines[k].startswith("EXAMPLE "):
            buf.append(lines[k])
            k += 1
        blob = "\n".join(buf)
        s = blob.find("{")
        # The facts array closes on its own line; `{"facts": []}` is one line.
        e = blob.find("\n]}", s)
        js = blob[s : e + 3] if e != -1 else blob[s : blob.find("}", s) + 1]
        out.append((n_msgs, json.loads(js)))
        i = k
    return out


def test_prompt_has_the_expected_number_of_examples() -> None:
    assert len(_examples()) >= 40


def test_every_example_fact_cites_an_in_range_source_message() -> None:
    missing: list[str] = []
    for n_msgs, obj in _examples():
        for fact in obj.get("facts", []):
            idx = fact.get("source_message_indices")
            ok = (
                isinstance(idx, list)
                and 1 <= len(idx) <= 3
                and all(isinstance(x, int) and 0 <= x < n_msgs for x in idx)
            )
            if not ok:
                missing.append(f"{fact.get('key')}: {idx!r} (messages={n_msgs})")
    assert not missing, "\n".join(missing)


def test_schema_marks_source_message_indices_required() -> None:
    assert '"source_message_indices": REQUIRED on every fact' in _EXTRACTION_SYSTEM_PROMPT
