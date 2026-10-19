"""Browse bounded camera history through Home Assistant's Media browser."""
from datetime import timedelta
import re

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import BrowseMediaSource, MediaSource, PlayMedia, Unresolvable

from .api import ApiError
from .const import DOMAIN


async def async_get_media_source(hass):
    return BaseusMedia(hass)


class BaseusMedia(MediaSource):
    name = "Baseus Security history"

    def __init__(self, hass):
        super().__init__(DOMAIN)
        self.hass = hass

    def _coordinator(self, entry_id):
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None or not hasattr(coordinator, "api"):
            raise BrowseError("Companion not loaded")
        return coordinator

    def _url(self, identifier):
        parts = identifier.split("/")
        if len(parts) != 3 or any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", part) for part in parts):
            raise BrowseError("Invalid image")
        self._coordinator(parts[0])
        return async_sign_path(self.hass, "/api/baseus_s1/" + identifier, timedelta(minutes=5))

    async def async_resolve_media(self, item):
        try:
            return PlayMedia(self._url(item.identifier), "image/jpeg")
        except BrowseError as error:
            raise Unresolvable("Image unavailable") from error

    async def async_browse_media(self, item):
        children = []
        if not item.identifier:
            for entry_id, coordinator in self.hass.data.get(DOMAIN, {}).items():
                if not hasattr(coordinator, "api"):
                    continue
                for camera in coordinator.data["cameras"]:
                    children.append(self._folder(f"{entry_id}/{camera['id']}", camera.get("name") or "Baseus camera"))
            return self._folder(None, self.name, children)
        parts = item.identifier.split("/")
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{16}", parts[1]):
            raise BrowseError("Invalid camera")
        coordinator = self._coordinator(parts[0])
        try:
            history = await coordinator.api.request("GET", f"cameras/{parts[1]}/history")
            for image in history:
                identifier = item.identifier + "/" + image["image"]
                children.append(BrowseMediaSource(domain=DOMAIN, identifier=identifier,
                    title=image["requested_at"], media_class=MediaClass.IMAGE, media_content_type="image/jpeg",
                    can_play=True, can_expand=False, thumbnail=self._url(identifier)))
        except (ApiError, KeyError, TypeError) as error:
            raise BrowseError("History unavailable") from error
        return self._folder(item.identifier, coordinator.camera(parts[1]).get("name") or "Baseus camera", children)

    @staticmethod
    def _folder(identifier, title, children=None):
        return BrowseMediaSource(domain=DOMAIN, identifier=identifier, title=title,
            media_class=MediaClass.DIRECTORY, media_content_type=MediaType.IMAGE,
            can_play=False, can_expand=True, children=children)
