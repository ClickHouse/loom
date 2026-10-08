# session-resume

Resume a conversation after a restart, with the session in Loom: the transcript, the extracted facts and a parked tool result all survive the process.

Run 1 tells the agent a fact and exits, taking the process and all in-memory state with it. Run 2 constructs a brand-new `Agent` with the same session id, resumes it (the context pack becomes the system prompt), and lets a model answer through loom's tools.

```bash
LOOM_BASE_URL=… LOOM_API_KEY=… LOOM_ORG=dev LOOM_NAMESPACE=demo node index.mjs tell "My rental car is a blue Nissan Micra"
LOOM_BASE_URL=… LOOM_API_KEY=… LOOM_ORG=dev LOOM_NAMESPACE=demo node index.mjs ask  "What car am I driving?"
```

With `OPENAI_API_KEY` set, `ask` runs a real tool-use turn against `gpt-4o-mini` (`tools.definitions("openai")`); without it the script calls the tools itself.

## What it shows

1. `bot.session(id, { userId })` — one (user, session) scope for every op.
2. `session.append(user, assistant)` — the turns go to the transcript (when the namespace logs conversations) and, on `close()`, to `memory.set_from_messages`, where the server extracts what is worth keeping.
3. `session.offload(printout, { summary, blobKey })` — 4,280 bytes parked once; the one-line `pointer` goes into working memory with `session.note()` so the next turn's pack carries it.
4. `session.resume({ query })` in a new process — `context.pack` for the scope as `system`, the transcript tail as `messages`.
5. `bot.tools(scope).definitions("openai")` + `execute()` — the model calls `loom_ask`; the script calls `loom_fetch(pattern="FAILED")` on the parked printout.

## Actual output

A run against an isolated Loom server on ClickHouse Cloud, extraction and synthesis by `gpt-4o-mini`. The namespace had conversation logging off (the default; an admin enables it with `conversation.set_logging`), so `transcriptStored` is `false`, resume returns 0 transcript turns, and the facts travel through extraction and working memory instead — the path the SDK warns about once.

```text
$ node index.mjs tell "My rental car is a blue Nissan Micra, plate M-AB 1234, picked up at Frankfurt airport"
offloaded 4280 bytes → [offloaded 4,280 bytes / 120 lines → context.fetch blob_key=rental/session-resume-demo-2 (offset/limit, or pattern=<regex>)]
(conversation logging is off for this namespace: turns go to extraction only)
flushed: extracted 1 memories: [ 'user_rental_car' ]

$ node index.mjs ask "What car am I driving, and was there a payment problem at the rental desk?"
resumed: 2 pack sections, 0 transcript turns
transcript tail:
system prompt (703 chars) starts: <working_memory> ⏎ <working-memory block="scratchpad">[offloaded 4,280 bytes / 120 lines → context.fetch blob_key=rental/session-resume-demo-2 (offset/limit, or p
gpt-4o-mini called loom_ask({"query": "What car am I driving?"}) → {"answer":"You are driving a blue Nissan Micra with plate M-AB 1234, a rental car picked up at Frankfurt airport [user_rental_car] [summary:session-resume-demo-2].","abstain":false,"cited":["summary:s
gpt-4o-mini called loom_ask({"query": "Was there a payment problem at the rental desk?"}) → {"answer":"The memories do not record any payment problem at the rental desk. The only rental record is that the user's rental car is a blue Nissan Micra, plate M-AB 1234, picked up at Frankfurt airpo
gpt-4o-mini → You are driving a blue Nissan Micra with the plate M-AB 1234, which you picked up at Frankfurt airport. There is no record of any payment problem at the rental desk.
loom_fetch(pattern=FAILED) → {"content":"3: line 3: FAILED payment retry, card declined once\n6: line 3: FAILED payment retry, card declined once\n9: line 3: FAILED payment retry, card declined once","next_offset":3,"matched_line
```

Two things the run shows honestly: the model answered the second question from `loom_ask` alone ("the memories do not record a payment problem") even though the pointer to the parked printout was in its system prompt — a model decides which tool to call, and the printout's `FAILED payment retry` line was there for `loom_fetch`, as the script's own call shows. And `loom_ask` cited a session digest (`summary:session-resume-demo-2`) beside the extracted memory: the server had already summarised the flushed turns.
