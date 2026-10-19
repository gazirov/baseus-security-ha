"""HA-authenticated proxy: images are never exposed through /local."""
from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView

from .api import ApiError
from .const import DOMAIN


class ImageView(HomeAssistantView):
    url = "/api/baseus_s1/{entry_id}/{camera_id}/{image_id}"
    name = "api:baseus_s1:image"
    requires_auth = True

    async def get(self, request, entry_id, camera_id, image_id):
        hass = request.app[KEY_HASS]
        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None or not hasattr(coordinator, "api"):
            raise web.HTTPNotFound
        try:
            image = await coordinator.api.image(camera_id, image_id)
        except ApiError:
            raise web.HTTPNotFound from None
        return web.Response(body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"})
