"""Protocol recovered from Baseus Security 1.11.0; needs live-account validation.

No thumbnails are requested here. The module provides login, device RPC and wake.
It does not implement the proprietary XM/PPCS video transport.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .common import AuthError, CameraError, write_json

LOG = logging.getLogger(__name__)
AREA_URL = "https://ipc-public.baseussecurity.com/doc/doc/area.json"
ROOT_URL = "https://root.xmitech.net/v1/api/app/host"
APP_KEY = "YGnzWsirKf55MxrjG5"
APP_SECRET = "Teyi6OmXJZxVDiZY4k"
PUBLIC_KEY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDACE9CZ0ZLsrlF0/QRxnhqufcbAR2Y8CJXK"
    "VgGBHL8XyPuSPcUhqJGCO9UE7FlDsq1BFyuqx9iLs786SEAg5BskkAm6BttV5uXQSIFOxFjuz6"
    "PRueq++TiP9KCuPOspvWhVuZFJrajeyTVJ65sViiwmnjOUTt/60qJr8Gk4ZqCPwIDAQAB"
)


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def body_signature(body: bytes, timestamp: int) -> str:
    digest = hashlib.md5(body).hexdigest().upper()
    signed = hashlib.sha1(f"GSiPpcmX{digest}{timestamp}".encode()).hexdigest()
    return f"ipc#CElYvAkK#{signed}"


def xm_signature(timestamp: int) -> str:
    return hashlib.md5(f"{timestamp}{APP_KEY}{APP_SECRET}".encode()).hexdigest()


def encrypt_password(password: str) -> str:
    key = serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY))
    try:
        encrypted = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    except ValueError as exc:
        raise CameraError("Пароль не помещается в RSA-блок приложения") from exc
    return base64.b64encode(encrypted).decode("ascii")


def login_error_message(response: dict, *private_values: str) -> str:
    """Expose only the server's explanatory text, never its response data/auth fields."""
    message = next((response[key] for key in ("message", "msg")
                    if isinstance(response.get(key), str) and response[key].strip()), "")
    for value in sorted(filter(None, private_values), key=len, reverse=True):
        message = message.replace(value, "<redacted>")
    message = re.sub(r"[\w.+-]+@[\w.-]+", "<email>", message)
    message = re.sub(r"(?i)\b(?:bearer\s+|(?:auth|token|password|pwd|secret)\s*[:=]\s*)[^\s,;]+",
                     "<redacted>", message)
    message = re.sub(r"[A-Za-z0-9_+/=-]{64,}", "<redacted>", message)
    message = " ".join("".join(c for c in message if c.isprintable() or c.isspace()).split())
    return message[:300] or "сервер не передал текст ошибки"


def https_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise CameraError("Ожидался HTTPS URL сервиса без userinfo/fragment")
    return value


def require_xm(response: dict) -> object:
    if str(response.get("code")) in {"401", "100401"}:
        raise AuthError("XM: сессия истекла; выполните login")
    if response.get("result") is not True:
        code = response.get("code")
        raise CameraError(f"XM отклонил запрос (code={code if isinstance(code, int) else 'unknown'})")
    if "payload" not in response:
        raise CameraError("XM: ответ без payload")
    return response["payload"]


class CloudClient:
    def __init__(self, config: dict, state_file: Path, http=None):
        self.config = config
        self.country = config.get("country", "RU").upper()
        self.state_file = state_file
        self.http = http or requests.Session()
        self.state: dict = {}
        if state_file.exists():
            self.state = json.loads(state_file.read_text(encoding="utf-8"))
            if self.state.get("country") != self.country:
                self.state = {}

    def close(self):
        self.http.close()

    def _request(self, method, url, *, body=None, headers=None, params=None):
        https_url(url)
        try:
            response = self.http.request(
                method, url, data=body, headers=headers, params=params,
                timeout=(10, 30), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise CameraError(f"HTTP transport: {type(exc).__name__}") from None
        LOG.info("HTTP %s %s → %d", method, urlsplit(url).hostname, response.status_code)
        if response.status_code in {401, 403}:
            raise AuthError(f"HTTP {response.status_code}: авторизация отклонена")
        if not 200 <= response.status_code < 300:
            # XM may put its normal auth code inside an HTTP 500 response.
            # Inspect only known codes; never echo/log the message or payload.
            if 400 <= response.status_code < 600:
                try:
                    error = response.json()
                except ValueError:
                    error = None
                code = error.get("code") if isinstance(error, dict) else None
                if code in (401, 100401, "401", "100401"):
                    raise AuthError(f"HTTP {response.status_code}: авторизация отклонена (code={code})")
            raise CameraError(f"HTTP {response.status_code}; тело ответа не записано в лог")
        try:
            value = response.json()
        except ValueError:
            raise CameraError("HTTP: сервер вернул не JSON") from None
        if not isinstance(value, dict):
            raise CameraError("HTTP: неожиданная структура JSON")
        return value

    def _xm_headers(self, action=None, token=None, timestamp=None):
        timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
        headers = {
            "User-Agent": "Android/1.11.0/python/baseus-s1/13",
            "Timestamp": str(timestamp), "Version": "3.0", "SecretId": APP_KEY,
            "Signature": xm_signature(timestamp), "RequestId": str(uuid.uuid4()),
            "Lang": "en-US", "Content-Type": "application/json; charset=utf-8",
            "platform": "1",
        }
        if action:
            headers["Action"] = action
        if token:
            headers["Authorization"] = token
        return headers

    def login(self, account: str, password: str):
        if not account or not password:
            raise CameraError("Нужны BASEUS_ACCOUNT и BASEUS_PASSWORD")
        areas = self._request("GET", AREA_URL)
        country = next((v for v in areas.get("data", []) if v.get("shortName") == self.country), None)
        if not country:
            raise CameraError("Страна аккаунта не найдена в area.json")
        region = next((v for v in areas.get("serverList", []) if v.get("label") == country["server"]), None)
        if not region:
            raise CameraError("Сервер страны не найден в area.json")
        # AccountApi.login has no Domain-Name override: it uses bsServer.
        server = https_url(region["servers"]["bsServer"])
        server = https_url(self.config.get("login_base_url") or server)
        timestamp = int(time.time() * 1000)
        encrypted_password = encrypt_password(password)
        body = json_bytes({"type": 0, "account": account, "password": encrypted_password})
        headers = {
            "Content-Type": "application/json; charset=utf-8", "auth": "",
            "platform": "1", "appType": "40", "region": self.country,
            "appVersion": "1.11.0", "versionCode": "52", "lang": "en-US",
            "osVersion": "13", "brand": "Android", "model": "PythonBridge",
            "channel": self.config.get("channel", ""),
            "timestamp": str(timestamp), "sign": body_signature(body, timestamp),
        }
        tel = str(country["tel"])
        value = self._request("POST", server.rstrip("/") + "/api/auth/account/login",
                              body=body, headers=headers,
                              params={"countryCode": "1" if tel == "91" else tel})
        data = value.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("auth"), str) or not data["auth"]:
            code = value.get("code")
            detail = login_error_message(value, account, password, encrypted_password)
            raise AuthError(f"Baseus: login не выдал auth (code={code if isinstance(code, int) else 'unknown'}): {detail}")
        self.state = {"country": self.country, "tel": tel, "token": data["auth"],
                      "login_at": int(time.time())}
        write_json(self.state_file, self.state)
        LOG.info("Авторизация Baseus успешна; сессия сохранена локально")

    def _token(self):
        token = self.state.get("token")
        if not token:
            raise AuthError("Нет сессии Python-клиента. Сначала выполните login")
        return token

    def _host(self):
        if self.config.get("xm_url"):
            return https_url(self.config["xm_url"])
        if self.state.get("xm_url"):
            return https_url(self.state["xm_url"])
        self._token()
        timestamp = int(time.time() * 1000)
        data = {"code": int(self.state["tel"]), "app_key": APP_KEY,
                "sign": xm_signature(timestamp), "sign_ts": timestamp}
        payload = require_xm(self._request("POST", ROOT_URL, body=json_bytes(data),
                                         headers=self._xm_headers(timestamp=timestamp)))
        if not isinstance(payload, dict) or not payload.get("host"):
            raise CameraError("XM discovery: нет host")
        host = payload["host"]
        if not host.startswith("http"):
            host = "https://" + host
        self.state["xm_url"] = https_url(host)
        write_json(self.state_file, self.state)
        return host

    def rpc(self, action: str, body=None):
        LOG.info("XM RPC %s", action)
        return require_xm(self._request("POST", self._host(), body=json_bytes(body or {}),
                                       headers=self._xm_headers(action, self._token())))

    def devices(self):
        return self.rpc("GetUserDeviceList")

    def device(self, sn: str, product: str):
        return self.rpc("GetUserDevice", {"sn": sn, "product": product})

    def mqtt_config(self):
        value = self.rpc("GetUserMQTTS")
        keys = {"host", "port", "client_id", "username", "password", "publish", "subscribe"}
        if not isinstance(value, dict) or not keys <= value.keys():
            raise CameraError("GetUserMQTTS: неизвестная схема ответа")
        return value


def find_wake_key(value, sn: str) -> str:
    """Only accept a platform belonging to the exact requested camera."""
    matches = set()

    def visit(node):
        if isinstance(node, dict):
            if node.get("device_sn", node.get("sn")) == sn:
                platform = node.get("platform", {})
                extend = node.get("device_extend", {})
                if not platform and isinstance(extend, dict):
                    platform = extend.get("platform", {})
                key = platform.get("wake_key") if isinstance(platform, dict) else None
                key = key or node.get("wake_key")
                if isinstance(key, str) and key:
                    matches.add(key)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    if len(matches) != 1:
        raise CameraError("Не найден однозначный wake_key для camera.sn; проверьте схему devices")
    return matches.pop()
