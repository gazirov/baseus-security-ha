"""Bounded ordinary XM frame assembly and SDK-compatible AES decryption."""
from dataclasses import dataclass, field
import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .common import CameraError
from .xm_p2p import MAGIC


@dataclass(frozen=True)
class XmFrame:
    model: int
    encryption: int
    command: int
    video_codec: str
    payload: bytes = field(repr=False)


def decrypt_payload(data: bytes, password: str, encryption: int) -> bytes:
    if encryption == 0:
        return data
    if encryption not in (1, 4) or not data or len(data) % 16:
        raise CameraError("Неподдерживаемое XM encryption или длина ciphertext")
    key = hashlib.md5((password + "xmitech").encode()).digest()
    mode = modes.ECB() if encryption == 1 else modes.CBC(hashlib.md5(b"xmitech").digest())
    try:
        decoder = Cipher(algorithms.AES(key), mode).decryptor()
        padded = decoder.update(data) + decoder.finalize()
        unpad = PKCS7(128).unpadder()
        return unpad.update(padded) + unpad.finalize()
    except ValueError:
        raise CameraError("XM: ошибка расшифровки payload") from None


class XmStream:
    def __init__(self, password: str, *, max_bytes: int = 4_194_304):
        self._password = password
        self._buffer = bytearray()
        self._parts = bytearray()
        self._identity = None
        self._next = 1
        self._max = max_bytes

    def feed(self, data: bytes) -> list[XmFrame]:
        if len(self._buffer) + len(data) > self._max:
            raise CameraError("XM stream превышает лимит буфера")
        self._buffer.extend(data)
        result = []
        while len(self._buffer) >= 24:
            header = self._buffer[:24]
            magic, flags, model, number, count, part, size, command, timestamp = struct.unpack("<IBBHHHHHQ", header)
            # Camera control replies use version 0 (flags=0x01), whereas
            # app commands and observed video frames use version 1. SDK g.java
            # parses both with this ordinary 24-byte layout.
            if magic != MAGIC or flags >> 4 not in (0, 1):
                raise CameraError("Неподдерживаемый XM frame header")
            if not 1 <= part <= count <= 1024:
                # Empty heartbeat is not a video fragment.
                if not (count == part == size == 0 and command == 2001):
                    raise CameraError("Некорректная XM fragmentation")
            if len(self._buffer) < 24 + size:
                break
            payload = bytes(self._buffer[24:24 + size])
            del self._buffer[:24 + size]
            identity = (model, number, count, command, timestamp, flags)
            if part == 1 or count == 0:
                self._identity, self._next = identity, 1
                self._parts.clear()
            if self._identity != identity or (count and part != self._next):
                raise CameraError("XM: потеря фрагмента frame")
            if len(self._parts) + len(payload) > self._max:
                raise CameraError("XM frame превышает лимит")
            self._parts.extend(payload)
            self._next += 1
            if part == count:
                plain = decrypt_payload(bytes(self._parts), self._password, flags & 15) if self._parts else b""
                result.append(XmFrame(model, flags & 15, command,
                                      "hevc" if header[15] & 128 else "h264", plain))
                self._parts.clear()
                self._identity = None
        return result
