from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import time
import uuid
from pathlib import Path

from .android import AndroidBridge
from .cloud import CloudClient, find_wake_key
from .common import AuthError, CameraError, write_json
from .wake import wake_camera

LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def process_lock(path: Path):
    """OS-managed lock, automatically released after a crash; don't unlink its inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CameraError("Другой экземпляр скрипта уже использует этот state_directory") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def cloud_call(config: dict, state: Path, operation):
    client = CloudClient(config["cloud"], state / "session.json")
    try:
        try:
            return operation(client)
        except AuthError:
            account, password = os.environ.get("BASEUS_ACCOUNT"), os.environ.get("BASEUS_PASSWORD")
            if not account or not password:
                raise
            LOG.info("Обновление сессии Python-клиента")
            client.login(account, password)
            return operation(client)
    finally:
        client.close()


def cloud_wake(config: dict, state: Path):
    camera = config["camera"]
    sn, product = camera["sn"], camera["product_id"]
    if not sn or not product:
        raise CameraError("Для прямого MQTT укажите camera.sn и product_id из данных устройства")

    def operation(client):
        key = find_wake_key(client.device(sn, product), sn)
        wake_camera(client.mqtt_config(), sn, product, key,
                    ca_file=config["cloud"].get("mqtt_ca_file") or None)

    cloud_call(config, state, operation)


def capture_cycle(config: dict, base: Path):
    output = base / config["output_directory"]
    state = base / config["state_directory"]
    bridge = AndroidBridge(config["android"], config["camera"]["sn"])
    wake = (lambda: cloud_wake(config, state)) if config["cloud"].get("wake_before_capture") else None
    attempts = config["attempts_per_cycle"]
    for attempt in range(attempts):
        requested = dt.datetime.now(dt.timezone.utc)
        name = requested.strftime("s1_%Y%m%dT%H%M%S_") + uuid.uuid4().hex[:8]
        LOG.info("Съёмка %s, попытка %d/%d, wake=%s", name, attempt + 1, attempts,
                 "python-mqtt" if wake else "baseus-app")
        try:
            info = bridge.capture(output / f"{name}.jpg", before_live=wake)
            info["requested_at_utc"] = requested.isoformat()
            info["received_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            write_json(output / f"{name}.json", info)
            return output / f"{name}.jpg"
        except CameraError as exc:
            LOG.error("Попытка не завершена: %s", exc)
            if attempt + 1 == attempts:
                raise
            delay = config["retry_delay_seconds"] * (2 ** attempt)
            LOG.info("Повтор через %.0f с", delay)
            time.sleep(delay)


def run_schedule(config: dict, base: Path):
    interval = config["interval_seconds"]
    due = time.monotonic()
    while True:
        time.sleep(max(0, due - time.monotonic()))
        try:
            capture_cycle(config, base)
        except CameraError:
            LOG.error("Цикл завершился без подтверждённого снимка; следующая попытка по расписанию")
        # Skip missed slots instead of waking the camera repeatedly after suspend/long errors.
        due += interval
        now = time.monotonic()
        if due <= now:
            due += (int((now - due) // interval) + 1) * interval
        LOG.info("Следующий цикл через %.0f с", due - now)
