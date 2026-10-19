"""A cached still image; dashboard refreshes never wake the camera."""
from homeassistant.components.camera import Camera

from .api import ApiError
from .const import DOMAIN
from .entity import BaseusEntity, discover


async def async_setup_entry(hass, entry, async_add_entities):
    discover(entry, hass.data[DOMAIN][entry.entry_id], async_add_entities,
             lambda coordinator, identifier: [SnapshotCamera(coordinator, identifier)])


class SnapshotCamera(BaseusEntity, Camera):
    _attr_translation_key = "snapshot"
    _attr_brand = "Baseus"
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator, identifier):
        Camera.__init__(self)
        BaseusEntity.__init__(self, coordinator, identifier, "camera")

    async def async_camera_image(self, width=None, height=None):
        try:
            return await self.coordinator.image(self.identifier)
        except ApiError:
            return None

    @property
    def extra_state_attributes(self):
        return {**{key: self.row.get(key) for key in ("last_capture", "last_attempt", "status", "error_code", "archive_error")},
                "service_error": self.coordinator.data.get("error_code"),
                "capture_in_progress": self.coordinator.data.get("busy", False)}
