from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def test_distribution_builders_ship_the_project_license_materials() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY LICENSE NOTICE THIRD_PARTY_NOTICES ./" in dockerfile
    assert "frontend_third_party_notices.mjs" in dockerfile
    assert "/frontend/dist/THIRD_PARTY_NOTICES.txt" in dockerfile
    extractor = (ROOT / "extractor" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY LICENSE NOTICE THIRD_PARTY_NOTICES /usr/share/doc/loom-extractor/" in extractor

    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert 'required_notice in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES")' in workflow
    assert 'zip -q "release/plugins/loom-claude-code-plugin-v${VERSION}.zip"' in workflow
    assert 'zip -q "release/plugins/loom-codex-plugin-v${VERSION}.zip"' in workflow
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"):
        assert name in workflow


def test_notices_name_the_non_permissive_material_accurately() -> None:
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    third_party = (ROOT / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
    assert "all permissive" not in notice
    assert "CC BY-SA 4.0" in notice
    assert "MPL-2.0" in notice
    assert "https://creativecommons.org/licenses/by-sa/4.0/" in third_party
    assert "Changes: none" in third_party


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_frontend_notice_generator_uses_only_production_packages(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    production = frontend / "node_modules" / "production-package"
    development = frontend / "node_modules" / "development-package"
    production.mkdir(parents=True)
    development.mkdir(parents=True)
    (frontend / "package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {},
                    "node_modules/production-package": {"version": "1.2.3", "license": "MIT"},
                    "node_modules/development-package": {
                        "version": "4.5.6",
                        "license": "MIT",
                        "dev": True,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    for directory, name, version in (
        (production, "production-package", "1.2.3"),
        (development, "development-package", "4.5.6"),
    ):
        (directory / "package.json").write_text(
            json.dumps({"name": name, "version": version, "license": "MIT"}),
            encoding="utf-8",
        )
        (directory / "LICENSE").write_text(f"Copyright holder for {name}\n", encoding="utf-8")

    output = tmp_path / "THIRD_PARTY_NOTICES.txt"
    subprocess.run(
        [
            shutil.which("node") or "node",
            str(ROOT / "scripts" / "release" / "frontend_third_party_notices.mjs"),
            "--root",
            str(frontend),
            "--output",
            str(output),
        ],
        check=True,
    )
    rendered = output.read_text(encoding="utf-8")
    assert "production-package@1.2.3" in rendered
    assert "Copyright holder for production-package" in rendered
    assert "development-package" not in rendered
