"""SWE-bench Verified file localization for code.search.

Given an instance's `problem_statement` (the issue text), does code.search
return the files the gold `patch` edits? Public, reproducible, no LLM judge.

Protocol:

* Gold = the files the instance's `patch` modifies that exist at
  `base_commit` (`test_patch` is never read). An instance whose patch only
  adds files has no findable gold and is skipped.
* One namespace per repository (`swe-<owner>-<name>`), indexed at the first
  sampled instance's `base_commit` with `loom code backfill --embed`, then
  moved instance to instance in commit-time order with the per-merge ingest
  step (the production delta path: diff the two commits, re-ingest the
  changed files, tombstone the deleted ones, re-resolve dependents). Each
  query runs against the namespace at exactly its instance's base commit.
* Query = the problem statement as an agent sends it: whole when it fits the
  op's 2,000-character wire limit, else its first 2,000 characters (the
  request a client sends after the full text is refused). `top_k=10`, the
  server's default bodies and every configured leg, `fresh=true` so no
  memoised answer is replayed. A second page through `next_cursor` (15
  units, no bodies) extends the ranking to 25 units for the file metrics.
* Results map to files in rank order (first unit of each path). Metrics:
  file hit@1/@5/@10 (any gold file), all-gold@10, MRR (first gold file),
  latency p50/p95 and response tokens of the first page.
* Baseline: BM25 (k1=1.2, b=0.75) over the same files the namespace holds
  live at that commit, one document per file (path + content, identifier
  splitting), queried with the same text.

Subcommands:

    python -m bench.swebench_localization fetch
    python -m bench.swebench_localization plan --sample 0 --seed 7
    python -m bench.swebench_localization run --base-url http://127.0.0.1:7971 \\
        --token-file swe/token --env-file /opt/loom/.env --repos-dir swe/repos \\
        --out bench/results/swebench_localization.jsonl
    python -m bench.swebench_localization report --rows bench/results/<file>.jsonl

`run` is resumable: an instance already in `--out` is skipped, and the
namespace's watermark (`git_head`) says where the next delta starts.
`index-step` is the internal per-instance indexing call `run` spawns with the
deployment's environment.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

DATASET = "princeton-nlp/SWE-bench_Verified"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
DEFAULT_CACHE = _HERE / "datasets" / "swebench_verified.json"
NS_PREFIX = "swe-"
QUERY_MAX_CHARS = 2000  # CodeSearchInput.query max_length
PAGE1_TOP_K = 10
PAGE2_TOP_K = 15
K_CUTS = (1, 5, 10)
BM25_K1 = 1.2
BM25_B = 0.75
BM25_MAX_BYTES = 1_000_000
STEP_MARK = "STEP_RESULT "
# text-embedding-3-large list price, USD per million tokens
EMBED_USD_PER_MTOK = 0.13


# ---------------------------------------------------------------------------
# dataset
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    gold: tuple[str, ...]
    added: tuple[str, ...] = ()


def gold_files(patch: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(files the patch modifies or deletes, files it adds), each in patch
    order. A modified file is named by its pre-image path: that is the path
    present at the base commit."""
    modified: list[str] = []
    added: list[str] = []
    current: str | None = None
    is_new = False

    def flush() -> None:
        if current is None:
            return
        bucket = added if is_new else modified
        if current not in bucket:
            bucket.append(current)

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            current = m.group(1) if m else None
            is_new = False
        elif current is not None and (line.startswith("new file mode") or line == "--- /dev/null"):
            is_new = True
    flush()
    return tuple(modified), tuple(added)


def instance_from_row(row: dict[str, Any]) -> Instance:
    modified, added = gold_files(str(row.get("patch") or ""))
    return Instance(
        instance_id=str(row["instance_id"]),
        repo=str(row["repo"]),
        base_commit=str(row["base_commit"]),
        problem_statement=str(row.get("problem_statement") or ""),
        gold=modified,
        added=added,
    )


def fetch_rows(page: int = 100) -> list[dict[str, Any]]:
    """Every row of the test split through the datasets-server rows API."""
    import httpx

    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=60.0) as client:
        offset = 0
        while True:
            r = client.get(
                ROWS_URL,
                params={
                    "dataset": DATASET,
                    "config": "default",
                    "split": "test",
                    "offset": offset,
                    "length": page,
                },
            )
            r.raise_for_status()
            data = r.json()
            batch = [x["row"] for x in data.get("rows") or []]
            rows.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(data.get("num_rows_total") or 0):
                break
    return rows


def load_instances(cache: Path) -> list[Instance]:
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(fetch_rows()))
    return [instance_from_row(r) for r in json.loads(cache.read_text())]


def stratified_sample(instances: Sequence[Instance], n: int, seed: int) -> list[Instance]:
    """`n` instances with every repository represented: each repository gets
    a share proportional to its size (at least one), drawn with `seed`.
    `n <= 0` or `n >= len` keeps them all. Returned in dataset order."""
    if n <= 0 or n >= len(instances):
        return list(instances)
    by_repo: dict[str, list[Instance]] = collections.defaultdict(list)
    for inst in instances:
        by_repo[inst.repo].append(inst)
    total = len(instances)
    quota = {r: max(1, round(n * len(v) / total)) for r, v in by_repo.items()}
    # trim or pad the largest repositories until the quotas sum to n
    order = sorted(by_repo, key=lambda r: (-len(by_repo[r]), r))
    i = 0
    while sum(quota.values()) > n and i < 10 * len(order):
        r = order[i % len(order)]
        if quota[r] > 1:
            quota[r] -= 1
        i += 1
    i = 0
    while sum(quota.values()) < n and i < 10 * len(order):
        r = order[i % len(order)]
        if quota[r] < len(by_repo[r]):
            quota[r] += 1
        i += 1
    rng = random.Random(seed)
    keep: set[str] = set()
    for r in sorted(by_repo):
        pool = sorted(by_repo[r], key=lambda x: x.instance_id)
        keep.update(x.instance_id for x in rng.sample(pool, min(quota[r], len(pool))))
    return [x for x in instances if x.instance_id in keep]


def namespace_for(repo: str) -> str:
    return NS_PREFIX + re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")


def split_chains(diffs: Sequence[int], *, chain_files: int, rebase_files: int) -> list[int]:
    """Where an ordered run of instances starts a new namespace. `diffs[i]` is
    the changed-file count from instance i to i+1. A new chain starts before
    instance i+1 when that one delta exceeds `rebase_files` (a fresh backfill
    is cheaper than so large a step) or when the chain's accumulated delta
    would pass `chain_files` (chains run side by side). Returns each chain's
    first index; always starts with 0."""
    starts = [0]
    acc = 0
    for i, d in enumerate(diffs):
        if d > rebase_files or (acc + d > chain_files and acc > 0):
            starts.append(i + 1)
            acc = 0
        else:
            acc += d
    return starts


def clone_dir_for(repos_dir: Path, repo: str) -> Path:
    return repos_dir / (repo.replace("/", "__") + ".git")


# ---------------------------------------------------------------------------
# queries and metrics (pure)
# ---------------------------------------------------------------------------


def query_text(problem_statement: str, cap: int = QUERY_MAX_CHARS) -> tuple[str, bool]:
    """The query an agent can send: the statement stripped, cut to the wire
    limit. Returns (text, truncated)."""
    text = problem_statement.strip()
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def ranked_files(paths: Iterable[str]) -> list[str]:
    """Distinct paths in first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def file_metrics(ranked: Sequence[str], gold: Sequence[str]) -> dict[str, Any]:
    """hit@k (any gold file in the first k files), all_gold@10, reciprocal rank
    of the first gold file (0 when none), and that rank (None when none)."""
    gold_set = set(gold)
    first = next((i + 1 for i, p in enumerate(ranked) if p in gold_set), None)
    out: dict[str, Any] = {f"hit@{k}": bool(first is not None and first <= k) for k in K_CUTS}
    out["all_gold@10"] = bool(gold_set) and gold_set.issubset(set(ranked[:10]))
    out["rr"] = 1.0 / first if first else 0.0
    out["first_gold_rank"] = first
    return out


_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercased identifier tokens of two or more characters: each word whole,
    plus its snake_case / camelCase parts when it has more than one."""
    out: list[str] = []
    for w in _WORD.findall(text):
        lw = w.lower()
        if len(lw) >= 2:
            out.append(lw)
        parts = [p.lower() for piece in w.split("_") for p in _CAMEL.findall(piece)]
        if len(parts) > 1:
            out.extend(p for p in parts if len(p) >= 2)
    return out


@dataclass
class Bm25Doc:
    tf: collections.Counter[str]
    length: int


def bm25_doc(path: str, content: str) -> Bm25Doc:
    toks = tokenize(path.replace("/", " ").replace(".", " ")) + tokenize(content)
    return Bm25Doc(tf=collections.Counter(toks), length=len(toks))


def bm25_rank(
    query: str,
    docs: dict[str, Bm25Doc],
    *,
    k1: float = BM25_K1,
    b: float = BM25_B,
    top: int = 25,
) -> list[str]:
    """Paths by BM25 score of the query's distinct terms, best first (ties by
    path); documents scoring 0 are left out."""
    terms = sorted(set(tokenize(query)))
    n = len(docs)
    if not terms or not n:
        return []
    avgdl = sum(d.length for d in docs.values()) / n or 1.0
    df = dict.fromkeys(terms, 0)
    for d in docs.values():
        for t in terms:
            if t in d.tf:
                df[t] += 1
    idf = {t: math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in terms if df[t]}
    scored: list[tuple[float, str]] = []
    for path, d in docs.items():
        s = 0.0
        norm = k1 * (1.0 - b + b * d.length / avgdl)
        for t, w in idf.items():
            f = d.tf.get(t)
            if f:
                s += w * f * (k1 + 1.0) / (f + norm)
        if s > 0.0:
            scored.append((s, path))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _s, p in scored[:top]]


_DOC_EXTS = (".txt", ".rst", ".md", ".html")


def path_class(path: str) -> str:
    """`doc` (prose: release notes, guides), `test` (a test tree or test file)
    or `source` (everything else) — the result-mix axis of the summary."""
    p = path.lower()
    if p.endswith(_DOC_EXTS) or p.startswith(("doc/", "docs/")):
        return "doc"
    parts = p.split("/")
    name = parts[-1]
    if any(x in ("tests", "test", "testing") for x in parts[:-1]) or name.startswith("test_"):
        return "test"
    return "source"


def result_mix(rows: Sequence[dict[str, Any]], files_of: Any, top: int) -> dict[str, float]:
    """Share of the first `top` results per `path_class`, over every row."""
    counts: collections.Counter[str] = collections.Counter()
    for r in rows:
        counts.update(path_class(p) for p in files_of(r)[:top])
    total = sum(counts.values()) or 1
    return {k: round(counts[k] / total, 3) for k in ("source", "test", "doc")}


def percentile(xs: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, q in [0, 100]."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(1, math.ceil(q / 100.0 * len(s)))
    return s[min(k, len(s)) - 1]


def _arm_summary(rows: Sequence[dict[str, Any]], arm: str) -> dict[str, Any]:
    scored = [r[arm] for r in rows if isinstance(r.get(arm), dict) and "rr" in r[arm]]
    n = len(scored)
    out: dict[str, Any] = {"n": n}
    if not n:
        return out
    for k in K_CUTS:
        out[f"hit@{k}"] = round(sum(bool(m[f"hit@{k}"]) for m in scored) / n, 4)
    out["all_gold@10"] = round(sum(bool(m["all_gold@10"]) for m in scored) / n, 4)
    out["mrr"] = round(sum(float(m["rr"]) for m in scored) / n, 4)
    return out


def summarize(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The run's headline numbers: per arm overall and per repository, with
    first-page latency and tokens for code.search."""
    ok = [r for r in rows if not r.get("error")]
    loom = [r for r in ok if isinstance(r.get("loom"), dict)]
    lat = [float(r["loom"]["ms"]) for r in loom if r["loom"].get("ms") is not None]
    toks = [int(r["loom"].get("tokens_returned") or 0) for r in loom]
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in ok:
        by_repo[str(r["repo"])].append(r)
    abstained = sum(bool(r["loom"].get("abstain")) for r in loom)
    return {
        "instances": len(rows),
        "errors": len(rows) - len(ok),
        "truncated_queries": sum(bool(r.get("query_truncated")) for r in ok),
        "loom": {
            **_arm_summary(ok, "loom"),
            "page1": _arm_summary(ok, "loom_page1"),
            "latency_ms_p50": percentile(lat, 50),
            "latency_ms_p95": percentile(lat, 95),
            "tokens_returned_mean": round(statistics.fmean(toks), 1) if toks else None,
            "tokens_returned_p50": percentile([float(t) for t in toks], 50),
            "abstained": abstained,
            "top5_unit_mix": result_mix(
                loom, lambda r: [u["path"] for u in r["loom"].get("units") or []], 5
            ),
            "top5_file_mix": result_mix(loom, lambda r: r["loom"].get("files") or [], 5),
        },
        "bm25": {
            **_arm_summary(ok, "bm25"),
            "top5_file_mix": result_mix(
                [r for r in ok if isinstance(r.get("bm25"), dict)],
                lambda r: r["bm25"].get("files") or [],
                5,
            ),
        },
        "bm25_py": _arm_summary(ok, "bm25_py"),
        "gold_mix": result_mix(ok, lambda r: r.get("gold") or [], 100),
        "gold_indexed_share": (
            round(
                sum(bool(r.get("gold_indexed")) for r in ok)
                / max(1, sum(r.get("gold_indexed") is not None for r in ok)),
                4,
            )
        ),
        "by_repo": {
            repo: {
                "n": len(rs),
                "loom": _arm_summary(rs, "loom"),
                "bm25": _arm_summary(rs, "bm25"),
            }
            for repo, rs in sorted(by_repo.items())
        },
    }


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git(git_dir: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def ensure_clone(repos_dir: Path, repo: str) -> Path:
    path = clone_dir_for(repos_dir, repo)
    if not path.exists():
        repos_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", f"https://github.com/{repo}.git", str(path)],
            check=True,
        )
    return path


def commit_time(git_dir: Path, sha: str) -> int:
    return int(_git(git_dir, "show", "-s", "--format=%ct", sha).strip() or 0)


def tree_blobs(git_dir: Path, sha: str) -> dict[str, tuple[str, int]]:
    """path -> (blob sha, size) at `sha`."""
    out: dict[str, tuple[str, int]] = {}
    for line in _git(git_dir, "ls-tree", "-r", "-l", "-z", sha).split(b"\0"):
        if not line:
            continue
        meta, _, path = line.partition(b"\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != b"blob" or not parts[3].isdigit():
            continue
        out[path.decode("utf-8", "replace")] = (parts[2].decode(), int(parts[3]))
    return out


def read_blobs(git_dir: Path, shas: Sequence[str]) -> dict[str, bytes]:
    """Blob contents through one `cat-file --batch`."""
    if not shas:
        return {}
    raw = _git(git_dir, "cat-file", "--batch", input_bytes=("\n".join(shas) + "\n").encode())
    out: dict[str, bytes] = {}
    pos = 0
    while pos < len(raw):
        nl = raw.index(b"\n", pos)
        header = raw[pos:nl].split()
        pos = nl + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        out[header[0].decode()] = raw[pos : pos + size]
        pos += size + 1
    return out


class Bm25Corpus:
    """Per-repository BM25 documents keyed by blob sha, so consecutive commits
    tokenize only the blobs that changed."""

    def __init__(self, git_dir: Path) -> None:
        self.git_dir = git_dir
        self._by_blob: dict[str, Bm25Doc | None] = {}

    def docs_at(self, sha: str, universe: set[str] | None) -> dict[str, Bm25Doc]:
        blobs = tree_blobs(self.git_dir, sha)
        if universe is not None:
            blobs = {p: v for p, v in blobs.items() if p in universe}
        blobs = {p: v for p, v in blobs.items() if v[1] <= BM25_MAX_BYTES}
        missing = sorted({b for b, _ in blobs.values() if b not in self._by_blob})
        path_of = {b: p for p, (b, _) in blobs.items()}
        for i in range(0, len(missing), 500):
            got = read_blobs(self.git_dir, missing[i : i + 500])
            for b in missing[i : i + 500]:
                data = got.get(b)
                if data is None or b"\0" in data[:8000]:
                    self._by_blob[b] = None
                    continue
                self._by_blob[b] = bm25_doc(path_of[b], data.decode("utf-8", "replace"))
        live = {b for b, _ in blobs.values()}
        for b in [b for b in self._by_blob if b not in live]:
            del self._by_blob[b]
        docs: dict[str, Bm25Doc] = {}
        for p, (b, _) in blobs.items():
            d = self._by_blob.get(b)
            if d is not None:
                # a blob shared by two paths keeps its own path tokens per path
                docs[p] = d if path_of.get(b) == p else bm25_doc_shared(p, d)
        return docs


def bm25_doc_shared(path: str, base: Bm25Doc) -> Bm25Doc:
    """`base` re-pathed: the content counts stay, the path tokens are `path`'s.
    (Identical files at two paths are rare; the base's own path tokens stay in
    the count, a negligible bias.)"""
    extra = tokenize(path.replace("/", " ").replace(".", " "))
    tf = collections.Counter(base.tf)
    tf.update(extra)
    return Bm25Doc(tf=tf, length=base.length + len(extra))


# ---------------------------------------------------------------------------
# index step (runs inside the deployment's environment)
# ---------------------------------------------------------------------------


def load_env(env_file: str) -> dict[str, str]:
    """os.environ with the .env's keys filled in (process env wins), parsed
    the way the server's settings parse it; OTEL exporters dropped."""
    from dotenv import dotenv_values

    env = dict(os.environ)
    if Path(env_file).exists():
        for k, v in dotenv_values(env_file).items():
            if v is not None:
                env.setdefault(k, v)
    for k in list(env):
        if k.startswith(("LOOM_OTEL", "OTEL_")):
            env.pop(k)
    return env


def step_env(env_file: str, org: str, ns: str, repo: str, git_dir: Path) -> dict[str, str]:
    env = load_env(env_file)
    env.update(
        {
            "LOOM_CODE_REPOS_JSON": json.dumps(
                [
                    {
                        "org": org,
                        "namespace": ns,
                        "repo": repo,
                        "git_dir": str(git_dir),
                        "path_prefixes": "",
                        "org_readable": False,
                    }
                ]
            ),
            "LOOM_CODE_ORG": org,
            "LOOM_CODE_NAMESPACE": ns,
            # a backfilled namespace carries no chunk descriptions; deltas match it
            "LOOM_CODE_CONTEXT_DESCRIPTION_ENABLED": "false",
            "LOOM_CODE_INGEST_MAX_FILES_PER_COMMIT": "100000",
            "LOOM_BACKGROUND_WORKERS": "off",
            "LOOM_BOOT_MIGRATE": "false",
            "LOOM_OTEL_ENABLED": "false",
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(_ROOT),
        }
    )
    return env


async def _repair_delta(
    repo: Any, cfg: Any, org: str, ns: str, head: str, sha: str, concurrency: int, scratch: str
) -> dict[str, Any]:
    """The operator's repair path between two commits: deleted paths
    tombstoned, added and modified paths re-ingested at `sha` by
    `loom code backfill --paths-file` (its file concurrency), then the
    watermark moved. Dependents are not re-resolved (the commit step's job)."""
    from server.cli.code_backfill import _backfill
    from server.code.app.index import gitrepo
    from server.code.app.index.ingest import tombstone_file
    from server.embedding.embedder import get_embedder

    entries = await gitrepo.diff_files(cfg.git_dir, head, sha)
    gone = [e.path for e in entries if e.status == "D"]
    changed = sorted({e.path for e in entries if e.status != "D"})
    model = get_embedder().model_name
    for path in gone:
        await tombstone_file(repo, org, ns, path=path, embedding_model=model)
    listed = Path(scratch + ".delta")
    listed.write_text("\n".join(changed))
    rc = 0
    if changed:
        rc = await _backfill(
            argparse.Namespace(
                org=org,
                namespace=ns,
                phase="index",
                embed=True,
                include_poisoned=False,
                allow_fake_embedder=False,
                no_fetch=True,
                limit=0,
                commit=sha,
                paths_file=str(listed),
                concurrency=concurrency,
            )
        )
    if rc == 0:
        await repo.set_state(org, ns, "git_head", sha, detail=f"bench repair from {head[:12]}")
    return {"mode": "repair", "rc": rc, "deleted": len(gone), "changed": len(changed)}


async def _index_step(
    org: str, ns: str, sha: str, concurrency: int, paths_out: str, delta_mode: str = "commit"
) -> dict:
    """Bring `org/ns` to `sha`: a full embedded backfill when the namespace has
    no watermark, else the per-merge ingest step from the watermark to `sha`.
    Returns wall time, billed embedding tokens and the live path count; writes
    the live paths to `paths_out`."""
    from server.code.app.index.pass_budget import CodeIngestResult
    from server.code.app.index.pass_stream import CommitStream
    from server.code.app.index.repo_registry import read_registry_or_exit
    from server.code.domain.index.repos import repo_for_namespace
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client
    from server.observability import cost_accumulator

    billed = {"tokens": 0}
    real_report = cost_accumulator.report_embed

    def counting(model: str, tokens: int) -> None:
        billed["tokens"] += int(tokens)
        real_report(model, tokens)

    cost_accumulator.report_embed = counting  # type: ignore[assignment]
    await get_client().connect()
    await read_registry_or_exit("index-step")
    repo = CodeRepo()
    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        raise SystemExit(f"{org}/{ns} is not configured")
    head = str(await repo.get_state(org, ns, "git_head") or "")
    started = time.monotonic()
    out: dict[str, Any] = {"from": head, "to": sha}
    if not head:
        from server.cli.code_backfill import _backfill

        args = argparse.Namespace(
            org=org,
            namespace=ns,
            phase="index",
            embed=True,
            include_poisoned=False,
            allow_fake_embedder=False,
            no_fetch=True,
            limit=0,
            commit=sha,
            paths_file="",
            concurrency=concurrency,
        )
        out["mode"] = "backfill"
        out["rc"] = await _backfill(args)
    elif head == sha:
        out["mode"] = "noop"
        out["rc"] = 0
    elif delta_mode == "repair":
        out.update(await _repair_delta(repo, cfg, org, ns, head, sha, concurrency, paths_out))
    else:
        result = CodeIngestResult()
        stream = CommitStream(cfg, repo, sha, head, [sha], result)
        stream._ctx = await stream._prepare()
        stream._ctx["build_brief"] = None  # review briefs are not what this bench reads
        landed = await stream.step(clock=None)
        out["mode"] = "delta"
        out["rc"] = 0 if landed else 1
        out["files_indexed"] = result.files_indexed
        out["chunks_new"] = result.chunks_new
        out["dependents"] = result.dependents_reingested
        out["errors"] = result.errors[:5]
    out["wall_s"] = round(time.monotonic() - started, 1)
    out["embed_tokens"] = billed["tokens"]
    paths = await repo.live_paths_under(org, ns, "", limit=1_000_000)
    if out.get("rc") == 0:
        # a live path the commit's tree does not hold (an interrupted step's
        # leftovers) is tombstoned, so the namespace is exactly the tree
        from server.code.app.index import gitrepo
        from server.code.app.index.ingest import tombstone_file
        from server.embedding.embedder import get_embedder

        tree = set(await gitrepo.ls_tree(cfg.git_dir, sha))
        extra = [p for p in paths if p not in tree]
        model = get_embedder().model_name
        for path in extra:
            await tombstone_file(repo, org, ns, path=path, embedding_model=model)
        out["stale_tombstoned"] = len(extra)
        paths = [p for p in paths if p in tree]
    Path(paths_out).write_text("\n".join(paths))
    out["live_paths"] = len(paths)
    # the watermark write is read back from any replica: give it a moment
    for _ in range(10):
        out["git_head"] = str(await repo.get_state(org, ns, "git_head") or "")
        if out["git_head"] == sha:
            break
        await asyncio.sleep(1.0)
    return out


def cmd_index_step(args: argparse.Namespace) -> int:
    out = asyncio.run(
        _index_step(
            args.org,
            args.namespace,
            args.commit,
            args.concurrency,
            args.paths_out,
            args.delta_mode,
        )
    )
    print(STEP_MARK + json.dumps(out), flush=True)
    return 0 if out.get("rc") == 0 and out.get("git_head") == args.commit else 1


def run_index_step(
    env_file: str,
    org: str,
    ns: str,
    repo: str,
    git_dir: Path,
    sha: str,
    *,
    concurrency: int,
    paths_out: Path,
    log: Path,
    delta_mode: str = "commit",
) -> dict[str, Any]:
    env = step_env(env_file, org, ns, repo, git_dir)
    cmd = [
        sys.executable,
        "-m",
        "bench.swebench_localization",
        "index-step",
        "--org",
        org,
        "--namespace",
        ns,
        "--commit",
        sha,
        "--concurrency",
        str(concurrency),
        "--paths-out",
        str(paths_out),
        "--delta-mode",
        delta_mode,
    ]
    with log.open("a") as fh:
        fh.write(f"\n# {' '.join(cmd)}\n")
        fh.flush()
        start = fh.tell()
        rc = subprocess.call(cmd, cwd=str(_ROOT), env=env, stdout=fh, stderr=subprocess.STDOUT)
    result: dict[str, Any] = {"rc": rc, "exit": rc}
    with log.open() as fh:
        fh.seek(start)
        for line in fh:
            if line.startswith(STEP_MARK):
                result = {**json.loads(line[len(STEP_MARK) :]), "exit": rc}
    return result


# ---------------------------------------------------------------------------
# code.search
# ---------------------------------------------------------------------------


async def search_files(
    client: Any, org: str, ns: str, query: str, token: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(the page-1 record, the 25-unit file ranking record)."""
    try:
        from bench._http import apost
    except ModuleNotFoundError:  # pragma: no cover — run as a script
        from _http import apost  # type: ignore[no-redef]

    body: dict[str, Any] = {
        "org": org,
        "namespace": ns,
        "query": query,
        "top_k": PAGE1_TOP_K,
        "consumer": "bench",
        "fresh": True,
    }
    t0 = time.perf_counter()
    r = await apost(client, "/v1/code.search", body, token=token, timeout_s=120.0)
    ms = (time.perf_counter() - t0) * 1000.0
    data = r.json()
    units = data.get("results") or []
    work = data.get("work") or {}
    page1 = {
        "ms": round(ms, 1),
        "units": [
            {"path": u.get("path"), "kind": u.get("kind"), "via": u.get("via")} for u in units
        ],
        "route": data.get("route"),
        "abstain": bool(data.get("abstain")),
        "abstain_code": data.get("abstain_code") or "",
        "retrieval_confidence": data.get("retrieval_confidence"),
        "tokens_returned": int(data.get("tokens_returned") or 0),
        "question_kind": work.get("question_kind") if isinstance(work, dict) else None,
        "legs_timed_out": data.get("legs_timed_out") or [],
        "partial": bool(data.get("partial")),
    }
    paths = [str(u.get("path") or "") for u in units]
    cursor = str(data.get("next_cursor") or "")
    page2_paths: list[str] = []
    if cursor:
        try:
            r2 = await apost(
                client,
                "/v1/code.search",
                {**body, "cursor": cursor, "top_k": PAGE2_TOP_K, "bodies": "none"},
                token=token,
                timeout_s=120.0,
            )
            page2_paths = [str(u.get("path") or "") for u in (r2.json().get("results") or [])]
        except Exception as e:  # noqa: BLE001 — page 2 only deepens the ranking
            page1["page2_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return page1, {"files": ranked_files(paths + page2_paths)}


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    base_url: str
    token: str
    env_file: str
    repos_dir: Path
    out: Path
    org: str
    concurrency: int
    work_dir: Path
    min_free_gb: float
    delta_mode: str = "commit"
    instances: list[Instance] = field(default_factory=list)


def _done_ids(out: Path) -> set[str]:
    if not out.exists():
        return set()
    ids: set[str] = set()
    for line in out.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            if not row.get("error"):
                ids.add(str(row["instance_id"]))
    return ids


def free_gb(path: str = "/") -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def order_by_commit_time(insts: Sequence[Instance], git_dir: Path) -> list[Instance]:
    return sorted(insts, key=lambda x: (commit_time(git_dir, x.base_commit), x.instance_id))


async def run_chain(cfg: RunConfig, repo: str, ns: str, todo: list[Instance]) -> None:
    """One namespace's instances, oldest base commit first."""
    import httpx

    git_dir = clone_dir_for(cfg.repos_dir, repo)
    corpus = Bm25Corpus(git_dir)
    done = _done_ids(cfg.out)
    log = cfg.work_dir / f"{ns}.index.log"
    paths_file = cfg.work_dir / f"{ns}.paths"
    async with httpx.AsyncClient(base_url=cfg.base_url, timeout=120.0) as client:
        for inst in todo:
            if inst.instance_id in done:
                continue
            if free_gb() < cfg.min_free_gb:
                raise SystemExit(f"stopping: {free_gb():.0f} GB free < {cfg.min_free_gb}")
            row: dict[str, Any] = {
                "instance_id": inst.instance_id,
                "repo": repo,
                "chain": ns,
                "base_commit": inst.base_commit,
                "namespace": ns,
                "gold": list(inst.gold),
                "added": list(inst.added),
            }
            step = run_index_step(
                cfg.env_file,
                cfg.org,
                ns,
                repo,
                git_dir,
                inst.base_commit,
                concurrency=cfg.concurrency,
                paths_out=paths_file,
                log=log,
                delta_mode=cfg.delta_mode,
            )
            row["index"] = step
            if step.get("exit") != 0:
                row["error"] = f"index step failed: {json.dumps(step)[:300]}"
                _append(cfg.out, row)
                print(f"{inst.instance_id}: {row['error']}", flush=True)
                continue
            live = set(paths_file.read_text().splitlines())
            row["gold_indexed"] = all(g in live for g in inst.gold)
            query, truncated = query_text(inst.problem_statement)
            row["query_chars"] = len(inst.problem_statement.strip())
            row["query_truncated"] = truncated
            try:
                page1, deep = await search_files(client, cfg.org, ns, query, cfg.token)
            except Exception as e:  # noqa: BLE001 — recorded, the run goes on
                row["error"] = f"code.search: {type(e).__name__}: {str(e)[:300]}"
                _append(cfg.out, row)
                print(f"{inst.instance_id}: {row['error']}", flush=True)
                continue
            p1_files = ranked_files(u["path"] for u in page1["units"])
            row["loom_page1"] = {**file_metrics(p1_files, inst.gold), "files": p1_files}
            row["loom"] = {**page1, **file_metrics(deep["files"], inst.gold), **deep}
            t0 = time.perf_counter()
            docs = corpus.docs_at(inst.base_commit, live or None)
            bm = bm25_rank(query, docs)
            row["bm25"] = {
                **file_metrics(bm, inst.gold),
                "files": bm[:25],
                "docs": len(docs),
                "ms": round((time.perf_counter() - t0) * 1000.0, 1),
            }
            _append(cfg.out, row)
            print(
                f"{inst.instance_id}: {step.get('mode')} {step.get('wall_s')}s "
                f"tok={step.get('embed_tokens')} loom@10={row['loom']['hit@10']} "
                f"rank={row['loom']['first_gold_rank']} bm25@10={row['bm25']['hit@10']} "
                f"{page1['ms']:.0f}ms",
                flush=True,
            )


_APPEND_LOCK = threading.Lock()


def _append(out: Path, row: dict[str, Any]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK, out.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def select(args: argparse.Namespace) -> list[Instance]:
    """The stratified sample of the whole set, then the `--repos` / `--ids`
    filters, so a filtered run is a slice of the same sample."""
    insts = [i for i in load_instances(Path(args.cache)) if i.gold]
    insts = stratified_sample(insts, args.sample, args.seed)
    if args.repos:
        wanted = set(args.repos.split(","))
        insts = [i for i in insts if i.repo in wanted]
    if args.ids:
        wanted = set(args.ids.split(","))
        insts = [i for i in insts if i.instance_id in wanted]
    return insts


def cmd_run(args: argparse.Namespace) -> int:
    token = Path(args.token_file).read_text().strip() if args.token_file else ""
    cfg = RunConfig(
        base_url=args.base_url,
        token=token,
        env_file=args.env_file,
        repos_dir=Path(args.repos_dir),
        out=Path(args.out),
        org=args.org,
        concurrency=args.concurrency,
        work_dir=Path(args.work_dir),
        min_free_gb=args.min_free_gb,
        delta_mode=args.delta_mode,
    )
    cfg.work_dir.mkdir(parents=True, exist_ok=True)
    chains = plan_chains(
        select(args),
        cfg.repos_dir,
        chain_files=args.chain_files,
        rebase_files=args.rebase_files,
    )
    (cfg.work_dir / "chains.json").write_text(
        json.dumps({ns: [i.instance_id for i in insts] for _r, ns, insts, _c in chains}, indent=1)
    )
    # longest chain first over `--lanes` workers: the makespan is the longest chain
    queue = sorted(chains, key=lambda c: -c[3])
    lock = threading.Lock()

    def lane() -> None:
        while True:
            with lock:
                if not queue:
                    return
                repo, ns, insts, _cost = queue.pop(0)
            asyncio.run(run_chain(cfg, repo, ns, insts))

    threads = [threading.Thread(target=lane) for _ in range(max(1, args.lanes))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return 0


def changed_files(git_dir: Path, a: str, b: str) -> int:
    return len(_git(git_dir, "diff", "--name-only", "-z", a, b).split(b"\0")) - 1


def plan_chains(
    insts: Sequence[Instance], repos_dir: Path, *, chain_files: int, rebase_files: int
) -> list[tuple[str, str, list[Instance], int]]:
    """(repo, namespace, ordered instances, estimated changed files incl. the
    first backfill's tree) per chain."""
    by_repo: dict[str, list[Instance]] = collections.defaultdict(list)
    for inst in insts:
        by_repo[inst.repo].append(inst)
    out: list[tuple[str, str, list[Instance], int]] = []
    for repo in sorted(by_repo):
        git_dir = ensure_clone(repos_dir, repo)
        ordered = order_by_commit_time(by_repo[repo], git_dir)
        diffs = [
            changed_files(git_dir, ordered[i].base_commit, ordered[i + 1].base_commit)
            for i in range(len(ordered) - 1)
        ]
        starts = split_chains(diffs, chain_files=chain_files, rebase_files=rebase_files)
        tree = len(tree_blobs(git_dir, ordered[0].base_commit))
        for k, a in enumerate(starts):
            b = starts[k + 1] if k + 1 < len(starts) else len(ordered)
            cost = tree + sum(diffs[a : b - 1])
            out.append((repo, f"{namespace_for(repo)}-c{k}", ordered[a:b], cost))
    return out


def cmd_plan(args: argparse.Namespace) -> int:
    insts = select(args)
    every = load_instances(Path(args.cache))
    counts = collections.Counter(i.repo for i in insts)
    print(
        json.dumps(
            {
                "dataset_rows": len(every),
                "added_only_skipped": sum(1 for i in every if not i.gold),
                "selected": len(insts),
                "by_repo": dict(counts.most_common()),
            },
            indent=1,
        )
    )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    insts = load_instances(Path(args.cache))
    print(f"{len(insts)} instances cached at {args.cache}")
    return 0


def _read_rows(path: Path) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            prev = rows.get(r["instance_id"])
            if prev is None or prev.get("error"):
                rows[r["instance_id"]] = r  # a later success replaces an earlier error
    return list(rows.values())


def index_cost(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    steps = [r.get("index") or {} for r in rows]
    toks = sum(int(s.get("embed_tokens") or 0) for s in steps)
    by_mode: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: {"n": 0, "wall_s": 0.0, "embed_tokens": 0}
    )
    for s in steps:
        m = by_mode[str(s.get("mode") or "?")]
        m["n"] += 1
        m["wall_s"] += float(s.get("wall_s") or 0.0)
        m["embed_tokens"] += int(s.get("embed_tokens") or 0)
    return {
        "embed_tokens": toks,
        "embed_usd": round(toks / 1e6 * EMBED_USD_PER_MTOK, 2),
        "wall_s": round(sum(float(s.get("wall_s") or 0.0) for s in steps), 1),
        "by_mode": {k: {**v, "wall_s": round(v["wall_s"], 1)} for k, v in by_mode.items()},
    }


def cmd_bm25_py(args: argparse.Namespace) -> int:
    """Add a `bm25_py` arm to every row: BM25 over the `.py` files of the tree at
    the instance's base commit (the usual file universe of Python localization
    baselines), same query and scorer. Offline: git only."""
    rows = _read_rows(Path(args.rows))
    by_repo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by_repo[str(r["repo"])].append(r)
    insts = {i.instance_id: i for i in load_instances(Path(args.cache))}
    for repo, rs in by_repo.items():
        git_dir = clone_dir_for(Path(args.repos_dir), repo)
        corpus = Bm25Corpus(git_dir)
        rs.sort(key=lambda r: commit_time(git_dir, r["base_commit"]))
        for r in rs:
            if r.get("error"):
                continue
            inst = insts[r["instance_id"]]
            py = {p for p in tree_blobs(git_dir, inst.base_commit) if p.endswith(".py")}
            docs = corpus.docs_at(inst.base_commit, py)
            ranked = bm25_rank(query_text(inst.problem_statement)[0], docs)
            r["bm25_py"] = {**file_metrics(ranked, inst.gold), "files": ranked, "docs": len(docs)}
    out = Path(args.out or args.rows)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(json.dumps(_arm_summary([r for r in rows if not r.get("error")], "bm25_py")))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rows = _read_rows(Path(args.rows))
    summary = {**summarize(rows), "index": index_cost(rows)}
    out = Path(args.summary or str(args.rows).replace(".jsonl", ".summary.json"))
    out.write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cache", default=str(DEFAULT_CACHE))
        p.add_argument("--sample", type=int, default=0, help="stratified sample size (0 = all)")
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--repos", default="", help="CSV of owner/name to keep")
        p.add_argument("--ids", default="", help="CSV of instance ids to keep")

    p = sub.add_parser("fetch")
    p.add_argument("--cache", default=str(DEFAULT_CACHE))
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("plan")
    common(p)
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("run")
    common(p)
    p.add_argument("--base-url", required=True)
    p.add_argument("--token-file", default="")
    p.add_argument("--env-file", required=True, help="the deployment .env the index step uses")
    p.add_argument("--repos-dir", required=True, help="where the bare clones live")
    p.add_argument("--work-dir", default="var/bench/swebench")
    p.add_argument("--out", required=True)
    p.add_argument("--org", default="dev")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--min-free-gb", type=float, default=50.0)
    p.add_argument(
        "--delta-mode",
        choices=["commit", "repair"],
        default="commit",
        help="commit: the per-merge ingest step; repair: tombstones + backfill --paths-file",
    )
    p.add_argument("--lanes", type=int, default=1, help="chains indexed side by side")
    p.add_argument(
        "--chain-files",
        type=int,
        default=4000,
        help="changed files one namespace's deltas may add up to before a new chain starts",
    )
    p.add_argument(
        "--rebase-files",
        type=int,
        default=1500,
        help="a single delta larger than this starts a new chain (a fresh backfill)",
    )
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("index-step")
    p.add_argument("--org", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--paths-out", required=True)
    p.add_argument("--delta-mode", choices=["commit", "repair"], default="commit")
    p.set_defaults(func=cmd_index_step)
    p = sub.add_parser("bm25-py")
    p.add_argument("--rows", required=True)
    p.add_argument("--out", default="", help="default: rewrite --rows")
    p.add_argument("--repos-dir", required=True)
    p.add_argument("--cache", default=str(DEFAULT_CACHE))
    p.set_defaults(func=cmd_bm25_py)
    p = sub.add_parser("report")
    p.add_argument("--rows", required=True)
    p.add_argument("--summary", default="")
    p.set_defaults(func=cmd_report)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
