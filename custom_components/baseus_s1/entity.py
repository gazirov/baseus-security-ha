"""Shared camera device identity and dynamic discovery."""
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class BaseusEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, identifier, key):
        super().__init__(coordinator)
        self.identifier = identifier
        identity = f"{coordinator.data['instance_id']}_{identifier}"
        self._attr_unique_id = f"{identity}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, identity)}, manufacturer="Baseus",
                                            name=coordinator.camera(identifier).get("name") or "Baseus camera",
                                            model="Snapshot camera")

    @property
    def row(self):
        return self.coordinator.camera(self.identifier)


def discover(entry, coordinator, async_add_entities, factory):
    seen = set()

    @callback
    def update():
        entities = []
        for row in coordinator.data["cameras"]:
            if row["id"] not in seen:
                seen.add(row["id"])
                entities.extend(factory(coordinator, row["id"]))
        if entities:
            async_add_entities(entities)

    update()
    entry.async_on_unload(coordinator.async_add_listener(update))
