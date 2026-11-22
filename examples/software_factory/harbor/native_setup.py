"""Trusted setup commands executed only inside the owned Loom container."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import subprocess
import sys
from pathlib import Path


def memory_diagnostics():
    return {
        name: path.read_text()
        for name in ("memory.events", "memory.peak", "memory.max")
        if (path := Path("/sys/fs/cgroup") / name).is_file()
    }


def external(request):
    operation = request["operation"]
    body = request.get("body", {})
    if operation == "start_memory_worker":
        log = open("/app/var/factory-memory-links.log", "ab", buffering=0)
        child = subprocess.Popen(
            [
                "/opt/venv/bin/python",
                "-m",
                "examples.software_factory.harbor.native_setup",
                "--memory-loop",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log.close()
        return {
            "pid": child.pid,
            "module": "server.memory.workers.code_links",
            "function": "run_pass",
            "interval": "product-default",
            "container_owned": True,
        }
    if operation in {"mint", "mint_setup"}:
        command = [
            "/opt/venv/bin/loom",
            "mint-token",
            "--subject",
            body["subject"],
            "--org",
            body["org"],
            "--ttl-days",
            "1",
            "--json",
        ]
        if operation == "mint_setup":
            command.extend(["--namespaces", *body["namespaces"], "--roles", "admin"])
        else:
            for namespace, role in body["ns_roles"].items():
                command.extend(["--namespace-role", namespace + "=" + role])
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        summary = json.loads(result.stdout)
        valid_roles = (
            summary.get("namespaces") == body["namespaces"] and summary.get("roles") == ["admin"]
            if operation == "mint_setup"
            else summary.get("ns_roles") == body["ns_roles"]
        )
        if summary.get("org") != body["org"] or not valid_roles:
            raise PermissionError("Native mint returned different scopes")
        return summary
    if operation in {"init", "backfill"}:
        command = [
            "/opt/venv/bin/loom",
            "code",
            operation,
            "--org",
            body["org"],
            "--namespace",
            body["namespace"],
        ]
        if operation == "backfill":
            command += [
                "--commit",
                body["commit"],
                "--no-fetch",
                "--embed",
                "--edges-second-pass",
                "--concurrency",
                "1",
            ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as error:
            diagnostics = {
                "returncode": error.returncode,
                "stdout": error.stdout,
                "stderr": error.stderr,
                "cgroup": memory_diagnostics(),
            }
            print(json.dumps(diagnostics), file=sys.stderr)
            raise
        return {
            "operation": operation,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "cgroup": memory_diagnostics(),
        }
    raise ValueError("Unknown external setup operation")


async def execute(request):
    operation = request["operation"]
    body = request.get("body", {})
    if operation in {"mint", "mint_setup", "init", "backfill", "start_memory_worker"}:
        return await asyncio.to_thread(external, request)
    from server.core.ch import get_client, set_query_tag

    client = get_client()
    await client.connect()
    set_query_tag("bench.factory.native_setup." + operation)
    try:
        if operation == "worker":
            from server.code.app.memory_links import checked_head
            from server.memory.workers.code_links import run_pass

            result = await run_pass()
            if result.get("errors") != 0:
                raise RuntimeError("Native memory link pass failed")
            return {
                "operation": "memory_code_links",
                "result": result,
                "checked_head": await checked_head(body["org"], body["namespace"]),
            }
        if operation == "scip_apply":
            from server.code.workers.scip_apply import run_pass

            result = dataclasses.asdict(await run_pass())
            if result["errors"] or result["batches_failed"]:
                raise RuntimeError("Native SCIP apply failed")
            return result
        if operation == "producer_metadata":
            from server.code.workers.scip_apply import watermark_key_for

            return {"watermark_key": watermark_key_for(body["producer"])}
        if operation == "audit":
            from examples.software_factory.harbor.index_setup import audit_index

            return await audit_index(body)
        if operation == "anchor_fingerprints":
            from examples.software_factory.harbor.anchors import capture

            return await capture(body)
        if operation == "embedding":
            from pathlib import Path

            from server.core.llm_provider import DEFAULT_MODEL_BY_PROVIDER
            from server.embedding.embedder import get_embedder
            from server.embedding.local import LocalEmbedder
            from server.embedding.local_models import PINNED, _source, model_dir
            from server.settings import settings

            pin = PINNED["bge-small"]
            where = Path(settings.local_embedding_model_dir) / "bge-small" / pin.revision
            if not all((where / name).is_file() for name in pin.files):
                raise PermissionError("Pinned embedding weights must already be in the image")
            model_dir("bge-small")
            embedder = get_embedder()
            if (
                not isinstance(embedder, LocalEmbedder)
                or settings.embedding_model != "local:bge-small"
                or settings.llm_provider != "anthropic"
                or settings.llm_model != DEFAULT_MODEL_BY_PROVIDER["anthropic"]
                or not settings.require_real_providers
            ):
                raise PermissionError("Runtime providers differ from the public plan")
            return {
                "embedding_model": embedder.model_name,
                "dimension": embedder.dim,
                "source": _source("bge-small"),
                "revision": pin.revision,
                "files": pin.files,
                "provider": "local",
                "llm_provider": settings.llm_provider,
                "llm_model": settings.llm_model,
                "require_real_providers": True,
            }
        if operation == "source_identity":
            import hashlib
            from pathlib import Path

            def hashes():
                return {
                    path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    for path in body["paths"]
                }

            return await asyncio.to_thread(hashes)
        raise ValueError("Unknown trusted setup operation")
    finally:
        await client.disconnect()


async def memory_loop():
    from server.core.ch import get_client, set_query_tag
    from server.memory.workers.code_links import run_pass
    from server.settings import settings

    client = get_client()
    await client.connect()
    try:
        while True:
            set_query_tag("bench.factory.native_memory_loop")
            await run_pass()
            await asyncio.sleep(settings.memory_code_links_interval_seconds)
    finally:
        await client.disconnect()


def main():
    if sys.argv[1:] == ["--memory-loop"]:
        asyncio.run(memory_loop())
        return
    request = json.load(sys.stdin)
    # Native CLI progress is captured separately; stdout remains one JSON receipt.
    with contextlib.redirect_stdout(io.StringIO()):
        result = asyncio.run(execute(request))
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
