"""ui.vitals — the dashboard reports its own paint.

One call per page view from the dashboard, two seconds after the view
settled or when the viewer leaves it: the route, the paint and long-task
times the browser measured, the node and call counts, the bundle's build.
The ledger row the transport writes for every operation is the record;
deploy-delta reads those rows per route, before and after a swap. Dashboard
only: hidden from the MCP listing, experimental maturity, reached only from
the dashboard source (`dashboard`).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class UiVitalsInput(BaseModel):
    org: str
    namespace: str
    agent: str = "loom-ui"
    # The dashboard route id (`memories`, `knowledge`, `admin/usage`...).
    route: str = Field(min_length=1, max_length=64)
    # Largest contentful paint of the first view of the session, ms; None on
    # a client-side navigation (the browser reports LCP once per document).
    lcp_ms: int | None = Field(default=None, ge=0, le=600_000)
    # Main-thread long tasks (>50 ms) summed over the view, ms.
    long_task_ms: int = Field(default=0, ge=0, le=600_000)
    # The longest interaction of the view (an
    # event-timing entry's duration — input delay + processing +
    # presentation), ms; None when none reached the client's threshold.
    inp_ms: int | None = Field(default=None, ge=0, le=600_000)
    # Elements in the document when the view was flushed (the count of `*`).
    dom_nodes: int = Field(default=0, ge=0, le=1_000_000)
    # /v1 calls the view issued and the span from the first request's start
    # to the last response's end, ms.
    api_calls: int = Field(default=0, ge=0, le=10_000)
    # A view held open for an hour of polls spans
    # more than 600 s. A day is the ceiling; the client clamps as well.
    api_ms: int = Field(default=0, ge=0, le=86_400_000)
    # The server build the bundle was served by (health's build_commit).
    build: str = Field(default="", max_length=40)
    # True for the first view of the session (a document load), False for a
    # navigation inside the SPA.
    cold: bool = True


class UiVitalsOutput(BaseModel):
    # Always true: the vitals ride the ledger row the transport writes for the call.
    recorded: bool = True
    # The route id the call reported (echoes the request).
    route: str = ""
