"""A coding agent finds the files to fix an issue, with and without Loom's code tools.

For each SWE-bench Verified instance, the repository is checked out at the instance's
`base_commit` and a headless Claude Code is asked to name the files its fix must change,
without editing anything. The `without` arm has the built-in read-only tools (Bash, Read,
Grep, Glob); the `with` arm has the same plus Loom's MCP tools over a code index of that
commit. `--session default` runs a stock session with no user or project settings (so no
personal hooks or plugins), where MCP tool definitions are deferred behind tool search as a
user's session defers them; `--session bare` lists every tool up front. Each run reports
tokens (fresh, cache-written and cache-read input, output), cost, turns and wall time, and
is scored against the files the gold patch modifies.

    python -m bench.agent_localization_ab --verified verified.json --repos-dir <bare clones> \\
        --work-dir /tmp/ab --instances id1,id2 --namespaces '{"id1": "swe-...-c0"}' \\
        --mcp-url http://127.0.0.1:7781/mcp --token-file <file> --org dev \\
        --model claude-sonnet-5 --out out/agent_ab.jsonl

`ANTHROPIC_API_KEY` must be set (bare mode reads no stored login).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any

PROMPT = """Below is a GitHub issue for the repository in the current directory, checked out at \
the commit the issue was filed against. Find the source files that a fix must change. Do not \
edit any file and do not run tests.

When you are done, end your reply with one line holding a JSON list of at most 3 repository-\
relative file paths, most likely first, for example ["src/pkg/module.py"].
{tools_note}
<issue>
{issue}
</issue>"""

QUESTION_PROMPT = """Below is a question about the repository in the current directory. Find \
the source files that answer it: where the behaviour is implemented, or the code a fix or \
change would touch. Do not edit any file and do not run tests or builds.

When you are done, end your reply with one line holding a JSON list of at most 3 repository-\
relative file paths, most relevant first, for example ["src/pkg/module.cpp"].
{tools_note}
<question>
{issue}
</question>"""

CALLERS_PROMPT = """Below is a question about the repository in the current directory. Find \
every function that calls the named function. Do not edit any file and do not run tests or \
builds.

When you are done, end your reply with one line holding a JSON list of the calling functions' \
qualified names, namespace and class included, for example ["DB::StorageMergeTree::read", \
"DB::executeQuery"].
{tools_note}
<question>
{issue}
</question>"""

WORK_PROMPT = """Below is a question from an engineer working in the repository in the current \
directory. Answer it as you would for a colleague: name the functions (Class::method) and files \
involved and explain what they do. Do not edit any file and do not run tests or builds.
{tools_note}
<question>
{issue}
</question>"""

TOOLS_NOTE = """
Loom's code tools index this repository at this commit: pass org "{org}" and namespace \
"{namespace}" on every loom_code_* call."""

BUILTIN = ["Bash", "Read", "Grep", "Glob"]


def gold_files(patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", patch, flags=re.M)))


def answer_files(text: str) -> list[str]:
    for line in reversed(text.strip().splitlines()):
        m = re.search(r"\[.*\]", line)
        if not m:
            continue
        try:
            got = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(got, list):
            return [str(p).lstrip("./") for p in got][:3]
    return []


def answer_callers(text: str) -> list[str]:
    """The caller names in the last JSON list of strings in the reply, in order, without
    repeats; the list may span lines."""
    for m in reversed(list(re.finditer(r"\[[^\[\]]*\]", text, flags=re.S))):
        try:
            got = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(got, list) and got and all(isinstance(n, str) for n in got):
            return list(dict.fromkeys(n.strip() for n in got if n.strip()))
    return []


def caller_key(name: str) -> str:
    """The last two `::` components of a function name, case kept: `DB::A::b(int) const` and
    `A::b` are the same caller. Anonymous-namespace components, which the index spells out and
    a reader does not, are dropped first."""
    n = re.sub(r"\(.*\)\s*(const)?\s*$", "", name.strip().strip("`"))
    parts = [
        p for p in n.split("::") if p and not p.startswith(("$anonymous", "(anonymous", "{anon"))
    ]
    return "::".join(parts[-2:])


def checkout(repos_dir: Path, work_dir: Path, repo: str, commit: str) -> Path:
    bare = repos_dir / f"{repo.replace('/', '__')}.git"
    dest = work_dir / f"{repo.replace('/', '__')}-{commit[:12]}"
    if not dest.exists():
        subprocess.run(
            ["git", "-C", str(bare), "worktree", "add", "--detach", str(dest), commit],
            check=True,
            capture_output=True,
        )
    return dest


def run_arm(
    arm: str,
    cwd: Path,
    issue: str,
    args: argparse.Namespace,
    namespace: str,
    prompt: str = PROMPT,
    answer: tuple[str, Any] = ("files", answer_files),
) -> dict[str, Any]:
    note = TOOLS_NOTE.format(org=args.org, namespace=namespace) if arm == "with" else ""
    cmd = [
        "claude",
        "-p",
        *(
            ["--bare"]
            if args.session == "bare"
            else ["--setting-sources", "local", "--strict-mcp-config"]
        ),
        "--model",
        args.model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--max-turns",
        str(args.max_turns),
        "--disallowedTools",
        # sub-agents are left out of both arms: their cost lands in the parent run at
        # random and hides what the measured agent did
        "Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent,Task",
    ]
    allowed = list(BUILTIN)
    env = dict(os.environ)
    if arm == "with":
        token = Path(args.token_file).read_text().strip()
        config = {
            "mcpServers": {
                "loom": {
                    "type": "http",
                    "url": args.mcp_url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }
        cfg = Path(tempfile.mkstemp(suffix=".json")[1])
        cfg.write_text(json.dumps(config))
        cmd += ["--mcp-config", str(cfg), "--strict-mcp-config"]
        # Tool search loads the deferred MCP definitions in a stock session.
        allowed += ["ToolSearch", "mcp__loom"]
        if args.plugin_dir:
            # The Loom plugin as a user installs it: its hooks read the server, the
            # token and the repository from the environment.
            cmd += ["--plugin-dir", args.plugin_dir]
            env.update(
                LOOM_BASE_URL=args.mcp_url.split("/mcp", 1)[0],
                LOOM_API_KEY=token,
                LOOM_CODE_REPO=args.code_repo,
            )
    # The tool flags take several values, so the prompt goes on stdin.
    cmd += ["--allowedTools", ",".join(allowed)]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        input=prompt.format(tools_note=note, issue=issue) + run_tag(args),
        capture_output=True,
        text=True,
        timeout=args.timeout_s,
        check=False,
        env=env,
    )
    wall = time.monotonic() - t0
    out, tools, steps = stream_trace(proc.stdout)
    if not out:
        return {"arm": arm, "error": (proc.stderr or proc.stdout)[-500:], "wall_s": round(wall, 1)}
    usage = out.get("usage") or {}
    fresh = int(usage.get("input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    return {
        "arm": arm,
        "error": "" if not out.get("is_error") else str(out.get("result"))[:500],
        answer[0]: answer[1](str(out.get("result") or "")),
        # the reply itself, for a judge to grade against a reference answer
        "answer_text": str(out.get("result") or "")[:20000],
        "turns": int(out.get("num_turns") or 0),
        "tool_calls": tools,
        "loom_calls": sum(n for name, n in tools.items() if name.startswith("mcp__loom")),
        "steps": steps,
        "input_tokens": fresh + cache_write + cache_read,
        "input_fresh": fresh,
        "input_cache_write": cache_write,
        "input_cache_read": cache_read,
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cost_usd": float(out.get("total_cost_usd") or 0.0),
        "wall_s": round(wall, 1),
    }


def run_tag(args: argparse.Namespace) -> str:
    """With --unique-prompts, a per-run line after the prompt: a real session's prompt is
    never byte-identical to an earlier one, so neither arm may read a repeat of the same
    question from the host's prompt cache."""
    return f"\n(run {uuid.uuid4().hex[:8]})" if args.unique_prompts else ""


def stream_result(stdout: str) -> tuple[dict[str, Any], dict[str, int]]:
    """The `result` event of a stream-json run and how often each tool was called."""
    result, tools, _ = stream_trace(stdout)
    return result, tools


def stream_trace(stdout: str) -> tuple[dict[str, Any], dict[str, int], list[dict[str, Any]]]:
    """The `result` event, the tool-call counts, and the run as steps: each model call's
    context size (input tokens, cache reads and writes) and the calls it made, each with a
    short form of its input and the size of what came back (characters / 4)."""
    result: dict[str, Any] = {}
    tools: dict[str, int] = {}
    steps: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            step: dict[str, Any] = {
                "context": sum(
                    int(usage.get(k) or 0)
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                ),
                "calls": [],
            }
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    step["text"] = str(block["text"])[:300]
                if block.get("type") == "tool_use":
                    tools[block["name"]] = tools.get(block["name"], 0) + 1
                    call = {"tool": block["name"], "input": json.dumps(block.get("input"))[:160]}
                    pending[str(block.get("id"))] = call
                    step["calls"].append(call)
            steps.append(step)
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result":
                    call = pending.pop(str(block.get("tool_use_id")), None)
                    if call is not None:
                        call["result_tokens"] = len(json.dumps(block.get("content"))) // 4
        elif kind == "result":
            result = event
    return result, tools, steps


def score(row: dict[str, Any], gold: list[str]) -> dict[str, Any]:
    files = row.get("files") or []
    row["hit_at_1"] = bool(files) and files[0] in gold
    row["hit_at_3"] = any(f in gold for f in files)
    return row


def path_matches(path: str, gold: str) -> bool:
    """A gold entry is a path prefix: a directory, a stem (the .h and the .cpp) or a file,
    as `bench/code_intel_qa.py` scores it."""
    g = gold.rstrip("/")
    return path == g or path.startswith(g + "/") or path.startswith(g + ".")


def moved_back(path: str, moved: dict[str, str]) -> str:
    """`path` under the prefix it had at the bench checkout, for a file that moved
    between that commit and the index's (`gold.moved`: current prefix -> checkout
    prefix): the index answers with the current path, and that is a right answer."""
    for now, then in moved.items():
        if path.startswith(now):
            return then + path[len(now) :]
    return path


def score_question(
    row: dict[str, Any], gold: list[str], moved: dict[str, str] | None = None
) -> dict[str, Any]:
    """hit@1 / hit@3 against the most specific gold entry (the first); area_hit_at_3
    against any entry, the directory included."""
    files = [moved_back(f, moved or {}) for f in row.get("files") or []]
    row["hit_at_1"] = bool(files) and path_matches(files[0], gold[0])
    row["hit_at_3"] = any(path_matches(f, gold[0]) for f in files)
    row["area_hit_at_3"] = any(path_matches(f, g) for f in files for g in gold)
    return row


def score_callers(row: dict[str, Any], gold: list[str]) -> dict[str, Any]:
    """Precision, recall and F1 of the callers named against the expected callers, each name
    compared by `caller_key`."""
    got = {caller_key(n) for n in row.get("callers") or []}
    want = {caller_key(n) for n in gold}
    hit = len(got & want)
    precision = hit / len(got) if got else 0.0
    recall = hit / len(want) if want else 0.0
    f1 = 2 * precision * recall / (precision + recall) if hit else 0.0
    row["caller_precision"] = round(precision, 3)
    row["caller_recall"] = round(recall, 3)
    row["caller_f1"] = round(f1, 3)
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for arm in ("without", "with"):
        rs = [r for r in rows if r["arm"] == arm and not r.get("error")]
        if not rs:
            continue
        n = len(rs)
        # A --questions run may mix file-scored and caller-scored questions: each metric is a
        # mean over the rows that carry it.
        fs = [r for r in rs if "hit_at_1" in r]
        cs = [r for r in rs if "caller_f1" in r]
        out[arm] = {
            "n": n,
            **(
                {
                    "hit_at_1": round(sum(r["hit_at_1"] for r in fs) / len(fs), 3),
                    "hit_at_3": round(sum(r["hit_at_3"] for r in fs) / len(fs), 3),
                }
                if fs
                else {}
            ),
            **(
                {"area_hit_at_3": round(sum(r["area_hit_at_3"] for r in fs) / len(fs), 3)}
                if fs and "area_hit_at_3" in fs[0]
                else {}
            ),
            **(
                {
                    "n_callers": len(cs),
                    **{
                        k: round(sum(r[k] for r in cs) / len(cs), 3)
                        for k in ("caller_precision", "caller_recall", "caller_f1")
                    },
                }
                if cs
                else {}
            ),
            "input_tokens_mean": round(sum(r["input_tokens"] for r in rs) / n),
            "output_tokens_mean": round(sum(r["output_tokens"] for r in rs) / n),
            "turns_mean": round(sum(r["turns"] for r in rs) / n, 1),
            "cost_usd_mean": round(sum(r["cost_usd"] for r in rs) / n, 4),
            "wall_s_mean": round(sum(r["wall_s"] for r in rs) / n, 1),
            "loom_calls_mean": round(sum(r.get("loom_calls", 0) for r in rs) / n, 1),
            "runs_calling_loom": sum(bool(r.get("loom_calls")) for r in rs),
        }
    return out


def tasks_from_swebench(args: argparse.Namespace) -> list[dict[str, Any]]:
    by_id = {r["instance_id"]: r for r in json.loads(Path(args.verified).read_text())}
    namespaces = json.loads(args.namespaces)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    tasks = []
    for iid in args.instances.split(","):
        inst = by_id[iid]
        tasks.append(
            {
                "id": iid,
                "cwd": checkout(Path(args.repos_dir), work, inst["repo"], inst["base_commit"]),
                "text": inst["problem_statement"],
                "gold": gold_files(inst["patch"]),
                "namespace": namespaces[iid],
                "prompt": PROMPT,
                "score": score,
            }
        )
    return tasks


def tasks_from_questions(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in Path(args.questions).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("//")
    ]
    kinds = set(args.question_types.split(","))
    tasks = []
    for q in rows:
        if "reference" in q:
            # a real-work question: answered in prose, graded later by bench.agent_work_judge
            tasks.append(
                {
                    "id": q["id"],
                    "cwd": Path(args.checkout),
                    "text": q["query"],
                    "gold": list(q["reference"].get("files") or []),
                    "prompt": WORK_PROMPT,
                    "score": score_question,
                    "answer": ("files", answer_files),
                    "namespace": args.namespace,
                }
            )
            continue
        if q["type"] not in kinds:
            continue
        gold = q.get("gold", {})
        task = {"id": q["id"], "cwd": Path(args.checkout), "text": q["query"]}
        callers = gold.get("expected_callers") or gold.get("callers")
        if q["type"] == "callers_hard" or (q.get("kind") == "impact" and callers):
            if not callers:
                continue
            task |= {
                "gold": list(callers),
                "prompt": CALLERS_PROMPT,
                "score": score_callers,
                "answer": ("callers", answer_callers),
            }
        elif gold.get("paths"):
            moved = dict(gold.get("moved") or {})
            task |= {
                "gold": list(gold["paths"]),
                "prompt": QUESTION_PROMPT,
                "score": partial(score_question, moved=moved) if moved else score_question,
            }
        else:
            continue
        tasks.append({**task, "namespace": args.namespace})
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--verified", help="SWE-bench Verified rows (JSON list)")
    ap.add_argument("--repos-dir", help="bare clones named owner__repo.git")
    ap.add_argument("--work-dir", default="")
    ap.add_argument("--instances", help="comma-separated instance ids")
    ap.add_argument("--namespaces", help='JSON {"instance_id": "namespace"}')
    ap.add_argument("--questions", help="a code_intel_qa.jsonl question set, in place of SWE-bench")
    ap.add_argument(
        "--question-types",
        default="conceptual",
        help="comma-separated: conceptual, multi_hop, recent (file-scored), callers_hard",
    )
    ap.add_argument("--checkout", help="with --questions: a checkout at the indexed commit")
    ap.add_argument("--namespace", help="with --questions: the code namespace of that checkout")
    ap.add_argument("--mcp-url", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--org", default="dev")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout-s", type=int, default=900)
    ap.add_argument("--arms", default="without,with", help="which arms to run")
    ap.add_argument(
        "--session",
        choices=["default", "bare"],
        default="default",
        help="default: a stock Claude Code session (MCP tools deferred behind tool search) "
        "with no user or project settings; bare: --bare, every tool listed up front",
    )
    ap.add_argument("--plugin-dir", default="", help="load this Claude Code plugin in the with arm")
    ap.add_argument("--code-repo", default="", help="owner/name the plugin resolves the index by")
    ap.add_argument(
        "--unique-prompts",
        action="store_true",
        help="end each run's prompt with its own run id, as a real session's prompt is unique",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    tasks = tasks_from_questions(args) if args.questions else tasks_from_swebench(args)
    rows: list[dict[str, Any]] = []
    with open(args.out, "a", encoding="utf-8") as fh:
        for t in tasks:
            for arm in args.arms.split(","):
                r = run_arm(
                    arm,
                    t["cwd"],
                    t["text"],
                    args,
                    t["namespace"],
                    prompt=t["prompt"],
                    answer=t.get("answer", ("files", answer_files)),
                )
                r = t["score"]({"instance_id": t["id"], "gold": t["gold"], **r}, t["gold"])
                rows.append(r)
                fh.write(json.dumps(r) + "\n")
                fh.flush()
                print(
                    json.dumps(
                        {
                            k: r.get(k)
                            for k in (
                                "instance_id",
                                "arm",
                                "hit_at_3",
                                "input_tokens",
                                "turns",
                                "loom_calls",
                                "cost_usd",
                                "error",
                            )
                        }
                    )
                )
    print(json.dumps(summarize(rows), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
