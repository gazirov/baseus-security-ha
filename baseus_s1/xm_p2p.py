"""XM application packets carried by PPCS channels.

This module intentionally does not implement PPCS transport.  The Android SDK
uses the vendor's ``PPCS_ConnectByServer`` implementation for that layer;
the wire format below starts only after it has returned a session handle.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import struct
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .common import CameraError


MAGIC = 0x55AA55AA
LOGIN_COMMAND = 1
OPEN_VIDEO_COMMAND = 2
SIGNAL_HEADER_SIZE = 24


@dataclass(frozen=True)
class SignalHeader:
    command: int
    payload_size: int
    packet_count: int
    packet_number: int
    encrypted: bool


def login_payload(username: str, p2p_password: str, user_id: str = "", *, timestamp_ms: int | None = None,
                  random_digits: str | None = None, video: int = 0, mic: int = 0, resolution: int = 1) -> bytes:
    """Build the JSON sent as XM command 1 after PPCS connects."""
    if not username or not p2p_password:
        raise CameraError("Для XM P2P login нужны имя пользователя и P2P-пароль")
    if random_digits is None:
        random_digits = "".join(str(secrets.randbelow(10)) for _ in range(6))
    if len(random_digits) != 6 or not random_digits.isdecimal():
        raise CameraError("XM P2P random должен состоять из шести цифр")
    if timestamp_ms is None:
        timestamp_ms = time.time_ns() // 1_000_000
    auth = hashlib.md5(f"{username}:{random_digits}:{p2p_password}".encode("utf-8")).hexdigest()
    value = {
        "username": username,
        "timestamp": timestamp_ms,
        "random": random_digits,
        "user_id": user_id,
        "auth": auth,
        "video": video,
        "mic": mic,
        "res": resolution,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def open_video_payload(channel: int, stream_type: int, keepalive: int) -> bytes:
    """Build the JSON used by ``startVideoStream`` (XM command 2)."""
    if channel < 0 or stream_type < 0 or keepalive < 0:
        raise CameraError("Параметры XM video не могут быть отрицательными")
    return json.dumps({"channel": channel, "streamType": stream_type, "sign": keepalive},
                      separators=(",", ":")).encode("utf-8")


def encrypt_signal(payload: bytes, p2p_password: str) -> bytes:
    """AES-128-ECB/PKCS7 used by the SDK when P2P signal encryption is enabled."""
    key = hashlib.md5((p2p_password + "xmitech").encode("utf-8")).digest()
    padder = PKCS7(128).padder()
    padded = padder.update(payload) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def build_signal_packet(command: int, payload: bytes, *, encrypted: bool = False,
                        packet_count: int = 1, packet_number: int = 1) -> bytes:
    """Build the 24-byte non-CAT XM signal frame written to PPCS channel 0."""
    if not 0 <= command <= 0xFFFF or not 1 <= packet_number <= packet_count <= 1024:
        raise CameraError("Некорректные поля заголовка XM signal")
    if len(payload) > 0xFFFF:
        raise CameraError("XM signal payload превышает 65535 байт")
    # The Android SDK writes this packed little-endian structure. Byte 4 is
    # version (1) plus encryption type (0/1); byte 5 is frame model 0xD2.
    header = struct.pack("<IBBHHHH2xQ", MAGIC, 0x10 | int(encrypted), 0xD2,
                         0, packet_count, packet_number, len(payload), 0)
    header = header[:14] + struct.pack("<H", command) + header[16:]
    return header + payload


def parse_signal_header(packet: bytes) -> SignalHeader:
    """Validate and parse an XM 24-byte header without retaining its payload."""
    if len(packet) < SIGNAL_HEADER_SIZE:
        raise CameraError("XM signal короче 24-байтного заголовка")
    magic, flags, _model, _frame, packet_count, packet_number, payload_size, _timestamp = struct.unpack(
        "<IBBHHHH2xQ", packet[:SIGNAL_HEADER_SIZE]
    )
    command = struct.unpack_from("<H", packet, 14)[0]
    if magic != MAGIC or packet_count > 1024 or packet_number > packet_count:
        raise CameraError("Некорректный заголовок XM signal")
    if len(packet) < SIGNAL_HEADER_SIZE + payload_size:
        raise CameraError("XM signal содержит неполный payload")
    return SignalHeader(command, payload_size, packet_count, packet_number, bool(flags & 0x0F))
