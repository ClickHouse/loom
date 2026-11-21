"""Own a fresh authenticated application for one persisted ASK scenario."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from tests.integration._redact import redact

_ROOT = Path(__file__).resolve().parents[2]
_SUCCESS = "CHECKED_ASK_ASGI_COMPLETE"


def _environment() -> dict[str, str]:
    from server.core import embedding_space
    from server.settings import settings

    assert settings.jwt_secret, "The owned profile needs its test signing configuration"
    assert settings.embedding_model.startswith("fake-")
    assert settings.embedding_provider in {"", "fake"}
    keep = {"HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "LOOM_PG_DSN"}
    env = {
        name: value
        for name, value in os.environ.items()
        if name in keep or name.startswith("LOOM_CH_")
    }
    env.update(
        PYTHONPATH=str(_ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        LOOM_ENV_FILE="",
        LOOM_AUTH_REQUIRED="true",
        LOOM_IDENTITY_PROVIDER="",
        LOOM_JWT_SECRET=settings.jwt_secret,
        LOOM_JWT_ISSUER=settings.jwt_issuer,
        LOOM_JWT_AUDIENCE=settings.jwt_audience,
        LOOM_STORAGE_AUTHORITY=settings.storage_authority,
        LOOM_PG_DSN=settings.pg_dsn,
        LOOM_BACKGROUND_WORKERS="off",
        LOOM_SHUTDOWN_READY_DELAY_S="0",
        LOOM_INSTANCE_LABEL="it:checked-ask:" + uuid4().hex,
        LOOM_LLM_PROVIDER="fake",
        LOOM_LLM_MODEL="fake-echo-1",
        LOOM_LLM_FALLBACK_MODEL="",
        LOOM_MEMORY_ASK_SYNTH_MODEL="fake-echo-1",
        LOOM_LATENCY_BUDGET_MULTIPLIER="10",
    )
    for field in embedding_space.FIELDS:
        value = getattr(settings, field)
        env["LOOM_" + field.upper()] = str(value).lower() if isinstance(value, bool) else str(value)
    return env


def _stop_owned(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def run_scenario(org: str, writer: str) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.integration._mirror_ask_asgi"],
        cwd=_ROOT,
        env=_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(json.dumps({"org": org, "writer": writer}), timeout=240)
        assert proc.returncode == 0, redact(output[-8000:])
        assert _SUCCESS in output.splitlines(), redact(output[-8000:])
    finally:
        try:
            _stop_owned(proc)
        finally:
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None:
                    pipe.close()


async def _main(payload: dict[str, str]) -> None:
    import httpx

    from server.core.ch import CHClient, set_query_tag
    from server.main import app
    from server.settings import settings
    from tests.integration._mirror_ask_case import check_generation_loss

    assert settings.auth_actually_required()
    assert settings.background_workers == "off"
    assert not settings.llm_fallback_model.strip()
    async with app.router.lifespan_context(app):
        ch = CHClient()
        try:
            await ch.connect()
            set_query_tag("test.checked_ask_asgi")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=45
            ) as client:
                health = await client.get("/v1/health")
                assert health.status_code == 200
                report = health.json()
                assert report["readiness"]["ready"] is True
                assert report["ch_ok"] is True
                assert report["fake_embedding"] is report["fake_llm"] is True
                assert report["providers_degraded"] is True
                denied = await client.post("/v1/memory.ask", json={"query": "launch code"})
                assert denied.status_code == 401
                await check_generation_loss(client, ch, payload["org"], payload["writer"])
        finally:
            await ch.disconnect()
    print(_SUCCESS, flush=True)


if __name__ == "__main__":
    asyncio.run(_main(json.load(sys.stdin)))
