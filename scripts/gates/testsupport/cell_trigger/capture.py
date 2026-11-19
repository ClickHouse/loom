"""Capture unchanged delivery intent and independently verify persisted rows."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import math
import os
import re
import socket
import stat
import struct
import sys
import uuid
from pathlib import Path

from fixture import (
    FILES,
    HERE,
    PROTOCOL,
    PROTOCOL_SHA,
    SPEC_FIELDS,
    digest,
    json_object,
    need,
    private_stream,
    read,
    ref,
    sha,
    source,
    validate,
    write,
)

LINE = 65536
ARM_BYTES = 32 * 1024 * 1024
TOTAL_BYTES = 320 * 1024 * 1024
ORDER = (True, False, False, True, True, False, False, True, True, False)
SUBJECT = "process:benchmark"


def protocol() -> dict:
    descriptor = os.open(HERE / PROTOCOL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_mode & 0o022 == 0
            and info.st_size <= 65536,
            "owned fixed protocol constant",
        )
        raw = stream.read(65537)
    need(
        len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == PROTOCOL_SHA,
        "fixed protocol bytes",
    )
    return json_object(raw)


def integer(value, low=0, high=2**32 - 1):
    need(type(value) is int and low <= value <= high, "typed bounded integer")
    return value


def identifier(value):
    if isinstance(value, uuid.UUID):
        value = str(value)
    need(
        type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0,
        "canonical UUID",
    )
    return value


def equal(left, right):
    need(
        json.dumps(left, sort_keys=True, allow_nan=False, separators=(",", ":"))
        == json.dumps(right, sort_keys=True, allow_nan=False, separators=(",", ":")),
        "exact typed equality",
    )


def instant(value, *, audit=False):
    if isinstance(value, str):
        need(
            re.fullmatch(
                r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)?", value
            ),
            "exact ISO datetime",
        )
        value = datetime.datetime.fromisoformat(value)
    need(type(value) is datetime.datetime, "typed datetime")
    value = (
        value.replace(tzinfo=datetime.UTC)
        if value.tzinfo is None
        else value.astimezone(datetime.UTC)
    )
    if audit:
        need(value.microsecond % 1000 == 0, "audit millisecond precision")
        return value.isoformat(timespec="microseconds")
    need(value.microsecond == 0 and 0 <= value.timestamp() < 2**32, "memory second precision/range")
    return value.replace(tzinfo=None).isoformat(timespec="seconds")


def hex64(value, *, native=False):
    if native and isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("ascii")
    need(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "typed hex64")
    return value


def payload(key):
    prefix = f"Cell trigger measurement {key}: "
    need(
        type(key) is str and key.isascii() and len(prefix) < 4096 and len(key) <= 128, "payload key"
    )
    return prefix + ("abcdefghijklmnopqrstuvwxyz " * 4096)[: 4096 - len(prefix)]


def content_hash(key):
    return hashlib.sha256(
        json.dumps(payload(key), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Codec:
    def __init__(self, specification):
        self.p = specification
        need(
            len(self.p["memory_fields"]) == 61
            and len(set(self.p["memory_fields"])) == 61
            and set(self.p["memory_types"]) == set(self.p["memory_fields"])
            and len(self.p["sparse_defaults"]) == 43
            and len(self.p["audit_fields"]) == 13,
            "closed protocol",
        )

    def row(self, original, *, origin="capture", comparison=False):
        need(
            type(original) is dict and set(original) <= set(self.p["memory_fields"]),
            "memory row fields",
        )
        value = dict(original)
        if origin == "pg":
            for key, default in self.p["sparse_defaults"].items():
                if key not in value:
                    value[key] = list(default) if isinstance(default, list) else default
        need(set(value) == set(self.p["memory_fields"]), "complete memory row")
        result = {}
        for key in self.p["memory_fields"]:
            item = value[key]
            kind = self.p["memory_types"][key]
            if kind.startswith("nullable_") and item is None:
                result[key] = None
                continue
            kind = kind.removeprefix("nullable_")
            if kind == "string":
                need(type(item) is str and len(item.encode()) <= 8192, "memory string")
            elif kind == "json_object_field":
                if origin == "ch":
                    need(type(item) is dict, "native CH JSON object")
                    parsed = json_object(json.dumps(item, allow_nan=False))
                else:
                    need(type(item) is str, "encoded object JSON")
                    parsed = json_object(item)
                need(parsed == {}, "primitive workload JSON object")
                if comparison:
                    item = parsed
            elif kind == "utc_second":
                item = instant(item)
            elif kind == "uuid":
                item = identifier(item)
            elif kind in {"array_uuid", "array_string"}:
                need(type(item) is list and len(item) <= 64, "typed array")
                if kind == "array_uuid":
                    item = [identifier(member) for member in item]
                else:
                    need(
                        all(type(member) is str and len(member.encode()) <= 512 for member in item),
                        "string array",
                    )
            elif kind == "hex64_fixedstring":
                item = hex64(item, native=origin == "ch")
            elif kind == "finite_exact_float32_not_bool":
                need(type(item) in {int, float} and math.isfinite(item), "finite Float32")
                rounded = struct.unpack("!f", struct.pack("!f", item))[0]
                need(rounded == item, "exact stored Float32")
                item = rounded
            elif kind == "positive_int63_not_bool":
                item = integer(item, 1, 2**63 - 1)
            elif kind == "uint8_0_or_1_not_bool":
                item = integer(item, 0, 1)
            elif kind == "uint32_not_bool":
                item = integer(item)
            else:
                need(False, "unknown protocol type")
            result[key] = item
        return result

    def audit(self, value, *, origin="capture"):
        need(
            type(value) is dict and set(value) == set(self.p["audit_fields"]),
            "complete audit fields",
        )
        result = {}
        for key in self.p["audit_fields"]:
            item = value[key]
            if key == "ts":
                item = instant(item, audit=True)
            elif key == "row_hash":
                item = hex64(item, native=origin == "ch")
            else:
                need(type(item) is str and len(item.encode()) <= 8192, "audit string")
            result[key] = item
        timestamp = (
            datetime.datetime.fromisoformat(result["ts"])
            .astimezone(datetime.UTC)
            .replace(tzinfo=None)
            .isoformat()
        )
        canonical = {
            key: result[key]
            for key in ("event_type", "org", "namespace", "actor", "subject", "request_id")
        }
        canonical["ts"] = timestamp
        actual = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        need(actual == result["row_hash"], "canonical raw-subject audit hash")
        return result

    def expected(self, row, audit, context, *, api, metadata, driver):
        key = row["memory_key"]
        equal(
            [
                row[k]
                for k in (
                    "org",
                    "namespace",
                    "agent",
                    "user_id",
                    "session_id",
                    "actor",
                    "source_class",
                )
            ],
            [context["org"], context["namespace"], "bench", "-", "-", SUBJECT, "asserted"],
        )
        need(
            row["memory_type"] == "semantic" and row["status"] == "active" and row["pinned"] == 0,
            "workload semantic/active",
        )
        need(row["confidence"] == 1.0, "workload confidence")
        for field, default in self.p["sparse_defaults"].items():
            equal(row[field], default)
        need(
            row["value"] == json.dumps(payload(key)) and row["content_sha256"] == content_hash(key),
            "exact stored payload/hash",
        )
        expected_subject = dict(
            memory_key=key,
            memory_id=row["memory_id"],
            version=row["version"],
            content_sha256=row["content_sha256"],
            source_class="asserted",
            status="active",
            pinned=False,
            tombstoned=False,
        )
        equal(json_object(audit["subject"]), expected_subject)
        need(
            audit["subject"] == json.dumps(expected_subject, sort_keys=True),
            "raw native subject spelling",
        )
        need(
            audit["event_type"] == "memory.revision"
            and audit["org"] == context["org"]
            and audit["namespace"] == context["namespace"]
            and audit["actor"] == SUBJECT,
            "audit event/scope/actor",
        )
        api_launch = context["_api_server"] if api else None
        instance = (
            api_launch["health_instance"] if api else driver["host"] + ":" + str(driver["pid"])
        )
        equal(
            {
                k: audit[k]
                for k in ("build_commit", "instance", "instance_label", "consumer", "token_id")
            },
            dict(
                build_commit=context["head"],
                instance=instance,
                instance_label=("iso:trigger:" + context["fixture_id"][:12])[:64],
                consumer="service-account" if api else "",
                token_id=metadata["summary"]["jti"] if api else "",
            ),
        )
        need(
            (
                type(audit["request_id"]) is str
                and audit["request_id"].isascii()
                and 0 < len(audit["request_id"]) <= 128
            )
            if api
            else audit["request_id"] == "",
            "audit request identity",
        )


def birth():
    pid = os.getpid()
    return dict(
        pid=pid,
        start_ticks=Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        uid=os.geteuid(),
        host=socket.gethostname(),
        environment_sha256=digest(dict(os.environ)),
    )


def line(value):
    raw = (
        json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode()
    need(len(raw) <= LINE, "JSONL line bound")
    return raw


class Capture:
    def __init__(self, context, directory, driver_birth, codec):
        self.s = context
        self.directory = directory
        self.birth = driver_birth
        self.codec = codec
        self.calls = 0
        self.total = 0
        need(
            directory == Path(context["workdir"]) / "delivery-intent" and not directory.exists(),
            "new exact intent directory",
        )
        directory.mkdir(mode=0o700)

    def header(self, arm):
        w = Path(self.s["workdir"])
        return dict(
            schema=1,
            kind="header",
            fixture_id=self.s["fixture_id"],
            arm=arm,
            on=ORDER[arm],
            batch_rows=500,
            expected_rows=2500,
            head=self.s["head"],
            tree=self.s["tree"],
            driver_sha256=self.s["source_hashes"]["driver"],
            capture_sha256=self.s["source_hashes"][FILES[3]],
            protocol_sha256=PROTOCOL_SHA,
            context_sha256=self.s["capture_context_sha256"],
            fixture_sha256=sha(w / "fixture.json"),
            launch_sha256=sha(w / "serving-cell/launch.json"),
            mint_metadata_sha256=self.s["mint_metadata"]["sha256"],
            birth=self.birth,
        )

    def delegate(self, original):
        async def build(args, batch):
            arm = self.calls
            need(arm < 10 and type(batch) is int and batch == 500, "exact build count/batch")
            self.calls += 1
            block = await original(args, batch)
            need(len(block.rows) == len(block.audits) == 2500, "exact delivery intent count")
            path = self.directory / f"arm-{arm}.jsonl"
            full = hashlib.sha256()
            prefix = hashlib.sha256()
            size = 0
            header = self.header(arm)
            with private_stream(path, output=True) as stream:
                for index in range(-1, 2501):
                    if index == -1:
                        value = header
                    elif index == 2500:
                        value = dict(
                            schema=1,
                            kind="trailer",
                            arm=arm,
                            count=2500,
                            prefix_sha256=prefix.hexdigest(),
                        )
                    else:
                        value = dict(
                            schema=1,
                            kind="delivery",
                            arm=arm,
                            index=index,
                            row=self.codec.row(block.rows[index]),
                            audit=self.codec.audit(block.audits[index]),
                        )
                    raw = line(value)
                    size += len(raw)
                    self.total += len(raw)
                    need(size <= ARM_BYTES and self.total <= TOTAL_BYTES, "intent byte bounds")
                    need(stream.write(raw) == len(raw), "complete intent write")
                    full.update(raw)
                    if index < 2500:
                        prefix.update(raw)
            write(
                self.directory / f"arm-{arm}-receipt.json",
                dict(
                    schema=1,
                    arm=arm,
                    bytes=size,
                    records=2502,
                    sha256=full.hexdigest(),
                    header=header,
                ),
            )
            return block

        return build


def mint_metadata(context, minted):
    w = Path(context["workdir"])
    with private_stream(w / "mint-summary.stdout") as stream:
        raw = stream.read(4097)
    need(len(raw) <= 4096 and raw.endswith(b"\n") and raw.count(b"\n") == 1, "mint stdout shape")
    summary = json_object(raw)
    need(set(summary) == set(protocol()["mint_summary_fields"]), "mint summary fields")
    need(type(summary["jti"]) is str and re.fullmatch(r"[a-f0-9]{32}", summary["jti"]), "mint jti")
    equal(
        {k: v for k, v in summary.items() if k != "jti"},
        dict(
            subject="benchmark",
            org=context["org"],
            namespaces=[context["namespace"]],
            roles=["writer"],
            ttl_days=1,
            is_service_account=True,
            read_scope="namespace",
            read_user_id="",
            traffic_class="",
            consumer="",
            token_path=str(w / "writer.jwt"),
        ),
    )
    with private_stream(w / "writer.jwt") as stream:
        token = stream.read(16385)
    need(
        0 < len(token) <= 16384 and token.endswith(b"\n") and token.count(b"\n") == 1,
        "private native bearer file",
    )
    born = ref(minted["launch_receipt"])
    terminal = ref(minted["terminal_receipt"])
    need(
        type(terminal["returncode"]) is int and terminal["returncode"] == 0, "mint terminal success"
    )
    equal(minted["source_sha256"], digest(context["source_hashes"]))
    equal(minted["cwd"], context["root"])
    expected = [
        context["python"],
        "-m",
        "server.cli",
        "mint-token",
        "--subject",
        "benchmark",
        "--org",
        context["org"],
        "--namespaces",
        context["namespace"],
        "--roles",
        "writer",
        "--service-account",
        "--ttl-days",
        "1",
        "--save",
        str(w / "writer.jwt"),
        "--json",
    ]
    equal(minted["redacted_argv"], expected)
    for key in ("pid", "start_ticks", "boot_id", "uid", "configuration_sha256"):
        equal(minted[key], born[key])
    retirement = read(w / "mint-proof.json")
    equal(retirement["birth"], born)
    need(retirement["state"] == "retired", "mint retirement receipt")
    return dict(
        schema=1,
        summary=summary,
        stdout=dict(
            path=str(w / "mint-summary.stdout"),
            sha256=hashlib.sha256(raw).hexdigest(),
            bytes=len(raw),
        ),
        token=dict(
            path=str(w / "writer.jwt"), sha256=hashlib.sha256(token).hexdigest(), bytes=len(token)
        ),
        process=minted,
        retirement=dict(path=str(w / "mint-proof.json"), sha256=sha(w / "mint-proof.json")),
    ), token.decode().strip()


def verify_token(metadata):
    expected = metadata["token"]
    with private_stream(Path(expected["path"])) as stream:
        raw = stream.read(16385)
    need(
        len(raw) == expected["bytes"] and hashlib.sha256(raw).hexdigest() == expected["sha256"],
        "same actual native bearer bytes",
    )
    return raw.decode().strip()


def closed(value, fields):
    need(type(value) is dict and set(value) == set(fields.split()), "closed artifact record")


def record_stream(path, *, maximum):
    full = hashlib.sha256()
    size = 0
    count = 0
    with private_stream(path) as stream:
        while True:
            raw = stream.readline(LINE + 1)
            if not raw:
                break
            count += 1
            size += len(raw)
            need(
                len(raw) <= LINE and raw.endswith(b"\n") and size <= maximum and count <= 101000,
                "bounded complete JSONL",
            )
            full.update(raw)
            yield json_object(raw), raw
    return dict(path=str(path), sha256=full.hexdigest(), bytes=size, records=count)


def api_cost_sample(value, arm):
    need(type(value) is dict, "typed API cost observation")
    equal(
        [value.get("kind"), value.get("arm"), value.get("window_seconds")], ["api_costs", arm, 60]
    )
    if "rows" in value:
        closed(value, "kind arm window_seconds rows")
        rows = value["rows"]
        need(type(rows) is list and len(rows) <= 20, "bounded API cost rows")
        for row in rows:
            closed(
                row,
                "shape calls elapsed_ms max_ms read_rows read_bytes written_rows cpu_us "
                "kind_mask table_mask",
            )
            for key in (
                "shape",
                "calls",
                "elapsed_ms",
                "max_ms",
                "read_rows",
                "read_bytes",
                "written_rows",
                "cpu_us",
            ):
                integer(row[key], 0, 2**64 - 1)
            integer(row["kind_mask"], 1, 7)
            integer(row["table_mask"], 0, 8191)
    else:
        closed(value, "kind arm window_seconds error_type error_code")
        need(
            type(value["error_type"]) is str
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value["error_type"]),
            "bounded API cost error type",
        )
        integer(value["error_code"], 0, 65535)
    return {key: item for key, item in value.items() if key not in {"kind", "arm"}}


class Workload:
    def __init__(self, context, codec):
        self.s = context
        self.w = Path(context["workdir"])
        self.codec = codec
        self.metadata = ref(context["mint_metadata"])
        verified, _ = mint_metadata(context, self.metadata["process"])
        equal(self.metadata, verified)
        verify_token(self.metadata)
        self.driver = read(self.w / "driver-birth.json")
        self.driver_birth = ref(self.driver["launch_receipt"])
        self.finished = read(self.w / "driver-finished.json")
        terminal = ref(self.finished["terminal_receipt"])
        need(
            type(terminal["returncode"]) is int and terminal["returncode"] in (0, 1),
            "completed actual driver status",
        )
        for key in (
            "pid",
            "start_ticks",
            "boot_id",
            "uid",
            "configuration_sha256",
            "launch_receipt",
        ):
            equal(self.driver[key], self.finished[key])
        for key in ("pid", "start_ticks", "boot_id", "uid"):
            equal(self.driver[key], self.driver_birth[key])
        equal(self.driver["source_sha256"], digest(context["source_hashes"]))
        equal(self.driver["cwd"], context["root"])
        self.launch = read(self.w / "serving-cell/launch.json")
        self.configuration = ref(self.launch["configuration_artifact"])
        fixture = read(self.w / "fixture.json")
        ownership = read(
            self.w / "ownership.json", expected_sha256=fixture["ownership_receipt_sha256"]
        )
        candidates = [
            item for item in ownership["processes"] if item["pid"] == self.launch["server"]["pid"]
        ]
        need(len(candidates) == 1, "one owner-recorded actual API birth")
        actual = ref(candidates[0]["launch_receipt"])
        for key in ("pid", "start_ticks", "boot_id", "uid"):
            equal(self.launch["server"][key], actual[key])
        equal(self.launch["source"], dict(head=context["head"], tree=context["tree"]))
        equal(self.launch["server"]["instance_id"], context["api_instance_id"])
        equal(self.launch["server"]["cwd"], context["root"])
        equal(
            self.launch["server"]["health_instance"],
            socket.gethostname() + ":" + str(self.launch["server"]["pid"]),
        )
        self.ids = set()
        self.keys = set()
        self.total = 0
        self.counts = {"api": 0, "delivery": 0}
        self.refs = [
            dict(path=str(self.w / name), sha256=sha(self.w / name))
            for name in (
                "fixture.json",
                "attached-context.json",
                "ownership.json",
                "serving-cell/launch.json",
                "driver-birth.json",
                "driver-finished.json",
            )
        ]
        self.refs.extend(
            [
                self.launch["configuration_artifact"],
                context["mint_metadata"],
                self.metadata["stdout"],
                self.metadata["token"],
                self.metadata["process"]["launch_receipt"],
                self.metadata["process"]["terminal_receipt"],
                self.metadata["retirement"],
            ]
        )
        self.driver_header = None
        self.iterator = None

    def __enter__(self):
        self.iterator = self.inputs()
        return self

    def __exit__(self, kind, error, traceback):
        try:
            if self.iterator is not None:
                self.iterator.close()
        except BaseException:
            if error is None:
                raise
        return False

    def unique(self, item):
        identifier(item["memory_id"])
        key = item["key"]
        need(
            item["memory_id"] not in self.ids and key not in self.keys,
            "unique independent workload identities",
        )
        self.ids.add(item["memory_id"])
        self.keys.add(key)
        need(len(self.ids) <= 45500 and len(self.keys) <= 45500, "workload set bound")
        self.counts[item["kind"]] += 1
        return item

    def delivery(self, arm):
        path = self.w / "delivery-intent" / f"arm-{arm}.jsonl"
        receipt = read(path.parent / f"arm-{arm}-receipt.json")
        closed(receipt, "schema arm bytes records sha256 header")
        equal([receipt[key] for key in ("schema", "arm", "records")], [1, arm, 2502])
        integer(receipt["bytes"], 1, ARM_BYTES)
        hex64(receipt["sha256"])
        prefix = hashlib.sha256()
        full = hashlib.sha256()
        size = 0
        records = record_stream(path, maximum=ARM_BYTES)
        try:
            header, raw = next(records)
            full.update(raw)
            prefix.update(raw)
            size += len(raw)
            closed(
                header,
                (
                    "schema kind fixture_id arm on batch_rows expected_rows head "
                    "tree driver_sha256 capture_sha256 protocol_sha256 "
                    "context_sha256 fixture_sha256 launch_sha256 "
                    "mint_metadata_sha256 birth"
                ),
            )
            equal(header, receipt["header"])
            for key, expected in dict(
                schema=1,
                kind="header",
                fixture_id=self.s["fixture_id"],
                arm=arm,
                on=ORDER[arm],
                batch_rows=500,
                expected_rows=2500,
                head=self.s["head"],
                tree=self.s["tree"],
                driver_sha256=self.s["source_hashes"]["driver"],
                capture_sha256=self.s["source_hashes"][FILES[3]],
                protocol_sha256=PROTOCOL_SHA,
                context_sha256=sha(self.w / "attached-context.json"),
                fixture_sha256=sha(self.w / "fixture.json"),
                launch_sha256=sha(self.w / "serving-cell/launch.json"),
                mint_metadata_sha256=self.s["mint_metadata"]["sha256"],
            ).items():
                equal(header[key], expected)
            born = header["birth"]
            closed(born, "pid start_ticks boot_id uid host environment_sha256")
            for key in ("pid", "start_ticks", "boot_id", "uid"):
                equal(born[key], self.driver[key])
            need(
                born["host"] == socket.gethostname()
                and born["environment_sha256"] == self.driver["configuration_sha256"],
                "actual driver capture birth/config",
            )
            if self.driver_header is None:
                self.driver_header = born
            else:
                equal(born, self.driver_header)
            pattern = None
            for index in range(2500):
                value, raw = next(records)
                size += len(raw)
                full.update(raw)
                prefix.update(raw)
                closed(value, "schema kind arm index row audit")
                equal(
                    [value[k] for k in ("schema", "kind", "arm", "index")],
                    [1, "delivery", arm, index],
                )
                row = self.codec.row(value["row"])
                audit = self.codec.audit(value["audit"])
                match = re.fullmatch(r"(gate-delivery-[a-f0-9]{32})-(\d+)", row["memory_key"])
                need(match is not None and match[2] == str(index), "delivery exact key counter")
                if pattern is None:
                    pattern = match[1]
                need(pattern == match[1], "one original delivery prefix")
                yield self.unique(
                    dict(
                        kind="delivery",
                        key=row["memory_key"],
                        memory_id=row["memory_id"],
                        version=row["version"],
                        content_sha256=row["content_sha256"],
                        row=row,
                        audit=audit,
                    )
                )
            trailer, raw = next(records)
            size += len(raw)
            full.update(raw)
            equal(
                trailer,
                dict(
                    schema=1, kind="trailer", arm=arm, count=2500, prefix_sha256=prefix.hexdigest()
                ),
            )
            try:
                next(records)
            except StopIteration:
                pass
            else:
                need(False, "extra intent record")
            need(
                receipt["bytes"] == size and receipt["sha256"] == full.hexdigest(),
                "complete intent bytes",
            )
            self.total += size
            need(self.total <= TOTAL_BYTES, "intent aggregate")
            self.refs.append(dict(path=str(path), sha256=full.hexdigest(), bytes=size))
            self.refs.append(
                dict(
                    path=str(path.parent / f"arm-{arm}-receipt.json"),
                    sha256=sha(path.parent / f"arm-{arm}-receipt.json"),
                )
            )
        finally:
            pending = sys.exception()
            try:
                records.close()
            except BaseException:
                if pending is None:
                    raise

    def inputs(self):
        records = record_stream(self.w / "driver.jsonl", maximum=64 * 1024 * 1024)

        def take(kind, fields=None):
            value, _ = next(records)
            need(value.get("kind") == kind, "original artifact event order")
            if fields is not None:
                closed(value, fields)
            return value

        try:
            config = take(
                "configuration",
                (
                    "kind manifest shared_configuration_excludes "
                    "credential_posture api_configuration_attested source launch "
                    "manifest_sha256 calls warmup rounds batches batch_rows "
                    "logical_keys value_bytes order driver_sha256"
                ),
            )
            equal(config["manifest"], read(self.w / "fixture.json"))
            need(config["manifest_sha256"] == digest(config["manifest"]), "artifact fixture digest")
            equal(config["source"], dict(head=self.s["head"], tree=self.s["tree"], clean=True))
            equal(
                config["launch"],
                dict(
                    health_instance=self.launch["server"]["health_instance"],
                    fingerprints=self.configuration["fingerprints"],
                    launch_sha256=sha(self.w / "serving-cell/launch.json"),
                    configuration_artifact_sha256=self.launch["configuration_artifact"]["sha256"],
                    birth_sha256=digest(self.launch["server"]),
                ),
            )
            equal(config["credential_posture"], self.configuration["credential_posture"])
            for key in (
                "api_configuration_sha256",
                "cli_configuration_sha256",
                "shared_configuration_sha256",
            ):
                equal(config["manifest"][key], self.configuration[key])
            for key, expected in dict(
                calls=2000,
                warmup=50,
                rounds=5,
                batches=5,
                batch_rows=500,
                logical_keys=45500,
                value_bytes=4096,
                order=list(ORDER),
                driver_sha256=self.s["source_hashes"]["driver"],
                shared_configuration_excludes=["cell_router_token"],
                api_configuration_attested=True,
            ).items():
                equal(config[key], expected)
            for arm in range(10):
                proof = take(
                    "arm_proof",
                    (
                        "kind arm on deployment_id epoch instance_id "
                        "fingerprint_sha256 api_configuration_sha256 "
                        "cli_configuration_sha256 shared_configuration_sha256 "
                        "shared_configuration_excludes credential_posture "
                        "api_configuration_attested source launch tables "
                        "invariant_sha256 org_triggers_sha256"
                    ),
                )
                equal([proof["arm"], proof["on"]], [arm, ORDER[arm]])
                for key in (
                    "source",
                    "launch",
                    "credential_posture",
                    "shared_configuration_excludes",
                    "api_configuration_attested",
                ):
                    equal(proof[key], config[key])
                for key in (
                    "deployment_id",
                    "epoch",
                    "instance_id",
                    "api_configuration_sha256",
                    "cli_configuration_sha256",
                    "shared_configuration_sha256",
                ):
                    equal(proof[key], config["manifest"][key])
                pattern = None
                for index in range(2050):
                    value = take(
                        "request",
                        (
                            "kind arm index status key memory_id version content_sha256 "
                            "elapsed_ms measured admission"
                        ),
                    )
                    equal(
                        [value[k] for k in ("arm", "index", "status", "measured", "admission")],
                        [arm, index, 200, index >= 50, "derived_ready"],
                    )
                    need(
                        type(value["elapsed_ms"]) in {int, float}
                        and math.isfinite(value["elapsed_ms"])
                        and value["elapsed_ms"] >= 0,
                        "finite recorded latency",
                    )
                    integer(value["version"], 1, 2**63 - 1)
                    match = re.fullmatch(
                        r"(gate-[a-f0-9]{32}-" + str(arm) + r")-(\d+)", value["key"]
                    )
                    need(match is not None and match[2] == str(index), "API exact key counter")
                    if pattern is None:
                        pattern = match[1]
                    need(
                        match[1] == pattern
                        and value["content_sha256"] == content_hash(value["key"]),
                        "API original prefix/hash",
                    )
                    yield self.unique(
                        dict(
                            kind="api",
                            **{
                                key: value[key]
                                for key in ("key", "memory_id", "version", "content_sha256")
                            },
                        )
                    )
                observation = api_cost_sample(take("api_costs"), arm)
                for index in range(5):
                    value = take(
                        "worker",
                        (
                            "kind arm index claimed published failed expired_keys "
                            "expired_evidence swept_settled published_audits"
                        ),
                    )
                    equal(
                        [value[k] for k in ("arm", "index", "claimed", "published", "failed")],
                        [arm, index, 500, 500, 0],
                    )
                    for key in (
                        "expired_keys",
                        "expired_evidence",
                        "swept_settled",
                        "published_audits",
                    ):
                        integer(value[key])
                value = take("delivery_block", "kind arm elapsed_seconds rows rps")
                equal([value["arm"], value["rows"]], [arm, 2500])
                need(
                    type(value["elapsed_seconds"]) in {int, float}
                    and math.isfinite(value["elapsed_seconds"])
                    and value["elapsed_seconds"] > 0
                    and type(value["rps"]) in {int, float}
                    and math.isfinite(value["rps"])
                    and value["rps"] > 0,
                    "finite worker record",
                )
                value = take("settled", "kind arm api_rows delivery_rows physical")
                equal([value["arm"], value["api_rows"], value["delivery_rows"]], [arm, 2050, 2500])
                need(type(value["physical"]) is dict, "typed physical observation")
                equal(value["physical"].get("clickhouse_query_costs"), observation)
                value = take("arm", "kind arm on p50_ms p99_ms delivery_rps")
                equal([value["arm"], value["on"]], [arm, ORDER[arm]])
                yield from self.delivery(arm)
            take(
                "provisional_statistics",
                (
                    "kind on off p99_delta_ms throughput_loss "
                    "latency_within_limit throughput_within_limit "
                    "paired_p99_differences_ms"
                ),
            )
            equal(
                take("triggers_restored", "kind verified"),
                dict(kind="triggers_restored", verified=True),
            )
            equal(
                take("driver_cleanup", "kind native_returned outer_database_cleanup_required"),
                dict(
                    kind="driver_cleanup",
                    native_returned=True,
                    outer_database_cleanup_required=True,
                ),
            )
            try:
                next(records)
            except StopIteration as end:
                self.refs.append(end.value)
            else:
                need(False, "extra driver event")
            equal(self.counts, dict(api=20500, delivery=25000))
            verify_token(self.metadata)
        finally:
            pending = sys.exception()
            try:
                records.close()
            except BaseException:
                if pending is None:
                    raise

    def pages(self):
        page = []
        for value in self.iterator:
            page.append(value)
            if len(page) == 100:
                yield page
                page = []
        if page:
            yield page


def entry(context, directory, argv):
    need(
        set(context)
        == SPEC_FIELDS
        | {
            "cell",
            "org",
            "namespace",
            "api_instance_id",
            "mint_metadata",
            "capture_context_sha256",
        },
        "capture context fields",
    )
    validate({key: context[key] for key in SPEC_FIELDS})
    source(context)
    need(
        len(argv) == 16
        and len(set(argv[::2])) == 8
        and set(argv[::2])
        == {
            "--base-url",
            "--pg-dsn",
            "--token",
            "--org",
            "--namespace",
            "--fixture-manifest",
            "--launch-manifest",
            "--output",
        },
        "original driver arguments",
    )
    # There are eight required original pairs; no private option reaches its parser.
    driver_path = Path(context["root"]) / "bench/cell_trigger_gate.py"
    name = "_fixture_measured_driver"
    need(name not in sys.modules, "single actual driver module")
    spec = importlib.util.spec_from_file_location(name, driver_path)
    need(spec is not None and spec.loader is not None, "exact driver module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    capture = Capture(context, directory, birth(), Codec(protocol()))
    module._build_block = capture.delegate(module._build_block)
    sys.argv = [str(driver_path), *argv]
    result = module.main()
    return 1 if result == 0 and capture.calls != 10 else result


def main():
    try:
        divider = sys.argv.index("--")
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--context", type=Path, required=True)
        parser.add_argument("--capture-directory", type=Path, required=True)
        args = parser.parse_args(sys.argv[1:divider])
        context = read(args.context)
        context["capture_context_sha256"] = sha(args.context)
        return entry(context, args.capture_directory, sys.argv[divider + 1 :])
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        print(type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
