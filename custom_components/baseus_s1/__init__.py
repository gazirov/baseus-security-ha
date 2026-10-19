"""Baseus Security companion integration."""
from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import logging

from .api import Api, ApiError
from .const import DOMAIN, NAME
from .views import ImageView

PLATFORMS = [Platform.CAMERA, Platform.SENSOR, Platform.BUTTON]
LOGGER = logging.getLogger(__name__)


class Coordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry,
                         update_interval=timedelta(seconds=15))
        self.api = Api(async_get_clientsession(hass), entry.data["url"], entry.data["api_key"])
        self.entry_id = entry.entry_id
        self.images = {}

    async def _async_update_data(self):
        try:
            return await self.api.status()
        except ApiError as error:
            raise UpdateFailed("Companion unavailable; check its address/API key") from error

    def camera(self, identifier):
        return next((row for row in self.data["cameras"] if row["id"] == identifier), {})

    async def image(self, identifier):
        timestamp = self.camera(identifier).get("last_capture")
        if not timestamp:
            return None
        cached = self.images.get(identifier)
        if cached is None or cached[0] != timestamp:
            content = await self.api.image(identifier, self.camera(identifier).get("last_image") or "latest")
            self.images[identifier] = timestamp, content
        return self.images[identifier][1]


async def async_setup_entry(hass, entry):
    # Preserve custom titles and stable IDs. Only rename the old default title.
    if entry.title == "Baseus S1 Snapshots":
        hass.config_entries.async_update_entry(entry, title=NAME)
    coordinator = Coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    # Service persists settings; apply saved HA options after a service rebuild.
    try:
        await coordinator.api.request("PUT", "settings", data=dict(entry.options))
    except ApiError as error:
        raise ConfigEntryNotReady("Cannot configure companion") from error
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if not hass.data[DOMAIN].get("view_registered"):
        hass.http.register_view(ImageView())
        hass.data[DOMAIN]["view_registered"] = True
    entry.async_on_unload(entry.add_update_listener(async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id, None)
        return True
    return False
