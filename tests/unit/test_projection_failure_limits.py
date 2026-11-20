"""Projection failure reads and retries bound the work admitted by each request."""

import pytest
from pydantic import ValidationError

from server.insights.api.models_ops import (
    ProjectionFailureListInput,
    ProjectionFailureRetryInput,
)


@pytest.mark.parametrize("model", [ProjectionFailureListInput, ProjectionFailureRetryInput])
@pytest.mark.parametrize("limit", [-1, 0, 1001, 50_000_000, 4_294_967_295])
def test_projection_failure_limit_rejects_out_of_range(model, limit: int) -> None:
    with pytest.raises(ValidationError) as exc:
        model(org="example", limit=limit)
    [error] = exc.value.errors()
    assert error["loc"] == ("limit",)
    assert error["type"] == ("greater_than_equal" if limit < 1 else "less_than_equal")


@pytest.mark.parametrize("model", [ProjectionFailureListInput, ProjectionFailureRetryInput])
@pytest.mark.parametrize("limit", [1, 1000])
def test_projection_failure_limit_accepts_boundaries(model, limit: int) -> None:
    assert model(org="example", limit=limit).limit == limit


@pytest.mark.parametrize(
    ("model", "default"), [(ProjectionFailureListInput, 100), (ProjectionFailureRetryInput, 50)]
)
def test_projection_failure_limit_defaults(model, default: int) -> None:
    assert model(org="example").limit == default
