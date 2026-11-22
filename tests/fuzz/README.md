# Endpoint identity probes

The target is `server/core/embedding_space.py` (`public_identity`). It converts an
external endpoint configuration into the public identity used by deployment checks
and calibration. The probes check URL userinfo, allowed query names, fragments,
case normalization, preserved endpoint differences and unchanged non-URL identity
fields. This is a URL-identity boundary, not a general secret scrubber, network
allowlist or proof of API authorization.

Replay the checked-in synthetic corpus without installing a fuzzing engine:

```bash
python3 -m tests.fuzz.embedding_identity_fuzzer --replay tests/fuzz/corpus/embedding_identity
python3 -m unittest tests.mutation.test_embedding_identity
python3 tests/fuzz/test_probe_publisher.py
```

Inputs are at most 4096 bytes. Malformed raw URLs may raise the parser's documented
validation error; unexpected exceptions and accepted identities that break the
public contract fail. Every input also exercises a structured accepted URL with
synthetic credentials. This keeps malformed input from starving the useful branch.
Never add real credentials, deployment data or a third-party target to this corpus.

The hosted workflow replays the corpus and runs the fixed cases. Its coverage-guided
job uses the Python engine through the continuous-fuzzing runner, one target, one
worker and explicit time, input, memory and per-input limits. The image, engine and
executor references are pinned; engine selection matches the image's Python ABI.
Crashes fail the job and retain the runner's replay evidence. Review any real
security finding through the private route in [SECURITY.md](../../SECURITY.md),
without pasting inputs into public issues. The build packages only the named
standard-library module and these probes; no Loom service or provider is started.

The mutation pilot stages an unchanged copy of the same module and tests. It selects
only `public_identity` mutants, uses two workers with a two-minute process budget,
and kills its own process group on completion or failure. Empty test or mutation
results fail. Reported survivors need inspection: add an independent assertion for a
meaningful behavior difference, or record why the mutant is equivalent. The report
is not a whole-module score, a security result or permission to suppress a case.

A trusted-main publisher automatically creates a maintenance task for a broken
probe or a mutation result needing review. It posts static status and a log link,
without raw source or crash input, and appends a comment only when status changes.
It preserves edited bodies and closed decisions; keep an identity marker in the
title or body. API, history and pagination limits fail explicitly. Pull requests
retain read-only tokens. Standard public runners are used without a provider,
account, storage repository, new token or paid service.
