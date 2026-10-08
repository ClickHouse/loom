"""The Docker rehearsal edits dotenv exactly as the user guide recommends."""

from scripts.gates.docker_quickstart_smoke import update_env


def test_update_replaces_duplicates_and_preserves_unrelated_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# Operator settings\nLOOM_PORT=7777\nCUSTOM=value\nLOOM_PORT=8888\n")
    update_env(path, {"LOOM_PORT": "9999", "LOOM_VERSION": "test"})
    assert path.read_text() == (
        "# Operator settings\nLOOM_PORT=9999\nCUSTOM=value\nLOOM_VERSION=test\n"
    )


def test_update_is_idempotent(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LOOM_LLM_MODEL=fake-old\n")
    updates = {"LOOM_LLM_MODEL": "fake-new"}
    update_env(path, updates)
    update_env(path, updates)
    assert path.read_text() == "LOOM_LLM_MODEL=fake-new\n"
    assert updates == {"LOOM_LLM_MODEL": "fake-new"}
