"""The independent proof reuses one checked delivery row without losing its oracles."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.observability.audit import row_matches_hash
from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case
from tests.unit.test_cell_trigger_ci_creator import protocol_reader as protocol_reader

# Frozen independently from the specified typed rows and length-prefixed chain format.
FROZEN_CHAINS = {
    "delivery1": [
        "b9de65407fb5cb99d9c1b7bdfe57cbc4bc53631ecd43f8d3cbe4ad43c6d0a9d9",
        "b9de65407fb5cb99d9c1b7bdfe57cbc4bc53631ecd43f8d3cbe4ad43c6d0a9d9",
        "2ff1b1e421e82c66fb3f2bf908287732ae165ca8085bd978406fa5d5da98e763",
        "460467af421698fa9dd5afc8245b401b95744c3400314bb3b0d9cac7e1e2998a",
    ],
    "delivery2": [
        "12dc10b5fd92b7040f1ce74a7f7117d65e7a0201cfb2fe253e49083d01b3d8e7",
        "12dc10b5fd92b7040f1ce74a7f7117d65e7a0201cfb2fe253e49083d01b3d8e7",
        "454e76f525cd9b82b5e37973b122f84b7eb4aa5cebbdd2e6eb3816d73edd29b0",
        "6d05bc5906e4a0d03f08d7e18f8b9402ce0d62d0708965d18c936850e18ea331",
    ],
    "api": [
        "78a3c6e80c1664b376cdf33619b47146c51d2b1740b2b69349446d937e45f45c",
        "78a3c6e80c1664b376cdf33619b47146c51d2b1740b2b69349446d937e45f45c",
        "5efc943155e492a999b0877f66eac86ed90fd1338e24f5388cac9e47bb18ef7e",
        "460467af421698fa9dd5afc8245b401b95744c3400314bb3b0d9cac7e1e2998a",
    ],
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _row_and_audit(specification, index, api):
    key = f"gate-proof-{index}"
    prefix = f"Cell trigger measurement {key}: "
    payload = prefix + ("abcdefghijklmnopqrstuvwxyz " * 4096)[: 4096 - len(prefix)]
    content = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    row = deepcopy(specification["sparse_defaults"])
    row.update(
        org="proof-org",
        namespace="proof-ns",
        agent="bench",
        user_id="-",
        session_id="-",
        memory_key=key,
        memory_type="semantic",
        value=json.dumps(payload),
        value_json="{}",
        confidence=1.0,
        version=101 + index,
        created_at="2026-01-02T03:04:05",
        memory_id=f"00000000-0000-0000-0000-{index + 1:012d}",
        updated_at="2026-01-02T03:04:05",
        last_reinforced_at="2026-01-02T03:04:05",
        content_sha256=content,
        actor="process:benchmark",
        source_class="asserted",
    )
    subject = json.dumps(
        dict(
            memory_key=key,
            memory_id=row["memory_id"],
            version=row["version"],
            content_sha256=content,
            source_class="asserted",
            status="active",
            pinned=False,
            tombstoned=False,
        ),
        sort_keys=True,
    )
    hashed = dict(
        ts="2026-01-02T03:04:05.123000",
        event_type="memory.revision",
        org="proof-org",
        namespace="proof-ns",
        actor="process:benchmark",
        subject=subject,
        request_id=f"api-{index}" if api else "",
    )
    audit = dict(
        hashed,
        ts="2026-01-02T03:04:05.123000+00:00",
        row_hash=hashlib.sha256(json.dumps(hashed, sort_keys=True).encode()).hexdigest(),
        build_commit="a" * 40,
        instance="proof-host:700" if api else "proof-host:701",
        instance_label="iso:trigger:proof-fixtur",
        consumer="service-account" if api else "",
        token_id="b" * 32 if api else "",
    )
    return row, audit


def _case(native, count, api):
    specification = native.protocol()
    context = dict(
        org="proof-org",
        namespace="proof-ns",
        head="a" * 40,
        fixture_id="proof-fixture",
        databases={"ch_schema": "proof_cell"},
        _api_server={"health_instance": "proof-host:700"},
    )
    items, stored, memories, audits, expected = [], [], [], [], []
    for index in range(count):
        row, audit = _row_and_audit(specification, index, api)
        assert set(row) == set(specification["memory_fields"]) and len(row) == 61
        item = dict(
            kind="api" if api else "delivery",
            key=row["memory_key"],
            memory_id=row["memory_id"],
            version=row["version"],
            content_sha256=row["content_sha256"],
        )
        if not api:
            item.update(row=deepcopy(row), audit=deepcopy(audit))
        sparse = {
            key: value for key, value in row.items() if key not in specification["sparse_defaults"]
        }
        record = {}
        for prefix, fields in (
            (
                "c_",
                "org namespace agent user_id session_id memory_key memory_id version content_sha256",
            ),
            ("r_", "org namespace user_id memory_id version content_sha256 actor source_class"),
        ):
            record.update({prefix + key: row[key] for key in fields.split()})
        record.update(
            c_seq=index + 1,
            r_seq=index + 1,
            r_request_id=audit["request_id"],
            head=json.dumps(sparse),
            revision=json.dumps(sparse),
            audit=json.dumps(audit),
            targets=None if api else 0,
            attempts=None if api else 0,
            last_error=None if api else "",
        )
        stored.append({key: record[key] for key in native.PG_FIELDS})
        comparable = dict(deepcopy(row), value_json={})
        memories.append(deepcopy(comparable))
        audits.append(deepcopy(audit))
        intended = {
            key: item[key] for key in ("kind", "key", "memory_id", "version", "content_sha256")
        }
        if not api:
            intended.update(row=deepcopy(comparable), audit=deepcopy(audit))
        expected.append(
            (
                intended,
                deepcopy(intended),
                dict(row=comparable, audit=deepcopy(audit), seq=index + 1),
                dict(head=sorted(sparse), revision=sorted(sparse)),
            )
        )
        items.append(item)
    return specification, context, items, stored, memories, audits, expected


async def _exercise(native, monkeypatch, count, api, fault=""):
    specification, context, items, stored, memories, audits, expected = _case(native, count, api)
    if fault in {"intent_value", "intent_version", "intent_json"}:
        key, value = {
            "intent_value": ("value", '"changed"'),
            "intent_version": ("version", 999),
            "intent_json": ("value_json", '{"changed":true}'),
        }[fault]
        items[0]["row"][key] = value
    elif fault in {"pg_value", "pg_revision", "pg_origin"}:
        key = "revision" if fault == "pg_revision" else "head"
        changed = json.loads(stored[0][key])
        field, value = {
            "pg_value": ("value", '"changed"'),
            "pg_revision": ("version", 999),
            "pg_origin": ("org", "foreign"),
        }[fault]
        changed[field] = value
        stored[0][key] = json.dumps(changed)
    elif fault == "pg_typed_version":
        stored[0]["c_version"] = 999
    elif fault == "audit_stamp":
        changed = json.loads(stored[0]["audit"])
        changed["build_commit"] = "foreign"
        stored[0]["audit"] = json.dumps(changed)
    elif fault == "native_hash":
        changed = json.loads(stored[0]["audit"])
        changed["row_hash"] = "0" * 64
        assert row_matches_hash(dict(changed, ts=datetime.fromisoformat(changed["ts"]))) is False
        stored[0]["audit"] = json.dumps(changed)
    elif fault == "ch_value":
        memories[0]["value"] = '"changed"'
    elif fault.endswith("missing"):
        (audits if fault.startswith("audit") else memories).pop()
    elif fault.endswith("duplicate"):
        data = audits if fault.startswith("audit") else memories
        data[-1] = deepcopy(data[0])
    codec = native.Codec(specification)
    normalizations = Counter()
    original_row = codec.row

    def counted_row(original, **options):
        if options == {"comparison": True}:
            for index, item in enumerate(items):
                if original is item.get("row"):
                    normalizations[index] += 1
        return original_row(original, **options)

    monkeypatch.setattr(codec, "row", counted_row)
    chain_inputs, chains, slots = [], [], {}
    original_update = native.chain_update

    def update(chain, index, value):
        if id(chain) not in slots:
            slots[id(chain)] = len(chains)
            chains.append(chain)
        slot = slots[id(chain)]
        assert _canonical(value) == _canonical(expected[index][slot])
        chain_inputs.append((index, slot))
        return original_update(chain, index, value)

    monkeypatch.setattr(native, "chain_update", update)
    calls = []

    async def fetch(sql, org, namespace, identifiers, limit):
        assert sql == native.pg_page_sql() and (org, namespace) == ("proof-org", "proof-ns")
        assert list(map(str, identifiers)) == [item["memory_id"] for item in items]
        assert limit == count + 1
        calls.append("pg")
        return stored

    async def query(sql, *, parameters, settings):
        audit_query = ".audit_log " in sql
        fields = specification["audit_fields" if audit_query else "memory_fields"]
        data = audits if audit_query else memories
        assert "`proof_cell`" in sql and "LIMIT " + str(count + 1) in sql
        assert parameters["org"] == "proof-org" and parameters["ns"] == "proof-ns"
        assert settings == {
            "max_result_rows": count + 1,
            "max_result_bytes": 20 * 1024 * 1024,
            "result_overflow_mode": "throw",
            "max_execution_time": 30,
        }
        if audit_query:
            assert parameters["hashes"] == [json.loads(row["audit"])["row_hash"] for row in stored]
        else:
            assert list(map(str, parameters["ids"])) == [item["memory_id"] for item in items]
        calls.append("audit" if audit_query else "memory")
        return SimpleNamespace(
            column_names=fields, result_rows=[tuple(row[key] for key in fields) for row in data]
        )

    end = RuntimeError("owned end-page sentinel")
    checked = []

    def pages():
        yield items
        assert calls == ["pg", "memory", "audit"]
        assert chain_inputs == [(index, slot) for index in range(count) for slot in range(4)]
        case = "api" if api else f"delivery{count}"
        assert [chain.hexdigest() for chain in chains] == FROZEN_CHAINS[case]
        checked.append(True)
        raise end

    workload = SimpleNamespace(
        pages=pages,
        metadata={"summary": {"jti": "b" * 32}},
        driver_header={"host": "proof-host", "pid": 701},
        driver={"pid": 701},
    )
    if fault:
        with pytest.raises(native.Refused):
            await native.verify_pages(
                SimpleNamespace(fetch=fetch),
                SimpleNamespace(query=query),
                context,
                workload,
                codec,
                row_matches_hash,
            )
        assert checked == []
        return
    with pytest.raises(RuntimeError) as caught:
        await native.verify_pages(
            SimpleNamespace(fetch=fetch),
            SimpleNamespace(query=query),
            context,
            workload,
            codec,
            row_matches_hash,
        )
    assert caught.value is end and checked == [True]
    # The owned sentinel is reached only after all page queries/full row/chain checks.
    assert normalizations == (Counter() if api else Counter(dict.fromkeys(range(count), 1)))


@pytest.mark.parametrize(
    ("count", "api"),
    [(1, False), (2, False), (1, True)],
    ids=["delivery1", "delivery2", "api-control"],
)
async def test_provenance_reuses_only_the_checked_delivery_normalization(
    keeper_config_case, protocol_reader, monkeypatch, count, api
):
    native, *_ = keeper_config_case
    await _exercise(native, monkeypatch, count, api)


@pytest.mark.parametrize(
    "fault",
    [
        "intent_value",
        "intent_version",
        "intent_json",
        "pg_value",
        "pg_revision",
        "pg_origin",
        "pg_typed_version",
        "audit_stamp",
        "native_hash",
        "ch_value",
        "memory_missing",
        "memory_duplicate",
        "audit_missing",
        "audit_duplicate",
    ],
)
async def test_provenance_reuse_keeps_full_wrong_row_and_native_hash_refusals(
    keeper_config_case, protocol_reader, monkeypatch, fault
):
    native, *_ = keeper_config_case
    await _exercise(native, monkeypatch, 2, False, fault)
