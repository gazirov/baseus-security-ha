"""Local authenticated companion API, bounded history and single capture worker."""
import asyncio
from dataclasses import asdict, dataclass, replace
import datetime as dt
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

from aiohttp import web

from .autonomous import DirectCapture, capture_all
from .cloud import CloudClient
from .common import AuthError, CameraError, atomic_write, write_json
from .runner import process_lock

LOG = logging.getLogger(__name__)
CAMERA_ID = re.compile(r"[0-9a-f]{16}")
IMAGE_ID = re.compile(r"([0-9a-f]{16})_(\d{8}T\d{6})_[0-9a-f]{8}\.jpg")


@dataclass(frozen=True)
class Settings:
    interval_seconds: int = 900
    history_limit: int = 20
    skip_frames: int = 10
    archive_days: int = 30

    def updated(self, value):
        limits = {"interval_seconds": (60, 86400), "history_limit": (10, 20),
                  "skip_frames": (0, 300), "archive_days": (1, 3650)}
        if not isinstance(value, dict) or set(value) - limits.keys():
            raise ValueError("invalid_settings")
        for key, item in value.items():
            low, high = limits[key]
            if type(item) is not int or not low <= item <= high:
                raise ValueError("invalid_settings")
        return replace(self, **value)


def camera_id(sn):
    return hashlib.sha256(sn.encode()).hexdigest()[:16]


class Store:
    def __init__(self, root: Path, archive: Path | None = None):
        self.root, self.archive = root, archive
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.settings = Settings()
        settings_file = root / "settings.json"
        if settings_file.exists():
            self.settings = self.settings.updated(json.loads(settings_file.read_text("utf-8")))
        instance_file = root / "instance_id"
        if not instance_file.exists():
            atomic_write(instance_file, uuid.uuid4().hex.encode())
        self.instance_id = instance_file.read_text("utf-8").strip()
        self.cameras = {}
        status_file = root / "status.json"
        if status_file.exists():
            # Only restore the public schema; never reflect arbitrary JSON.
            for row in json.loads(status_file.read_text("utf-8")):
                identifier = row.get("id")
                if isinstance(identifier, str) and CAMERA_ID.fullmatch(identifier):
                    self.cameras[identifier] = {k: row.get(k) for k in (
                        "id", "name", "battery_percent", "battery_source", "last_capture", "last_image",
                        "last_attempt", "status", "error_code", "archive_error")}

    def _directory(self, identifier):
        if not CAMERA_ID.fullmatch(identifier):
            raise ValueError("invalid_camera")
        directory = self.root / "history" / identifier
        if (self.root / "history").is_symlink() or directory.is_symlink():
            raise ValueError("invalid_camera")
        return directory

    def _files(self, directory, identifier):
        if not directory.exists() or directory.is_symlink():
            return []
        return sorted((p for p in directory.iterdir() if p.is_file() and not p.is_symlink()
                       and IMAGE_ID.fullmatch(p.name) and p.name.startswith(identifier + "_")),
                      key=lambda p: p.name, reverse=True)

    def history(self, identifier):
        with self.lock:
            return [{"image": p.name, "requested_at": dt.datetime.strptime(
                IMAGE_ID.fullmatch(p.name)[2], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc).isoformat()}
                    for p in self._files(self._directory(identifier), identifier)]

    def image(self, identifier, name):
        with self.lock:
            directory = self._directory(identifier)
            if name == "latest":
                files = self._files(directory, identifier)
                if not files:
                    raise FileNotFoundError
                path = files[0]
            else:
                if not IMAGE_ID.fullmatch(name) or not name.startswith(identifier + "_"):
                    raise ValueError("invalid_image")
                path = directory / name
            if path.is_symlink() or path.stat().st_size > 16_777_216:
                raise ValueError("invalid_image")
            return path.read_bytes()

    def _prune(self, identifier):
        for path in self._files(self._directory(identifier), identifier)[self.settings.history_limit:]:
            path.unlink()
        if self.archive is not None:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.settings.archive_days)
            for path in self._files(self.archive / identifier, identifier):
                timestamp = dt.datetime.strptime(IMAGE_ID.fullmatch(path.name)[2], "%Y%m%dT%H%M%S").replace(tzinfo=dt.timezone.utc)
                if timestamp < cutoff:
                    path.unlink()

    def update_settings(self, value):
        with self.lock:
            previous = self.settings
            settings = self.settings.updated(value)
            write_json(self.root / "settings.json", asdict(settings))
            self.settings = settings
            for key, new in asdict(settings).items():
                old = getattr(previous, key)
                if old != new:
                    LOG.info("Settings changed: %s=%d -> %d", key, old, new)
            if settings == previous:
                LOG.info("Settings applied: unchanged")
            for identifier in self.cameras:
                try:
                    self._prune(identifier)
                except OSError:
                    LOG.warning("Retention cleanup failed: camera=%s", identifier)
            return asdict(settings)

    def ingest(self, results):
        with self.lock:
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            for result in results:
                identifier = camera_id(result["sn"])
                previous = self.cameras.get(identifier, {})
                row = {"id": identifier, "name": result["name"],
                       "battery_percent": result.get("battery_percent"),
                       "battery_source": result.get("battery_source"),
                       "status": result["status"], "last_attempt": now,
                       "last_capture": previous.get("last_capture"),
                       "last_image": previous.get("last_image"),
                       "error_code": result.get("error_code", "capture_error" if result["status"] == "error" else None),
                       "archive_error": None}
                if result["status"] == "ok":
                    source = Path(result["image"])
                    if (source.parent != self.root / "staging" or source.is_symlink()
                            or not IMAGE_ID.fullmatch(source.name) or not source.name.startswith(identifier + "_")):
                        raise ValueError("invalid_capture_path")
                    data = source.read_bytes()
                    target = self._directory(identifier) / source.name
                    atomic_write(target, data)
                    row["last_capture"] = result["received_at_utc"]
                    row["last_image"] = source.name
                    if self.archive is not None:
                        try:
                            if (self.archive / identifier).is_symlink():
                                raise OSError
                            atomic_write(self.archive / identifier / source.name, data)
                            LOG.info("Archive JPEG saved: camera=%s bytes=%d", identifier, len(data))
                        except OSError:
                            row["archive_error"] = "archive_unavailable"
                            LOG.warning("Archive unavailable: camera=%s; local JPEG retained", identifier)
                    source.unlink()
                    LOG.info("History JPEG saved: camera=%s bytes=%d", identifier, len(data))
                self.cameras[identifier] = row
                try:
                    self._prune(identifier)
                except OSError:
                    row["archive_error"] = "retention_failed"
                    LOG.warning("Retention cleanup failed: camera=%s", identifier)
            write_json(self.root / "status.json", list(self.cameras.values()))

    def status(self):
        with self.lock:
            return {"instance_id": self.instance_id, "settings": asdict(self.settings),
                    "archive_enabled": self.archive is not None,
                    "cameras": [dict(row) for row in self.cameras.values()]}

    def cleanup_staging(self):
        directory = self.root / "staging"
        if directory.is_symlink() or not directory.exists():
            return
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and IMAGE_ID.fullmatch(path.name):
                path.unlink()


class Companion:
    def __init__(self, store, *, country="RU", capture=None):
        self.store, self.country = store, country
        self.capture = capture or self._capture
        self.job = None
        self.scheduler = None
        self.error_code = None
        self.next_capture = 0
        self.wakeup = asyncio.Event()
        self.auth_blocked = False
        self.login_job = None
        self.auth = {"session_present": False, "session_status": "unknown", "login_state": "idle",
                     "job_id": None, "last_login_at": None, "login_error": None,
                     "credentials_stored": False}
        report = store.root / "auth_status.json"
        if report.exists():
            try:
                value = json.loads(report.read_text("utf-8"))
                if value.get("login_state") in ("success", "rejected", "error"):
                    self.auth["login_state"] = value["login_state"]
                if value.get("login_error") in ("login_rejected", "login_error"):
                    self.auth["login_error"] = value["login_error"]
                timestamp = value.get("last_login_at")
                if isinstance(timestamp, str) and len(timestamp) <= 40:
                    dt.datetime.fromisoformat(timestamp)
                    self.auth["last_login_at"] = timestamp
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self._refresh_session_presence()

    def _refresh_session_presence(self):
        try:
            value = json.loads((self.store.root / "state" / "session.json").read_text("utf-8"))
            present = isinstance(value, dict) and value.get("country") == self.country and bool(value.get("token"))
        except (OSError, ValueError):
            present = False
        self.auth["session_present"] = present
        if not present:
            self.auth["session_status"] = "missing"
        elif self.auth["session_status"] == "missing":
            self.auth["session_status"] = "unknown"

    @property
    def login_busy(self):
        return self.login_job is not None and not self.login_job.done()

    def request_login(self, value):
        if (not isinstance(value, dict) or set(value) != {"account", "password", "confirm_session_reset"}
                or value["confirm_session_reset"] is not True
                or not isinstance(value["account"], str) or not 1 <= len(value["account"].strip()) <= 254
                or not isinstance(value["password"], str) or not 1 <= len(value["password"]) <= 1024):
            raise ValueError("invalid_login_request")
        if self.busy:
            return None
        job_id = uuid.uuid4().hex
        self.auth.update(login_state="running", job_id=job_id, login_error=None,
                         last_login_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self.login_job = asyncio.create_task(self._run_login(value["account"].strip(), value["password"]))
        LOG.info("Explicit cloud login requested; credentials are not stored")
        return job_id

    def _login(self, account, password):
        with process_lock(self.store.root / "state" / "capture.lock"):
            client = CloudClient({"country": self.country}, self.store.root / "state" / "session.json")
            try:
                client.login(account, password)
            finally:
                client.close()

    async def _run_login(self, account, password):
        try:
            await asyncio.to_thread(self._login, account, password)
            self.auth.update(login_state="success", login_error=None, session_status="valid")
            self.auth_blocked, self.error_code, self.next_capture = False, None, 0
            LOG.info("Explicit cloud login succeeded; capture schedule resumed")
        except AuthError:
            self.auth.update(login_state="rejected", login_error="login_rejected")
            LOG.warning("Explicit cloud login rejected; no automatic retry")
        except Exception:
            self.auth.update(login_state="error", login_error="login_error")
            LOG.warning("Explicit cloud login failed; no automatic retry")
        finally:
            account = password = None
            await asyncio.to_thread(self._refresh_session_presence)
            try:
                await asyncio.to_thread(write_json, self.store.root / "auth_status.json", {
                    key: self.auth[key] for key in ("login_state", "last_login_at", "login_error")})
            except OSError:
                LOG.warning("Cloud login status could not be persisted")
            self.wakeup.set()

    def _capture(self, settings):
        with process_lock(self.store.root / "state" / "capture.lock"):
            client = CloudClient({"country": self.country}, self.store.root / "state" / "session.json")
            try:
                if not client.state.get("token"):
                    raise AuthError("No saved session")
                return capture_all(client, self.store.root / "staging", wake=True,
                                   capture=DirectCapture(skip_frames=settings.skip_frames))
            finally:
                client.close()

    @property
    def busy(self):
        return (self.job is not None and not self.job.done()) or self.login_busy

    def request_capture(self, *, trigger="manual"):
        if self.busy:
            LOG.info("Capture request ignored: already busy; no new job queued")
            return False
        self.auth_blocked = False  # Explicit manual retry can use a renewed session.
        trigger = trigger if trigger in ("manual", "scheduled") else "manual"
        LOG.info("Capture requested: trigger=%s", trigger)
        self.job = asyncio.create_task(self._run())
        return True

    async def _run(self):
        started = time.monotonic()
        settings = self.store.settings
        LOG.info("Capture cycle started: interval_seconds=%d skip_frames=%d",
                 settings.interval_seconds, settings.skip_frames)
        try:
            await asyncio.to_thread(self.store.cleanup_staging)
            results = await asyncio.to_thread(self.capture, settings)
            await asyncio.to_thread(self.store.ingest, results)
            self.error_code = None
            await asyncio.to_thread(self._refresh_session_presence)
            self.auth["session_status"] = "valid" if self.auth["session_present"] else "missing"
            LOG.info("Capture cycle completed: cameras=%d ok=%d error=%d disabled=%d elapsed=%.1fs",
                     len(results), sum(r["status"] == "ok" for r in results),
                     sum(r["status"] == "error" for r in results),
                     sum(r["status"] == "disabled" for r in results), time.monotonic() - started)
        except AuthError:
            self.error_code, self.auth_blocked = "auth_required", True
            await asyncio.to_thread(self._refresh_session_presence)
            self.auth["session_status"] = "rejected" if self.auth["session_present"] else "missing"
            LOG.warning("Capture cycle stopped: auth_required; schedule paused; automatic login disabled")
        except Exception:
            # No raw cloud/transport exception text in API or log.
            self.error_code = "capture_error"
            LOG.warning("Capture cycle failed: code=capture_error elapsed=%.1fs", time.monotonic() - started)
        finally:
            try:
                await asyncio.to_thread(self.store.cleanup_staging)
            except OSError:
                self.error_code = "storage_unavailable"
                LOG.warning("Staging cleanup failed: code=storage_unavailable")
            # Interval is measured from completion: slow sessions never overlap.
            self.next_capture = asyncio.get_running_loop().time() + self.store.settings.interval_seconds
            if not self.auth_blocked:
                self.log_next_capture()
            self.wakeup.set()

    def log_next_capture(self):
        delay = max(0, self.next_capture - asyncio.get_running_loop().time())
        at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)
        LOG.info("Next capture: in=%.0fs at_utc=%s", delay, at.isoformat(timespec="seconds"))

    async def _schedule(self):
        while True:
            self.wakeup.clear()
            if not self.busy and not self.auth_blocked:
                delay = self.next_capture - asyncio.get_running_loop().time()
                if delay <= 0:
                    self.request_capture(trigger="scheduled")
                else:
                    try:
                        await asyncio.wait_for(self.wakeup.wait(), delay)
                    except TimeoutError:
                        pass
                    continue
            await self.wakeup.wait()

    async def start(self, app):
        settings = self.store.settings
        LOG.info("Companion started: interval_seconds=%d history_limit=%d skip_frames=%d archive_enabled=%s archive_days=%d",
                 settings.interval_seconds, settings.history_limit, settings.skip_frames,
                 self.store.archive is not None, settings.archive_days)
        self.scheduler = asyncio.create_task(self._schedule())

    async def stop(self, app):
        LOG.info("Companion stopping: capture_busy=%s", self.busy)
        self.scheduler.cancel()
        await asyncio.gather(self.scheduler, return_exceptions=True)
        if self.job is not None and not self.job.done():
            # A Python worker cannot be safely cancelled mid-PPCS transaction.
            await asyncio.shield(self.job)
        if self.login_busy:
            await asyncio.shield(self.login_job)


def create_app(companion, token):
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or token.strip() != token:
        raise ValueError("API token must contain at least 32 ASCII characters")

    last_authenticated = None
    next_auth_warning = 0

    @web.middleware
    async def authenticated(request, handler):
        nonlocal last_authenticated, next_auth_warning
        now = asyncio.get_running_loop().time()
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            if now >= next_auth_warning:
                LOG.warning("API authentication rejected; further warnings limited to once per minute")
                next_auth_warning = now + 60
            return web.json_response({"error": "unauthorized"}, status=401)
        if last_authenticated is None or now - last_authenticated >= 120:
            # This proves a valid local API key, not the client's identity as HA.
            LOG.info("API client authenticated (first request or after inactivity)")
        last_authenticated = now
        LOG.debug("API request: operation=%s", request.match_info.route.name or "unknown")
        try:
            return await handler(request)
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "invalid_request"}, status=400)
        except FileNotFoundError:
            return web.json_response({"error": "not_found"}, status=404)
        except OSError:
            return web.json_response({"error": "storage_unavailable"}, status=503)

    app = web.Application(middlewares=[authenticated], client_max_size=4096)

    async def status(request):
        value = await asyncio.to_thread(companion.store.status)
        return web.json_response({**value, "busy": companion.busy, "error_code": companion.error_code,
                                  "auth": dict(companion.auth)})

    async def login(request):
        job_id = companion.request_login(await request.json())
        if job_id is None:
            return web.json_response({"error": "busy"}, status=409)
        return web.json_response({"job_id": job_id}, status=202)

    async def settings(request):
        value = await request.json()
        LOG.info("Settings update requested via authenticated API")
        response = await asyncio.to_thread(companion.store.update_settings, value)
        companion.next_capture = asyncio.get_running_loop().time() + companion.store.settings.interval_seconds
        companion.wakeup.set()
        if companion.auth_blocked:
            LOG.info("Settings applied; capture schedule remains paused: auth_required")
        elif companion.busy:
            LOG.info("Settings applied; active capture continues; new interval applies after completion")
        else:
            companion.log_next_capture()
        return web.json_response(response)

    async def capture(request):
        accepted = companion.request_capture()
        return web.json_response({"accepted": accepted}, status=202 if accepted else 409)

    async def history(request):
        return web.json_response(await asyncio.to_thread(companion.store.history, request.match_info["camera"]))

    async def image(request):
        data = await asyncio.to_thread(companion.store.image, request.match_info["camera"], request.match_info["image"])
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    app.router.add_get("/v1/status", status, name="status")
    app.router.add_put("/v1/settings", settings, name="settings")
    app.router.add_post("/v1/capture", capture, name="capture")
    app.router.add_post("/v1/auth/login", login, name="login")
    app.router.add_get("/v1/cameras/{camera}/history", history, name="history")
    app.router.add_get("/v1/cameras/{camera}/images/{image}", image, name="image")
    app.on_startup.append(companion.start)
    app.on_cleanup.append(companion.stop)
    return app


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "api_token"
    if not path.exists():
        atomic_write(path, secrets.token_urlsafe(32).encode())
        path.chmod(0o600)
