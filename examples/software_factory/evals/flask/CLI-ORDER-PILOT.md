# Flask CLI option-order pilot

The CLI pilot selects `pallets__flask-4544` from the pinned SWE-bench dataset.
The public issue describes an error when the key option appears before the
certificate option. Thirty-two retained attempt manifests contain no attempt
on this task. The pilot uses the original issue, source revision, reference
patch and hidden selectors. [Original Flask issue](https://github.com/pallets/flask/issues/4459)

The base passes the 12 visible cases and fails the targeted hidden test.
The upstream reference passes all 53 hidden cases. Those controls use the
tracked source fixtures and the pinned Click version from the base revision.
Harbor reruns the base and reference controls before inference.

Both memory arms receive the original historical corpus plus one linked note
about Click's parameter callback order. Its source was public before the
historical Flask index revision and the issue. It describes behavior of the
dependency. The original issue and fixing PR are excluded from the memory
corpus. The note names a Flask callback present at both indexed revisions.
[Pinned Click manual](https://github.com/pallets/click/blob/1784558ed7c75c65764d2a434bd9cbb206ca939d/docs/advanced.rst#callback-evaluation-order)

Run one native attempt per arm with the same Claude Code model and tool
selection, base, public issue and graders. Inspect whether a memory-enabled
attempt receives the callback-order note before it edits. Report the five
patch outcomes, blind verdicts, regressions, provider costs and agent times
along with cache tokens, tool calls and memory activity. Keep all outcomes.
A single development pilot gives no estimate of run-to-run variance.

```bash
python3 -m examples.software_factory plan --eval flask --pilot-task pallets__flask-4544
ANTHROPIC_API_KEY=... python3 -m examples.software_factory run --eval flask \
  --pilot-task pallets__flask-4544 --output ./factory-cli-order-pilot
```

Use the frozen CLI pilot revision with the environment described in
[REPRODUCE.md](REPRODUCE.md).
