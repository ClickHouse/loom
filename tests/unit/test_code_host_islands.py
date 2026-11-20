"""Shell inside CI YAML, Dockerfiles, Makefiles and
Jenkinsfiles — islands, host symbols, program / dialect facets, includes."""

from __future__ import annotations

import pytest

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract import lang_hosts as lh
from server.code.domain.extract.treesitter import ParsedFile

_WORKFLOW = """\
name: CI
on: [push]
jobs:
  style_check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Prepare
        run: |
          rm -rf ./ci/tmp
          python3 ./ci/praktika/main.py --job "${{ inputs.job }}"
          curl -sS https://example.org | jq .version
      - name: One liner
        run: bash tests/ci/run_style.sh --fast
        shell: bash

  build:
    needs: style_check
    runs-on: ubuntu-latest
    steps:
      - run: 'docker build -t x .'
"""

_GITLAB = """\
stages: [test]
variables:
  GIT_DEPTH: "3"
unit:
  stage: test
  script:
    - make test
    - shellcheck scripts/*.sh
.hidden:
  script:
    - echo template
"""

_DOCKERFILE = """\
ARG BASE=ubuntu:24.04
FROM ${BASE} AS builder
RUN --mount=type=cache,target=/var/cache/apt apt-get update \\
    && apt-get install -y wget curl \\
    # a comment inside the continuation
    && wget -O /tmp/x https://example.org/x
RUN <<EOF
  mkdir -p /opt/app
  clickhouse-client -q "SELECT version()"
EOF
RUN ["sh", "-c", "chmod +x /opt/app/run.sh"]
ENTRYPOINT ["/opt/app/run.sh"]

FROM ubuntu:24.04
COPY --from=builder /opt/app /opt/app
CMD /opt/app/run.sh --serve
HEALTHCHECK --interval=30s CMD curl -f http://localhost/ || exit 1
"""

_MAKEFILE = """\
SHELL := bash
CC ?= gcc
.PHONY: test lint

test: lint
\t@$(MAKE) -C sub all
\t$$HOME/bin/runner $(ARGS) $@
\tshellcheck $(shell find . -name '*.sh')

lint:
\t-shfmt -d .

%.o: %.c
\t$(CC) -c $< -o $@
"""

_JENKINS = """\
pipeline {
  stages {
    stage('Build') {
      steps {
        sh 'make build'
        sh '''
          ./scripts/package.sh --release
          docker push example/app
        '''
        sh(script: "python3 tools/report.py", returnStdout: true)
      }
    }
  }
}
"""


def _units(path: str, text: str) -> lh.HostUnits:
    u = lh.host_units(ParsedFile(path=path, lang="config", source=text.encode()))
    assert u is not None
    return u


def test_hosts_are_known_by_name() -> None:
    assert lh.host_kind("docker/server/Dockerfile") == lh.HOST_DOCKER
    assert lh.host_kind("docker/test/Dockerfile.ubuntu") == lh.HOST_DOCKER
    assert lh.host_kind("build/app.dockerfile") == lh.HOST_DOCKER
    assert lh.host_kind("Makefile") == lh.HOST_MAKE
    assert lh.host_kind("rules/common.mk") == lh.HOST_MAKE
    assert lh.host_kind("ci/Jenkinsfile") == lh.HOST_JENKINS
    assert lh.host_kind(".github/workflows/pull_request.yml") == lh.HOST_YAML
    assert lh.host_kind(".gitlab-ci.yml") == lh.HOST_YAML
    assert lh.host_kind("azure-pipelines.yml") == lh.HOST_YAML
    assert lh.host_kind("config/settings.yml") == ""
    assert lh.host_kind("src/main.cpp") == ""
    assert lh.detect_host("Dockerfile") == "config" and lh.detect_host(".gitlab-ci.yml") == ""


def test_workflow_islands_jobs_programs_and_includes() -> None:
    u = _units(".github/workflows/ci.yml", _WORKFLOW)
    by_line = {i.line: i for i in u.islands}
    assert set(by_line) == {10, 14, 21}  # `shell: bash` names an interpreter, not a command
    block = by_line[10]
    assert block.text.split("\n") == [
        "rm -rf ./ci/tmp",
        'python3 ./ci/praktika/main.py --job "__expr__"',
        "curl -sS https://example.org | jq .version",
    ]
    assert by_line[14].text == "bash tests/ci/run_style.sh --fast"
    assert by_line[21].text == "docker build -t x ."  # quotes off
    jobs = {s.name: s for s in u.symbols}
    assert set(jobs) == {"style_check", "build"}
    assert (jobs["style_check"].kind, jobs["style_check"].start_line) == ("job", 4)
    assert jobs["style_check"].end_line == 15 and jobs["build"].start_line == 17
    assert jobs["build"].qualified_name == ".github.workflows.ci::build"
    programs = {f.name for f in u.facets if f.kind == "program"}
    assert {"python3", "curl", "jq", "docker"} <= programs
    assert "rm" not in programs  # a builtin / everyday utility is no fact
    # includes resolve from the repository root, where a job runs
    assert u.includes == ["ci/praktika/main.py", "tests/ci/run_style.sh"]


def test_gitlab_script_lists_and_jobs() -> None:
    u = _units(".gitlab-ci.yml", _GITLAB)
    assert [(i.line, i.text) for i in u.islands] == [
        (7, "make test"),
        (8, "shellcheck scripts/*.sh"),
        (11, "echo template"),
    ]
    assert [(s.name, s.kind, s.start_line, s.end_line) for s in u.symbols] == [
        ("unit", "job", 4, 8),
    ]  # `.hidden` templates and reserved keys are not jobs
    assert {"make", "shellcheck"} <= {f.name for f in u.facets if f.kind == "program"}


def test_dockerfile_run_forms_stages_and_sql_dialect() -> None:
    u = _units("docker/server/Dockerfile", _DOCKERFILE)
    by_line = {i.line: i for i in u.islands}
    assert set(by_line) == {3, 8, 11, 16, 17}
    cont = by_line[3]
    assert cont.text.startswith("apt-get update \\")  # --mount flag gone, RUN keyword gone
    assert (
        "wget -O /tmp/x" in cont.text and "\\\n\\\n" in cont.text
    )  # comment row kept as a blank continuation
    assert by_line[8].text == 'mkdir -p /opt/app\nclickhouse-client -q "SELECT version()"'
    assert by_line[11].text == "chmod +x /opt/app/run.sh"
    assert by_line[16].text == "/opt/app/run.sh --serve" and by_line[16].via == "CMD"
    assert by_line[17].text.strip() == "curl -f http://localhost/ || exit 1"
    stages = [(s.name, s.kind, s.start_line, s.end_line) for s in u.symbols]
    assert stages == [("builder", "stage", 2, 12)]
    assert u.includes == []  # image paths are not repository paths
    facets = {(f.kind, f.name) for f in u.facets}
    assert ("program", "wget") in facets and ("program", "apt-get") in facets
    assert ("program", "clickhouse-client") in facets or ("program", "clickhouse") in facets
    assert ("dialect", "clickhouse") in facets and ("function", "version") in facets
    dialect_lines = {f.line for f in u.facets if f.kind == "dialect"}
    assert dialect_lines == {9}  # the heredoc's second line, in the Dockerfile's numbering


def test_makefile_targets_and_recipes() -> None:
    u = _units("Makefile", _MAKEFILE)
    targets = [(s.name, s.kind, s.start_line, s.end_line, s.signature) for s in u.symbols]
    assert targets == [
        ("test", "target", 5, 8, "test: lint"),
        ("lint", "target", 10, 11, "lint:"),
    ]  # `.PHONY` and `%.o` are not targets a reader names
    rec = {i.line: i for i in u.islands}
    assert set(rec) == {6, 11, 14}
    assert rec[6].text.split("\n") == [
        "make -C sub all",  # `@$(MAKE)`
        "$HOME/bin/runner __var__ __var__",  # `$$HOME`, `$(ARGS)`, `$@`
        "shellcheck $(find . -name '*.sh')",  # `$(shell …)`
    ]
    assert rec[11].text == "shfmt -d ."  # `-` prefix off
    assert rec[14].text == "__var__ -c __var__ -o __var__"
    programs = {f.name for f in u.facets if f.kind == "program"}
    assert {"make", "shellcheck", "shfmt"} <= programs
    assert u.includes == []


def test_jenkinsfile_sh_steps() -> None:
    u = _units("ci/Jenkinsfile", _JENKINS)
    assert [(i.line, i.text) for i in u.islands] == [
        (5, "make build"),
        (7, "./scripts/package.sh --release\ndocker push example/app"),
        (10, "python3 tools/report.py"),
    ]
    assert u.includes == ["scripts/package.sh", "tools/report.py"]  # from the workspace root
    assert {"make", "docker", "python3"} <= {f.name for f in u.facets if f.kind == "program"}


def test_synthetic_text_keeps_the_host_lines() -> None:
    text = lh.synthetic_shell([lh.HostIsland("a\nb", 3, "run"), lh.HostIsland("c", 6, "run")], 7)
    assert text.split("\n")[:7] == ["", "", "a", "b", "", "c", ""]


def test_ingest_dispatch_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_host_islands", False, raising=False)
    # With the islands off a Dockerfile is a plain text file,
    # chunked and searchable, with no jobs or includes
    off = parse_file("Dockerfile", _DOCKERFILE.encode())
    assert off is not None and off.lang == "text" and off.symbols == [] and off.includes == []
    pf = parse_file(".github/workflows/ci.yml", _WORKFLOW.encode())
    assert pf is not None and pf.lang == "config" and pf.symbols == [] and pf.includes == []

    monkeypatch.setattr(settings, "code_host_islands", True, raising=False)
    pf = parse_file("Dockerfile", _DOCKERFILE.encode())
    assert pf is not None and pf.lang == "config" and [s.kind for s in pf.symbols] == ["stage"]
    pf = parse_file(".github/workflows/ci.yml", _WORKFLOW.encode())
    assert pf is not None and [s.name for s in pf.symbols] == ["style_check", "build"]
    assert pf.includes == ["ci/praktika/main.py", "tests/ci/run_style.sh"]
    assert pf.tree is None  # chunk-only: the line-window chunker, as for every config file


def test_a_row_that_is_no_instruction_keeps_its_continuation_rows() -> None:
    # Docker joins a `\` row with the next, so `RUN tests` is not an instruction of its own.
    islands, _ = lh.dockerfile_islands(["run make \\", "RUN tests", "RUN lint"])
    assert [(i.text, i.line) for i in islands] == [("lint", 3)]
