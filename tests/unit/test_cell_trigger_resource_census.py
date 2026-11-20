"""A vanished descendant requires a fresh complete resource census."""

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.gates.testsupport.cell_trigger import fixture
from scripts.gates.testsupport.cell_trigger.owned_children import OwnedChildren


@pytest.fixture
def census(monkeypatch, tmp_path):
    class Owned:
        pid = 100

        def __init__(self):
            self.round = 0
            self.reads = Counter()
            self.sampled_pids = []
            self.rows = {100: (1, "self", "S"), 200: (1, "store", "S")}
            self.trees = [[300], [400]]
            self.identify = lambda pid: self.rows.get(pid, (100, str(pid), "S"))
            self.enumerate = lambda pid: (
                self.trees[min(self.round - 1, len(self.trees) - 1)] if pid == 100 else []
            )
            self.measure = lambda pid: 1

        def identity(self, pid):
            self.reads[pid] += 1
            return self.identify(pid)

        def children(self, pid):
            if pid == self.pid:
                self.round += 1
            return self.enumerate(pid)

        def rss(self, pid):
            self.sampled_pids.append(pid)
            return self.measure(pid)

    owned = Owned()
    clock = SimpleNamespace(now=10.0)
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/proc/meminfo":
            return f"MemAvailable: {20 * 1024**2} kB\n"
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return "boot"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(
        fixture.shutil, "disk_usage", lambda _: SimpleNamespace(free=32 * fixture.GIB)
    )
    original_stat = fixture.os.stat
    monkeypatch.setattr(
        fixture.os,
        "stat",
        lambda path, *args, **kwargs: (
            SimpleNamespace(st_uid=fixture.os.geteuid())
            if str(path) in {"/proc/100", "/proc/200"}
            else original_stat(path, *args, **kwargs)
        ),
    )
    spec = {
        "workdir": str(tmp_path),
        "stores": {
            "pg": {
                "host_pid": 200,
                "start_ticks": "store",
                "boot_id": "boot",
                "uid": fixture.os.geteuid(),
                "disk_path": str(tmp_path),
            }
        },
    }
    owner = fixture.Owner(spec, owned, lambda *args, **kwargs: None)
    owner.sampled = 7.0
    return owner, owned, clock


@pytest.fixture
def rss_census(census, monkeypatch):
    owner, owned, clock = census
    original_read, original_identity = Path.read_text, owned.identify
    gone, failures = set(), {}

    def read(path, *args, **kwargs):
        if path.parent.parent == Path("/proc") and path.name == "status":
            pid = int(path.parent.name)
            if pid in failures:
                return failures[pid]()
            return "Name:\tfixture\nVmRSS:\t1024 kB\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    owned.identify = lambda pid: None if pid in gone else original_identity(pid)
    owned.measure = OwnedChildren.rss
    return owner, owned, clock, gone, failures


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_a_vanished_rss_read_requires_a_new_complete_forest(rss_census, error):
    owner, owned, _, gone, failures = rss_census

    def vanish():
        gone.add(300)
        raise error("gone during RSS read")

    failures[300] = vanish
    owner.sample()
    assert owner.first is None
    assert owner.sampled == 10.0
    assert owned.round == 2
    assert owned.sampled_pids.count(200) == 2
    assert 400 in owned.sampled_pids


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_a_live_process_with_unreadable_rss_never_refreshes_the_sample(rss_census, error):
    owner, owned, _, _, failures = rss_census

    def unreadable():
        raise error("RSS unavailable while identity remains present")

    failures[300] = unreadable
    owner.sample()
    assert owner.first is not None
    assert owner.sampled == 7.0
    assert owned.round == 1
    assert 400 not in owned.sampled_pids


def test_a_readable_rss_forest_keeps_the_existing_admission_path(rss_census):
    owner, owned, _, _, _ = rss_census
    owner.sample()
    assert owner.first is None
    assert owner.sampled == 10.0
    assert owned.round == 1
    assert set(owned.sampled_pids) == {100, 200, 300}


@pytest.mark.parametrize("stage", ["before", "after", "children"])
def test_a_vanished_descendant_restarts_the_whole_forest(census, stage):
    owner, owned, _ = census
    original_identity, original_children = owned.identify, owned.enumerate

    def identity(pid):
        if pid == 300 and (stage != "after" or owned.reads[pid] > 1):
            return None
        return original_identity(pid)

    def children(pid):
        if pid == 300 and stage == "children":
            raise FileNotFoundError("gone")
        return original_children(pid)

    owned.identify, owned.enumerate = identity, children
    if stage == "children":
        owned.identify = lambda pid: (
            None if pid == 300 and owned.reads[pid] > 1 else original_identity(pid)
        )
    owner.sample()
    assert owner.first is None
    assert owner.sampled == 10.0
    assert owned.round == 2
    assert owned.sampled_pids.count(200) == 2
    assert 400 in owned.sampled_pids


def test_a_complete_first_forest_needs_no_retry(census):
    owner, owned, _ = census
    owner.sample()
    assert owner.first is None and owner.sampled == 10.0
    assert owned.round == 1 and set(owned.sampled_pids) == {100, 200, 300}


@pytest.mark.parametrize(
    "fault",
    [
        "churn",
        "root-gone",
        "store-gone",
        "store-birth",
        "pid-reuse",
        "identity-change",
        "permission",
        "malformed",
        "rss",
        "stale",
    ],
)
def test_incomplete_or_unsafe_census_never_refreshes_the_sample(census, fault):
    owner, owned, clock = census
    original = owned.identify

    def identify(pid):
        if fault == "root-gone" and pid == 100:
            return None
        if fault == "store-gone" and pid == 200:
            return None
        if fault == "store-birth" and pid == 200:
            return (1, "changed", "S")
        if pid >= 300:
            if fault in {"churn", "stale"}:
                if fault == "stale":
                    clock.now = 13.0
                return None
            if fault == "pid-reuse" and owned.round == 1:
                return None
            if fault == "identity-change" and owned.reads[pid] > 1:
                return (100, "new-birth", "S")
            if fault == "permission":
                raise PermissionError("unknown")
            if fault == "malformed":
                raise ValueError("unknown")
        return original(pid)

    owned.identify = identify
    if fault == "churn":
        owned.trees = [[300], [400], [500], [600]]
    if fault == "pid-reuse":
        owned.trees = [[300], [300]]
    if fault == "rss":
        owned.measure = lambda pid: fixture.POLICY["stop_process_rss_bytes"] // 1024 + 1
    owner.sample()
    assert owner.first is not None
    assert owner.sampled == 7.0
    assert owned.round <= 3
    if fault not in {"churn", "pid-reuse", "stale"}:
        assert owned.round <= 1


@pytest.mark.parametrize(
    "fault", ["root-birth", "store-birth", "new-child-rss", "live-child-census"]
)
def test_restarted_forest_retains_root_and_descendant_guards(census, fault):
    owner, owned, _ = census
    original_identity, original_children = owned.identify, owned.enumerate

    def identity(pid):
        if pid == 300:
            return original_identity(pid) if fault == "live-child-census" else None
        if owned.round and (
            (pid == 100 and fault == "root-birth") or (pid == 200 and fault == "store-birth")
        ):
            return (1, "reused", "S")
        return original_identity(pid)

    def children(pid):
        if pid == 300 and fault == "live-child-census":
            raise FileNotFoundError("unverified disappearance")
        return original_children(pid)

    owned.identify, owned.enumerate = identity, children
    if fault == "new-child-rss":
        owned.measure = lambda pid: (
            fixture.POLICY["stop_process_rss_bytes"] // 1024 + 1 if pid == 400 else 1
        )
    owner.sample()
    assert owner.first is not None
    assert owner.sampled == 7.0
    if fault == "new-child-rss":
        assert owned.round == 2 and 400 in owned.sampled_pids
    else:
        assert owned.round == 1


def test_census_deadline_stops_before_the_next_descendant(census):
    owner, owned, clock = census
    original_children = owned.enumerate
    owned.enumerate = lambda pid: [301] if pid == 300 else original_children(pid)

    def measure(pid):
        if pid == 300:
            clock.now = 13.0
        return 1

    owned.measure = measure
    owner.sample()
    assert isinstance(owner.first, fixture.Refused)
    assert owner.sampled == 7.0
    assert 300 in owned.sampled_pids and 301 not in owned.sampled_pids


def test_restarted_forest_refuses_store_uid_drift(census, monkeypatch):
    owner, owned, _ = census
    original_identity, original_stat = owned.identify, fixture.os.stat
    owned.identify = lambda pid: None if pid == 300 else original_identity(pid)

    def stat(path, *args, **kwargs):
        if str(path) == "/proc/200" and owned.round:
            return SimpleNamespace(st_uid=fixture.os.geteuid() + 1)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(fixture.os, "stat", stat)
    owner.sample()
    assert isinstance(owner.first, fixture.Refused)
    assert owner.sampled == 7.0
    assert owned.round == 1


def test_store_identity_refusals_keep_distinct_failure_sites(census, monkeypatch):
    owner, owned, _ = census
    original_identity, original_stat = owned.identify, fixture.os.stat
    sites = []
    for fault in ("missing", "start", "boot", "uid"):
        owner.failures.clear()
        owner.sampled = 7.0
        owner.spec["stores"]["pg"]["boot_id"] = "changed" if fault == "boot" else "boot"
        owned.identify = lambda pid, fault=fault: (
            None
            if pid == 200 and fault == "missing"
            else (1, "changed", "S")
            if pid == 200 and fault == "start"
            else original_identity(pid)
        )

        def stat(path, *args, fault=fault, **kwargs):
            if str(path) == "/proc/200" and fault == "uid":
                return SimpleNamespace(st_uid=fixture.os.geteuid() + 1)
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(fixture.os, "stat", stat)
        owner.sample()
        assert isinstance(owner.first, fixture.Refused)
        assert str(owner.first) == "resource store birth changed"
        assert owner.sampled == 7.0
        record = owner.failure_summary()["records"][0]
        assert record["primary"] is True and record["phase"] == "resource"
        assert record["site"]["file"] == "fixture.py"
        assert record["site"]["function"] == "sample"
        sites.append(record["site"]["line"])
    assert len(set(sites)) == 4
