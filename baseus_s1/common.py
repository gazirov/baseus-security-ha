from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class CameraError(RuntimeError):
    """An actionable error whose message contains no credentials."""


class AuthError(CameraError):
    pass


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".baseus-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, value: object) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def redact(value):
    """For explicit diagnostic exports; HTTP responses are never logged wholesale."""
    if isinstance(value, dict):
        return {
            key: "<redacted>" if any(word in key.lower() for word in
                ("password", "pwd", "auth", "token", "secret", "wake_key", "signature", "service_string"))
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
