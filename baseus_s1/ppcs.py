"""PPCS DRW wire primitives; no discovery, sockets, or credential logging.

Packets are validated at the boundary. ReceiveWindow is for a newly established
session (sequence zero); capture replay may explicitly supply a different start.
Missing packets are never skipped. Window limits bound untrusted peer memory.
"""
from dataclasses import dataclass, field
import math
import struct

from .common import CameraError


def frame(message_type: int, payload: bytes = b"") -> bytes:
    if type(message_type) is not int or not 0 <= message_type <= 255 or len(payload) > 65535:
        raise CameraError("Некорректный PPCS frame")
    return struct.pack(">BBH", 0xF1, message_type, len(payload)) + payload


@dataclass(frozen=True)
class Packet:
    message_type: int
    payload: bytes = field(repr=False)
    trailer: bytes = field(default=b"", repr=False)


def parse_frame(data: bytes) -> Packet:
    if len(data) < 4 or data[0] != 0xF1:
        raise CameraError("Некорректный PPCS header")
    size = struct.unpack_from(">H", data, 2)[0]
    end = 4 + size
    if len(data) < end:
        raise CameraError("Неполный PPCS frame")
    # Camera datagrams can contain a vendor trailer beyond declared length.
    # Keep it separate: it is not DRW data or ACK sequence numbers.
    return Packet(data[1], data[4:end], data[end:])


def _channel(channel: int) -> None:
    if type(channel) is not int or not 0 <= channel < 8:
        raise CameraError("Некорректный PPCS channel")


@dataclass(frozen=True)
class DataPacket:
    channel: int
    sequence: int
    payload: bytes = field(repr=False)


def parse_data(packet: Packet) -> DataPacket:
    data = packet.payload
    if packet.message_type != 0xD0 or len(data) < 4 or data[0] != 0xD1:
        raise CameraError("Некорректный PPCS DRW")
    _channel(data[1])
    return DataPacket(data[1], struct.unpack_from(">H", data, 2)[0], data[4:])


def data_frame(channel: int, sequence: int, payload: bytes) -> bytes:
    _channel(channel)
    if type(sequence) is not int or not 0 <= sequence <= 65535:
        raise CameraError("Некорректный PPCS sequence")
    return frame(0xD0, struct.pack(">BBH", 0xD1, channel, sequence) + payload)


def ack_frame(channel: int, sequences: list[int]) -> bytes:
    _channel(channel)
    if not 1 <= len(sequences) <= 32765 or any(type(s) is not int or not 0 <= s <= 65535 for s in sequences):
        raise CameraError("Некорректный PPCS ACK")
    return frame(0xD1, struct.pack(">BBH", 0xD1, channel, len(sequences))
                 + struct.pack(">" + "H" * len(sequences), *sequences))


def parse_ack(packet: Packet) -> tuple[int, tuple[int, ...]]:
    data = packet.payload
    if packet.message_type != 0xD1 or len(data) < 4 or data[0] != 0xD1:
        raise CameraError("Некорректный PPCS ACK")
    _channel(data[1])
    count = struct.unpack_from(">H", data, 2)[0]
    if count == 0 or len(data) != 4 + 2 * count:
        raise CameraError("Некорректная длина PPCS ACK")
    return data[1], struct.unpack_from(">" + "H" * count, data, 4)


class ReceiveWindow:
    """One channel/direction; caller ACKs duplicates as well as new packets.

    feed returns only contiguous data. It raises rather than dropping data on
    overflow or conflicting retransmission. Neither payloads nor keys are logged.
    """
    def __init__(self, *, start: int = 0, max_packets: int = 2048, max_bytes: int = 2_097_152):
        if type(start) is not int or not 0 <= start <= 65535:
            raise CameraError("Некорректный PPCS start sequence")
        if type(max_packets) is not int or not 1 <= max_packets < 32768 or type(max_bytes) is not int or max_bytes < 1:
            raise CameraError("Некорректный лимит PPCS window")
        self.expected = start
        self.max_packets = max_packets
        self.max_bytes = max_bytes
        self._pending: dict[int, bytes] = {}
        self.buffered_bytes = 0

    def feed(self, sequence: int, payload: bytes) -> bytes:
        if type(sequence) is not int or not 0 <= sequence <= 65535:
            raise CameraError("Некорректный PPCS sequence")
        distance = (sequence - self.expected) & 65535
        if distance >= 32768:
            return b""  # Already delivered; ACK again, but do not deliver twice.
        if sequence in self._pending:
            if self._pending[sequence] != payload:
                raise CameraError("Конфликтующий PPCS retransmission")
            return b""
        if distance >= self.max_packets or self.buffered_bytes + len(payload) > self.max_bytes:
            raise CameraError("Переполнение PPCS receive window")
        self._pending[sequence] = payload
        self.buffered_bytes += len(payload)
        chunks = []
        while self.expected in self._pending:
            chunk = self._pending.pop(self.expected)
            self.buffered_bytes -= len(chunk)
            chunks.append(chunk)
            self.expected = (self.expected + 1) & 65535
        return b"".join(chunks)


@dataclass
class _PendingSend:
    wire: bytes = field(repr=False)
    sent_at: float
    attempts: int = 1


class SendWindow:
    """Bounded reliable sender for one channel; caller owns socket and clock.

    queue atomically reserves sequences and returns datagrams for immediate
    sending. acknowledge accepts only this channel's ACK. retransmit returns
    overdue datagrams; exhaustion raises instead of retrying forever.
    """
    def __init__(self, channel: int, *, max_packets: int = 128):
        _channel(channel)
        if type(max_packets) is not int or not 1 <= max_packets < 32768:
            raise CameraError("Некорректный лимит PPCS send window")
        self.channel = channel
        self.max_packets = max_packets
        self.next_sequence = 0
        self._pending: dict[int, _PendingSend] = {}

    def queue(self, payload: bytes, now: float) -> list[bytes]:
        if not math.isfinite(now):
            raise CameraError("Некорректное время PPCS sender")
        if len(payload) > 1024 * (self.max_packets - len(self._pending)):
            raise CameraError("PPCS send window заполнено; дождитесь ACK")
        chunks = [payload[i:i + 1024] for i in range(0, len(payload), 1024)]
        if len(self._pending) + len(chunks) > self.max_packets:
            raise CameraError("PPCS send window заполнено; дождитесь ACK")
        sequences = [(self.next_sequence + i) & 65535 for i in range(len(chunks))]
        if any(seq in self._pending for seq in sequences):
            raise CameraError("PPCS sequence ещё ожидает ACK")
        wires = [data_frame(self.channel, seq, chunk) for seq, chunk in zip(sequences, chunks)]
        for seq, wire in zip(sequences, wires):
            self._pending[seq] = _PendingSend(wire, now)
        self.next_sequence = (self.next_sequence + len(chunks)) & 65535
        return wires

    def acknowledge(self, packet: Packet) -> int:
        channel, sequences = parse_ack(packet)
        if channel != self.channel:
            raise CameraError("PPCS ACK относится к другому каналу")
        removed = 0
        for seq in sequences:
            if self._pending.pop(seq, None) is not None:
                removed += 1
        return removed

    def retransmit(self, now: float, *, interval: float = 0.3, max_attempts: int = 6) -> list[bytes]:
        if not math.isfinite(now) or not math.isfinite(interval) or interval <= 0 or type(max_attempts) is not int or max_attempts < 1:
            raise CameraError("Некорректная политика PPCS retransmission")
        due = [item for item in self._pending.values() if now - item.sent_at >= interval]
        if any(item.attempts >= max_attempts for item in due):
            raise CameraError("PPCS: исчерпаны попытки ожидания ACK")
        for item in due:
            item.attempts += 1
            item.sent_at = now
        return [item.wire for item in due]
