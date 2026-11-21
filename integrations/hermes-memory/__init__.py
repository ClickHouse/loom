"""Native Hermes memory-provider entry point."""

from .provider import LoomMemoryProvider


def register(ctx):
    """Construct a provider for the profile loading this plugin."""
    ctx.register_memory_provider(LoomMemoryProvider())
