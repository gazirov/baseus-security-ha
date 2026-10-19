"""Flow logic tests using small HA doubles, not a real HA runtime/UI smoke test."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import voluptuous
except ImportError:
    voluptuous = None


class FlowDouble:
    def __init_subclass__(cls, **kwargs):
        pass

    def add_suggested_values_to_schema(self, schema, values):
        self.suggested = dict(values)
        return schema

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_show_progress(self, **kwargs):
        return {"type": "progress", **kwargs}

    def async_show_progress_done(self, **kwargs):
        return {"type": "progress_done", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    async def async_set_unique_id(self, value):
        self.unique_id = value

    def _abort_if_unique_id_configured(self):
        pass


@unittest.skipIf(voluptuous is None, "install requirements-test.txt")
class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/baseus_s1"
        modules = {}
        for name in ("homeassistant", "homeassistant.core", "homeassistant.config_entries",
                     "homeassistant.helpers", "homeassistant.helpers.aiohttp_client", "test_baseus_flow"):
            modules[name] = types.ModuleType(name)
        modules["test_baseus_flow"].__path__ = [str(root)]
        entries = modules["homeassistant.config_entries"]
        entries.ConfigFlow = entries.OptionsFlow = FlowDouble
        modules["homeassistant.core"].callback = lambda f: f
        modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = lambda hass: None
        modules["homeassistant.helpers"].selector = types.SimpleNamespace(
            TextSelector=lambda config: str, TextSelectorConfig=lambda **kw: kw,
            TextSelectorType=types.SimpleNamespace(PASSWORD="password"))
        self.modules_patch = patch.dict(sys.modules, modules)
        self.modules_patch.start()
        # Import under an isolated namespace: never import the integration init.
        import importlib
        self.module = importlib.import_module("test_baseus_flow.config_flow")
        self.api = MagicMock()
        self.api.status = AsyncMock(return_value={"instance_id": "a" * 32, "settings": {"history_limit": 20}})
        self.api.request = AsyncMock()
        self.api.start_login = AsyncMock(return_value="b" * 32)
        self.api.wait_login = AsyncMock(return_value="success")
        self.api_patch = patch.object(self.module, "Api", return_value=self.api)
        self.api_patch.start()

    async def asyncTearDown(self):
        self.api_patch.stop()
        for name in list(sys.modules):
            if name.startswith("test_baseus_flow."):
                sys.modules.pop(name)
        self.modules_patch.stop()

    def flow(self, options=False):
        flow = self.module.BaseusOptions() if options else self.module.BaseusFlow()
        flow.hass = types.SimpleNamespace(async_create_task=asyncio.create_task)
        flow.config_entry = types.SimpleNamespace(data={"url": "http://localhost:8099", "api_key": "local-key"},
                                                 options={"history_limit": 15})
        return flow

    async def test_invalid_server_and_api_key_preserve_exact_inputs(self):
        for error in (self.module.ApiAuthError("invalid_api_key"), self.module.ApiError("cannot_connect")):
            flow = self.flow()
            self.api.status.side_effect = error
            values = {"url": "http://localhost:8099/", "api_key": "private-api-key"}
            result = await flow.async_step_user(values)
            self.assertEqual(result["type"], "form")
            self.assertEqual(flow.suggested, values)
            self.assertEqual(values["url"], "http://localhost:8099/")
        flow = self.flow()
        values = {"url": "bad-server", "api_key": "private-api-key"}
        await flow.async_step_user(values)
        self.assertEqual(flow.suggested, values)

    async def test_settings_failure_preserves_submitted_values(self):
        flow = self.flow(options=True)
        self.api.request.side_effect = self.module.ApiError("cannot_connect")
        result = await flow.async_step_settings({"history_limit": 10, "interval_seconds": 600})
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertEqual(result["data_schema"]({})["history_limit"], 10)
        self.assertEqual(result["data_schema"]({})["interval_seconds"], 600)

    async def test_login_requires_explicit_confirmation(self):
        flow = self.flow(options=True)
        result = await flow.async_step_cloud_login({"account": "private-account", "password": "private-password",
                                                   "confirm_session_reset": False})
        self.assertEqual(result["errors"], {"base": "confirm_required"})
        self.api.start_login.assert_not_called()
        self.assertEqual(flow.suggested["password"], "private-password")

    async def test_new_entry_uses_model_neutral_title(self):
        result = await self.flow().async_step_user({"url": "http://localhost:8099", "api_key": "private-api-key"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["title"], "Baseus Security")

    async def test_login_busy_preserves_inputs_without_persisting_credentials(self):
        flow = self.flow(options=True)
        self.api.start_login.side_effect = self.module.ApiBusyError("busy")
        result = await flow.async_step_cloud_login({"account": "private-account", "password": "private-password",
                                                   "confirm_session_reset": True})
        self.assertEqual(result["errors"], {"base": "busy"})
        self.assertEqual(flow.suggested["password"], "private-password")
        self.assertNotIn("password", flow.config_entry.options)

    async def test_login_success_waits_once_and_saves_only_existing_options(self):
        flow = self.flow(options=True)
        result = await flow.async_step_cloud_login({"account": "private-account", "password": "private-password",
                                                   "confirm_session_reset": True})
        self.assertEqual(result["type"], "progress")
        self.assertNotIn("password", flow._login_values)
        await flow._login_task
        result = await flow.async_step_login_progress()
        self.assertEqual(result["next_step_id"], "login_result")
        result = await flow.async_step_login_result()
        self.assertEqual(result["step_id"], "login_success")
        saved = await flow.async_step_login_success({})
        self.assertEqual(saved["data"], {"history_limit": 15})
        self.assertIsNone(flow._login_values)
        self.api.start_login.assert_awaited_once()
        self.api.wait_login.assert_awaited_once()

    async def test_lost_status_does_not_repeat_login(self):
        flow = self.flow(options=True)
        self.api.wait_login.side_effect = self.module.ApiError("cannot_connect")
        await flow.async_step_cloud_login({"account": "private-account", "password": "private-password",
                                          "confirm_session_reset": True})
        await flow._login_task
        await flow.async_step_login_progress()
        result = await flow.async_step_login_result()
        self.assertEqual(result["step_id"], "login_unknown")
        self.api.start_login.assert_awaited_once()

    async def test_auth_status_distinguishes_missing_rejected_and_unverified(self):
        flow = self.flow(options=True)
        for state in ("missing", "unknown", "valid", "rejected"):
            self.api.status.return_value = {"auth": {"session_status": state}}
            result = await flow.async_step_auth_status()
            self.assertEqual(result["step_id"], "auth_" + state)
        self.api.status.return_value = {"auth": {"session_status": "missing", "login_state": "running"}}
        self.assertEqual((await flow.async_step_auth_status())["step_id"], "auth_running")
