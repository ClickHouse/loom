"""Select the pilot issue in a frozen schedule."""

from .arms import PRIMARY, validate_schedule
from .runtime import read, write
from .source import sha


def select(prepared, task_id, output, *, repeats=1):
    validate_schedule(prepared["schedule"])
    if type(repeats) is not int or repeats not in (1, 3):
        raise ValueError("Choose one or three repeats for the pilot issue")
    if task_id not in prepared["tasks"]:
        raise ValueError("The pilot task must name a frozen evaluation scenario")
    selected = {
        (row["repeat"], row["arm"]): row
        for row in prepared["schedule"]
        if row["task_id"] == task_id and row["repeat"] < repeats
    }
    arms = tuple(PRIMARY)
    rows = [
        {**selected[repeat, arm], "order": repeat * len(arms) + position, "pilot": True}
        for repeat in range(repeats)
        for position, arm in enumerate(arms)
    ]
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
