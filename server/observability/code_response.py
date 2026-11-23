"""Numeric delivery facts from code answers, without retaining their content."""

from __future__ import annotations

from typing import Any

_LINK_STATES = ("fresh", "moved", "changed", "unresolved", "missing")


def code_response_counts(result: dict[str, Any]) -> dict[str, int]:
    """Count the source and lessons in the answer, including repeated returned rows.

    Only the root and its definition/result rows carry bodies or lessons. Walking
    those known containers avoids treating examples or arbitrary metadata as source.
    Character counts use Python string length, not bytes or provider tokens.
    """
    rows = [result]
    for field in ("definitions", "results"):
        if isinstance(values := result.get(field), list):
            rows.extend(value for value in values if isinstance(value, dict))
    bodies = [row["body"] for row in rows if isinstance(row.get("body"), str) and row["body"]]
    lessons = [
        lesson
        for row in rows
        if isinstance(row.get("lessons"), list)
        for lesson in row["lessons"]
        if isinstance(lesson, dict)
    ]
    counts = {
        "source_bodies": len(bodies),
        "source_chars": sum(map(len, bodies)),
        "elided_bodies": sum(row.get("body_elided") is True for row in rows),
        "lesson_rows": len(lessons),
        "lesson_chars": sum(
            len(excerpt) for lesson in lessons if isinstance(excerpt := lesson.get("excerpt"), str)
        ),
        **{
            "lesson_" + state: sum(lesson.get("code_state") == state for lesson in lessons)
            for state in _LINK_STATES
        },
        "lesson_unclassified": sum(
            lesson.get("code_state") not in _LINK_STATES for lesson in lessons
        ),
    }
    return counts


def record_response_counts(projected: dict[str, str]) -> None:
    """Put the ledger's numeric delivery facts on its active OTel span too.

    Both trace exporters receive the same attributes. No payloads, memory keys or
    paths leave the request through this projection.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span.is_recording():
            return
        names = (
            "source_bodies",
            "source_chars",
            "elided_bodies",
            "lesson_rows",
            "lesson_chars",
            *("lesson_" + state for state in _LINK_STATES),
            "lesson_unclassified",
        )
        for name in names:
            if (value := projected.get("out_" + name)) is not None:
                span.set_attribute("loom.response.code." + name, int(value))
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        pass
