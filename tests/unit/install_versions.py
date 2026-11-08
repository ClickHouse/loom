"""GUARD_CASES: (installed, bundle, refused) rows of install.sh's downgrade guard.
test_install_script.py runs them, and test_installer_rehearsal.py checks
release_order against them."""

GUARD_CASES: list[tuple[str, str, bool]] = [
    ("9.9.8", "9.9.9", False),
    ("9.9.8", "9.9.9-rc.1", False),  # a candidate over the previous final
    ("10.0.0", "9.9.9", True),
    ("9.9.9", "9.9.9", False),  # a re-run
    ("9.9.9-rc.1", "9.9.9", False),  # a final over its own candidate
    ("9.10.0", "9.9.9", True),
    ("9.9.10-rc.1", "9.9.9", True),
    ("9.9.9", "9.9.9-rc.1", True),  # a candidate is below its own final release
    ("9.9.9-rc.2", "9.9.9-rc.1", True),
    ("9.9.9-rc.1", "9.9.9-rc.2", False),
    ("9.9.9-rc.9", "9.9.9-rc.10", False),  # by number, not as text
    ("9.9.9-rc.2", "9.9.9-rc.2", False),  # a re-run of the same candidate
]
