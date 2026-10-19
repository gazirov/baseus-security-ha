"""Read-only tablet UI diagnostics, including for an unvalidated app version."""
import argparse
import json
import time
from pathlib import Path
from xml.dom import minidom

from baseus_s1.android import AndroidBridge
from baseus_s1.common import atomic_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("label", choices=["home", "live", "info", "settings"])
    parser.add_argument("--tap-text")
    parser.add_argument("--tap-id")
    parser.add_argument("--back", action="store_true")
    parser.add_argument("--swipe-up", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    bridge = AndroidBridge(config["android"], config["camera"]["sn"])
    if args.back:
        bridge.shell("input", "keyevent", "KEYCODE_BACK")
        time.sleep(1)
    if args.swipe_up:
        bridge.shell("input", "swipe", "1000", "2160", "1000", "900", "500")
        time.sleep(2)
    if args.tap_text or args.tap_id:
        selector = {"text": args.tap_text} if args.tap_text else {"resource_id": args.tap_id}
        bridge.tap_selector(selector, time.monotonic() + 20)
        time.sleep(2)
    folder = root / "diagnostics"
    atomic_write(folder / f"tablet-{args.label}.png", bridge.run("exec-out", "screencap", "-p"))
    xml = bridge.ui_xml()
    atomic_write(folder / f"tablet-{args.label}.xml", minidom.parseString(xml).toprettyxml().encode("utf-8"))
    print(folder / f"tablet-{args.label}.xml")


if __name__ == "__main__":
    main()
