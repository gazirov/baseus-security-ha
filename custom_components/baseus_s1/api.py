"""Async client; API credentials never appear in URLs or exception messages."""
import asyncio
import json
import re
from urllib.parse import urlsplit

import aiohttp


class ApiError(Exception):
    """Safe companion error."""


class ApiAuthError(ApiError):
    """Local API key rejected (not a Baseus login error)."""


class ApiBusyError(ApiError):
    """The companion is already capturing or logging in."""


def normalize_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("invalid_url")
    _ = parsed.port  # Validate port without echoing the original URL.
    return value.rstrip("/")


class Api:
    def __init__(self, session, url, token):
        self.session, self.url, self.token = session, normalize_url(url), token

    async def request(self, method, path, *, data=None, image=False):
        try:
            async with self.session.request(
                method, self.url + "/v1/" + path, json=data,
                headers={"Authorization": "Bearer " + self.token}, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                if response.status == 401:
                    raise ApiAuthError("invalid_api_key")
                if response.status == 409 and path == "capture":
                    return {"accepted": False}
                if response.status == 409 and path == "auth/login":
                    raise ApiBusyError("busy")
                if not 200 <= response.status < 300:
                    raise ApiError("companion_request_failed")
                limit = 16_777_216 if image else 1_048_576
                parts, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ApiError("response_too_large")
                    parts.append(chunk)
                content = b"".join(parts)
                if image:
                    if not content.startswith(b"\xff\xd8") or not content.endswith(b"\xff\xd9"):
                        raise ApiError("invalid_image")
                    return content
                return json.loads(content)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError):
            raise ApiError("cannot_connect") from None

    async def status(self):
        result = await self.request("GET", "status")
        if (not isinstance(result, dict) or not isinstance(result.get("instance_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", result["instance_id"])
                or not isinstance(result.get("cameras"), list)
                or not isinstance(result.get("settings"), dict)):
            raise ApiError("invalid_status")
        for camera in result["cameras"]:
            if (not isinstance(camera, dict) or not isinstance(camera.get("id"), str)
                    or not re.fullmatch(r"[0-9a-f]{16}", camera["id"])):
                raise ApiError("invalid_status")
        return result

    async def start_login(self, account, password):
        result = await self.request("POST", "auth/login", data={
            "account": account, "password": password, "confirm_session_reset": True})
        if not isinstance(result, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(result.get("job_id", ""))):
            raise ApiError("invalid_login_status")
        return result["job_id"]

    async def wait_login(self, job_id):
        # Poll only. A timeout or disconnect never repeats POST /auth/login.
        async with asyncio.timeout(180):
            while True:
                auth = (await self.status()).get("auth", {})
                if auth.get("job_id") != job_id:
                    raise ApiError("login_status_changed")
                state = auth.get("login_state")
                if state in ("success", "rejected", "error"):
                    return state
                if state != "running":
                    raise ApiError("invalid_login_status")
                await asyncio.sleep(1)

    async def image(self, camera, image="latest"):
        if not re.fullmatch(r"[0-9a-f]{16}", camera) or not re.fullmatch(
            r"latest|[0-9a-f]{16}_\d{8}T\d{6}_[0-9a-f]{8}\.jpg", image
        ):
            raise ApiError("invalid_image_id")
        return await self.request("GET", f"cameras/{camera}/images/{image}", image=True)
