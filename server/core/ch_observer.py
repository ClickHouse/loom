"""A one-shot HTTP session for native access-policy observations.

The ordinary driver's bootstrap can replay on another backend. This transport never
replays its unchecked POST; later requests can only use that same native session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from server.core.ch_errors import error_code
from server.core.ch_names import qualify

log = logging.getLogger(__name__)

# not a setting: ten seconds of proof plus room for bounded native cleanup.
_SESSION_TTL_S = 15
# not a setting: a request cannot outlive the ten-second authority proof.
_REQUEST_TIMEOUT_S = 10
# not a setting: all owned sessions close concurrently within one second.
_CLOSE_TIMEOUT_S = 1
# not settings: bounded replica discovery shares the ten-second authority proof.
_DISCOVERY_TIMEOUT_S = 5
_DISCOVERY_ATTEMPTS_PER_REPLICA = 8
# not a setting: a larger access catalog refuses instead of returning a partial proof.
_BODY_BYTES = 4 * 1024 * 1024
_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ObservationUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("ClickHouse observation authority could not be confirmed")


def _log_unavailable(proof_stage: str, error: Exception, http_status: int | None) -> None:
    # Response bodies and transport errors can contain policy text or credentials.
    try:
        log.warning(
            "Native observation refused: stage=%s exception=%s code=%s http_status=%s",
            proof_stage,
            type(error).__name__,
            error_code(error),
            http_status,
        )
    except Exception:  # noqa: BLE001 — diagnostics must not replace the authority refusal
        pass


def observer_origin(value: str, *, require_tls: bool) -> str:
    """An explicitly configured origin; never derive HTTP routes from native ports."""
    try:
        if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
            raise ObservationUnavailable()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (require_tls and parsed.scheme != "https")
            or parsed.port == 0
        ):
            raise ObservationUnavailable()
        host = parsed.hostname
        host = f"[{host}]" if ":" in host else host
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return urlunsplit((parsed.scheme, f"{host}:{port}", "/", "", ""))
    except (ValueError, TypeError):
        raise ObservationUnavailable() from None


def observer_origins(configuration: dict[str, Any], additional: tuple[str, ...]) -> tuple[str, ...]:
    secure = bool(configuration["secure"])
    host = str(configuration["host"])
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    primary = f"{'https' if secure else 'http'}://{host}:{configuration['port']}"
    origins = tuple(observer_origin(value, require_tls=secure) for value in (primary, *additional))
    if len(origins) > 33 or len(set(origins)) != len(origins):
        raise ObservationUnavailable()
    return origins


class PolicyObserver:
    """Owned by CHClient, which supplies the normal schema, pool and deadline guard."""

    def __init__(
        self,
        origin: str,
        user: str,
        password: str,
        database: str,
        guarded: Callable[
            [str, Callable[[], Awaitable[list[dict[str, Any]]]]],
            Awaitable[list[dict[str, Any]]],
        ],
        statement_settings: Callable[[str], dict[str, Any]],
    ) -> None:
        self.origin = origin
        self.user = user
        self.database = database
        self._guarded = guarded
        self._settings = statement_settings
        self._id = str(uuid.uuid4())
        self._checked = False
        self._failed = False
        self.server_uuid = ""
        self.roles: tuple[str, ...] = ()
        self._http = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(user, password),
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S),
        )

    async def _post(
        self, sql: str, parameters: dict[str, str] | None = None, *, closing: bool = False
    ) -> list[dict[str, Any]]:
        if self._failed and not closing:
            raise ObservationUnavailable()
        proof_stage = "prepare"
        http_status: int | None = None
        try:
            sql = qualify(sql)
            params = {
                **{k: str(v) for k, v in self._settings(sql).items()},
                "database": self.database,
                "session_id": self._id,
                "session_check": str(int(self._checked or closing)),
                "session_timeout": str(_SESSION_TTL_S),
                "wait_end_of_query": "1",
                "default_format": "JSON",
                "send_logs_level": "none",
                "log_queries_cut_to_length": "1",
                "log_formatted_queries": "0",
                "additional_table_filters": "{}",
                "additional_result_filter": "",
                "limit": "0",
                "offset": "0",
                "extremes": "0",
                "output_format_json_quote_64bit_integers": "0",
                "use_query_cache": "0",
                "enable_reads_from_query_cache": "0",
                "enable_writes_to_query_cache": "0",
                **{f"{kind}_overflow_mode": "throw" for kind in ("read", "result", "timeout")},
                "read_overflow_mode_leaf": "throw",
                "timeout_overflow_mode_leaf": "throw",
            }
            if closing:
                params["close_session"] = "1"
            for key, value in (parameters or {}).items():
                if not _PARAMETER.fullmatch(key) or not isinstance(value, str):
                    raise ObservationUnavailable()
                params[f"param_{key}"] = value

            async def send() -> list[dict[str, Any]]:
                nonlocal proof_stage, http_status
                # POST is excluded from aiohttp's automatic connection retry. Redirects
                # are separate and must not repeat an unchecked session bootstrap.
                proof_stage = "send"
                async with self._http.post(
                    self.origin, params=params, data=sql.encode(), allow_redirects=False
                ) as response:
                    proof_stage = "http_status"
                    http_status = response.status
                    if response.status != 200:
                        raise ObservationUnavailable()
                    proof_stage = "body"
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(body) + len(chunk) > _BODY_BYTES:
                            raise ObservationUnavailable()
                        body.extend(chunk)
                proof_stage = "json"
                result = json.loads(body, object_pairs_hook=_unique_object)
                proof_stage = "result"
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("meta"), list)
                    or not isinstance(result.get("data"), list)
                    or type(result.get("rows")) is not int
                    or result["rows"] != len(result["data"])
                    or "exception" in result
                    or not all(isinstance(row, dict) for row in result["data"])
                ):
                    raise ObservationUnavailable()
                columns = result["meta"]
                proof_stage = "columns"
                if (
                    not columns
                    or any(
                        not isinstance(column, dict)
                        or set(column) != {"name", "type"}
                        or not isinstance(column["name"], str)
                        or not isinstance(column["type"], str)
                        for column in columns
                    )
                    or len({column["name"] for column in columns}) != len(columns)
                    or any(
                        set(row) != {column["name"] for column in columns} for row in result["data"]
                    )
                ):
                    raise ObservationUnavailable()
                return cast(list[dict[str, Any]], result["data"])

            proof_stage = "admission"
            return await self._guarded(sql, send)
        except asyncio.CancelledError:
            self._failed = True
            raise
        except Exception as error:  # noqa: BLE001 — policy text and transport credentials must stay private
            self._failed = True
            _log_unavailable("close" if closing else proof_stage, error, http_status)
            raise ObservationUnavailable() from None

    async def bootstrap(self) -> None:
        if self._checked or self.server_uuid or self._failed:
            raise ObservationUnavailable()
        rows = await self._post(
            "SELECT toString(serverUUID()) AS server_uuid, currentUser() AS observer_user, "
            "enabledRoles() AS observer_roles"
        )
        try:
            if len(rows) != 1 or rows[0]["observer_user"] != self.user:
                raise ObservationUnavailable()
            identity = uuid.UUID(rows[0]["server_uuid"])
            roles = rows[0]["observer_roles"]
            if (
                identity.int == 0
                or not isinstance(roles, list)
                or not all(isinstance(role, str) for role in roles)
            ):
                raise ObservationUnavailable()
            self.server_uuid = str(identity)
            self.roles = tuple(sorted(roles))
            self._checked = True
        except Exception as error:  # noqa: BLE001 — malformed authority evidence must fail closed
            self._failed = True
            _log_unavailable("bootstrap_identity", error, 200)
            raise ObservationUnavailable() from None

    async def query_all(
        self, sql: str, parameters: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        if not self._checked or not sql.startswith("SELECT "):
            raise ObservationUnavailable()
        statement = (
            "SELECT currentUser() AS observer_user, enabledRoles() AS observer_roles, "
            + sql.removeprefix("SELECT ")
        )
        rows = await self._post(statement, parameters)
        try:
            if not rows or any(
                row.pop("observer_user") != self.user
                or not isinstance(row.get("observer_roles"), list)
                or not all(isinstance(role, str) for role in row["observer_roles"])
                or tuple(sorted(row.pop("observer_roles"))) != self.roles
                or row.get("server_uuid") != self.server_uuid
                for row in rows
            ):
                raise ObservationUnavailable()
        except Exception as error:  # noqa: BLE001 — malformed authority evidence must fail closed
            self._failed = True
            _log_unavailable("query_identity", error, 200)
            raise ObservationUnavailable() from None
        return rows

    async def policies(self) -> list[str]:
        if not self._checked:
            raise ObservationUnavailable()
        rows = await self._post("SHOW CREATE ROW POLICIES")
        try:
            if any(len(row) != 1 or not isinstance(next(iter(row.values())), str) for row in rows):
                raise ObservationUnavailable()
            return [next(iter(row.values())) for row in rows]
        except Exception as error:  # noqa: BLE001 — policy text must never appear in a refusal
            self._failed = True
            _log_unavailable("policy_result", error, 200)
            raise ObservationUnavailable() from None

    async def close(self) -> None:
        try:
            # Never create a session during cleanup, including an ambiguous bootstrap.
            # An unavailable native close leaves only the original bounded TTL.
            async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                await self._post("SELECT 1 AS closed", closing=True)
        finally:
            self._failed = True
            await self._http.close()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationUnavailable()
        result[key] = value
    return result


async def discover_policy_observers(
    origins: tuple[str, ...],
    servers: frozenset[str],
    create: Callable[[str], PolicyObserver],
    owned: list[PolicyObserver],
) -> tuple[PolicyObserver, ...]:
    """Keep one checked session per replica through the configured trusted origins."""
    try:
        valid = (
            bool(servers)
            and len(servers) <= 33
            and all(
                str(uuid.UUID(server)) == server and uuid.UUID(server).int for server in servers
            )
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid or not origins:
        raise ObservationUnavailable()
    selected: dict[str, PolicyObserver] = {}

    async def open_session(origin: str) -> None:
        observer = create(origin)
        owned.append(observer)
        await observer.bootstrap()
        if observer.server_uuid not in servers:
            raise ObservationUnavailable()
        if observer.server_uuid in selected:
            # A fresh session can land on a replica already covered by another session.
            await observer.close()
            owned.pop()
        else:
            selected[observer.server_uuid] = observer

    try:
        async with asyncio.timeout(_DISCOVERY_TIMEOUT_S):
            for origin in origins:
                await open_session(origin)
            for _ in range(len(servers) * _DISCOVERY_ATTEMPTS_PER_REPLICA):
                if selected.keys() == servers:
                    break
                await open_session(origins[0])
    except TimeoutError:
        raise ObservationUnavailable() from None
    if selected.keys() != servers:
        raise ObservationUnavailable()
    return tuple(selected[server] for server in sorted(servers))
