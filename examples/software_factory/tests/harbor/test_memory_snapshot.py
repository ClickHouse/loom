"""Native snapshot receipts must prove each historical fact and its actual code link."""

import copy
import hashlib
import json
import unittest

from examples.software_factory.harbor.memory import _snapshot_events


class HistoricalSnapshot(unittest.TestCase):
    def setUp(self):
        self.binding = dict(
            org="org", memory_namespace="memory", code_namespace="code", base_commit="a" * 40
        )
        value = "A historical project decision."
        self.fixture = dict(
            history_events=[
                dict(
                    key="history/fact",
                    value=value,
                    files=["src/main.py"],
                    operation="set",
                    revision="B",
                )
            ],
            public_documents={"history/fact": value},
        )
        response = dict(
            memory_id="id",
            version=1,
            content_sha256=hashlib.sha256(json.dumps(value).encode()).hexdigest(),
        )
        request = dict(org="org", namespace="memory", memory_key="history/fact")
        common = dict(phase="B", source_commit="a" * 40, physical_requests=1, status_code=200)
        read = {
            **response,
            "found": True,
            "value": value,
            "code": [dict(code_namespace="code", path="src/main.py", state="fresh")],
        }
        self.cert = dict(
            events=[
                dict(
                    common,
                    operation="memory.set",
                    request_id="write",
                    request=request,
                    response=dict(response, status="active", noop=False, dry_run=False),
                ),
                dict(
                    common,
                    operation="memory.get",
                    request_id="read",
                    request=request,
                    response=read,
                ),
            ],
            final_snapshot=[dict(read, memory_key="history/fact")],
        )

    def test_real_write_read_and_link_evidence_is_required(self):
        _snapshot_events(self.cert, self.fixture, self.binding)
        for mutate in (
            lambda c: c["events"][0]["response"].update(noop=True),
            lambda c: c["events"][1].update(request_id="write"),
            lambda c: c["events"][1]["response"].update(version=2),
            lambda c: c["events"][1]["response"]["code"][0].update(state="changed"),
            lambda c: c["events"][1]["response"]["code"][0].update(code_namespace="other"),
            lambda c: c["events"][1].update(source_commit="b" * 40),
            lambda c: c["events"][1].update(physical_requests=0),
            lambda c: c.update(final_snapshot=[]),
        ):
            with self.subTest(mutation=mutate):
                cert = copy.deepcopy(self.cert)
                mutate(cert)
                with self.assertRaises(PermissionError):
                    _snapshot_events(cert, self.fixture, self.binding)
