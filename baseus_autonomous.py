#!/usr/bin/env python3
"""Experimental autonomous Baseus client: cloud login, cameras, fresh JPEGs."""
import argparse
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from baseus_s1.autonomous import DirectCapture, battery_metadata, cameras_from_response, capture_all, validate_retry_settings
from baseus_s1.cloud import CloudClient
from baseus_s1.common import AuthError, CameraError, write_json
from baseus_s1.runner import process_lock


def configure_logging(path, verbose=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), handler], force=True)
    # Only our metadata loggers get DEBUG. Never enable third-party HTTP/MQTT
    # wire logging, which could expose credentials or payloads.
    for name in ("baseus_s1.autonomous", "baseus_s1.ppcs_udp"):
        logging.getLogger(name).setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in ("urllib3", "PIL", "paho"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("login", "cameras", "capture-all"))
    parser.add_argument("--country", default="RU")
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--output", type=Path, default=Path("captures/autonomous"))
    parser.add_argument("--timeout", type=float, default=120, help="XM/JPEG deadline after connecting (seconds, max 300)")
    parser.add_argument("--connect-timeout", type=float, default=45, help="UDP handshake deadline (seconds, max 300)")
    parser.add_argument("--bind-address", help="Local IPv4 for PPCS UDP only; cloud HTTPS routing is unchanged")
    parser.add_argument("--interface-index", type=int, help="Windows outgoing interface index for PPCS UDP only")
    parser.add_argument("--skip-frames", type=int, default=0, help="Discard initial decoded frames to let camera OSD appear (0..300; try 10)")
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3, help="Maximum attempts per camera")
    parser.add_argument("--retry-delay", type=float, default=20, help="Pause before another attempt (seconds, 0..300)")
    parser.add_argument("--log-file", type=Path, default=Path("logs/autonomous.log"))
    parser.add_argument("--verbose", action="store_true", help="Protocol metadata only, never payloads/keys")
    parser.add_argument("--wake", action="store_true", help="MQTT wake before each capture")
    args = parser.parse_args(argv)
    client = None
    try:
        backend = DirectCapture(timeout=args.timeout, connect_timeout=args.connect_timeout,
                                bind_address=args.bind_address, interface_index=args.interface_index,
                                skip_frames=args.skip_frames)
        validate_retry_settings(args.attempts, args.retry_delay)
        configure_logging(args.log_file, args.verbose)
        with process_lock(args.state / "capture.lock"):
            client = CloudClient({"country": args.country}, args.state / "session.json")

            def login():
                account, password = os.environ.get("BASEUS_ACCOUNT"), os.environ.get("BASEUS_PASSWORD")
                if not account or not password:
                    if not sys.stdin.isatty():
                        raise AuthError("Задайте BASEUS_ACCOUNT/BASEUS_PASSWORD или выполните login интерактивно")
                    account = account or input("Baseus email: ").strip()
                    password = password or getpass.getpass("Baseus password: ")
                client.login(account, password)

            if args.command == "login":
                print("Внимание: новый login может сбросить другие сессии Baseus. Поведение пока не установлено.", file=sys.stderr)
                login()
                print("Авторизация успешна; сессия сохранена локально.")
                return 0
            if not client.state.get("token"):
                raise AuthError("Нет сохранённой сессии. Новый вход выполняется только явной командой login")

            def operation():
                if args.command == "cameras":
                    return [{"sn": c.sn, "name": c.name, "product_id": c.product_id,
                             "online_status": c.online_status, **battery_metadata(c)} for c in cameras_from_response(client.devices())]
                return capture_all(client, args.output, capture=backend, wake=args.wake,
                                   attempts=args.attempts, retry_delay=args.retry_delay)

            try:
                result = operation()
            except AuthError:
                raise AuthError("Сохранённая авторизация отклонена. Автоматический login отключён; новый вход может сбросить другие сессии") from None
            if args.command == "capture-all":
                write_json(args.output / "last-run.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if args.command == "capture-all" and any(row["status"] == "error" for row in result) else 0
    except KeyboardInterrupt:
        return 130
    except (CameraError, OSError, ValueError, TypeError) as exc:
        message = str(exc) if isinstance(exc, CameraError) else type(exc).__name__
        print(message, file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
