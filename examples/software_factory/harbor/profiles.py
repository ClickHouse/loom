"""Resolve the explicitly frozen repository evaluation adapter."""

import importlib


def load_profile(protocol):
    module = protocol["suite"]["adapter"]
    if not isinstance(module, str) or not module.startswith("examples.software_factory.evals."):
        raise ValueError("A repository evaluation adapter must be explicitly frozen")
    return importlib.import_module(module)
