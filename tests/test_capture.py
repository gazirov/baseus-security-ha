import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from baseus_s1.android import AndroidBridge, PACKAGE, new_files, parse_files, select_node, verify_jpeg
from baseus_s1.common import CameraError
from baseus_s1.configure import candidates
from baseus_s1.runner import process_lock, run_schedule


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "green").save(output, "JPEG")
    return output.getvalue()


class FakeBridge(AndroidBridge):
    def __init__(self, produce=True, image=None):
        super().__init__({"open_live": [{"text": "Camera"}], "capture_selector": {"text": "Capture"},
                          "capture_timeout": 5, "stream_wait_seconds": 0.01}, "SN")
        self.events = []
        self.tapped = False
        self.produce = produce
        self.image = jpeg() if image is None else image

    def check(self):
        self.events.append("check")

    def stop(self):
        self.events.append("stop")

    def files(self):
        existing = {"/sdcard/Pictures/xm_baseusipc/SN100.jpg": (2000, 100)}
        if self.tapped and self.produce:
            existing["/sdcard/Pictures/xm_baseusipc/SN200.jpg"] = (len(self.image), 200)
        return existing

    def shell(self, *args):
        self.events.append(args)
        return "200" if args[0] == "date" else ""

    def tap_selector(self, selector, deadline):
        self.events.append(selector)
        if selector.get("text") == "Capture":
            self.tapped = True

    def run(self, *args, **kwargs):
        self.events.append(args)
        return self.image


class CaptureTests(unittest.TestCase):
    def test_setup_uses_unique_resource_id_when_text_is_ambiguous(self):
        xml = (f'<hierarchy><node package="{PACKAGE}" enabled="true" text="Photo" '
               'resource-id="one" bounds="[0,0][20,20]"/>'
               f'<node package="{PACKAGE}" enabled="true" text="Photo" '
               'resource-id="two" bounds="[20,20][40,40]"/></hierarchy>')
        self.assertEqual(candidates(xml), [({"resource_id": "one"}, (10, 10)),
                                           ({"resource_id": "two"}, (30, 30))])

    def test_selectors_reject_ambiguous_disabled_and_wrong_package(self):
        node = f'<node package="{PACKAGE}" enabled="true" text="Capture" bounds="[10,20][30,40]"/>'
        self.assertEqual(select_node(f"<hierarchy>{node}</hierarchy>", {"text": "Capture"}), (20, 30))
        with self.assertRaises(CameraError):
            select_node(f"<hierarchy>{node}{node}</hierarchy>", {"text": "Capture"})
        self.assertIsNone(select_node(f"<hierarchy>{node.replace(PACKAGE, 'other')}</hierarchy>", {"text": "Capture"}))
        self.assertIsNone(select_node(f"<hierarchy>{node.replace('true', 'false')}</hierarchy>", {"text": "Capture"}))
        hidden = node.replace('enabled="true"', 'enabled="true" visible-to-user="false"')
        self.assertIsNone(select_node(f"<hierarchy>{hidden}</hierarchy>", {"text": "Capture"}))

    def test_adb_dump_error_is_not_returned_as_xml(self):
        bridge = AndroidBridge({}, "SN")
        with patch.object(bridge, "shell"), patch.object(bridge, "run", return_value=b"cat: file not found"):
            with self.assertRaisesRegex(CameraError, "uiautomator2"):
                bridge.ui_xml()

    def test_uiautomator2_disables_idle_wait_and_reuses_connection(self):
        device = Mock()
        device.dump_hierarchy.return_value = "<hierarchy/>"
        bridge = AndroidBridge({"ui_backend": "uiautomator2", "serial": "tablet"}, "SN")
        with patch("uiautomator2.connect", return_value=device) as connect:
            self.assertEqual(bridge.ui_xml(), "<hierarchy/>")
            self.assertEqual(bridge.ui_xml(), "<hierarchy/>")
        connect.assert_called_once_with("tablet")
        device.jsonrpc.setConfigurator.assert_called_once_with(
            {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})

    def test_uiautomator2_failure_discards_connection(self):
        device = Mock()
        device.dump_hierarchy.side_effect = RuntimeError("private diagnostics")
        bridge = AndroidBridge({"ui_backend": "uiautomator2", "serial": "tablet"}, "SN")
        with patch("uiautomator2.connect", return_value=device):
            with self.assertRaises(CameraError) as error:
                bridge.ui_xml()
        self.assertNotIn("private diagnostics", str(error.exception))
        self.assertIsNone(bridge._ui_device)

    def test_only_exact_camera_capture_names_and_directory(self):
        text = ("/sdcard/Pictures/xm_baseusipc/SN123.jpg|100|200\n"
                "/sdcard/Pictures/xm_baseusipc/SN_other.jpg|100|200\n"
                "/sdcard/Pictures/xm_baseusipc/SN1preview.jpg|100|200\n"
                "/sdcard/Pictures/other/SN123.jpg|100|200\n")
        self.assertEqual(len(parse_files(text, "/sdcard/Pictures/xm_baseusipc", "SN")), 1)

    def test_existing_and_old_newly_discovered_file_are_never_capture(self):
        before = {"old.jpg": (100, 200)}
        after = {**before, "stale.jpg": (100, 199), "new.jpg": (100, 201), "empty.jpg": (0, 201)}
        self.assertEqual(new_files(before, after, 200), ["new.jpg"])
        self.assertEqual(new_files(before, {"old.jpg": (101, 201)}, 200), ["old.jpg"])

    def test_partial_jpeg_and_screen_png_rejected(self):
        with self.assertRaises(CameraError):
            verify_jpeg(jpeg()[:100])
        png = io.BytesIO()
        Image.new("RGB", (640, 480)).save(png, "PNG")
        with self.assertRaises(CameraError):
            verify_jpeg(png.getvalue())
        self.assertEqual(verify_jpeg(jpeg()), (640, 480))

    def test_success_cold_starts_and_closes_session(self):
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as tmp, patch("baseus_s1.android.time.sleep"):
            path = Path(tmp) / "frame.jpg"
            metadata = bridge.capture(path, before_live=lambda: bridge.events.append("wake"))
            self.assertEqual(path.read_bytes(), bridge.image)
            self.assertEqual(metadata["width"], 640)
        self.assertLess(bridge.events.index("stop"), bridge.events.index("wake"))
        self.assertEqual(bridge.events[-1], "stop")
        self.assertEqual(bridge.events.count("stop"), 2)

    def test_timeout_never_returns_previous_capture(self):
        for bridge in (FakeBridge(produce=False), FakeBridge(image=b"partial jpeg")):
            ticks = iter(range(100))
            with tempfile.TemporaryDirectory() as tmp, patch("baseus_s1.android.time.sleep"), \
                    patch("baseus_s1.android.time.monotonic", side_effect=lambda: next(ticks)):
                path = Path(tmp) / "frame.jpg"
                with self.assertRaises(CameraError):
                    bridge.capture(path)
                self.assertFalse(path.exists())
                self.assertEqual(bridge.events[-1], "stop")

    def test_wake_failure_and_interrupt_close_app(self):
        for error in (CameraError("wake failed"), KeyboardInterrupt()):
            bridge = FakeBridge()
            def wake():
                raise error
            with self.assertRaises(type(error)):
                bridge.capture(Path("not-created.jpg"), before_live=wake)
            self.assertEqual(bridge.events.count("stop"), 2)

    def test_lock_rejects_second_process_and_releases_after_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.lock"
            command = ("from pathlib import Path; from baseus_s1.runner import process_lock; "
                       f"lock=process_lock(Path({str(path)!r})); lock.__enter__()")
            with process_lock(path):
                result = subprocess.run([sys.executable, "-c", command], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            with process_lock(path):
                pass

    def test_scheduler_skips_missed_slots(self):
        # Start=100, first cycle takes 7500s: next slot should be 10900, not immediate.
        with patch("baseus_s1.runner.time.monotonic", side_effect=[100, 100, 7600, 7600, 7600]), \
                patch("baseus_s1.runner.time.sleep") as sleep, \
                patch("baseus_s1.runner.capture_cycle", side_effect=[None, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                run_schedule({"interval_seconds": 3600}, Path("."))
        self.assertEqual(sleep.call_args_list[1].args[0], 3300)


if __name__ == "__main__":
    unittest.main()
