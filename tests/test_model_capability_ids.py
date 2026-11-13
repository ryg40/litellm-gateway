"""Bound the 1.109 capability-id compatibility shim to named GPT metadata."""
import unittest
from unittest.mock import patch

import litellm
import model_capability_ids


class CapabilityIdTests(unittest.TestCase):
    def test_only_explicit_gpt_capability_ids_enter_provider_sets(self):
        cases = [
            ("gpt-gateway-fixture", {"id": "gpt-gateway-fixture", "litellm_provider": "openai",
                                     "supports_reasoning": True}, True),
            ("opaque-id", {"id": "opaque-id", "litellm_provider": "openai",
                           "supports_reasoning": True}, False),
            ("gpt-gateway-fixture", {"id": "different", "litellm_provider": "openai",
                                     "supports_reasoning": True}, False),
            ("gpt-gateway-fixture", {"id": "gpt-gateway-fixture", "litellm_provider": "chatgpt",
                                     "supports_reasoning": True}, False),
        ]
        for key, info, expected in cases:
            with self.subTest(key=key, info=info), \
                    patch.object(litellm, "register_model") as upstream, \
                    patch.object(litellm, "add_known_models") as known, \
                    patch.object(model_capability_ids, "version", return_value="1.109.1"):
                del upstream._gateway_capability_ids
                model_capability_ids.install()
                litellm.register_model({key: info}, add_to_provider_model_sets=False)
                self.assertEqual(known.called, expected)
                upstream.assert_called_once()

    def test_response_alias_copies_capabilities_without_prices(self):
        key = "responses/gpt-gateway-new-fixture"
        bare = key.removeprefix("responses/")
        info = {"id": key, "litellm_provider": "openai", "supports_reasoning": True,
                "supports_xhigh_reasoning_effort": True, "input_cost_per_token": 123}
        with patch.object(litellm, "register_model") as upstream, \
                patch.object(litellm, "add_known_models"), \
                patch.object(model_capability_ids, "version", return_value="1.109.1"):
            del upstream._gateway_capability_ids
            model_capability_ids.install()
            litellm.register_model({key: info}, add_to_provider_model_sets=False)
            copied = upstream.call_args_list[1].args[0][bare]
            self.assertNotIn("input_cost_per_token", copied)
            self.assertNotIn("id", copied)
            self.assertIs(copied["supports_xhigh_reasoning_effort"], True)

    def test_existing_bare_metadata_is_not_overwritten(self):
        key = "responses/gpt-gateway-existing-fixture"
        bare = key.removeprefix("responses/")
        info = {"id": key, "litellm_provider": "openai", "supports_reasoning": True}
        with patch.object(litellm, "register_model") as upstream, \
                patch.object(litellm, "add_known_models"), \
                patch.dict(litellm.model_cost, {bare: {"supports_reasoning": False}}), \
                patch.object(model_capability_ids, "version", return_value="1.109.1"):
            del upstream._gateway_capability_ids
            model_capability_ids.install()
            litellm.register_model({key: info}, add_to_provider_model_sets=False)
            upstream.assert_called_once()

    def test_other_versions_keep_upstream(self):
        with patch.object(litellm, "register_model") as upstream, \
                patch.object(model_capability_ids, "version", return_value="1.108.2"):
            model_capability_ids.install()
            self.assertIs(litellm.register_model, upstream)
