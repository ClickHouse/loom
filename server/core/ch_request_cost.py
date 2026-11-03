"""What each statement of a request cost: its `query_id` inside the request,
and the response summary folded into the request's work bag and span."""

from __future__ import annotations

from typing import Any

from server.core import request_work


# A request statement's `query_id` is `<request_id>:<n>:<nonce>`: the request id
# from the query tag, n counting the request's statements and attempts, and the
# request's nonce (`RequestWork.ch_nonce`). A carried insert prefixes it with
# `loom-carry/<process>/`. A worker pass keeps the driver's own uuid.
def request_query_id(query_tag: str) -> str | None:
    """`<request_id>:<n>:<nonce>` for the next statement under `query_tag`, or
    None outside a request."""
    parts = query_tag.split(" ")
    if len(parts) < 2 or not parts[1]:
        return None
    seq = request_work.next_query_seq()
    if seq is None:
        return None
    n, nonce = seq
    return f"{parts[1]}:{n}:{nonce}"


_SUMMARY_SPAN_ATTRS = (
    ("read_rows", "ch.read_rows"),
    ("read_bytes", "ch.read_bytes"),
    ("elapsed_ms", "ch.elapsed_ms"),
    ("query_id", "ch.query_id"),
)


def note_result_summary(result: Any) -> dict[str, Any]:
    """After a statement returns, read the driver's response
    summary (`X-ClickHouse-Summary` — `read_rows`, `read_bytes`,
    `elapsed_ns`, `query_id`; on every response, no extra round trip) into the
    request's work bag and onto the enclosing `ch.*` span. A result without a
    summary (a `command()` that returned a scalar, a fake) is a no-op. Never
    raises; returns what it parsed."""
    summary = getattr(result, "summary", None)
    if not isinstance(summary, dict) or not summary:
        return {}
    try:
        parsed = request_work.note_ch_summary(summary)
    except Exception:  # noqa: BLE001 — telemetry never fails a statement
        return {}
    if not parsed:
        return {}
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is not None and span.is_recording():
            for key, attr in _SUMMARY_SPAN_ATTRS:
                if key in parsed:
                    span.set_attribute(attr, parsed[key])
    except Exception:  # noqa: BLE001 — telemetry never fails a statement
        pass
    return parsed


__all__ = ["note_result_summary", "request_query_id"]
