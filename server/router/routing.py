"""Which cell a request goes to (docs/architecture/cells.md, "The router", rules 1 to 4).

Pure: `Table` is built from the directory's cells and host aliases, and `decide` reads
only the request's host, path, `Authorization` header and, for a token revocation, the
revoked token. A token's `iss` is read unverified; the cell it is sent to verifies it.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from server.core.cell import normalise_host

log = logging.getLogger("loom.router")
# A label `inet_aton` reads as a number (decimal, octal or hex).
_NUMBER = re.compile(r"0[xX][0-9a-fA-F]*|[0-9]+")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?"
# A DNS name (IDNA-encoded, a fully qualified one's final dot kept) or an IPv4 address, or
# an IPv6 address (httpx drops its brackets).
_HOST = re.compile(rf"(?:{_LABEL}\.)*{_LABEL}\.?|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*")


@dataclass(frozen=True)
class CellRoute:
    """A cell as routing needs it: its name, its directory state, and the address
    inside the deployment the router forwards to (never its public host)."""

    cell: str
    state: str
    upstream: str


@dataclass(frozen=True)
class Table:
    shared: frozenset[str]  # the shared host and its aliases
    by_host: dict[str, CellRoute] = field(default_factory=dict)  # each cell's own host
    by_issuer: dict[str, CellRoute] = field(default_factory=dict)
    first: CellRoute | None = None
    passthrough: str = ""  # rule 4's cell before the first cell is registered

    @property
    def first_serving(self) -> bool:
        return self.first is not None and self.first.state == "serving"

    def rule4(self) -> str:
        """The first cell's address once it is registered (prepare writes the pass-through
        upstream into its row), else the pass-through upstream."""
        return self.first.upstream if self.first else self.passthrough


@dataclass(frozen=True)
class Decision:
    upstream: str = ""  # where to forward, or ""
    status: int = 0  # else the refusal's status
    reason: str = ""


def build(
    cells: Iterable[dict[str, Any]],
    aliases: Iterable[tuple[str, str]],
    *,
    shared_host: str,
    passthrough: str,
) -> Table:
    """The table from the directory's `cells` rows (cell, host, state, is_first,
    issuers, upstream_url) and `hosts` rows (host, cell)."""
    routes: dict[str, CellRoute] = {}
    by_host: dict[str, CellRoute] = {}
    by_issuer: dict[str, CellRoute] = {}
    shared_issuers: set[str] = set()
    first: CellRoute | None = None
    for row in cells:
        upstream = str(row["upstream_url"] or "")
        if upstream and not usable_upstream(upstream):
            log.warning(
                "routing: cell %s has an upstream that is no http(s)://host[:port]", row["cell"]
            )
            upstream = ""  # answered as a cell with none: 503
        route = CellRoute(str(row["cell"]), str(row["state"]), upstream)
        routes[route.cell] = route
        by_host[normalise_host(str(row["host"]))] = route
        for issuer in row["issuers"] or ():
            if by_issuer.setdefault(str(issuer), route) is not route:
                shared_issuers.add(str(issuer))
        if row["is_first"]:
            first = route
    shared = {normalise_host(shared_host)} if shared_host else set()
    for host, cell in aliases:
        if first is not None and cell == first.cell:
            shared.add(normalise_host(host))
    if first is not None:
        shared.update(h for h, r in by_host.items() if r is first)
    shared.discard("")
    for issuer in shared_issuers:
        # Attach refuses an issuer two cells list; a directory that has one anyway
        # routes it nowhere rather than to whichever row it read first.
        log.warning("routing: issuer %s is listed by more than one cell; refused", issuer)
        del by_issuer[issuer]
    return Table(frozenset(shared), by_host, by_issuer, first, passthrough)


def usable_upstream(url: str) -> bool:
    """Whether `url` is `http(s)://host[:port]` (a trailing `/` at most), the only shape
    the forward appends a request's path to, read as httpx reads it: the forward sends
    with httpx. The host is a DNS name or an IP address, and a port 1 to 65535 in ASCII
    digits (httpx would take `-1`, `1_0` or another script's digits)."""
    if not url.isprintable() or "?" in url or "#" in url:
        return False
    try:
        u = httpx.URL(url)
        port = urlsplit(url).port
        # A non-ASCII IPv6 zone id passes httpx's parse and fails here (a ValueError).
        host = u.raw_host.decode("ascii")
    except (httpx.InvalidURL, ValueError):
        return False
    if port == 0:
        return False
    # A host of numbers (`10.1`, `0xa.0.0.1`, `010.0.0.1`, `10.0.0.1.`) resolves as an
    # IPv4 address: it is one only in dotted decimal, so one address has one spelling.
    if all(_NUMBER.fullmatch(label) for label in host.removesuffix(".").split(".")):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return False
    return (
        u.scheme in ("http", "https")
        and _HOST.fullmatch(host) is not None
        and u.path in ("", "/")
        and not u.userinfo
    )


def usable_host(host: str) -> bool:
    """Whether `host`, as `normalise_host` writes it, may be a cell's or the shared host:
    the address rule, label by label, and an IPv6 address only in its compressed spelling
    (as `normalise_host` leaves it as given), so one address has one directory row."""
    if not host or not usable_upstream(f"https://{host}"):
        return False
    if not host.startswith("["):
        return True
    bare = host[1:].split("]", 1)[0]
    return ipaddress.ip_address(bare).compressed == bare


def issuer(token: str) -> str:
    """The `iss` claim of a JWT, read unverified; '' for anything that is not one."""
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError, RecursionError):  # a payload nested past the parser
        return ""
    value = claims.get("iss") if isinstance(claims, dict) else None
    return value if isinstance(value, str) else ""


def bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def reads_revoked(table: Table, host: str, path: str) -> bool:
    """Whether the request is a revocation that goes by the revoked token's issuer, so
    its body is read: only on the shared host once the first cell serves."""
    return (
        path.rstrip("/") == "/oauth/revoke"
        and normalise_host(host) in table.shared
        and table.first_serving
    )


def decide(
    table: Table, *, host: str, path: str, authorization: str = "", revoked: str = ""
) -> Decision:
    """Rule 1 by host, 2 SCIM, 3 by issuer, 4 the first cell; `revoked` is the token a
    `POST /oauth/revoke` names."""
    h = normalise_host(host)
    if h not in table.shared:
        route = table.by_host.get(h)
        if route is None:
            return Decision(status=421, reason=f"no cell answers for host {h or host!r}")
        return _to(route.upstream, f"cell {route.cell!r} has no upstream address yet")
    # The shared host. Until the first cell serves, rule 4's cell verifies everything.
    if not table.first_serving:
        return _to(table.rule4(), _rule4_missing(table))
    if path.rstrip("/") == "/oauth/revoke":
        iss = issuer(revoked)
    else:
        iss = issuer(bearer(authorization))
        scim = path == "/scim/v2" or path.startswith("/scim/v2/")
        if scim and not (path.startswith("/scim/v2/Groups") and iss):
            iss = ""  # SCIM on the shared host is the first cell's
    if iss:
        route = table.by_issuer.get(iss)
        if route is None:
            return Decision(status=401, reason="no cell accepts this token's issuer")
        return _to(route.upstream, f"cell {route.cell!r} has no upstream address yet")
    return _to(table.rule4(), _rule4_missing(table))


def _rule4_missing(table: Table) -> str:
    if table.first is not None:
        return f"the first cell {table.first.cell!r} has no upstream address yet"
    return "the router has no upstream for the shared host (LOOM_ROUTER_UPSTREAM_URL)"


def _to(upstream: str, missing: str) -> Decision:
    if not upstream:
        return Decision(status=503, reason=missing)
    return Decision(upstream=upstream.rstrip("/"))
