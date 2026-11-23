"""Freeze a single-scenario pilot without changing its task or grader."""

from .arms import CACHE_STARTS, validate_schedule
from .runtime import read, write
from .source import sha


def select(prepared, task_id, output, *, cache_pairs=False):
    if task_id is None:
        return prepared
    validate_schedule(prepared["schedule"])
    if task_id not in prepared["tasks"]:
        raise ValueError("The pilot task must name a frozen evaluation scenario")
    rows = []
    for row in prepared["schedule"]:
        if row["task_id"] != task_id or row["repeat"] != 0:
            continue
        order = len(rows)
        if cache_pairs:
            rows.extend(
                {
                    **row,
                    "order": order + repeat,
                    "pilot": True,
                    "repeat": repeat,
                    "cache_start": cache,
                }
                for repeat, cache in enumerate(CACHE_STARTS)
            )
        else:
            rows.append({**row, "order": order, "pilot": True})
    validate_schedule(rows)
    output.mkdir(parents=True, exist_ok=False)
    manifest = read(prepared["historical_manifest"])
    manifest["tasks"] = [row for row in manifest["tasks"] if row["task_id"] == task_id]
    historical = output / "historical-manifest.json"
    controls = output / "reference-controls.json"
    write(historical, manifest)
    write(
        controls,
        [row for row in read(prepared["reference_controls_file"]) if row["instance_id"] == task_id],
    )
    result = {
        **prepared,
        "tasks": {task_id: prepared["tasks"][task_id]},
        "memory_histories": {task_id: prepared["memory_histories"][task_id]},
        "schedule": rows,
        "historical_manifest": str(historical),
        "reference_controls_file": str(controls),
        "public_inputs": {
            **prepared["public_inputs"],
            str(historical): sha(historical),
            str(controls): sha(controls),
        },
    }
    write(output / "inputs.json", result)
    return result
