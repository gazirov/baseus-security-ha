import asyncio
import ast
from dataclasses import asdict
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image

try:
    from aiohttp.test_utils import TestClient, TestServer
    from baseus_s1.service import Companion, Settings, Store, camera_id, create_app, initialize
except ImportError:
    TestClient = None

from baseus_s1.common import AuthError


def picture():
    out = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(out, "JPEG")
    return out.getvalue()


def result(root, number=0, *, sn="synthetic-camera", age_days=0):
    timestamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days, seconds=100 - number)
    name = f"{camera_id(sn)}_{timestamp:%Y%m%dT%H%M%S}_{number:08x}.jpg"
    source = root / "staging" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(picture())
    return {"sn": sn, "name": "Test camera", "status": "ok", "image": str(source),
            "battery_percent": 96, "battery_source": "cloud_device_info", "received_at_utc": timestamp.isoformat()}


@unittest.skipIf(TestClient is None, "install requirements-service.txt")
class StorageTests(unittest.TestCase):
    def test_debug_logging_enables_only_project_metadata_loggers(self):
        from baseus_service import configure_logging
        from unittest.mock import MagicMock
        loggers = {}
        with patch("baseus_service.logging.basicConfig"), patch(
            "baseus_service.logging.getLogger", side_effect=lambda name: loggers.setdefault(name, MagicMock())
        ):
            configure_logging("DEBUG")
        loggers["baseus_s1.service"].setLevel.assert_called_once_with("DEBUG")
        loggers["baseus_s1.ppcs_udp"].setLevel.assert_called_once_with("DEBUG")
        for name in ("urllib3", "aiohttp", "paho", "PIL"):
            loggers[name].setLevel.assert_called_once_with(30)
        with self.assertRaises(ValueError):
            configure_logging("unknown")

    def test_settings_validation(self):
        for data in ({"history_limit": 9}, {"history_limit": 21}, {"interval_seconds": True},
                     {"interval_seconds": 59}, {"skip_frames": 301}, {"archive_days": 0},
                     {"archive_path": "secret"}, [], {"skip_frames": "10"}):
            with self.assertRaises(ValueError):
                Settings().updated(data)
        self.assertEqual(Settings().updated({"history_limit": 10}).history_limit, 10)

    def test_history_retention_archive_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root, archive = Path(directory) / "data", Path(directory) / "archive"
            store = Store(root, archive)
            identifier = camera_id("synthetic-camera")
            for number in range(25):
                store.ingest([result(root, number)])
            self.assertEqual(len(store.history(identifier)), 20)
            self.assertEqual(len(list((archive / identifier).glob("*.jpg"))), 25)
            store.update_settings({"history_limit": 10})
            self.assertEqual(len(store.history(identifier)), 10)
            latest = store.image(identifier, "latest")
            restored = Store(root, archive)
            self.assertEqual(restored.instance_id, store.instance_id)
            self.assertEqual(restored.settings.history_limit, 10)
            self.assertEqual(restored.image(identifier, "latest"), latest)
            self.assertEqual(restored.status()["cameras"][0]["battery_percent"], 96)
            self.assertNotIn("synthetic-camera", json.dumps(restored.status()))

    def test_failed_capture_preserves_last_image_and_labels_stale_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            good = result(root)
            store.ingest([good])
            identifier = camera_id(good["sn"])
            store.ingest([{**good, "status": "error", "error_code": "timeout"}])
            row = store.status()["cameras"][0]
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["last_capture"], good["received_at_utc"])
            self.assertTrue(store.image(identifier, "latest").startswith(b"\xff\xd8"))

    def test_archive_failure_does_not_lose_short_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            archive = Path(directory) / "not-a-directory"
            archive.write_text("unrelated", encoding="utf-8")
            store = Store(root, archive)
            store.ingest([result(root)])
            self.assertTrue(store.image(camera_id("synthetic-camera"), "latest"))
            self.assertEqual(store.status()["cameras"][0]["archive_error"], "archive_unavailable")
            self.assertEqual(archive.read_text("utf-8"), "unrelated")

    def test_archive_retention_only_deletes_owned_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root, archive = Path(directory) / "data", Path(directory) / "archive"
            store = Store(root, archive)
            identifier = camera_id("synthetic-camera")
            store.ingest([result(root, age_days=40)])
            unrelated = archive / identifier / "family-photo.jpg"
            unrelated.write_bytes(b"keep")
            store.ingest([result(root)])
            self.assertEqual(len(list((archive / identifier).glob("*.jpg"))), 2)
            self.assertTrue(unrelated.exists())

    def test_path_traversal_and_cross_camera_image_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            for identifier in ("../state", "/etc", "a" * 17):
                with self.assertRaises(ValueError):
                    store.history(identifier)
            identifier = camera_id("synthetic-camera")
            for name in ("../api_token", "session.json", "a" * 16 + "_20260101T000000_00000000.jpg"):
                with self.assertRaises(ValueError):
                    store.image(identifier, name)

    def test_initialization_never_overwrites_api_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            original = (root / "api_token").read_bytes()
            initialize(root)
            self.assertEqual(original, (root / "api_token").read_bytes())
            self.assertGreaterEqual(len(original), 32)


@unittest.skipIf(TestClient is None, "install requirements-service.txt")
class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = Store(self.root)
        self.companion = Companion(self.store, capture=lambda _: [])
        self.companion.next_capture = asyncio.get_running_loop().time() + 3600
        self.token = "synthetic-local-key-" + "x" * 32
        self.client = TestClient(TestServer(create_app(self.companion, self.token)))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_authentication_on_status_settings_capture_and_images(self):
        for method, path in (("GET", "/v1/status"), ("PUT", "/v1/settings"),
                             ("POST", "/v1/capture"), ("GET", "/v1/cameras/123/images/latest")):
            response = await self.client.request(method, path)
            self.assertEqual(response.status, 401)
            self.assertNotIn(self.token, await response.text())
        response = await self.client.get("/v1/status", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertNotIn(self.token, await response.text())

    async def test_settings_validate_persist_and_never_accept_filesystem_paths(self):
        for value in ({"history_limit": 100}, {"archive_path": "secret"}):
            response = await self.client.put("/v1/settings", headers=self.headers, json=value)
            self.assertEqual(response.status, 400)
        response = await self.client.put("/v1/settings", headers=self.headers, json={"history_limit": 10})
        self.assertEqual(response.status, 200)
        self.assertEqual(Store(self.root).settings.history_limit, 10)

    async def test_logs_connection_once_and_settings_without_request_secrets(self):
        with self.assertLogs("baseus_s1.service", level="DEBUG") as captured:
            for _ in range(3):
                response = await self.client.get("/v1/status?private=synthetic-query-secret", headers=self.headers)
                self.assertEqual(response.status, 200)
            response = await self.client.put("/v1/settings", headers=self.headers, json={"interval_seconds": 300})
            self.assertEqual(response.status, 200)
            response = await self.client.put("/v1/settings", headers=self.headers,
                                             json={"unexpected-private-key": "synthetic-body-secret"})
            self.assertEqual(response.status, 400)
        text = "\n".join(captured.output)
        self.assertEqual(text.count("API client authenticated"), 1)
        self.assertIn("Settings changed: interval_seconds=900 -> 300", text)
        self.assertIn("Next capture:", text)
        self.assertIn("API request: operation=status", text)
        for secret in (self.token, "synthetic-query-secret", "synthetic-body-secret", "unexpected-private-key"):
            self.assertNotIn(secret, text)

    async def test_rejected_authentication_log_is_rate_limited_and_redacted(self):
        with self.assertLogs("baseus_s1.service", level="WARNING") as captured:
            for _ in range(3):
                response = await self.client.get("/v1/status?token=synthetic-query-secret", headers={
                    "Authorization": "Bearer synthetic-invalid-secret"})
                self.assertEqual(response.status, 401)
        text = "\n".join(captured.output)
        self.assertEqual(text.count("API authentication rejected"), 1)
        self.assertNotIn("synthetic-invalid-secret", text)
        self.assertNotIn("synthetic-query-secret", text)

    async def test_explicit_login_validates_confirmation_and_rejects_unauthenticated_calls(self):
        payload = {"account": "test@example.invalid", "password": "synthetic-password", "confirm_session_reset": True}
        response = await self.client.post("/v1/auth/login", json=payload)
        self.assertEqual(response.status, 401)
        for value in ({**payload, "confirm_session_reset": False}, {**payload, "password": ""},
                      {**payload, "extra": "secret"}, []):
            response = await self.client.post("/v1/auth/login", headers=self.headers, json=value)
            self.assertEqual(response.status, 400)
        self.assertIsNone(self.companion.login_job)

    async def test_login_job_feedback_exclusion_and_secret_redaction(self):
        started, finish = threading.Event(), threading.Event()
        account, password = "private-account@example.invalid", "private-login-password"
        def login(received_account, received_password):
            self.assertEqual((received_account, received_password), (account, password))
            started.set()
            finish.wait(5)
            path = self.root / "state/session.json"
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps({"country": "RU", "token": "private-cloud-token"}), encoding="utf-8")
        with patch.object(self.companion, "_login", side_effect=login), self.assertLogs("baseus_s1.service", level="DEBUG") as logs:
            try:
                response = await self.client.post("/v1/auth/login", headers=self.headers, json={
                    "account": account, "password": password, "confirm_session_reset": True})
                self.assertEqual(response.status, 202)
                job_id = (await response.json())["job_id"]
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                response = await self.client.get("/v1/status", headers=self.headers)
                value = await response.json()
                self.assertTrue(value["busy"])
                self.assertEqual(value["auth"]["login_state"], "running")
                self.assertEqual(value["auth"]["job_id"], job_id)
                self.assertEqual((await self.client.post("/v1/capture", headers=self.headers)).status, 409)
                self.assertEqual((await self.client.post("/v1/auth/login", headers=self.headers, json={
                    "account": account, "password": password, "confirm_session_reset": True})).status, 409)
            finally:
                finish.set()
                await self.companion.login_job
            response = await self.client.get("/v1/status", headers=self.headers)
            value = await response.json()
        self.assertEqual(value["auth"]["login_state"], "success")
        self.assertTrue(value["auth"]["session_present"])
        self.assertEqual(value["auth"]["session_status"], "valid")
        self.assertFalse(value["auth"]["credentials_stored"])
        report = (self.root / "auth_status.json").read_text("utf-8")
        for secret in (account, password, "private-cloud-token", self.token):
            self.assertNotIn(secret, json.dumps(value) + report + str(logs.output))
        restored = Companion(Store(self.root), capture=lambda _: [])
        self.assertTrue(restored.auth["session_present"])
        self.assertEqual(restored.auth["session_status"], "unknown")
        self.assertEqual(restored.auth["login_state"], "success")

    async def test_failed_login_is_not_retried_and_preserves_existing_session(self):
        path = self.root / "state/session.json"
        path.parent.mkdir(exist_ok=True)
        original = json.dumps({"country": "RU", "token": "private-existing-token"})
        path.write_text(original, encoding="utf-8")
        with patch.object(self.companion, "_login", side_effect=AuthError("private-password")) as login:
            with self.assertLogs("baseus_s1.service", level="WARNING") as logs:
                self.companion.request_login({"account": "private-account", "password": "private-password",
                                              "confirm_session_reset": True})
                await self.companion.login_job
            login.assert_called_once()
        self.assertEqual(path.read_text("utf-8"), original)
        self.assertEqual(self.companion.auth["login_state"], "rejected")
        self.assertEqual(self.companion.auth["login_error"], "login_rejected")
        self.assertNotIn("private-password", str(logs.output))

    async def test_login_transport_error_does_not_expose_exception_or_save_credentials(self):
        with patch.object(self.companion, "_login", side_effect=RuntimeError("private-password private-account")) as login:
            with self.assertLogs("baseus_s1.service", level="WARNING") as logs:
                self.companion.request_login({"account": "private-account", "password": "private-password",
                                              "confirm_session_reset": True})
                await self.companion.login_job
            login.assert_called_once()
        self.assertEqual(self.companion.auth["login_state"], "error")
        self.assertEqual(self.companion.auth["login_error"], "login_error")
        report = (self.root / "auth_status.json").read_text("utf-8")
        self.assertNotIn("private-password", report + str(logs.output))
        self.assertNotIn("private-account", report + str(logs.output))
        self.assertFalse((self.root / "state/session.json").exists())

    async def test_nonblocking_capture_rejects_overlap_and_retains_safe_failures(self):
        started, finish = threading.Event(), threading.Event()
        def slow_capture(_):
            started.set()
            finish.wait(5)
            raise RuntimeError("synthetic secret cloud-token")
        self.companion.capture = slow_capture
        try:
            first = await self.client.post("/v1/capture", headers=self.headers)
            self.assertEqual(first.status, 202)
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = await self.client.post("/v1/capture", headers=self.headers)
            self.assertEqual(second.status, 409)
            response = await self.client.get("/v1/status", headers=self.headers)
            self.assertTrue((await response.json())["busy"])
        finally:
            finish.set()
            await self.companion.job
        response = await self.client.get("/v1/status", headers=self.headers)
        value = await response.json()
        self.assertEqual(value["error_code"], "capture_error")
        self.assertNotIn("cloud-token", json.dumps(value))

    async def test_auth_failure_blocks_automatic_relogin_and_retry(self):
        calls = []
        def denied(_):
            calls.append(1)
            raise AuthError("synthetic cloud-token")
        self.companion.capture = denied
        self.companion.request_capture()
        await self.companion.job
        self.assertTrue(self.companion.auth_blocked)
        self.assertEqual(self.companion.error_code, "auth_required")
        self.companion.next_capture = 0
        self.companion.wakeup.set()
        # A few event-loop turns are enough to let an erroneously scheduled
        # worker start; no wall-clock sleep or live cloud request.
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)

    async def test_history_and_image_served_only_after_authentication(self):
        self.store.ingest([result(self.root)])
        identifier = camera_id("synthetic-camera")
        response = await self.client.get(f"/v1/cameras/{identifier}/history", headers=self.headers)
        history = await response.json()
        response = await self.client.get(f"/v1/cameras/{identifier}/images/{history[0]['image']}", headers=self.headers)
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue((await response.read()).startswith(b"\xff\xd8"))

    async def test_integration_api_client_against_real_local_http_server(self):
        # Load this pure client without importing Home Assistant itself.
        path = Path(__file__).resolve().parents[1] / "custom_components/baseus_s1/api.py"
        spec = importlib.util.spec_from_file_location("baseus_ha_api", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import aiohttp
        async with aiohttp.ClientSession() as session:
            api = module.Api(session, str(self.client.make_url("/")).rstrip("/"), self.token)
            self.assertEqual((await api.status())["instance_id"], self.store.instance_id)
            self.store.ingest([result(self.root)])
            self.assertTrue(await api.image(camera_id("synthetic-camera")))
            with self.assertRaises(module.ApiAuthError):
                await module.Api(session, api.url, "wrong-key").status()
            for url in ("file:///etc/passwd", "http://user:secret@localhost", "http://localhost/?token=secret"):
                with self.assertRaises(ValueError):
                    module.normalize_url(url)
            def login(account, password):
                path = self.root / "state/session.json"
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({"country": "RU", "token": "synthetic-token"}), encoding="utf-8")
            with patch.object(self.companion, "_login", side_effect=login):
                job_id = await api.start_login("synthetic-account", "synthetic-password")
                self.assertEqual(await api.wait_login(job_id), "success")

    async def test_capture_to_history_and_api_is_end_to_end_without_cloud(self):
        self.companion.capture = lambda settings: [result(self.root)]
        with self.assertLogs("baseus_s1.service", level="INFO") as captured:
            response = await self.client.post("/v1/capture", headers=self.headers)
            self.assertEqual(response.status, 202)
            await self.companion.job
        text = "\n".join(captured.output)
        self.assertIn("Capture requested: trigger=manual", text)
        self.assertIn("Capture cycle started:", text)
        self.assertIn("History JPEG saved:", text)
        self.assertIn("cameras=1 ok=1 error=0 disabled=0", text)
        self.assertIn("Next capture:", text)
        self.assertNotIn("synthetic-camera", text)
        self.assertNotIn(self.token, text)
        response = await self.client.get("/v1/status", headers=self.headers)
        value = await response.json()
        self.assertFalse(value["busy"])
        self.assertIsNone(value["error_code"])
        self.assertEqual(value["cameras"][0]["status"], "ok")
        response = await self.client.get(
            f"/v1/cameras/{camera_id('synthetic-camera')}/images/latest", headers=self.headers)
        self.assertEqual(await response.read(), picture())
        self.assertFalse(list((self.root / "staging").glob("*.jpg")))

    async def test_failed_batch_cleans_only_owned_staging_files(self):
        def failed(settings):
            result(self.root)
            (self.root / "staging" / "keep.txt").write_text("unrelated", encoding="utf-8")
            raise AuthError("synthetic secret")
        self.companion.capture = failed
        self.companion.request_capture()
        await self.companion.job
        self.assertFalse(list((self.root / "staging").glob("*.jpg")))
        self.assertTrue((self.root / "staging" / "keep.txt").exists())

    async def test_scheduler_runs_at_start_and_schedules_from_completion(self):
        calls = []
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        def capture(settings):
            calls.append(settings)
            loop.call_soon_threadsafe(started.set)
            return []
        companion = Companion(self.store, capture=capture)
        await companion.start(None)
        try:
            await asyncio.wait_for(started.wait(), 2)
            await companion.job
            self.assertEqual(len(calls), 1)
            self.assertGreater(companion.next_capture, loop.time() + 899)
            self.assertFalse(companion.busy)
        finally:
            await companion.stop(None)


class PackageTests(unittest.TestCase):
    def test_release_allowlist_excludes_runtime_data_and_compiles(self):
        from tools.build_ha_package import build
        with tempfile.TemporaryDirectory() as directory:
            path = build(Path(directory) / "test-release.zip")
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                self.assertIn("custom_components/baseus_s1/config_flow.py", names)
                self.assertIn("compose.yaml", names)
                self.assertIn("README_EN.md", names)
                self.assertIn("AUTONOMOUS_EN.md", names)
                self.assertIn("home_assistant/README_EN.md", names)
                self.assertIn("home_assistant/dashboard.example.en.yaml", names)
                self.assertIn("custom_components/baseus_s1/brand/icon.png", names)
                self.assertIn("custom_components/baseus_s1/brand/icon@2x.png", names)
                for name in names:
                    self.assertFalse(any(part in name.split("/") for part in (
                        "state", "logs", "captures", "diagnostics", "analysis", ".venv", "__pycache__")))
                    self.assertNotIn("session.json", name)
                    if name.endswith(".py"):
                        ast.parse(archive.read(name).decode("utf-8"), filename=name)
                    if name.endswith(".json"):
                        json.loads(archive.read(name))

    def test_english_documentation_links_are_packaged(self):
        import re
        from tools.build_ha_package import release_files
        root = Path(__file__).resolve().parents[1]
        names = {p.relative_to(root).as_posix() for p in release_files()}
        for relative in ("README_EN.md", "AUTONOMOUS_EN.md", "home_assistant/README_EN.md"):
            path = root / relative
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text("utf-8")):
                target = target.split("#", 1)[0]
                if not target or "://" in target:
                    continue
                resolved = (path.parent / target).resolve()
                self.assertTrue(resolved.is_file(), relative + " -> " + target)
                self.assertIn(resolved.relative_to(root).as_posix(), names)

    def test_brand_images_and_stable_domain(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/baseus_s1"
        manifest = json.loads((root / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["domain"], "baseus_s1")
        self.assertEqual(manifest["name"], "Baseus Security")
        for size, name in ((256, "icon.png"), (512, "icon@2x.png")):
            with Image.open(root / "brand" / name) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (size, size))
                self.assertEqual(image.mode, "RGBA")

    def test_translations_have_matching_keys(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/baseus_s1"
        def keys(value, prefix=""):
            result = set()
            for key, item in value.items():
                path = prefix + "/" + key
                result.add(path)
                if isinstance(item, dict):
                    result |= keys(item, path)
            return result
        strings = json.loads((root / "strings.json").read_text("utf-8"))
        for language in ("en", "ru"):
            translation = json.loads((root / "translations" / f"{language}.json").read_text("utf-8"))
            self.assertEqual(keys(strings), keys(translation))
