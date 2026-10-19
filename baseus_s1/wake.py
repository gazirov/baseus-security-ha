from __future__ import annotations

import json
import logging
import secrets
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from .common import CameraError
from .cloud import json_bytes

LOG = logging.getLogger(__name__)


def wake_message(sn, product_id, wake_key, seq, timestamp):
    if not sn or not product_id or not wake_key:
        raise CameraError("Для пробуждения нужны sn, product_id, wake_key")
    return {
        "method": "Action", "actionId": "WatchDevice",
        "expands": {"productId": product_id, "sn": sn, "wake_key": wake_key},
        "payload": {"timestamp": timestamp, "mode": 1}, "seq": str(seq),
    }


def wake_reply(value, seq: str, retained: bool):
    if retained or not isinstance(value, dict) or str(value.get("seq")) != seq:
        return None
    if value.get("actionId") != "WatchDevice":
        return None
    if not isinstance(value.get("result"), bool):
        return None
    return value["result"]


def wake_camera(settings: dict, sn: str, product_id: str, wake_key: str,
                timeout: float = 30, ca_file: str | None = None):
    """A PUBACK is NOT a wake acknowledgement; wait for correlated WatchDevice reply."""
    for key in ("host", "client_id", "username", "password", "publish", "subscribe"):
        if not isinstance(settings.get(key), str) or not settings[key]:
            raise CameraError(f"MQTT: отсутствует поле {key}")
    if "://" in settings["host"] or "/" in settings["host"]:
        raise CameraError("MQTT: ожидалось имя хоста, а не URL")
    seq = str(secrets.randbelow(1_000_000_000))
    connected, subscribed, replied = threading.Event(), threading.Event(), threading.Event()
    result = {"connect": False, "subscribe": False, "wake": False}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=settings["client_id"], clean_session=True,
                         protocol=mqtt.MQTTv311, reconnect_on_failure=False)
    client.username_pw_set(settings["username"], settings["password"])
    client.tls_set_context(ssl.create_default_context(cafile=ca_file))
    client.connect_timeout = min(timeout, 10)

    def on_connect(_client, _userdata, _flags, reason, _properties):
        result["connect"] = not reason.is_failure
        connected.set()

    def on_subscribe(_client, _userdata, _mid, reasons, _properties):
        result["subscribe"] = bool(reasons) and all(not code.is_failure for code in reasons)
        subscribed.set()

    def on_message(_client, _userdata, message):
        try:
            value = json.loads(message.payload)
        except (ValueError, UnicodeError):
            return
        answer = wake_reply(value, seq, message.retain)
        if answer is not None:
            result["wake"] = answer
            replied.set()

    client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
    deadline = time.monotonic() + timeout

    def remaining():
        return max(0, deadline - time.monotonic())

    try:
        client.connect(settings["host"], int(settings["port"]), keepalive=60)
        client.loop_start()
        if not connected.wait(remaining()) or not result["connect"]:
            raise CameraError("MQTT: подключение не подтверждено")
        rc, _ = client.subscribe(settings["subscribe"], qos=1)
        if rc != mqtt.MQTT_ERR_SUCCESS or not subscribed.wait(remaining()) or not result["subscribe"]:
            raise CameraError("MQTT: подписка не подтверждена")
        payload = wake_message(sn, product_id, wake_key, seq, int(time.time() * 1000))
        message = client.publish(settings["publish"], json_bytes(payload), qos=2, retain=False)
        message.wait_for_publish(timeout=remaining())
        if not message.is_published():
            raise CameraError("MQTT: истекло время публикации")
        LOG.info("WatchDevice отправлен; ожидание ответа камеры/сервиса")
        if not replied.wait(remaining()):
            raise CameraError("Нет коррелированного ответа WatchDevice; пробуждение не подтверждено")
        if not result["wake"]:
            raise CameraError("WatchDevice: сервер отклонил пробуждение")
        LOG.info("WatchDevice подтверждён; это ещё не подтверждение получения видеокадра")
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, CameraError):
            raise
        raise CameraError(f"MQTT transport: {type(exc).__name__}") from None
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()
