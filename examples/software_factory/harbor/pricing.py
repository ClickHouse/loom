"""Frozen standard prices for qualified evaluation models."""

from decimal import Decimal

MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
CACHE_WRITE_5M = Decimal("1.25")
CACHE_WRITE_1H = Decimal("2")


def rates(model, incoming, *, ceiling=False):
    if model == "claude-opus-5-5":
        return Decimal("4"), Decimal("20")
    if model == "claude-sonnet-5-5":
        return Decimal("2"), Decimal("10")
    if model == "claude-haiku-5-5":
        return (
            (Decimal("0.5"), Decimal("2.5"))
            if ceiling or incoming > 100000
            else (Decimal("0.1"), Decimal("0.5"))
        )
    raise ValueError("Unknown model price")


def cache_read_multiplier(model):
    return Decimal("0.05" if model in {"claude-sonnet-5-5", "claude-opus-5-5"} else "0.1")


def first_prompt_input_prices(
    model, *, prompt_tokens, output_tokens, cached_tokens, written_tokens, long_writes, step_cost
):
    """Price one recorded input under three cache states, keeping its output fixed."""
    if (
        model not in MODELS
        or any(
            type(n) is not int or n < 0
            for n in (prompt_tokens, output_tokens, cached_tokens, written_tokens, long_writes)
        )
        or not prompt_tokens
        or cached_tokens + written_tokens > prompt_tokens
        or long_writes > written_tokens
        or step_cost is None
    ):
        return {}
    incoming, outgoing = rates(model, prompt_tokens)
    read_ratio = cache_read_multiplier(model)
    input_weight = (
        prompt_tokens
        - cached_tokens
        - written_tokens
        + cached_tokens * read_ratio
        + (written_tokens - long_writes) * CACHE_WRITE_5M
        + long_writes * CACHE_WRITE_1H
    )
    observed = input_weight * incoming / 1000000
    computed = observed + output_tokens * outgoing / 1000000
    if abs(computed - Decimal(str(step_cost))) > Decimal("0.00000001"):
        return {}
    return {
        "first_observed_input_cost_usd": float(observed),
        "first_cold_input_cost_usd": float(prompt_tokens * incoming * CACHE_WRITE_5M / 1000000),
        "first_warm_input_cost_usd": float(prompt_tokens * incoming * read_ratio / 1000000),
    }


def first_prompt_cost_sensitivity(result):
    """Replace only the recorded first input charge within the observed total."""
    total = result.get("total_provider_cost_usd")
    observed = result.get("first_observed_input_cost_usd")
    remainder = total - observed if total is not None and observed is not None else None
    estimates = {}
    for state in ("cold", "warm"):
        price = result.get(f"first_{state}_input_cost_usd")
        estimates[f"equal_{state}_provider_cost_usd"] = (
            remainder + price
            if remainder is not None and remainder >= 0 and price is not None
            else None
        )
    return estimates


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
