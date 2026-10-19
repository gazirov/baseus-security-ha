"""Experimental cloud-discovered PPCS direct UDP transport.

No recorded endpoints are reused. TCP/UDP relay negotiation is not implemented;
failure to establish a direct peer raises, never masquerades as a connection.
"""
import math
from collections import Counter
import logging
import ipaddress
import socket
import struct
import sys
import time

from .common import CameraError
from .ppcs import ReceiveWindow, SendWindow, ack_frame, frame, parse_ack, parse_data, parse_frame
from .ppcs_discovery import Address, DeviceId, discovery_address, p2p_request, parse_device_id, parse_service_string, punch_packet, request_ack_status

LOG = logging.getLogger(__name__)


class ReceiveTimeout(CameraError):
    """A polling deadline elapsed; the established session remains usable."""


# Baseus libPPCS_API.so proprietary table at 0x10838. The native routine
# masks the lookup index to 8 bits; the unrelated reference uses another table.
_XOR_TABLE = bytes.fromhex(
    "7c9ce84a13dedcb22f2123e4307b3d8cbc0b270c3cf79ae7087196009785efc11fc4"
    "dba1c2ebd901faba3b05b81587832872d18b5ad6da9358feaacc6e1bf0a388ab4"
    "3c00db545384f502266207f075b14981d9ba72ab9a8cbf1fc4947063eb10e043a94"
    "5eee541134dd4df9ecc7c9e3781a6f706ba4bda95dd5f8e5bb26af4237d8e1020a"
    "ae5f1cc573094e6924906d12b319ad748a2940f52dbea559e0f479d24bce8982488"
    "425c6912ba2fb8fe9a6b09e3f65f603312eac0f952c5ced39b7336c567eb4a0fd7"
    "a815351868d9f77ff6a80dfe2bf10d775645776f355cdd0c818e6364162cf99f232"
    "4c67606192cad3ea637d16b68ed46835c3529d46441e17"
)


def protect(data: bytes, key: bytes, *, decrypt: bool = False) -> bytes:
    if not key or not data:
        return data
    if len(key) > 20 or b"\0" in key:
        raise CameraError("Некорректный PPCS transport key")
    total = sum(key) & 255
    xor = 0
    for byte in key:
        xor ^= byte
    state = (total, (-sum(key)) & 255, sum(byte // 3 for byte in key) & 255, xor)
    result = bytearray([data[0] ^ _XOR_TABLE[total]])
    for i in range(1, len(data)):
        previous = data[i - 1] if decrypt else result[-1]
        result.append(data[i] ^ _XOR_TABLE[(previous + state[previous & 3]) & 255])
    return bytes(result)


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 300:
        raise CameraError("PPCS timeout должен быть числом от 0 до 300 секунд")


def validate_source_options(bind_address=None, interface_index=None):
    if bind_address is not None:
        try:
            if not isinstance(bind_address, str):
                raise ValueError
            ip = ipaddress.IPv4Address(bind_address)
            if ip.is_unspecified or ip.is_multicast or str(ip) == "255.255.255.255":
                raise ValueError
        except ValueError:
            raise CameraError("Для P2P bind-address нужен конкретный локальный IPv4") from None
    if interface_index is not None:
        if type(interface_index) is not int or not 1 <= interface_index <= 0xffffffff:
            raise CameraError("P2P interface-index должен быть положительным целым числом")
        if sys.platform != "win32":
            raise CameraError("P2P interface-index поддерживается только на Windows")


def _select_interface(sock, interface_index):
    if interface_index is not None:
        try:
            # Winsock IP_UNICAST_IF: interface index is a network-order DWORD.
            # Per-socket option only; never modifies system routes or VPN state.
            sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_UNICAST_IF", 31), struct.pack("!I", interface_index))
        except OSError:
            raise CameraError("PPCS: не удалось выбрать исходящий интерфейс") from None


class UdpSession:
    """Single-threaded established peer, reliable channels and bounded retries.

    Caller owns the session and must close it. recv takes an absolute monotonic
    deadline so unrelated/malformed datagrams cannot extend the request timeout.
    """
    def __init__(self, sock, peer, key=b"", *, clock=time.monotonic):
        self._socket, self._peer, self._key, self._clock = sock, peer, key, clock
        self._send = [SendWindow(channel) for channel in range(8)]
        self._receive = [ReceiveWindow(max_bytes=262144) for _ in range(8)]
        self._last_alive = clock()
        self._closed = False

    @classmethod
    def connect(cls, did: str, service_string: str, *, timeout: float = 45,
                bind_address=None, interface_index=None):
        _timeout(timeout)
        validate_source_options(bind_address, interface_index)
        device = DeviceId.from_string(did)
        service = parse_service_string(service_string)
        LOG.info("PPCS discovery: DNS start; timeout=%.1fs", timeout)
        # System DNS has its own OS timeout; the UDP deadline starts after DNS.
        try:
            servers = set()
            for host in service.hosts:
                answers = socket.getaddrinfo(host, 32100, socket.AF_INET, socket.SOCK_DGRAM)
                if not answers:
                    raise CameraError("PPCS discovery DNS не вернул IPv4")
                ip = answers[0][4][0]
                servers.update((ip, port) for port in range(32100, 32103))
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            LOG.info("PPCS discovery: DNS ok; server_endpoints=%d", len(servers))
        except OSError:
            raise CameraError("PPCS: ошибка DNS/socket") from None
        try:
            _select_interface(sock, interface_index)
            sock.bind((bind_address or "0.0.0.0", 0))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                _select_interface(route, interface_index)
                if bind_address is not None:
                    route.bind((bind_address, 0))
                route.connect(next(iter(servers)))
                ip = route.getsockname()[0]
            LOG.info("PPCS source selection: explicit_bind=%s explicit_interface=%s", bind_address is not None, interface_index is not None)
            local = Address(ip, sock.getsockname()[1])
            peer = cls._handshake(sock, servers, device, local, service.transport_key, timeout)
            LOG.info("PPCS peer established")
            return cls(sock, peer, service.transport_key)
        except BaseException:
            sock.close()
            raise

    @staticmethod
    def _handshake(sock, servers, device, local, key, timeout, *, clock=time.monotonic):
        deadline, next_send, next_punch, attempts = clock() + timeout, 0, 0, 0
        sock.settimeout(min(0.1, timeout))
        candidates = set()
        announcements = set()
        received, ignored, malformed = 0, 0, 0
        statuses = Counter()
        public_mappings = set()
        punch_rounds = 0
        # Native/recorded client requests discovery about once per second.
        # Punching must not slow down as the user increases the timeout.
        max_rounds = max(1, math.ceil(timeout))
        for server in servers:
            sock.sendto(protect(frame(0), key), server)
        while clock() < deadline:
            now = clock()
            if now >= next_send and attempts < max_rounds:
                for server in servers:
                    sock.sendto(protect(p2p_request(device, local), key), server)
                attempts += 1
                next_send = now + 1.0
                LOG.info("PPCS discovery: round=%d/%d candidates=%d", attempts, max_rounds, len(candidates))
            if candidates and now >= next_punch:
                for candidate in sorted(candidates):
                    sock.sendto(protect(punch_packet(device), key), candidate)
                punch_rounds += 1
                next_punch = now + 0.5
                LOG.debug("PPCS punch round: round=%d candidates=%d", punch_rounds, len(candidates))
            sock.settimeout(max(0.001, min(0.1, deadline - clock())))
            try:
                raw, source = sock.recvfrom(65535)
            except socket.timeout:
                continue
            received += 1
            if source not in servers and source not in candidates:
                ignored += 1
                continue
            try:
                packet = parse_frame(protect(raw, key, decrypt=True))
                LOG.debug("PPCS handshake receive: type=0x%02x bytes=%d", packet.message_type, len(raw))
                if packet.message_type == 0x21 and source in servers:
                    status = request_ack_status(packet)
                    statuses[status] += 1
                    if statuses[status] == 1:
                        LOG.info("PPCS request ACK: status=%d (0x%02x); not peer readiness", status, status)
                elif packet.message_type == 0x01 and source in servers:
                    # Record only mapping count, never its address/port. More
                    # than one distinct mapping is a NAT diagnostic, not proof
                    # that relay is required.
                    address = discovery_address(packet)
                    if len(public_mappings) < 32:
                        public_mappings.add(address)
                    LOG.debug("PPCS hello ACK: distinct_public_mappings=%d", len(public_mappings))
                elif packet.message_type == 0x40 and source in servers:
                    address = discovery_address(packet)
                    if address.ip == "0.0.0.0" or not address.port:
                        continue
                    if address in announcements or len(announcements) >= 6:
                        continue
                    announcements.add(address)
                    for port in range(max(1, address.port - 3), min(65535, address.port + 3) + 1):
                        if len(candidates) < 42:
                            candidates.add((address.ip, port))
                    LOG.info("PPCS candidate announcement: candidates=%d", len(candidates))
                    for candidate in sorted(candidates):
                        sock.sendto(protect(punch_packet(device), key), candidate)
                    punch_rounds += 1
                    next_punch = clock() + 0.5
                elif packet.message_type in (0x41, 0x42) and source in candidates:
                    if parse_device_id(packet.payload) != device:
                        continue
                    if packet.message_type == 0x41:
                        LOG.info("PPCS punch received; waiting for ready")
                        sock.sendto(protect(punch_packet(device), key), source)
                    else:
                        return source
            except CameraError:
                malformed += 1
                continue
        LOG.warning("PPCS discovery timeout: rounds=%d punch_rounds=%d received=%d ignored=%d malformed=%d candidates=%d public_mappings=%d ack_status_counts=%s",
                    attempts, punch_rounds, received, ignored, malformed, len(candidates), len(public_mappings), dict(statuses))
        raise CameraError("PPCS: direct peer не установлен; relay fallback пока не реализован")

    def _write(self, wire):
        if self._closed:
            raise CameraError("PPCS session закрыта")
        try:
            self._socket.sendto(protect(wire, self._key), self._peer)
        except OSError:
            raise CameraError("PPCS: ошибка отправки UDP") from None

    def send(self, channel: int, data: bytes):
        if type(channel) is not int or not 0 <= channel < 8:
            raise CameraError("Некорректный PPCS channel")
        for wire in self._send[channel].queue(data, self._clock()):
            self._write(wire)

    def recv(self, deadline: float) -> tuple[int, bytes]:
        while self._clock() < deadline:
            now = self._clock()
            for window in self._send:
                for wire in window.retransmit(now):
                    LOG.debug("PPCS DRW retransmit: bytes=%d", len(wire))
                    self._write(wire)
            if now - self._last_alive >= 2:
                self._write(frame(0xE0))
                self._last_alive = now
            self._socket.settimeout(max(0.001, min(0.1, deadline - now)))
            try:
                raw, source = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                raise CameraError("PPCS: ошибка приёма UDP") from None
            if source != self._peer:
                continue
            try:
                packet = parse_frame(protect(raw, self._key, decrypt=True))
                if packet.message_type == 0xD1:
                    channel, _ = parse_ack(packet)
                    self._send[channel].acknowledge(packet)
                    LOG.debug("PPCS ACK received: channel=%d", channel)
                elif packet.message_type == 0xD0:
                    data = parse_data(packet)
                    LOG.debug("PPCS DRW received: channel=%d sequence=%d bytes=%d", data.channel, data.sequence, len(data.payload))
                    # Overflow/conflict must terminate, not be silently ignored.
                    chunk = self._receive[data.channel].feed(data.sequence, data.payload)
                    self._write(ack_frame(data.channel, [data.sequence]))
                    if chunk:
                        return data.channel, chunk
                elif packet.message_type == 0xE0:
                    self._write(frame(0xE1))
                elif packet.message_type == 0xF0:
                    raise CameraError("PPCS peer закрыл соединение")
            except CameraError:
                raise
        raise ReceiveTimeout("PPCS: истёк срок ожидания данных")

    def close(self):
        if not self._closed:
            try:
                self._write(frame(0xF0))
            except CameraError:
                pass
            finally:
                self._socket.close()
                self._closed = True
                LOG.info("PPCS session closed")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
