"""Companion connection, snapshot options and explicit cloud login."""
try:
    import probatio as vol
except ImportError:  # HA releases before the schema-library migration.
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from .api import Api, ApiAuthError, ApiBusyError, ApiError, normalize_url
from .const import DEFAULT_OPTIONS, DOMAIN, NAME


def options_schema(values):
    return vol.Schema({
        vol.Required("interval_seconds", default=values["interval_seconds"]): vol.All(vol.Coerce(int), vol.Range(min=60, max=86400)),
        vol.Required("history_limit", default=values["history_limit"]): vol.All(vol.Coerce(int), vol.Range(min=10, max=20)),
        vol.Required("skip_frames", default=values["skip_frames"]): vol.All(vol.Coerce(int), vol.Range(min=0, max=300)),
        vol.Required("archive_days", default=values["archive_days"]): vol.All(vol.Coerce(int), vol.Range(min=1, max=3650)),
    })


class BaseusFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                data = {"url": normalize_url(user_input["url"]), "api_key": user_input["api_key"]}
                api = Api(async_get_clientsession(self.hass), data["url"], data["api_key"])
                status = await api.status()
                await self.async_set_unique_id(status["instance_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data=data,
                                               options=status["settings"])
            except ApiAuthError:
                errors["base"] = "invalid_auth"
            except (ApiError, ValueError):
                errors["base"] = "cannot_connect"
        schema = vol.Schema({
            vol.Required("url"): str,
            vol.Required("api_key"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        })
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(
            schema, user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BaseusOptions()


class BaseusOptions(config_entries.OptionsFlow):
    _login_task = None
    _login_values = None
    _login_result = None

    def _api(self):
        entry = self.config_entry
        return Api(async_get_clientsession(self.hass), entry.data["url"], entry.data["api_key"])

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["settings", "cloud_login", "auth_status"])

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            api = self._api()
            try:
                await api.request("PUT", "settings", data=user_input)
                return self.async_create_entry(title="", data=user_input)
            except ApiError:
                errors["base"] = "cannot_connect"
        values = {**DEFAULT_OPTIONS, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(step_id="settings", data_schema=options_schema(values), errors=errors)

    async def async_step_cloud_login(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._login_values = {key: user_input.get(key, "") for key in ("account", "password")}
            if user_input.get("confirm_session_reset") is not True:
                errors["base"] = "confirm_required"
            else:
                try:
                    job_id = await self._api().start_login(user_input["account"], user_input["password"])
                    # The POST has been accepted. Do not retain the password or
                    # repeat the login if waiting for its result later fails.
                    self._login_values = {"account": user_input["account"]}
                    self._login_task = self.hass.async_create_task(self._wait_login(job_id))
                    return await self.async_step_login_progress()
                except ApiBusyError:
                    errors["base"] = "busy"
                except ApiError:
                    errors["base"] = "login_start_failed"
        schema = vol.Schema({
            vol.Required("account"): str,
            vol.Required("password"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
            vol.Required("confirm_session_reset", default=False): bool,
        })
        return self.async_show_form(step_id="cloud_login", data_schema=self.add_suggested_values_to_schema(
            schema, self._login_values or {}), errors=errors)

    async def _wait_login(self, job_id):
        try:
            return await self._api().wait_login(job_id)
        except (ApiError, TimeoutError):
            return "unknown"

    async def async_step_login_progress(self, user_input=None):
        if not self._login_task.done():
            return self.async_show_progress(step_id="login_progress", progress_action="cloud_login",
                                            progress_task=self._login_task)
        self._login_result = "unknown" if self._login_task.cancelled() else self._login_task.result()
        self._login_task = None
        self._login_values = None
        return self.async_show_progress_done(next_step_id="login_result")

    async def async_step_login_result(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        # A static translation step for each result, never upstream error text.
        return self.async_show_form(step_id="login_" + (self._login_result or "unknown"), data_schema=vol.Schema({}))

    async def async_step_login_success(self, user_input=None):
        return await self.async_step_login_result(user_input)

    async def async_step_login_rejected(self, user_input=None):
        return await self.async_step_login_result(user_input)

    async def async_step_login_error(self, user_input=None):
        return await self.async_step_login_result(user_input)

    async def async_step_login_unknown(self, user_input=None):
        return await self.async_step_login_result(user_input)

    async def async_step_auth_status(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        try:
            auth = (await self._api().status()).get("auth")
            if not isinstance(auth, dict):
                return self.async_abort(reason="update_companion")
            state = auth.get("session_status", "unknown")
            if state not in ("valid", "missing", "rejected", "unknown"):
                state = "unknown"
            if auth.get("login_state") == "running":
                state = "running"
            return self.async_show_form(step_id="auth_" + state, data_schema=vol.Schema({}))
        except ApiError:
            return self.async_abort(reason="cannot_connect")

    async def async_step_auth_valid(self, user_input=None):
        return await self.async_step_auth_status(user_input)

    async def async_step_auth_missing(self, user_input=None):
        return await self.async_step_auth_status(user_input)

    async def async_step_auth_rejected(self, user_input=None):
        return await self.async_step_auth_status(user_input)

    async def async_step_auth_unknown(self, user_input=None):
        return await self.async_step_auth_status(user_input)

    async def async_step_auth_running(self, user_input=None):
        return await self.async_step_auth_status(user_input)
