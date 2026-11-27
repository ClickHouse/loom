"""Paired offline replay from native memory certificates and recorded grep results.

No tests, providers, services or benchmark inputs are modified. Timestamp ties
are enumerated rather than assigned an invented order. Only tie orders that
reproduce the recorded old answer support a counterfactual answer.
"""

import argparse
import datetime
import hashlib
import itertools
import json
import math
import time
from pathlib import Path

from server.memory.app import for_file, path_lessons
from server.memory.domain import code_change
from server.settings import settings

MAX_TIE_ORDERS = 10000


def candidates(certificate, path):
    files = {
        event["request"]["memory_key"]: event["request"].get("files", [])
        for event in certificate["events"]
        if event["operation"] == "memory.set"
    }
    rows = []
    for memory in certificate["final_snapshot"]:
        assert memory["status"] == "active" and not memory["superseded_by"]
        links = [c for c in memory["code"] if c["path"] == path]
        matched = [f for f in files[memory["memory_key"]] if path_lessons._names_path(f, path)]
        if not matched and not links:
            continue
        raw = json.dumps(memory["value"], ensure_ascii=False)
        assert len(raw.encode()) <= 600, "Replay requires the actual truncated read bytes"
        rows.append(
            {
                **memory,
                "namespace": certificate["memory_namespace"],
                "value": raw,
                "value_truncated": False,
                "updated_at": datetime.datetime.fromisoformat(memory["updated_at"]),
                "code_state": code_change.worst([link["state"] for link in links]),
                "code_changed_since": code_change.worst_commit(links),
                "matched_files": matched,
            }
        )
    rows.sort(key=for_file._order_key)
    if len(rows) > path_lessons.PER_PATH_ROWS:
        return None
    groups = [list(group) for _, group in itertools.groupby(rows, key=for_file._order_key)]
    if math.prod(math.factorial(len(group)) for group in groups) > MAX_TIE_ORDERS:
        return None
    permutations = [list(itertools.permutations(group)) for group in groups]
    return [
        list(itertools.chain.from_iterable(parts)) for parts in itertools.product(*permutations)
    ]


def serve(paths, rows_by_path, query, budget):
    output, seen, spent = [], set(), 0
    candidates = [
        (path, row) for path, rows in zip(paths, rows_by_path, strict=True) for row in rows
    ]
    ordered = (
        path_lessons._ordered_candidates(candidates, query) if query is not None else candidates
    )
    for path, row in ordered:
        key = row["memory_key"]
        if key in seen:
            continue
        lesson = path_lessons._lesson(row, path)
        if lesson is None:
            continue
        cost = path_lessons._tokens(lesson)
        if spent + cost > budget:
            continue
        output.append(key)
        seen.add(key)
        spent += cost
    return tuple(output)


def replay(root):
    entries = json.loads((root / "study/live-attempts.json").read_text())
    cases = []
    budget = settings.code_result_lessons_tokens
    path_count = settings.code_result_lessons_paths
    started = time.perf_counter()
    for entry in entries:
        result = entry["result"]
        if not result["arm"].endswith("-memory"):
            continue
        certificate_path = root / "history-certificates" / (str(entry["order"]) + ".json")
        certificate = json.loads(certificate_path.read_text())
        events = [
            json.loads(line)
            for line in (Path(result["harbor_trial"]) / "agent/factory/events.jsonl")
            .read_text()
            .splitlines()
        ]
        requests = {
            event["tool_use_id"]: event for event in events if event["kind"] == "tool_request"
        }
        for event in events:
            if event["kind"] != "tool_result" or event["name"] != "loom_code_grep":
                continue
            answer = event["result"]
            if not answer.get("matches"):
                continue
            paths = list(dict.fromkeys(match["path"] for match in answer["matches"]))[:path_count]
            variants = [candidates(certificate, path) for path in paths]
            actual = tuple(lesson["memory_key"] for lesson in answer.get("lessons", []))
            query = requests[event["tool_use_id"]]["arguments"]["pattern"]
            too_many = (
                any(variant is None for variant in variants)
                or math.prod(len(variant) for variant in variants if variant is not None)
                > MAX_TIE_ORDERS
            )
            feasible = (
                [
                    combination
                    for combination in itertools.product(*variants)
                    if serve(paths, combination, None, budget) == actual
                ]
                if not too_many
                else []
            )
            new = sorted({serve(paths, combination, query, budget) for combination in feasible})
            cases.append(
                {
                    "order": entry["order"],
                    "task": result["task_id"],
                    "arm": result["arm"],
                    "query": query,
                    "paths": paths,
                    "old_recorded": actual,
                    "consistent_old_tie_orders": len(feasible),
                    "new_possible_answers": new,
                    "native_certificate_sha256": hashlib.sha256(
                        certificate_path.read_bytes()
                    ).hexdigest(),
                    "unknown_reason": "tie_enumeration_or_listing_cap"
                    if too_many
                    else "old_answer_not_reconstructed"
                    if not feasible
                    else None,
                }
            )

    matched = [case for case in cases if case["consistent_old_tie_orders"]]
    report = {
        "kind": "paired-offline-native-certificate-replay",
        "provider_requests": 0,
        "case_count": len(cases),
        "old_answer_reconstructed": len(matched),
        "unreconstructed": len(cases) - len(matched),
        "changed_answer_cases": sum(
            case["new_possible_answers"] != [tuple(case["old_recorded"])] for case in matched
        ),
        "token_budget": budget,
        "path_count": path_count,
        "elapsed_seconds": time.perf_counter() - started,
        "interpretation": (
            "Counterfactual lesson order only. This is not native request latency, solver quality, "
            "or an E2E improvement measurement. Unreconstructed answers remain unknown. "
            "All consistent timestamp-tie orders are retained."
        ),
        "cases": cases,
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    report = replay(args.recording.resolve(strict=True))
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}))


if __name__ == "__main__":
    main()
