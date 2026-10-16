"""Serving settings reconstructed from the completed rebuild's frozen recipe."""

import json

from server.core.vectors.prefix import parse_prefix_label


def serving_vector_settings(
    model: str, dimensions: int, prefixes: tuple[int, ...], *, prior_default: str
) -> dict[str, str]:
    """Route only labels whose rows the completed rebuild generated.

    Preserve an enabled prefix default at its previous width, when that width
    remains in the frozen recipe. A disabled or unsupported default stays off.
    """
    if not model or type(dimensions) is not int or not 1 <= dimensions <= 65535:
        raise ValueError("Invalid serving embedding space")
    if (
        not isinstance(prefixes, tuple)
        or len(set(prefixes)) != len(prefixes)
        or any(type(dim) is not int or not 1 <= dim <= dimensions for dim in prefixes)
    ):
        raise ValueError("Serving prefix policy differs from rebuilt dimensions")
    labels = [f"prefix:{dim}:{model}" for dim in prefixes]
    mapping = {
        label: {"table": "loom.code_chunks_prefix", "dim": dim}
        for label, dim in zip(labels, prefixes, strict=True)
    }
    old_default = parse_prefix_label(prior_default)
    default = (
        f"prefix:{old_default[0]}:{model}"
        if old_default is not None and old_default[0] in prefixes
        else ""
    )
    return {
        "code_vector_tables": json.dumps(mapping, sort_keys=True),
        "code_search_vector_models": ",".join(labels),
        "code_search_vector_model_default": default,
    }
