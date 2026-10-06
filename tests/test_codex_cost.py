"""Check the pinned proxy's Codex API-equivalent price lookups."""
import unittest

import litellm


class CodexCostTest(unittest.TestCase):
    def test_gateway_and_account_lookups_have_prices_and_capabilities(self):
        prices = {
            "luna": (0.1, 0.5, 0.01, 0.125),
            "sol": (2, 10, 0.2, 2.5),
            "astra": (10, 50, 1, 12.5),
        }
        fields = ("input_cost_per_token", "output_cost_per_token",
                  "cache_read_input_token_cost", "cache_creation_input_token_cost")
        for name, rates in prices.items():
            for lookup in (f"gpt-6-{name}", f"responses/gpt-6-{name}"):
                with self.subTest(lookup=lookup):
                    info = litellm.model_cost[lookup]
                    for field, rate in zip(fields, rates):
                        self.assertAlmostEqual(info[field], rate / 1_000_000)
                    self.assertTrue(info["supports_function_calling"])
                    self.assertTrue(info["supports_xhigh_reasoning_effort"])


if __name__ == "__main__":
    unittest.main()
