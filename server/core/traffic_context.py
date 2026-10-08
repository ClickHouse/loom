"""The slot a request's traffic class is published on.

`server.observability.traffic_class` resolves the class and binds it here;
the ClickHouse client reads it to add the class's per-statement settings.
The value is `(class, source)`; `("", "")` outside a request.
"""

from __future__ import annotations

from contextvars import ContextVar

traffic_class_var: ContextVar[tuple[str, str]] = ContextVar("loom_traffic_class", default=("", ""))


__all__ = ["traffic_class_var"]
