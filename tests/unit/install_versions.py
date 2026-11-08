"""Release versions as install.sh's downgrade guard orders them."""

# (installed, bundle, refused): what install.sh's downgrade guard does, which the
# rehearsal's release_order must agree with (test_installer_rehearsal.py).
GUARD_CASES = [
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
