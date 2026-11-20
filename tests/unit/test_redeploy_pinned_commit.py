"""Pinned configuration deploys keep their source when main advances."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "redeploy.sh"


@pytest.fixture
def deployment(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "checkout"
    repo.mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls"
    git = shutil.which("git")
    assert git
    env = {"PATH": f"{bins}:/usr/bin:/bin", "HOME": str(tmp_path), "CALLS": str(calls)}

    def command(*args: str) -> str:
        return subprocess.check_output([git, *args], cwd=repo, env=env, text=True).strip()

    def executable(path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o755)

    shutil.copyfile(SCRIPT, repo / "redeploy.sh")
    (repo / "dev").mkdir()
    for name in ("apparmor_check.sh", "redeploy_preflight.sh", "sandbox_probe.sh"):
        executable(
            repo / "dev" / name,
            '#!/bin/sh\nprintf "%s %s\\n" "$0" "$*" >> "$CALLS"\ncase "${0##*/}" in\n  apparmor_check.sh) exit "${FAKE_APPARMOR_RC:-0}" ;;\n  sandbox_probe.sh) exit "${FAKE_SANDBOX_RC:-0}" ;;\nesac\n[ "${FAKE_PREFLIGHT_RC:-0}" -eq 0 ] || exit "$FAKE_PREFLIGHT_RC"\nif [ "${FAKE_SAME_BUILD:-0}" -eq 1 ]; then\n  case " $* " in *" --allow-same "*) ;; *) exit 3 ;; esac\nfi\nexit 0\n',
        )
    (repo / ".gitignore").write_text("var/\n.env\n")
    command("init", "-q")
    command("config", "user.email", "fixture@example.invalid")
    command("config", "user.name", "Fixture")
    command("add", ".")
    command("commit", "-qm", "candidate")
    target = command("rev-parse", "HEAD")
    (repo / "later.txt").write_text("Later main source\n")
    command("add", "later.txt")
    command("commit", "-qm", "later main")
    later = command("rev-parse", "HEAD")
    command("update-ref", "refs/remotes/origin/main", later)
    command("checkout", "--detach", "-q", target)
    (repo / "var" / "proxy").mkdir(parents=True)
    (repo / "var" / "proxy" / "upstream.conf").write_text("server loom-blue:7781;\n")
    env.update(LOOM_REPO=str(repo), EXPECT_COMMIT=target)
    executable(bins / "git", f'#!/bin/sh\nif [ "$1" = fetch ]; then exit 0; fi\nexec {git} "$@"\n')
    executable(bins / "ss", "#!/bin/sh\nexit 0\n")
    executable(bins / "sleep", "#!/bin/sh\nexit 0\n")
    executable(
        bins / "curl",
        """#!/usr/bin/python3
import json,os
print(json.dumps({'status':'ok','ch_ok':True,'readiness':{'ready':True},'schema':{'pending':[]},'providers_degraded':False,'embedding_dim_mismatch':False,'build_commit':os.environ.get('FAKE_BUILD',os.environ['EXPECT_COMMIT'])}))
""",
    )
    executable(
        bins / "docker",
        """#!/usr/bin/python3
import json,os,sys
from pathlib import Path
a=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(a)+'\\n')
if a[:2]==['image','inspect']:print('sha256:fixture')
elif a and a[0]=='inspect':
 field=a[a.index('-f')+1] if '-f' in a else ''
 print('sha256:fixture' if '.Image' in field else 'healthy' if 'Health' in field else 'running')
elif a[:1]==['exec'] and 'python' in a:print('holds fixture')
if a and a[0]=='compose' and 'build' in a and os.environ.get('FAKE_DIRTY_BUILD'):
 Path(os.environ['LOOM_REPO'],'untracked.py').write_text('changed during build')
""",
    )

    def run(*args: str, **values: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(repo / "redeploy.sh"), *args],
            cwd=repo,
            env={**env, **values},
            capture_output=True,
            text=True,
            timeout=10,
        )

    return SimpleNamespace(
        repo=repo, target=target, later=later, run=run, command=command, calls=calls
    )


def test_target_deploy_keeps_full_commit_when_main_advances(deployment: SimpleNamespace) -> None:
    d = deployment
    result = d.run("--target-commit", d.target, "--reapply-config")
    assert result.returncode == 0, result.stdout + result.stderr
    assert d.command("rev-parse", "HEAD") == d.target
    record = json.loads((d.repo / "var" / "deployed.json").read_text())
    assert record["sha"] == d.target
    calls = d.calls.read_text()
    assert f"--target {d.target} --allow-same" in calls
    assert "sandbox_probe.sh green" in calls
    assert '"nginx", "-t"' in calls and '"nginx", "-s", "reload"' in calls
    assert f"now at {d.target[:7]}" in result.stdout


@pytest.mark.parametrize("kind", ["head", "tracked", "untracked", "abbreviated", "symbolic"])
def test_target_refuses_mismatched_or_dirty_source(deployment: SimpleNamespace, kind: str) -> None:
    d = deployment
    target = d.target
    if kind == "head":
        target = d.later
    elif kind == "tracked":
        (d.repo / "redeploy.sh").write_text(SCRIPT.read_text() + "\n# local source\n")
    elif kind == "untracked":
        (d.repo / "untracked.py").write_text("local source")
    elif kind == "abbreviated":
        target = target[:12]
    else:
        target = "HEAD"
    result = d.run("--target-commit", target, "--reapply-config")
    assert result.returncode != 0
    assert not d.calls.exists(), result.stdout + result.stderr
    assert not (d.repo / "var" / "deployed.json").exists()


def test_config_reapply_requires_an_explicit_commit(deployment: SimpleNamespace) -> None:
    result = deployment.run("--reapply-config")
    assert result.returncode == 2
    assert not deployment.calls.exists()


def test_pinned_config_keeps_preflight_refusal(deployment: SimpleNamespace) -> None:
    result = deployment.run(
        "--target-commit", deployment.target, "--reapply-config", FAKE_PREFLIGHT_RC="1"
    )
    assert result.returncode != 0
    calls = deployment.calls.read_text()
    assert "apparmor_check.sh" in calls and "redeploy_preflight.sh" in calls
    assert '"compose"' not in calls


def test_wrong_build_never_reaches_the_proxy(deployment: SimpleNamespace) -> None:
    result = deployment.run(
        "--target-commit", deployment.target, "--reapply-config", FAKE_BUILD="wrong"
    )
    assert result.returncode != 0
    assert '"nginx"' not in deployment.calls.read_text()
    assert not (deployment.repo / "var" / "deployed.json").exists()


def test_source_changed_during_build_never_starts_the_colour(deployment: SimpleNamespace) -> None:
    result = deployment.run(
        "--target-commit", deployment.target, "--reapply-config", FAKE_DIRTY_BUILD="1"
    )
    assert result.returncode != 0
    calls = deployment.calls.read_text()
    assert '"build"' in calls
    assert '"up"' not in calls


def test_a_target_can_deploy_without_reapplying_config(deployment: SimpleNamespace) -> None:
    result = deployment.run("--target-commit", deployment.target)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = deployment.calls.read_text()
    assert "--allow-same" not in calls and "--no-strict" not in calls
    assert '"build"' in calls and '"nginx", "-s", "reload"' in calls


def test_same_build_skips_without_explicit_config_reapply(deployment: SimpleNamespace) -> None:
    result = deployment.run("--target-commit", deployment.target, FAKE_SAME_BUILD="1")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = deployment.calls.read_text()
    assert "redeploy_preflight.sh" in calls and '"build"' not in calls
    assert "use --reapply-config" in result.stdout
    assert "use --force" not in result.stdout
    assert not (deployment.repo / "var" / "deployed.json").exists()


def test_two_config_stages_keep_source_as_main_advances(deployment: SimpleNamespace) -> None:
    d = deployment
    (d.repo / ".env").write_text("CONFIG_STAGE=validation\n")
    first = d.run("--target-commit", d.target)
    assert first.returncode == 0, first.stdout + first.stderr
    tree = d.command("rev-parse", d.later + "^{tree}")
    newer = d.command("commit-tree", tree, "-p", d.later, "-m", "main advances again")
    d.command("update-ref", "refs/remotes/origin/main", newer)
    (d.repo / ".env").write_text("CONFIG_STAGE=normal\n")
    second = d.run("--reapply-config", "--target-commit", d.target, FAKE_SAME_BUILD="1")
    assert second.returncode == 0, second.stdout + second.stderr
    assert d.command("rev-parse", "HEAD") == d.target
    assert d.command("rev-parse", "origin/main") == newer
    record = json.loads((d.repo / "var" / "deployed.json").read_text())
    assert record["sha"] == d.target and not record["dirty"]
    calls = d.calls.read_text()
    assert calls.count('"build"') == 2
    assert calls.count('"nginx", "-s", "reload"') == 2
    assert "sandbox_probe.sh green" in calls and "sandbox_probe.sh blue" in calls
    assert "--no-strict" not in calls


@pytest.mark.parametrize("gate", ["APPARMOR", "SANDBOX"])
def test_pinned_config_retains_host_and_sandbox_refusals(
    deployment: SimpleNamespace, gate: str
) -> None:
    result = deployment.run(
        "--target-commit", deployment.target, "--reapply-config", **{f"FAKE_{gate}_RC": "1"}
    )
    assert result.returncode != 0
    assert '"nginx"' not in deployment.calls.read_text()
    assert not (deployment.repo / "var" / "deployed.json").exists()


def test_pinned_mode_cannot_be_combined_with_force(deployment: SimpleNamespace) -> None:
    result = deployment.run("--target-commit", deployment.target, "--force")
    assert result.returncode == 2
    assert not deployment.calls.exists()


def test_ordinary_same_build_retains_force_guidance(deployment: SimpleNamespace) -> None:
    result = deployment.run(FAKE_SAME_BUILD="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert deployment.command("rev-parse", "HEAD") == deployment.later
    assert "use --force" in result.stdout
    assert "use --reapply-config" not in result.stdout
    assert not (deployment.repo / "var" / "deployed.json").exists()
