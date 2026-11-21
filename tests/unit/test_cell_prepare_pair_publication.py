"""Normal preparation publishes a later cell's verified pair atomically."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from server.cli import cell_attach_pg, cell_control, cell_prepare
from server.core import cell
from server.router import app as router_app
from server.router import directory
from server.settings import settings
from tests.unit.test_cell_prepare import _args, _beat
from tests.unit.test_cell_register import OPERATOR, _CountingConnection, _Listener
from tests.unit.test_router_attach import _cell_row


class RegistrationConnection(_CountingConnection):
    """Apply the real INSERT's explicit values over the directory's NULL defaults."""

    def __init__(self, later):
        super().__init__(first="first" if later else None)
        self.row = None
        self.insert_sql = []

    async def execute(self, sql, *args):
        await super().execute(sql, *args)
        if not sql.startswith("INSERT INTO loom_router.cells "):
            assert sql.startswith(
                (
                    "SELECT pg_advisory_xact_lock(",
                    "INSERT INTO loom_router.hosts ",
                    "INSERT INTO loom_router.audit ",
                )
            ), sql
            return ""
        self.insert_sql.append(sql)
        parsed = re.fullmatch(r"INSERT INTO loom_router.cells \((.+)\) VALUES \((.+)\)", sql)
        assert parsed is not None, sql
        columns = [value.strip() for value in parsed[1].split(",")]
        values = [value.strip() for value in parsed[2].split(",")]
        assert len(columns) == len(values)
        self.row = _cell_row()
        for name, expression in zip(columns, values, strict=True):
            if re.fullmatch(r"\$[1-9][0-9]*", expression):
                self.row[name] = copy.deepcopy(args[int(expression[1:]) - 1])
            elif expression == "'attaching'":
                self.row[name] = "attaching"
            else:
                assert name == "mode_set_at" and expression == "now()", expression
        return ""

    async def fetchrow(self, sql, *args):
        if "WHERE is_first" in sql:
            return await super().fetchrow(sql, *args)
        assert sql.startswith("SELECT " + directory._CELL_COLUMNS + " FROM") or sql.startswith(
            "SELECT "
            + directory._CELL_COLUMNS
            + ", bucket, bucket_prefix, probe_url, upstream_url FROM"
        ), sql
        self.statements += 1
        if self.row is None:
            return None
        columns = [name.strip() for name in directory._CELL_COLUMNS.split(",")]
        if "bucket_prefix" in sql:
            columns += list(directory._REGISTERED)
        return {name: copy.deepcopy(self.row[name]) for name in columns}

    async def fetchval(self, sql, *args):
        assert sql.startswith(
            (
                "SELECT cell FROM loom_router.cells WHERE credential_sha256 = $1",
                "SELECT cell FROM loom_router.cells WHERE deployment_id = $1",
                "SELECT cell FROM loom_router.cells WHERE host = $1 ",
            )
        ), sql
        return await super().fetchval(sql, *args)

    async def fetch(self, sql, *args):
        from server.router.limits import MAX_BODY_BYTES, MAX_CELLS, MAX_CENSUS_BYTES

        assert sql == directory._REGISTRATION_RESERVATIONS
        assert args == (
            MAX_CELLS + 1,
            MAX_CELLS,
            MAX_CENSUS_BYTES,
            MAX_BODY_BYTES,
        )
        self.statements += 1
        assert self.first is not None
        return [{"cell": self.first, "current_fp": None, "next_fp": None, "fits": True}]


class LocalConnection:
    """Only native acceptance rows and the already-held backend's read/write I/O."""

    def __init__(self):
        self.first = None
        self.mode = ""
        self.own = None

    async def fetchval(self, sql):
        if sql == "SELECT deployment_id FROM loom.authority":
            return "dep-1"
        if sql == "SELECT mode FROM loom.tenancy_mode":
            return self.mode
        if sql == "SELECT count(*) FROM loom.tenancy":
            return 0
        assert sql == cell_attach_pg._HELD, sql
        return True

    async def fetchrow(self, sql):
        if sql == "SELECT is_first, cell FROM loom.cell_first":
            return copy.deepcopy(self.first)
        assert sql == "SELECT current_fp, next_fp FROM loom.cell_self", sql
        return copy.deepcopy(self.own)

    async def execute(self, sql, *args):
        if sql.startswith("INSERT INTO loom.cell_first "):
            self.first = dict(zip(("is_first", "cell"), args, strict=True))
        elif sql.startswith("INSERT INTO loom.tenancy_mode "):
            self.mode = args[0]
        elif sql.startswith("INSERT INTO loom.cell_self "):
            self.own = dict(zip(("current_fp", "next_fp"), copy.deepcopy(args), strict=True))
        else:
            assert sql.startswith(("SET LOCAL ", "SELECT pg_logical_emit_message(")), sql

    def transaction(self):
        class Transaction:
            async def start(self):
                return None

            async def commit(self):
                return None

        return Transaction()


@pytest.mark.parametrize("later", [True, False], ids=["later", "first-control"])
@pytest.mark.parametrize("boundary", ["native-request", "directory-row"])
async def test_normal_prepare_publishes_later_pair_before_local_acknowledgment(
    monkeypatch, later, boundary
):
    name = "eu-2" if later else "eu-1"
    host = "eu-2.loom.example" if later else "loom.example"
    monkeypatch.setattr(settings, "cell", name)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://cell.internal/loom")
    monkeypatch.setattr(settings, "router_operator_token", OPERATOR)
    monkeypatch.setattr(
        settings, "router_operator_sha256", hashlib.sha256(OPERATOR.encode()).hexdigest()
    )
    monkeypatch.setattr(settings, "router_shared_host", "loom.example")
    monkeypatch.setattr(settings, "router_upstream_probe_url", "http://upstream.internal:7790")
    monkeypatch.setattr(settings, "router_upstream_url", "http://upstream.internal:7777")
    monkeypatch.setattr(settings, "public_base_url", "https://" + host)
    monkeypatch.setattr(settings, "oauth_issuer", "https://" + host)
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    conn = RegistrationConnection(later)
    local = LocalConnection()
    listener = _Listener()
    sent = []
    replies = []

    @asynccontextmanager
    async def connection(**kwargs):
        yield conn

    monkeypatch.setattr(directory, "_connection", connection)
    # The actual register parser, preprobe refusal, both probes, transaction and readback run.
    instance = router_app.Router(probe_transport=httpx.MockTransport(listener))
    app_transport = httpx.ASGITransport(app=router_app.build_app(instance))

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            sent.append(json.loads(request.content))
            response = await app_transport.handle_async_request(request)
            await response.aread()
            replies.append(response.json())
            return response

    fields = cell.fingerprint("dep-1")
    beat = _beat("web", fields, on=name)

    async def beats(_client):
        return [beat]

    async def write_nonces(_client, nonces):
        listener.answer = {
            "deployment_id": "dep-1",
            "nonces": dict(nonces),
            "bucket": {"name": "", "prefix": ""},
        }

    async def delete_nonces(_client, nonces):
        assert nonces == listener.answer["nonces"]

    async def snapshot(*args, **kwargs):
        assert kwargs == {"method": "GET"}
        assert conn.row is not None
        return {**copy.deepcopy(conn.row), "placed_orgs": conn.row["placed"]}

    async def claim(_conn, _call, row):
        # Claim transport is outside this regression; no accepted pair is supplied here.
        return row

    async def run(work, **kwargs):
        return await work()

    monkeypatch.setattr(cell_prepare, "_read_beats", beats)
    monkeypatch.setattr(cell_prepare, "_write_nonces", write_nonces)
    monkeypatch.setattr(cell_prepare, "_delete_nonces", delete_nonces)
    monkeypatch.setattr(cell_control, "request", snapshot)
    monkeypatch.setattr(cell_control, "claim", claim)
    owner = SimpleNamespace(conn=local, transaction=run, call=run)
    args = _args(
        cell=name,
        host=host,
        probe_url="http://eu-2.internal:7790" if later else "",
        upstream_url="http://eu-2.internal:7777" if later else None,
    )
    result = await cell_prepare._prepare_locked(
        None, args, httpx.URL("http://router.internal"), Transport(), owner
    )
    assert result == 0
    assert len(sent) == len(replies) == len(conn.insert_sql) == 1
    assert local.own == {"current_fp": cell.as_value(fields), "next_fp": None}
    assert local.first == {"is_first": not later, "cell": name}
    assert local.mode == ("enforced" if later else "recording")
    if boundary == "native-request":
        if later:
            assert (sent[0].get("current_fp"), sent[0].get("next_fp")) == (
                local.own["current_fp"],
                local.own["next_fp"],
            )
        else:
            assert "current_fp" not in sent[0] and "next_fp" not in sent[0]
    elif later:
        assert (conn.row["current_fp"], conn.row["next_fp"]) == (
            local.own["current_fp"],
            local.own["next_fp"],
        )
        assert (replies[0]["current_fp"], replies[0]["next_fp"]) == (
            local.own["current_fp"],
            local.own["next_fp"],
        )
    else:
        assert conn.row["current_fp"] is None and conn.row["next_fp"] is None
        assert replies[0]["current_fp"] is None and replies[0]["next_fp"] is None
