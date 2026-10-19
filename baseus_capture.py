#!/usr/bin/env python3
"""Baseus S1: Python cloud tools + rootless ADB fresh-frame capture."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import math
import os
import sys
import xml.etree.ElementTree as ET
from logging.handlers import RotatingFileHandler
from pathlib import Path

from baseus_s1.android import AndroidBridge
from baseus_s1.cloud import CloudClient
from baseus_s1.common import CameraError, atomic_write, redact, write_json
from baseus_s1.configure import configure_ui
from baseus_s1.runner import capture_cycle, cloud_call, cloud_wake, process_lock, run_schedule


def load_config(path: Path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("camera", "cloud", "android"):
        if not isinstance(config.get(key), dict):
            raise CameraError(f"В конфигурации отсутствует объект {key}")
    for key in ("interval_seconds", "retry_delay_seconds"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise CameraError(f"{key} должен быть положительным конечным числом")
    attempts = config.get("attempts_per_cycle")
    if type(attempts) is not int or not 1 <= attempts <= 3:
        raise CameraError("attempts_per_cycle должен быть целым числом от 1 до 3")
    for key in ("output_directory", "state_directory", "log_directory"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise CameraError(f"Укажите {key}")
    for key in ("command_timeout", "capture_timeout", "stream_wait_seconds"):
        value = config["android"].get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise CameraError(f"android.{key} должен быть положительным числом")
    if config["android"].get("ui_backend", "adb") not in {"adb", "uiautomator2"}:
        raise CameraError("android.ui_backend должен быть adb или uiautomator2")
    for key in ("sn", "product_id"):
        if not isinstance(config["camera"].get(key), str):
            raise CameraError(f"camera.{key} должен быть строкой")
    if not isinstance(config["cloud"].get("wake_before_capture"), bool):
        raise CameraError("cloud.wake_before_capture должен быть true или false")
    return config


def configure_logging(directory: Path, verbose=False):
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "baseus.log", maxBytes=5_000_000,
                                  backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), handler], force=True)
    # urllib3 debug can include URLs; protocol responses and credentials are never logged.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "doctor", "configure-ui", "inspect", "login", "devices", "device", "wake", "once", "run"])
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--label", choices=["home", "live"], default="home", help="Имя UI-диагностики")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if args.config.exists():
                raise CameraError("Файл конфигурации уже существует; он не был перезаписан")
            source = Path(__file__).parent / "config.example.json"
            # Exclusive creation, including in a race with another init process.
            with args.config.open("xb") as stream:
                stream.write(source.read_bytes())
            print(f"Создан {args.config}. Настройка: README.md")
            return 0
        config = load_config(args.config)
        base = args.config.resolve().parent
        configure_logging(base / config["log_directory"], args.verbose)
        state = base / config["state_directory"]
        bridge = AndroidBridge(config["android"], config["camera"]["sn"])
        with process_lock(state / "capture.lock"):
            if args.command == "doctor":
                bridge.check()
                bridge.ui_xml()
                count = len(bridge.files()) if config["camera"]["sn"] else 0
                print(f"ADB и UI dump доступны. Известных JPEG выбранной камеры: {count}.")
                print("Приём P2P в этой версии работает через планшет. Root не требуется.")
            elif args.command == "configure-ui":
                configure_ui(config, args.config)
            elif args.command == "inspect":
                bridge.check()
                directory = base / "diagnostics"
                xml = bridge.ui_xml()
                atomic_write(directory / f"{args.label}.xml", xml.encode("utf-8"))
                atomic_write(directory / f"{args.label}.png", bridge.run("exec-out", "screencap", "-p"))
                # Diagnostic screen image is never returned by capture().
                for node in ET.fromstring(xml).iter("node"):
                    attrs = {k: node.get(k) for k in ("resource-id", "text", "content-desc", "bounds", "enabled")}
                    if any(attrs[k] for k in ("resource-id", "text", "content-desc")):
                        print(json.dumps(attrs, ensure_ascii=False))
                print(f"UI XML и экран сохранены в {directory}; это диагностика, не кадр камеры.")
            elif args.command == "login":
                account = os.environ.get("BASEUS_ACCOUNT") or input("Baseus email: ").strip()
                password = os.environ.get("BASEUS_PASSWORD") or getpass.getpass("Baseus password: ")
                client = CloudClient(config["cloud"], state / "session.json")
                try:
                    client.login(account, password)
                finally:
                    client.close()
            elif args.command == "devices":
                value = cloud_call(config, state, lambda client: client.devices())
                write_json(state / "devices.redacted.json", redact(value))
                print(json.dumps(redact(value), ensure_ascii=False, indent=2))
            elif args.command == "device":
                camera = config["camera"]
                if not camera["sn"] or not camera["product_id"]:
                    raise CameraError("Укажите camera.sn и camera.product_id")
                value = cloud_call(config, state, lambda client: client.device(camera["sn"], camera["product_id"]))
                write_json(state / "device.redacted.json", redact(value))
                print(json.dumps(redact(value), ensure_ascii=False, indent=2))
            elif args.command == "wake":
                cloud_wake(config, state)
            elif args.command == "once":
                print(capture_cycle(config, base))
            elif args.command == "run":
                run_schedule(config, base)
        return 0
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Остановлено пользователем")
        return 130
    except (CameraError, OSError, ValueError, KeyError, TypeError) as exc:
        # Raw network/JSON errors can contain credentials; only curated CameraError text is exposed.
        message = str(exc) if isinstance(exc, CameraError) else f"{type(exc).__name__}: проверьте конфигурацию и доступ к файлам"
        logging.getLogger(__name__).error("%s", message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
