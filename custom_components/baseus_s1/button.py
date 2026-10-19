"""Manual asynchronous capture button."""
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import ApiError
from .const import DOMAIN
from .entity import BaseusEntity, discover


async def async_setup_entry(hass, entry, async_add_entities):
    discover(entry, hass.data[DOMAIN][entry.entry_id], async_add_entities,
             lambda coordinator, identifier: [CaptureButton(coordinator, identifier)])


class CaptureButton(BaseusEntity, ButtonEntity):
    _attr_translation_key = "capture"
    _attr_icon = "mdi:camera"

    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, identifier, "capture")

    async def async_press(self):
        try:
            await self.coordinator.api.request("POST", "capture")
            await self.coordinator.async_request_refresh()
        except ApiError as error:
            raise HomeAssistantError("Cannot request snapshot from companion") from error
