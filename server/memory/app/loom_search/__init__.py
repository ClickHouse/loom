"""loom_search: one question fanned over memory, knowledge, profile, conversations
and (for a defect report under a code scope) the tracker record, answered as one
payload. `fanout` runs the legs and assembles it; `answer_first` and
`tracker_leg` are its assembly rules (the defect-report rules they share live in
`server.tracker.domain.defect_answer`)."""
