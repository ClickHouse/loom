"""A number JSON cannot carry back (`1e400` parses to inf) is a 422 that echoes
it as a string, never a 500 from the response encoder."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from server.core.errors import register_exception_handler
from server.transport._common import validation_errors


class _Limit(BaseModel):
    capacity: float = Field(ge=0, allow_inf_nan=False)


def test_a_route_body_with_an_infinite_number_is_a_422() -> None:
    app = FastAPI()
    register_exception_handler(app)

    @app.post("/limit")
    def limit(body: _Limit) -> None:
        return None

    r = TestClient(app).post(
        "/limit", content=b'{"capacity": 1e400}', headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["input"] == "Infinity"


def test_a_registry_ops_validation_errors_echo_an_infinite_input_as_a_string() -> None:
    with pytest.raises(ValidationError) as e:
        _Limit.model_validate({"capacity": float("inf")})
    assert validation_errors(e.value)[0]["input"] == "Infinity"
