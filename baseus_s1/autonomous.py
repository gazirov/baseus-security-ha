"""Cloud enumeration -> experimental direct PPCS -> fresh JPEG, no Android.

All cameras are isolated: one failure does not abort remaining captures.
Only public camera descriptors/results may be persisted. P2P credentials stay
in memory. Availability is established by an actual capture, not online_status.
"""
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import io
import json
import logging
import math
import re
from pathlib import Path
import time
import uuid

from PIL import Image

from .common import AuthError, CameraError, atomic_write
from .ppcs_udp import ReceiveTimeout, UdpSession, _timeout, validate_source_options
from .wake import wake_camera
from .xm_p2p import build_signal_packet, encrypt_signal, login_payload
from .xm_stream import XmStream

LOG = logging.getLogger(__name__)


class CaptureFailure(CameraError):
    """Only allowlisted stage/code diagnostics; never retain the cause text."""
    def __init__(self, stage, code):
        self.stage = stage if stage in {"prepare", "connect", "xm_login", "video", "decode"} else "capture"
        self.code = code if code in {"timeout", "ack_timeout", "unsupported_login_reply", "login_rejected",
                                    "decrypt_failed", "invalid_frame", "camera_error", "transport_error"} else "camera_error"
        super().__init__(f"Capture failed: stage={self.stage} code={self.code}")


def _failure_code(exc):
    # Exact matching of internally curated messages, not echoing arbitrary text.
    return {
        "PPCS: direct peer не установлен; relay fallback пока не реализован": "timeout",
        "PPCS: исчерпаны попытки ожидания ACK": "ack_timeout",
        "PPCS: ошибка DNS/socket": "transport_error",
        "PPCS: ошибка отправки UDP": "transport_error",
        "PPCS: ошибка приёма UDP": "transport_error",
        "PPCS peer закрыл соединение": "transport_error",
        "XM login: неподдерживаемый формат ответа": "unsupported_login_reply",
        "XM login: неподдерживаемый ответ": "unsupported_login_reply",
        "XM login отклонён или схема ответа неизвестна": "unsupported_login_reply",
        "XM login отклонён": "login_rejected",
        "XM: ошибка расшифровки payload": "decrypt_failed",
        "Неподдерживаемый XM frame header": "invalid_frame",
    }.get(str(exc), "transport_error" if isinstance(exc, OSError) else "camera_error")


@dataclass(frozen=True)
class Camera:
    sn: str
    product_id: str
    name: str
    online_status: int | None
    raw: dict = field(repr=False, compare=False)


def cameras_from_response(value) -> list[Camera]:
    if not isinstance(value, dict) or not isinstance(value.get("device_list"), list):
        raise CameraError("GetUserDeviceList: неизвестная схема")
    rows = value["device_list"]
    total = value.get("total", len(rows))
    if type(total) is not int or total != len(rows) or len(rows) > 1000:
        raise CameraError("GetUserDeviceList: неполный список; pagination пока не поддерживается")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise CameraError("GetUserDeviceList: некорректное устройство")
        sn, product = row.get("device_sn"), row.get("product_id")
        if not isinstance(sn, str) or not 1 <= len(sn) <= 128 or not isinstance(product, str) or not 1 <= len(product) <= 128:
            raise CameraError("GetUserDeviceList: нет SN/product_id")
        if sn in seen:
            raise CameraError("GetUserDeviceList: повторяющийся SN")
        seen.add(sn)
        name = row.get("device_name", "")
        status = row.get("online_status")
        result.append(Camera(sn, product, name if isinstance(name, str) else "",
                             status if type(status) is int else None, row))
    return result


def _platform(camera):
    extend = camera.raw.get("device_extend")
    platform = extend.get("platform") if isinstance(extend, dict) else None
    if not isinstance(platform, dict) or any(
        not isinstance(platform.get(key), str) or not platform[key] or platform[key] == "<redacted>"
        for key in ("device_did", "service_string", "p2p_password")
    ):
        raise CameraError("Камера не содержит поддерживаемые XM/PPCS settings")
    return platform


def battery_metadata(camera):
    """Last battery report in the cloud list, not a live P2P measurement."""
    info = camera.raw.get("device_info")
    battery = info.get("battery") if isinstance(info, dict) else None
    if type(battery) is not int or not 0 <= battery <= 100:
        return {"battery_percent": None, "battery_source": None}
    return {"battery_percent": battery, "battery_source": "cloud_device_info"}


def _successful_reply(payload):
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        raise CameraError("XM login: неподдерживаемый формат ответа") from None
    if not isinstance(value, dict):
        raise CameraError("XM login: неподдерживаемый ответ")
    for key in ("ret", "code", "errno"):
        if key not in value:
            continue
        status = value[key]
        if type(status) is int:
            valid = status == 0
        elif isinstance(status, str) and re.fullmatch(r"-?[0-9]{1,10}", status):
            valid = int(status) == 0
        else:
            valid = False
        if not valid:
            raise CameraError("XM login отклонён")
    result = value.get("result")
    if "result" in value and type(result) not in (bool, int, str):
        raise CameraError("XM login: неподдерживаемый ответ")
    if (result is False or (isinstance(result, str) and result != "ok")
            or any(type(value.get(key)) is int and value[key] != 0 for key in ("ret", "code", "result", "errno"))):
        raise CameraError("XM login отклонён")
    # SDK SignaCallback.isOk compares the string result with "ok". Real S1
    # control responses use this schema, not just boolean/numeric results.
    if result is True or result == "ok":
        return
    for key in ("ret", "code", "result", "errno"):
        if type(value.get(key)) is int and value[key] == 0:
            return
    raise CameraError("XM login отклонён или схема ответа неизвестна")


class DirectCapture:
    def __init__(self, *, connect=UdpSession.connect, timeout=120, connect_timeout=45, decoder_factory=None,
                 bind_address=None, interface_index=None, skip_frames=0):
        _timeout(timeout)
        _timeout(connect_timeout)
        validate_source_options(bind_address, interface_index)
        if type(skip_frames) is not int or not 0 <= skip_frames <= 300:
            raise CameraError("Количество пропускаемых кадров должно быть целым числом от 0 до 300")
        self.connect, self.timeout = connect, timeout
        self.connect_timeout = connect_timeout
        self.bind_address, self.interface_index = bind_address, interface_index
        self.decoder_factory = decoder_factory or VideoDecoder
        self.skip_frames = skip_frames

    def __call__(self, camera: Camera) -> bytes:
        self._stage = "prepare"
        started = time.monotonic()
        identifier = hashlib.sha256(camera.sn.encode()).hexdigest()[:8]
        LOG.info("Capture start: camera=%s connect_timeout=%.1fs frame_timeout=%.1fs", identifier, self.connect_timeout, self.timeout)
        try:
            return self._capture(camera)
        except (CameraError, OSError, ValueError, TypeError) as exc:
            failure = exc if isinstance(exc, CaptureFailure) else CaptureFailure(self._stage, _failure_code(exc))
            LOG.error("Capture failed: camera=%s stage=%s code=%s exception=%s elapsed=%.1fs",
                      identifier, failure.stage, failure.code, type(exc).__name__, time.monotonic() - started)
            raise failure from None

    def _capture(self, camera):
        settings = _platform(camera)
        mode = settings.get("p2p_connect")
        if type(mode) is int and 0 <= mode <= 255:
            LOG.info("Cloud PPCS connect_mode=%d (0x%02x); backend=direct_udp", mode, mode)
            if 122 <= mode <= 126:
                LOG.warning("Cloud mode supports additional native connection paths; TCP/relay is not implemented in this backend")
        decoder = self.decoder_factory()
        password = settings["p2p_password"]
        self._stage = "connect"
        LOG.info("Capture stage: connect")
        options = {"timeout": self.connect_timeout}
        if self.bind_address is not None:
            options["bind_address"] = self.bind_address
        if self.interface_index is not None:
            options["interface_index"] = self.interface_index
        with self.connect(settings["device_did"], settings["service_string"], **options) as session:
            deadline = time.monotonic() + self.timeout
            streams = {channel: XmStream(password) for channel in (0, 1)}

            def command(number, payload):
                session.send(0, build_signal_packet(number, encrypt_signal(payload, password), encrypted=True))
                LOG.info("XM command sent: command=%d", number)

            self._stage = "xm_login"
            command(1, login_payload(camera.sn, password))
            logged_in, next_heartbeat = False, time.monotonic() + 3
            decoded_frames = 0
            while time.monotonic() < deadline:
                if time.monotonic() >= next_heartbeat:
                    # SDK empty command heartbeat has frame model 0xE0 and no AES payload.
                    heartbeat = bytearray(build_signal_packet(2001, b""))
                    heartbeat[5] = 0xE0
                    session.send(0, bytes(heartbeat))
                    next_heartbeat = time.monotonic() + 3
                try:
                    # Poll to the next XM heartbeat, not the entire frame deadline.
                    channel, chunk = session.recv(min(deadline, next_heartbeat))
                except ReceiveTimeout:
                    continue
                if channel not in streams:
                    continue  # Audio is ACKed by transport but not decoded for JPEG.
                for packet in streams[channel].feed(chunk):
                    LOG.debug("XM frame received: channel=%d model=%d command=%d bytes=%d", channel, packet.model, packet.command, len(packet.payload))
                    if channel == 0 and packet.command == 1 and not logged_in:
                        _successful_reply(packet.payload)
                        logged_in = True
                        LOG.info("XM login accepted")
                        self._stage = "video"
                        payload = json.dumps({"channel": 0, "camera_sn": camera.sn,
                                              "streamType": 0, "videoKeepAlive_2": 1}, separators=(",", ":")).encode()
                        command(2, payload)
                    elif channel == 1 and logged_in and packet.model in (1, 2):
                        if self._stage != "decode":
                            LOG.info("First video frame received: codec_hint=%s bytes=%d", packet.video_codec, len(packet.payload))
                        self._stage = "decode"
                        jpeg = decoder.feed(packet.video_codec, packet.payload)
                        if jpeg is not None:
                            decoded_frames += 1
                            if decoded_frames <= self.skip_frames:
                                LOG.debug("Skipping initial decoded frame: frame=%d limit=%d", decoded_frames, self.skip_frames)
                                continue
                            validate_jpeg(jpeg)
                            LOG.info("Fresh JPEG decoded: bytes=%d", len(jpeg))
                            return jpeg
            raise CaptureFailure(self._stage, "timeout")


def annexb_codec(data):
    """Detect parameter-set NALs; the S1 header codec bit is unreliable."""
    found = set()
    for match in re.finditer(b"\x00\x00(?:\x00)?\x01", data):
        pos = match.end()
        if pos >= len(data):
            continue
        first = data[pos]
        if first & 0x80:
            continue
        hevc_header = (pos + 1 < len(data) and (first & 1) == 0
                       and (data[pos + 1] & 0xF8) == 0 and (data[pos + 1] & 7))
        # HEVC IDR type 20 has first byte 0x28, whose low five bits
        # resemble H.264 PPS. Validate both HEVC header bytes first.
        if (first & 31) in (7, 8) and not hevc_header:
            found.add("h264")
        if hevc_header and ((first >> 1) & 63) in (32, 33, 34):
            found.add("hevc")
    if len(found) > 1:
        raise CameraError("Video: неоднозначные codec parameter sets")
    return next(iter(found)) if found else None


class VideoDecoder:
    def __init__(self):
        try:
            import av
        except ImportError:
            raise CameraError("Для JPEG установите requirements-autonomous.txt (PyAV)") from None
        self._av = av
        self._codec, self._context = None, None
        self._bytes = 0

    def feed(self, codec, data):
        self._bytes += len(data)
        if self._bytes > 16_777_216:
            raise CameraError("Video: превышен лимит данных до первого кадра")
        if codec not in ("h264", "hevc"):
            raise CameraError("Video: неподдерживаемый codec")
        detected = annexb_codec(data)
        if self._codec is None:
            if detected is None:
                return None  # Wait for real parameter sets, not a stale codec hint.
            self._codec = detected
            self._context = self._av.CodecContext.create(detected, "r")
            self._context.thread_count = 1
            LOG.info("Video codec confirmed from NAL parameter sets: codec=%s header_hint=%s", detected, codec)
        if detected is not None and detected != self._codec:
            raise CameraError("Video: codec изменился в сессии")
        try:
            for packet in self._context.parse(data):
                for frame in self._context.decode(packet):
                    if frame.width * frame.height > 16_777_216:
                        raise CameraError("Video: слишком большое изображение")
                    out = io.BytesIO()
                    frame.to_image().save(out, format="JPEG", quality=90)
                    return out.getvalue()
        except self._av.FFmpegError:
            # Joining an existing stream may begin before the first keyframe.
            return None
        return None


def validate_jpeg(data):
    if not isinstance(data, bytes) or len(data) > 16_777_216 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise CameraError("Backend не вернул полноценный JPEG")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG" or image.width * image.height > 16_777_216:
                raise CameraError("Некорректное JPEG изображение")
            image.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise CameraError("Не удалось проверить JPEG") from None


def validate_retry_settings(attempts, retry_delay):
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise CameraError("Количество попыток должно быть целым числом от 1 до 3")
    if (isinstance(retry_delay, bool) or not isinstance(retry_delay, (int, float))
            or not math.isfinite(retry_delay) or not 0 <= retry_delay <= 300):
        raise CameraError("Пауза между попытками должна быть числом от 0 до 300 секунд")


def _retryable_failure(exc, stage):
    if isinstance(exc, CaptureFailure):
        return exc.stage in {"connect", "xm_login", "video", "decode"} and exc.code in {"timeout", "ack_timeout", "transport_error"}
    # Wake failure messages are internally curated. Do not retry rejection,
    # missing settings, malformed replies, storage errors, or unknown failures.
    return stage == "wake" and isinstance(exc, CameraError) and str(exc) in {
        "MQTT: подключение не подтверждено", "MQTT: подписка не подтверждена",
        "MQTT: истекло время публикации",
        "Нет коррелированного ответа WatchDevice; пробуждение не подтверждено",
        "MQTT transport: OSError", "MQTT transport: TimeoutError",
        "MQTT transport: ConnectionError", "MQTT transport: ConnectionRefusedError",
        "MQTT transport: ConnectionResetError",
    }


def capture_all(client, output: Path, *, capture=None, wake=False, ca_file=None,
                attempts=3, retry_delay=20):
    """Fresh list on every call. Return public per-camera results only.

    Unsupported/offline cameras are attempted and reported, not inferred from
    stale online_status. A session-wide AuthError aborts instead of looping login.
    """
    validate_retry_settings(attempts, retry_delay)
    cameras = cameras_from_response(client.devices())
    backend = capture or DirectCapture()
    results = []
    for camera in cameras:
        identifier = hashlib.sha256(camera.sn.encode()).hexdigest()[:16]
        info = camera.raw.get("device_info", {})
        if isinstance(info, dict) and info.get("camera_on") == 0:
            results.append({"sn": camera.sn, "name": camera.name, "status": "disabled", "attempts": 0,
                            **battery_metadata(camera)})
            continue
        for attempt in range(1, attempts + 1):
            # Build a fresh result so successful retries do not retain old errors.
            result = {"sn": camera.sn, "name": camera.name, "status": "error", "attempts": attempt,
                      **battery_metadata(camera)}
            stage = "capture"
            LOG.info("Camera attempt: camera=%s attempt=%d/%d", identifier, attempt, attempts)
            try:
                if wake:
                    stage = "wake"
                    platform = _platform(camera)
                    key = platform.get("wake_key")
                    if not isinstance(key, str) or not key or key == "<redacted>":
                        raise CameraError("Нет wake key выбранной камеры")
                    LOG.info("MQTT wake start")
                    wake_camera(client.mqtt_config(), camera.sn, camera.product_id, key, ca_file=ca_file)
                    LOG.info("MQTT wake completed")
                requested = dt.datetime.now(dt.timezone.utc)
                stage = "capture"
                data = backend(camera)
                validate_jpeg(data)
                # Never derive a path directly from a cloud-provided name or SN.
                name = f"{identifier}_{requested:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}.jpg"
                path = output / name
                stage = "save"
                atomic_write(path, data)
                LOG.info("JPEG saved")
                result.update(status="ok", image=str(path), requested_at_utc=requested.isoformat(),
                              received_at_utc=dt.datetime.now(dt.timezone.utc).isoformat())
                break
            except AuthError:
                raise
            except (CameraError, OSError, ValueError, TypeError) as exc:
                # Backend/server errors may embed passwords; do not persist messages.
                result["error"] = type(exc).__name__
                result["error_stage"] = stage
                if isinstance(exc, CaptureFailure):
                    result["failure_stage"] = exc.stage
                    result["error_code"] = exc.code
                retry = attempt < attempts and _retryable_failure(exc, stage)
                LOG.warning("Camera attempt failed: camera=%s attempt=%d/%d stage=%s exception=%s retry=%s",
                            identifier, attempt, attempts, stage, type(exc).__name__, retry)
                if not retry:
                    break
                # DirectCapture has already exited/closed its previous session.
                LOG.info("Retry pause: camera=%s seconds=%.1f next_attempt=%d/%d", identifier, retry_delay, attempt + 1, attempts)
                time.sleep(retry_delay)
        results.append(result)
    return results
