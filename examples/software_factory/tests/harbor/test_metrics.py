import unittest

from examples.software_factory.harbor.metrics import paid_token_totals, token_totals


class RecordedTokens(unittest.TestCase):
    def test_combined_llm_totals_require_both_solver_and_loom_evidence(self):
        result = dict(
            model_total_tokens=100, loom_provider_input_tokens=30, loom_provider_output_tokens=5
        )
        self.assertEqual(
            paid_token_totals(result),
            dict(loom_provider_total_tokens=35, total_paid_llm_tokens=135),
        )
        result["loom_provider_input_tokens"] = None
        self.assertEqual(
            paid_token_totals(result),
            dict(loom_provider_total_tokens=None, total_paid_llm_tokens=None),
        )

    def test_native_and_converse_tokens_include_cache_once_and_retain_output(self):
        result = {
            "provider_logical_requests": 2,
            "provider_failed_requests": 0,
            "provider_usage": [
                {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                },
                {
                    "inputTokens": 50,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 4,
                    "cacheWriteInputTokens": 6,
                },
            ],
        }
        totals = token_totals(result)
        self.assertEqual(totals["model_input_tokens"], 210)
        self.assertEqual(totals["model_output_tokens"], 15)
        self.assertEqual(totals["model_total_tokens"], 225)
        self.assertEqual(totals["model_cache_read_tokens"], 24)

    def test_failed_or_missing_usage_is_unknown_instead_of_zero(self):
        for result in (
            {},
            {"provider_logical_requests": 1, "provider_failed_requests": 0, "provider_usage": []},
            {"provider_logical_requests": 0, "provider_failed_requests": 1, "provider_usage": []},
        ):
            self.assertTrue(all(value is None for value in token_totals(result).values()))

    def test_proven_zero_requests_is_zero_and_malformed_values_are_unknown(self):
        result = {
            "provider_logical_requests": 0,
            "provider_failed_requests": 0,
            "provider_usage": [],
        }
        self.assertEqual(token_totals(result)["model_total_tokens"], 0)
        result.update(provider_logical_requests=1, provider_usage=[{"input_tokens": True}])
        self.assertIsNone(token_totals(result)["model_input_tokens"])
