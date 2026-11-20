"""Bound the 1.109 and 1.110.2 capability-id compatibility shim to named GPT metadata."""
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import litellm
import model_capability_ids

ROOT = Path(__file__).resolve().parents[1]


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
        for other in ("1.108.2", "1.110.1", "1.110.3"):
            with self.subTest(version=other), patch.object(litellm, "register_model") as upstream, \
                    patch.object(model_capability_ids, "version", return_value=other):
                model_capability_ids.install()
                self.assertIs(litellm.register_model, upstream)

    def test_reviewed_versions_wrap_upstream(self):
        self.assertEqual(model_capability_ids.VERSIONS, ("1.109.1", "1.110.2"))
        for reviewed in model_capability_ids.VERSIONS:
            with self.subTest(version=reviewed), patch.object(litellm, "register_model") as upstream, \
                    patch.object(model_capability_ids, "version", return_value=reviewed):
                del upstream._gateway_capability_ids
                model_capability_ids.install()
                self.assertIsNot(litellm.register_model, upstream)
                self.assertTrue(litellm.register_model._gateway_capability_ids)

    def test_wrapper_follows_the_installed_version(self):
        # sitecustomize installs the wrapper only on a version that needs it.
        # A gate that skips such a version is a silent no-op; on other versions upstream stays.
        installed = model_capability_ids.version("litellm")
        wrapped = getattr(litellm.register_model, "_gateway_capability_ids", False)
        self.assertEqual(wrapped, installed in model_capability_ids.VERSIONS, f"LiteLLM {installed}")

    def test_dockerfile_pin_needs_the_wrapper(self):
        # The pin of the gateway image needs the wrapper (tests/test_model_info.py shows why).
        # Review the gate when a new pin passes the model-info tests without it.
        dockerfile = ROOT / "Dockerfile"
        if not dockerfile.is_file():
            self.skipTest("no Dockerfile next to tests/")
        pins = re.findall(r"^ARG LITELLM_VERSION=(\S+)$", dockerfile.read_text(), re.M)
        self.assertEqual(len(pins), 1, pins)
        self.assertIn(pins[0], model_capability_ids.VERSIONS)

    def test_router_registers_deployment_ids_through_the_wrapper(self):
        # The router must call litellm.register_model with add_to_provider_model_sets=False,
        # or the wrapper never sees a deployment id. Only the versions in VERSIONS register so.
        installed = model_capability_ids.version("litellm")
        if installed not in model_capability_ids.VERSIONS:
            self.skipTest(f"LiteLLM {installed} does not register deployment ids without the provider sets")
        calls = []
        original = litellm.register_model

        def spy(model_cost, **kwargs):
            calls.append(kwargs.get("add_to_provider_model_sets"))
            return original(model_cost, **kwargs)

        with patch.object(litellm, "register_model", spy):
            litellm.Router(model_list=[{
                "model_name": "fixture", "litellm_params": {"model": "openai/gpt-gateway-router-fixture"},
                "model_info": {"id": "gpt-gateway-router-fixture", "litellm_provider": "openai",
                               "supports_reasoning": True}}])
        self.assertIn(False, calls)
