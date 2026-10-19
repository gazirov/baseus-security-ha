"""Offline PPCS discovery codecs recovered from the vendor ARM64 library.

No sockets or DNS queries. Service strings and DID components are sensitive
and excluded from representations. These codecs do not authenticate a peer.
"""
from dataclasses import dataclass, field
import ipaddress
import re
import struct

from .common import CameraError
from .ppcs import Packet, frame


# libPPCS_API.so: DecodeString references virtual address 0x107c2, 54 bytes.
# This is NOT the padded CRC_SBOX from the third-party Rust reference.
_STRING_TABLE = bytes((
    73, 89, 67, 61, 181, 191, 109, 163, 71, 83, 79, 97,
    101, 227, 113, 233, 103, 127, 2, 3, 11, 173, 179, 137,
    43, 47, 53, 193, 107, 139, 149, 151, 17, 229, 167, 13,
    239, 241, 5, 7, 131, 251, 157, 59, 197, 199, 19, 23,
    29, 31, 37, 41, 211, 223,
))


def decode_service_bytes(value: str) -> bytes:
    """Decode the part before ':'; reject truncation instead of copying C bugs."""
    if not isinstance(value, str) or not 2 <= len(value) <= 2047:
        raise CameraError("Некорректная длина PPCS service string")
    encoded = value.partition(":")[0]
    if len(encoded) % 2 or not encoded or any(not "A" <= c <= "P" for c in encoded):
        raise CameraError("Некорректное кодирование PPCS service string")
    output = bytearray()
    accumulator = 0x39
    for i in range(len(encoded) // 2):
        pair = (ord(encoded[2 * i]) * 16 + ord(encoded[2 * i + 1]) - 0x51) & 255
        byte = accumulator ^ pair ^ _STRING_TABLE[i % 54]
        output.append(byte)
        accumulator ^= byte
    return bytes(output)


@dataclass(frozen=True)
class ServiceConfig:
    hosts: tuple[str, ...] = field(repr=False)
    transport_key: bytes = field(default=b"", repr=False)


def parse_service_string(value: str) -> ServiceConfig:
    """Return three discovery hosts and optional proprietary transport key.

    The vendor string is a comma-delimited list with a trailing comma. There
    is no relay address here: relay/peer candidates arrive during negotiation.
    Nonempty suffixes are retained, but must not be sent as plaintext keys.
    """
    raw = decode_service_bytes(value)
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise CameraError("PPCS service string не содержит ASCII hosts") from None
    hosts = text.split(",")
    if hosts[-1] == "":
        hosts.pop()
    if len(hosts) != 3:
        raise CameraError("PPCS service string: ожидались три discovery hosts")
    for host in hosts:
        if len(host) > 253 or not host or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.split(".")
        ):
            raise CameraError("Некорректный PPCS discovery host")
    suffix = value.partition(":")[2]
    if len(suffix) > 20 or any(not 33 <= ord(c) <= 126 for c in suffix):
        raise CameraError("Неизвестный формат PPCS transport key")
    return ServiceConfig(tuple(hosts), suffix.encode("ascii"))


@dataclass(frozen=True)
class DeviceId:
    prefix: str = field(repr=False)
    number: int = field(repr=False)
    suffix: str = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.prefix, str) or not isinstance(self.suffix, str)
                or not re.fullmatch(r"[A-Za-z]{1,7}", self.prefix)
                or not re.fullmatch(r"[A-Za-z]{1,7}", self.suffix)
                or type(self.number) is not int or not 0 <= self.number <= 0x7fffffff):
            raise CameraError("Некорректный PPCS device ID")

    @classmethod
    def from_string(cls, value: str):
        if not isinstance(value, str) or len(value) > 32:
            raise CameraError("Некорректный PPCS device ID")
        match = re.fullmatch(r"([A-Za-z]{1,7})-?(\d{1,10})-?([A-Za-z]{1,7})", value)
        if not match:
            raise CameraError("Неизвестный формат PPCS device ID")
        return cls(match[1], int(match[2]), match[3])

    def to_bytes(self) -> bytes:
        return (self.prefix.encode("ascii").ljust(8, b"\0")
                + struct.pack(">I", self.number)
                + self.suffix.encode("ascii").ljust(8, b"\0"))


def parse_device_id(data: bytes) -> DeviceId:
    if len(data) != 20:
        raise CameraError("Некорректная длина PPCS device ID")
    try:
        prefix, suffix = data[:8].split(b"\0", 1)[0], data[12:].split(b"\0", 1)[0]
        result = DeviceId(prefix.decode("ascii"), int.from_bytes(data[8:12], "big"), suffix.decode("ascii"))
    except UnicodeError:
        raise CameraError("Некорректный PPCS device ID") from None
    if result.to_bytes() != data:
        raise CameraError("Неканонический PPCS device ID")
    return result


@dataclass(frozen=True)
class Address:
    ip: str
    port: int

    def __post_init__(self):
        if not isinstance(self.ip, str):
            raise CameraError("Некорректный PPCS IPv4 address")
        try:
            ipaddress.IPv4Address(self.ip)
        except (ipaddress.AddressValueError, ValueError):
            raise CameraError("Некорректный PPCS IPv4 address") from None
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise CameraError("Некорректный PPCS port")

    def to_bytes(self) -> bytes:
        # Vendor swaps native sockaddr fields: port and IPv4 octets are reversed.
        return b"\0\x02" + struct.pack("<H", self.port) + ipaddress.IPv4Address(self.ip).packed[::-1] + b"\0" * 8


def parse_address(data: bytes) -> Address:
    if len(data) != 16 or data[:2] != b"\0\x02" or any(data[8:]):
        raise CameraError("Некорректный PPCS sockaddr")
    return Address(str(ipaddress.IPv4Address(data[4:8][::-1])), int.from_bytes(data[2:4], "little"))


def p2p_request(device: DeviceId, local: Address) -> bytes:
    return frame(0x20, device.to_bytes() + local.to_bytes())


def punch_packet(device: DeviceId, *, ready: bool = False) -> bytes:
    return frame(0x42 if ready else 0x41, device.to_bytes())


def discovery_address(packet: Packet) -> Address:
    if packet.message_type not in (0x01, 0x40):
        raise CameraError("Неожиданный PPCS discovery response")
    return parse_address(packet.payload)


def request_ack_status(packet: Packet) -> int:
    """P2PReqAck is one status byte and three zero bytes, not a peer ready.

    Do not infer device authentication or video availability from this status.
    """
    if packet.message_type != 0x21 or len(packet.payload) != 4 or any(packet.payload[1:]):
        raise CameraError("Некорректный PPCS P2PReqAck")
    return packet.payload[0]
