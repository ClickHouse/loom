---
type: fixed
---

{"ops":[{"op":"memory.record.query","required":["schema_name"],"optional":["filters","predicates","as_of","limit"],"purpose":"Typed records of one schema by field filters and predicates, as of a time."},{"op":"task.get","required":["task_id"],"purpose":"One task by task_id: its state, lease, poster, result and history."},{"op":"task.list","required":[],"optional":["inbox","posted_by_me","posted_by","statuses","kinds","owner","limit","fields","newest_first"],"purpose":"Read tasks in an inbox you own (or that you posted) by status, kind or owner, without claiming."}],"matched":3,"truncated":false}
