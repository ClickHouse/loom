"""Scope probe: one namespace per task versus one shared namespace with run_id.

Replays the formal-reasoning arm's stored lemmas (``loom-verbatim-unit``
result files: ``query`` + ``response`` per subtask) into loom twice — once per
paper namespace, once into a single shared namespace tagged ``run_id=<paper>``
— then, for every subtask after the first, runs the same query under three
conditions: A = the paper's namespace, B = shared namespace, C = shared
namespace with the ``run_id`` filter. Reports per condition: previous lemma in
the top 3, hits from another paper, hits from another namespace (the
``namespace_scope`` fan-out), and the Jaccard overlap of A and
C. Every lemma of a paper is written up front, so a query can also retrieve a
later lemma; the cross-task and cross-namespace counts are the point, not the
previous-lemma rate.

Env: LOOM_BASE_URL (default the isolated :7841), LOOM_API_KEY file next to this
script or LOOM_API_KEY, MA_RESULTS (arm result directory), SEARCH_ONLY=1 to skip
the writes, NS_SCOPE=this to pin the scope, OUT_TAG for the output file name.
"""

import collections
import glob
import json
import os
import sys
import time

import requests

S = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("LOOM_BASE_URL", "http://127.0.0.1:7841")
TOK = os.environ.get("LOOM_API_KEY") or open(f"{S}/ma_loom_token").read().strip()
ARM = os.environ.get("MA_RESULTS", f"{S}/memoryarena/results/json/math/loom-verbatim-unit")
H = {"Authorization": f"Bearer {TOK}", "content-type": "application/json"}


def call(op, inp):
    for a in range(12):
        r = requests.post(f"{BASE}/v1/{op}", headers=H, json=inp, timeout=120)
        if r.status_code == 429:
            time.sleep(2 + 2 * a)
            continue
        if r.status_code >= 500:
            time.sleep(3 + 2 * a)
            continue
        r.raise_for_status()
        d = r.json()
        return d.get("result", d)
    raise RuntimeError(f"{op} failed {r.status_code} {r.text[:200]}")


papers = {}
for f in sorted(glob.glob(f"{ARM}/*/result.jsonl")):
    p = f.split("/")[-2]
    rows = [json.loads(line) for line in open(f) if line.strip()]
    papers[p] = rows
SHARED = "bench-ma-probe-shared"
tag = sys.argv[1] if len(sys.argv) > 1 else "v1"
SHARED += "-" + tag


def ns_of(p):
    return f"bench-ma-probe-{tag}-{p[:8].replace('.', '')}"


t0 = time.time()
nset = 0
SEARCH_ONLY = os.environ.get("SEARCH_ONLY") == "1"
for p, rows in ({} if SEARCH_ONLY else papers).items():
    for i, r in enumerate(rows):
        val = f"## Task: {r['query']}\n## solution: {r['response']}\n"
        for ns, run in ((SHARED, p), (ns_of(p), p)):
            call(
                "memory.set",
                {
                    "org": "dev",
                    "namespace": ns,
                    "agent": "ma",
                    "user_id": "-",
                    "memory_key": f"{p}/session_{i:04d}",
                    "memory_type": "semantic",
                    "value": val,
                    "run_id": run,
                },
            )
            nset += 1
print(f"wrote {nset} memories in {time.time() - t0:.0f}s", flush=True)
time.sleep(3)


def search(ns, q, run=None):
    inp = {
        "org": "dev",
        "namespace": ns,
        "agent": "*",
        "query": q[:4000],
        "top_k": 3,
        "search_mode": "rrf",
    }
    if os.environ.get("NS_SCOPE"):
        inp["namespace_scope"] = os.environ["NS_SCOPE"]
    if run:
        inp["run_id"] = run
    res = call("memory.search", inp)
    hits = res.get("results") or res.get("hits") or []
    NS_LOG.append([(h.get("namespace"), h.get("memory_key")) for h in hits])
    return [h.get("memory_key") or h.get("key") for h in hits]


agg = collections.defaultdict(list)
out = []
NS_LOG = []
for p, rows in papers.items():
    for i, r in enumerate(rows):
        if i == 0:
            continue
        q = r["query"]
        prev = f"{p}/session_{i - 1:04d}"
        A = search(ns_of(p), q)
        B = search(SHARED, q)
        C = search(SHARED, q, run=p)
        rec = dict(paper=p, n=i, A=A, B=B, C=C)
        agg["A_prev"].append(prev in A)
        agg["B_prev"].append(prev in B)
        agg["C_prev"].append(prev in C)
        agg["B_foreign"].append(sum(1 for k in B if not k.startswith(p + "/")) / max(1, len(B)))
        agg["B_foreign_any"].append(any(not k.startswith(p + "/") for k in B))
        agg["C_foreign"].append(sum(1 for k in C if not k.startswith(p + "/")))
        agg["A_n"].append(len(A))
        agg["B_n"].append(len(B))
        agg["C_n"].append(len(C))
        sa, sc = set(A), set(C)
        agg["AC_jacc"].append(len(sa & sc) / max(1, len(sa | sc)))
        agg["B_same_future"].append(
            any(k.startswith(p + "/") and int(k.split("_")[-1]) >= i for k in B)
        )
        out.append(rec)
n = len(out)
print(f"queries {n}")
for k, v in agg.items():
    print(f"{k:14s} {sum(v) / n:.3f}")
json.dump(out, open(f"{S}/ma_shared_probe_{os.environ.get('OUT_TAG', tag)}.json", "w"))
json.dump(NS_LOG, open(f"{S}/ma_shared_probe_{os.environ.get('OUT_TAG', tag)}_ns.json", "w"))
