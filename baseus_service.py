#!/usr/bin/env python3
"""Docker companion entry point. Login is always an explicit separate command."""
import argparse
import logging
import os
from pathlib import Path

from aiohttp import web

from baseus_s1.service import Companion, Store, create_app, initialize


def configure_logging(level):
    if level not in ("INFO", "DEBUG", "WARNING", "ERROR"):
        raise ValueError("BASEUS_LOG_LEVEL must be INFO, DEBUG, WARNING or ERROR")
    logging.basicConfig(level=logging.INFO if level == "DEBUG" else level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    # DEBUG only for our metadata loggers, never HTTP/MQTT wire logging.
    for name in ("baseus_s1.service", "baseus_s1.autonomous", "baseus_s1.ppcs_udp", "baseus_s1.cloud", "baseus_s1.wake"):
        logging.getLogger(name).setLevel(level)
    for name in ("urllib3", "aiohttp", "paho", "PIL"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "serve"))
    args = parser.parse_args()
    root = Path(os.environ.get("BASEUS_DATA", "/data"))
    initialize(root)
    if args.command == "init":
        print("Local API key initialized in data/api_token; existing key was not replaced.")
        return
    configure_logging(os.environ.get("BASEUS_LOG_LEVEL", "INFO").upper())
    archive = os.environ.get("BASEUS_ARCHIVE")
    companion = Companion(Store(root, Path(archive) if archive else None), country=os.environ.get("BASEUS_COUNTRY", "RU"))
    app = create_app(companion, (root / "api_token").read_text("utf-8").strip())
    web.run_app(app, host="0.0.0.0", port=8099, access_log=None, shutdown_timeout=25)


if __name__ == "__main__":
    main()
