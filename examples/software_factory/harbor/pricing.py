"""Frozen standard prices for qualified evaluation models."""

from decimal import Decimal

MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5"}
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


def rates(model, incoming, *, ceiling=False):
    if model == "claude-sonnet-5-5":
        return Decimal("2"), Decimal("10")
    if model == "claude-haiku-5-5":
        return (
            (Decimal("0.5"), Decimal("2.5"))
            if ceiling or incoming > 100000
            else (Decimal("0.1"), Decimal("0.5"))
        )
    raise ValueError("Unknown model price")


def cost_basis(model):
    short, long = rates(model, 0), rates(model, 100001)
    return {
        "type": "list-price estimates from actual provider usage; setup reported separately",
        "source": SOURCE,
        "input_usd_per_million": float(short[0]),
        "output_usd_per_million": float(short[1]),
        "prompts_over_100000_input": float(long[0]),
        "prompts_over_100000_output": float(long[1]),
        "infrastructure": "resource usage measured; monetary cost unknown",
    }
