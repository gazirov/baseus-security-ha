"""Battery report and last successful capture time."""
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .entity import BaseusEntity, discover


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([CloudAuth(hass.data[DOMAIN][entry.entry_id])])
    discover(entry, hass.data[DOMAIN][entry.entry_id], async_add_entities,
             lambda coordinator, identifier: [Battery(coordinator, identifier), LastCapture(coordinator, identifier)])


class Battery(BaseusEntity, SensorEntity):
    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, identifier, "battery")

    @property
    def native_value(self):
        return self.row.get("battery_percent")

    @property
    def extra_state_attributes(self):
        return {"source": self.row.get("battery_source")}


class LastCapture(BaseusEntity, SensorEntity):
    _attr_translation_key = "last_capture"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, identifier):
        super().__init__(coordinator, identifier, "last_capture")

    @property
    def native_value(self):
        value = self.row.get("last_capture")
        return datetime.fromisoformat(value) if value else None


class CloudAuth(CoordinatorEntity, SensorEntity):
    """Companion-level auth feedback, available before discovering any camera."""
    _attr_has_entity_name = True
    _attr_translation_key = "cloud_auth"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["missing", "unknown", "valid", "rejected", "logging_in"]

    def __init__(self, coordinator):
        super().__init__(coordinator)
        instance = coordinator.data["instance_id"]
        self._attr_unique_id = instance + "_cloud_auth"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, instance)}, name="Baseus Security companion",
                                            manufacturer="Baseus")

    @property
    def native_value(self):
        auth = self.coordinator.data.get("auth", {})
        if auth.get("login_state") == "running":
            return "logging_in"
        state = auth.get("session_status", "unknown")
        return state if state in self._attr_options else "unknown"

    @property
    def extra_state_attributes(self):
        auth = self.coordinator.data.get("auth", {})
        return {key: auth.get(key) for key in (
            "session_present", "credentials_stored", "login_state", "login_error", "last_login_at")}
