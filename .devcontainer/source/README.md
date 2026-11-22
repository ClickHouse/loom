# Source contribution lab

Use a task-specific checkout to investigate a small defect, write its regression and
send a reviewable contribution. This configuration builds a Python source environment
with pinned base and installer images, the repository's dependency lock and a private
virtualenv. It installs compiler dependencies and runs a focused unit check. It starts
no Loom service, database or provider, and exposes no port.

Select **Loom source contribution** when reopening the checkout in a development
container with a local container engine. The first check exercises writer provenance.
Edit the relevant source and test, then run the matching check again:

```bash
uv run --no-sync python dev/contributor-lab/check.py functional
```

The virtualenv lives in the container at `/venv`. Removing the container removes that
environment; source edits remain in the task checkout. Close the development-container
session and remove that specific container through the local engine when finished.
The released-stack try-out is configured separately by the parent configuration.

## Run a bounded check in a public fork

The **contributor lab** workflow uses standard public runners without provider secrets
and require no provider account. Fork the repository, push a task branch and choose that branch
under **Actions → contributor lab → Run workflow**. Select one track. A relevant change
to the lab itself runs all tracks in its pull request. Each selected test command has a
two-minute limit; the whole disposable-container job has a fifteen-minute limit.

| Track            | Starting check                                          | Useful contribution                                                       |
| ---------------- | ------------------------------------------------------- | ------------------------------------------------------------------------- |
| Functional       | Writer/source provenance cases                          | A minimal input and regression for an incorrect result                    |
| Security         | Identity-bearing operation validation                   | A missing scope check, reported through the private security route        |
| Performance      | Forwarded request-timeout values with stubbed clients   | A small reproduction of an incorrect deadline or blocking behavior        |
| Stress and scale | Synthetic code-context budget and oversized-input cases | A bounded input that breaks a size, cardinality or refusal invariant      |
| Usability        | CLI argument/help snapshots                             | A reproducible confusing instruction, missing option or inaccessible flow |
| Code quality     | Source-domain lint and formatting                       | A focused maintainability correction with preserved behavior              |

These seeds cover specific contracts. They do not establish live latency, load
capacity, complete security coverage or human usability results. Extend the smallest
relevant case with synthetic data and preserve its existing assertions and limits.
The workflow reports actual check counts and deletes only its own container. It
uploads no artifact and does not run a benchmark or send a provider request.

## Report what failed

Use the repository's existing [issue forms](../../.github/ISSUE_TEMPLATE) for a public
functional, documentation or contribution report. Include the tested commit, selected
track, exact command, synthetic reproduction, expected behavior and observed result.
Check open work and preserve an existing contributor's claim before starting a fix.
Follow [CONTRIBUTING.md](../../CONTRIBUTING.md) for the patch and required review/CI.

Security-sensitive findings use [SECURITY.md](../../SECURITY.md). Keep credentials,
personal data and exploit inputs out of public issues. Run experiments only on the
disposable synthetic checkout; deployed services and third-party systems are outside
this lab. Contributions do not require a star, paid tool, mentoring arrangement or
provider account.
