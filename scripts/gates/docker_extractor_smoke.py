"""Run inside the loom container of a quickstart stack: the server reaches the
extractor service at its configured URL with its token, gets each format the
reference extractor declares, and reads an extraction back through the
contract's checks.

    docker compose exec -T loom python - < scripts/gates/docker_extractor_smoke.py
"""

from __future__ import annotations

import asyncio

from server.knowledge.app import extractors


async def main() -> None:
    formats = await extractors.available_formats()
    assert set(formats) == {
        "markdown",
        "html",
        "text",
        "sql",
        "toml",
        "csv",
        "pdf",
        "docx",
        "pptx",
        "xlsx",
        "parquet",
    }, formats
    artifact = await extractors.extract(b"[server]\nport = 7777\n", name="loom.toml")
    assert [b.text for b in artifact.blocks] == ["[server]", "server.port = 7777"], artifact
    assert artifact.coverage.complete, artifact.coverage


if __name__ == "__main__":  # `python -` runs it as __main__ too
    asyncio.run(main())
    print("extractor: reached, declared eleven formats, extraction checked")
