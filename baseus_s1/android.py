"""Rootless ADB bridge: cold-start Baseus, press LIVE capture, pull a NEW JPEG.

No screen crop or cached preview is ever used as the capture result.
UI selectors must be calibrated on the user's tablet before unattended use.
"""
from __future__ import annotations

import io
import logging
import posixpath
import re
import shlex
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .common import CameraError, atomic_write

LOG = logging.getLogger(__name__)
PACKAGE = "com.baseus.security.ipc"


def select_node(xml: str, selector: dict):
    allowed = {"text": "text", "resource_id": "resource-id", "description": "content-desc"}
    if not selector or not set(selector) <= allowed.keys():
        raise CameraError("UI selector должен содержать text, resource_id и/или description")
    nodes = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise CameraError("ADB: некорректный UI XML") from None
    for node in root.iter("node"):
        if (node.get("package") != PACKAGE or node.get("enabled") != "true"
                or node.get("visible-to-user") == "false"):
            continue
        if all(node.get(allowed[key]) == value for key, value in selector.items()):
            bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if bounds:
                x1, y1, x2, y2 = map(int, bounds.groups())
                if x2 > x1 and y2 > y1:
                    nodes.append(((x1 + x2) // 2, (y1 + y2) // 2))
    if len(nodes) > 1:
        raise CameraError("UI selector неоднозначен: найдено несколько элементов; уточните его")
    return nodes[0] if nodes else None


def parse_files(text: str, directory: str, sn: str):
    result = {}
    for line in text.splitlines():
        parts = line.rsplit("|", 2)
        if len(parts) != 3:
            continue
        path, size, mtime = parts
        # The app uses exactly SN + decimal timestamp-derived suffix + .jpg.
        if posixpath.dirname(path) != directory.rstrip("/"):
            continue
        if not re.fullmatch(re.escape(sn) + r"\d+\.jpg", posixpath.basename(path)):
            continue
        try:
            result[path] = (int(size), int(mtime))
        except ValueError:
            continue
    return result


def new_files(before: dict, after: dict, not_before: int):
    return [path for path, stat in after.items()
            if stat[0] > 0 and stat[1] >= not_before and before.get(path) != stat]


def verify_jpeg(data: bytes):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or min(image.size) < 100:
                raise CameraError("Сохранённый файл не похож на JPEG-кадр камеры")
            size = image.size
            image.load()  # Decode the whole file: reject a partial pull, not just a valid header.
            return size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise CameraError("JPEG повреждён или ещё не дописан") from None


class AndroidBridge:
    def __init__(self, config: dict, sn: str):
        self.config, self.sn = config, sn
        self.executable = config.get("adb", "adb")
        self.serial = config.get("serial", "")
        self.timeout = float(config.get("command_timeout", 20))
        self._ui_device = None
        self.directory = config.get("image_directory", "/sdcard/Pictures/xm_baseusipc").rstrip("/")

    def run(self, *args: str, timeout=None) -> bytes:
        command = [self.executable]
        if self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        try:
            proc = subprocess.run(command, capture_output=True, timeout=timeout or self.timeout, check=False)
        except FileNotFoundError:
            raise CameraError("ADB не найден. Установите Android Platform Tools и укажите android.adb") from None
        except subprocess.TimeoutExpired:
            raise CameraError("ADB: таймаут команды") from None
        if proc.returncode:
            # stderr can include device identifiers/paths; no wholesale logging.
            raise CameraError(f"ADB: команда {args[0]} завершилась с кодом {proc.returncode}; проверьте doctor")
        return proc.stdout

    def shell(self, *args: str):
        # ADB invokes the *device* shell even with shell=False on the host.
        return self.run("shell", shlex.join(args)).decode("utf-8", errors="replace")

    def check(self):
        rows = [line.split() for line in self.run("devices").decode("utf-8", errors="replace").splitlines()
                if "\t" in line]
        if self.serial:
            rows = [row for row in rows if row[0] == self.serial]
        if not rows:
            raise CameraError("ADB работает, но планшет не найден. Подключите USB и включите отладку по USB")
        if len(rows) != 1:
            raise CameraError("В ADB несколько устройств; укажите android.serial из adb devices")
        if rows[0][1] != "device":
            raise CameraError("Планшет offline/unauthorized. Разблокируйте его и подтвердите разрешение отладки")
        if self.run("get-state").strip() != b"device":
            raise CameraError("Планшет не авторизован для ADB")
        details = self.shell("dumpsys", "package", PACKAGE)
        match = re.search(r"versionName=([^\s]+)", details)
        if not match:
            raise CameraError("Приложение Baseus Security не установлено")
        version = match.group(1)
        expected = self.config.get("expected_app_version", "1.11.0")
        if version != expected:
            raise CameraError(f"Версия приложения {version}, ожидалась {expected}; нужна проверка UI и способа снимка")
        LOG.info("ADB подключён, Baseus Security %s", version)
        return version

    def ui_xml(self):
        if self.config.get("ui_backend", "adb") == "uiautomator2":
            return self._uiautomator_xml()
        path = f"/sdcard/.baseus-ui-{uuid.uuid4().hex}.xml"
        try:
            self.shell("uiautomator", "dump", "--compressed", path)
            xml = self.run("exec-out", shlex.join(("cat", path))).decode("utf-8")
            try:
                ET.fromstring(xml)
            except ET.ParseError:
                raise CameraError("ADB UI dump не получен (возможно, could not get idle state). Используйте android.ui_backend=uiautomator2") from None
            return xml
        finally:
            try:
                self.shell("rm", "-f", path)
            except CameraError:
                LOG.warning("Не удалось удалить временный UI XML на планшете")

    def _uiautomator_xml(self):
        try:
            import uiautomator2 as u2
            if self._ui_device is None:
                serial = self.serial or self.run("get-serialno").decode().strip()
                self._ui_device = u2.connect(serial)
                self._ui_device.jsonrpc.setConfigurator({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
            xml = self._ui_device.dump_hierarchy(compressed=True, pretty=False)
            ET.fromstring(xml)
            return xml
        except ImportError:
            raise CameraError("Установите зависимости: python -m pip install -r requirements.txt") from None
        except Exception as exc:
            self._ui_device = None
            raise CameraError(f"UIAutomator2: {type(exc).__name__}; проверьте подключение и разрешение USB-отладки") from None

    def tap_selector(self, selector, deadline):
        while time.monotonic() < deadline:
            point = select_node(self.ui_xml(), selector)
            if point:
                self.shell("input", "tap", str(point[0]), str(point[1]))
                return
            time.sleep(1)
        raise CameraError("UI: нужный элемент не найден/не активен. Проверьте inspect, вход и блокировку экрана")

    def files(self):
        # Directory may not exist before the very first screenshot.
        exists = self.shell("sh", "-c", 'if [ -d "$1" ]; then echo yes; fi', "sh", self.directory)
        if exists.strip() != "yes":
            return {}
        text = self.shell("find", self.directory, "-maxdepth", "1", "-type", "f", "-name", "*.jpg",
                          "-exec", "stat", "-c", "%n|%s|%Y", "{}", ";")
        return parse_files(text, self.directory, self.sn)

    def stop(self):
        self.shell("am", "force-stop", PACKAGE)
        if self.shell("sh", "-c", f"pidof {PACKAGE} || true").strip():
            raise CameraError("Не удалось остановить Baseus; новая сессия не гарантирована")

    def capture(self, output: Path, before_live=None):
        steps = self.config.get("open_live", [])
        capture = self.config.get("capture_selector", {})
        if not self.sn or not re.fullmatch(r"[A-Za-z0-9_-]+", self.sn):
            raise CameraError("Укажите camera.sn — настоящий серийный номер S1")
        if not steps or not capture:
            raise CameraError("Сначала настройте android.open_live и capture_selector с помощью inspect")
        # Validate before opening a session or sending a wake command.
        for selector in [*steps, capture]:
            select_node("<hierarchy/>", selector)
        self.check()
        started = time.monotonic()
        deadline = started + float(self.config.get("capture_timeout", 90))
        self.stop()  # Empty decoder state: no old live-session frame can survive this.
        launched = False
        try:
            before = self.files()
            self.shell("input", "keyevent", "KEYCODE_WAKEUP")
            self.shell("wm", "dismiss-keyguard")
            if before_live:
                before_live()
            self.shell("monkey", "-p", PACKAGE, "-c", "android.intent.category.LAUNCHER", "1")
            launched = True
            for selector in steps:
                self.tap_selector(selector, deadline)
            LOG.info("Открыта новая live-сессия; ждём кадр для штатной кнопки снимка")
            wait = float(self.config.get("stream_wait_seconds", 10))
            time.sleep(min(wait, max(0, deadline - time.monotonic())))
            not_before = int(self.shell("date", "+%s").strip())
            # Discard any images created by launch/navigation, before the capture tap.
            before.update(self.files())
            self.tap_selector(capture, deadline)
            attempts = 1
            next_tap = time.monotonic() + 15
            while time.monotonic() < deadline:
                candidates = new_files(before, self.files(), not_before)
                if len(candidates) > 1:
                    raise CameraError("Появилось несколько кадров: не могу однозначно выбрать снимок этой попытки")
                if candidates:
                    path = candidates[0]
                    data = self.run("exec-out", shlex.join(("cat", path)))
                    try:
                        width, height = verify_jpeg(data)
                    except CameraError:
                        time.sleep(1)
                        continue
                    atomic_write(output, data)
                    LOG.info("Новый JPEG сохранён: %s (%dx%d, %d байт)", output.name, width, height, len(data))
                    return {"backend": "android-live-capture", "width": width, "height": height,
                            "bytes": len(data), "session_seconds": round(time.monotonic() - started, 2),
                            "freshness_evidence": "cold app process + live capture button + newly written SN-prefixed JPEG",
                            "sensor_timestamp_verified": False}
                if time.monotonic() >= next_tap and attempts < 3:
                    # A capture before decoder startup can legitimately produce no file.
                    self.tap_selector(capture, deadline)
                    attempts += 1
                    next_tap = time.monotonic() + 15
                time.sleep(1)
            raise CameraError("Новый JPEG не появился; старые файлы и превью не используются")
        finally:
            try:
                if launched:
                    self.shell("input", "keyevent", "KEYCODE_BACK")
            finally:
                self.stop()  # Also on timeout, keyboard interrupt, failed login or MQTT.
                LOG.info("Baseus остановлен; Android P2P-сессия закрыта")
