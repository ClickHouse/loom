"""The Windows provider's fixed source ships with each self-contained plugin."""

from pathlib import Path

from scripts.codegen import agent_plugin_sync

ROOT = Path(__file__).resolve().parents[2]


def test_windows_provider_assets_are_mirrored_and_self_contained() -> None:
    source = ROOT / "integrations" / "claude-code-plugin"
    target = ROOT / "integrations" / "codex-plugin"
    generated_sources = set(agent_plugin_sync._files(source))
    for name in (
        "windows-session-protection.mjs",
        "windows-session-protection.ps1",
        "windows-native-bootstrap.ps1",
        "windows-compiler-job.ps1",
        "windows-private-records.cs",
    ):
        src = source / "lib" / name
        dst = target / "lib" / name
        assert src in generated_sources, f"Windows source asset is omitted by the generator: {name}"
        assert src.is_file() and dst.is_file(), (
            f"Windows source asset is absent from a plugin: {name}"
        )
        assert src.read_bytes() == dst.read_bytes(), (
            f"Windows source asset differs between plugins: {name}"
        )
