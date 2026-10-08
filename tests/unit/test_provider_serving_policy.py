"""Serving labels and widths match the completed rebuild, across provider changes."""

import json

import pytest

from server.insights.domain.provider_serving_policy import (
    embedding_space_fingerprint,
    serving_vector_settings,
)


def test_new_labels_route_to_rebuilt_prefixes_and_keep_the_enabled_width():
    actual = serving_vector_settings(
        "new-model", 1024, (256, 512), prior_default="prefix:512:old-model"
    )
    assert json.loads(actual["code_vector_tables"]) == {
        "prefix:256:new-model": {"table": "loom.code_chunks_prefix", "dim": 256},
        "prefix:512:new-model": {"table": "loom.code_chunks_prefix", "dim": 512},
    }
    assert actual["code_search_vector_models"] == "prefix:256:new-model,prefix:512:new-model"
    assert actual["code_search_vector_model_default"] == "prefix:512:new-model"


@pytest.mark.parametrize("default", ["", "prefix:2048:old-model", "other-model"])
def test_absent_or_unsupported_default_cannot_route_to_unbuilt_vectors(default):
    assert (
        serving_vector_settings("new-model", 1024, (512,), prior_default=default)[
            "code_search_vector_model_default"
        ]
        == ""
    )


def test_empty_projection_recipe_removes_obsolete_routes():
    actual = serving_vector_settings("new-model", 1024, (), prior_default="prefix:512:old-model")
    assert json.loads(actual["code_vector_tables"]) == {}
    assert actual["code_search_vector_models"] == ""
    assert actual["code_search_vector_model_default"] == ""


@pytest.mark.parametrize("prefixes", [(1025,), (True,), (512, 512)])
def test_invalid_recipe_is_rejected_before_any_activation(prefixes):
    with pytest.raises(ValueError, match="prefix policy"):
        serving_vector_settings("new-model", 1024, prefixes, prior_default="")


def test_provider_fingerprint_distinguishes_endpoints_and_dimension_protocol():
    identity = ("openai-compatible", "model", 1024, "https://one.example/v1", True)
    original = embedding_space_fingerprint(*identity)
    assert original == embedding_space_fingerprint(*identity[:3], identity[3] + "/", True)
    assert original != embedding_space_fingerprint(*identity[:3], "https://two.example/v1", True)
    assert original != embedding_space_fingerprint(*identity[:4], False)
