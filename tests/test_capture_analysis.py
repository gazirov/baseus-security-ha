import importlib.util
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest

from baseus_s1.ppcs import ack_frame, data_frame, frame
from baseus_s1.ppcs_discovery import Address, DeviceId
from baseus_s1.ppcs_udp import protect


def udp_packet(source, destination, sport, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                     socket.inet_aton(source), socket.inet_aton(destination))
    return b"\0" * 12 + b"\x08\x00" + ip + udp


def pcapng(packets):
    def block(kind, body):
        body += b"\0" * (-len(body) % 4)
        size = len(body) + 12
        return struct.pack("<II", kind, size) + body + struct.pack("<I", size)
    result = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    result += block(1, struct.pack("<HHI", 1, 0, 65535))
    for i, packet in enumerate(packets):
        timestamp = 1700000000000000 + i * 100000
        result += block(6, struct.pack("<IIIII", 0, timestamp >> 32, timestamp & 0xffffffff,
                                       len(packet), len(packet)) + packet)
    return result


class CaptureAnalysisTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("scapy"), "optional research dependency Scapy")
    def test_report_mode_and_capture_direction_without_payload_leaks(self):
        from analysis.inspect_tablet_capture import inspect
        tablet, remote = "192.168.88.42", "203.0.113.10"
        # Int at offset 20 must not be mistaken for the mode at byte 31.
        report = (DeviceId("TEST", 1, "ABC").to_bytes() + struct.pack(">I", 1) + b"\0" * 7 + b"\x01" + b"\0" * 4
                  + Address(tablet, 1111).to_bytes() + Address("198.51.100.1", 2222).to_bytes()
                  + Address(remote, 3333).to_bytes())
        packets = [udp_packet(tablet, remote, 1111, 3333, ack_frame(1, [0])),
                   udp_packet(tablet, "203.0.113.20", 1111, 32100,
                              frame(0xF9, protect(report, b"SSD@cs2-network.")))]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.pcapng"
            path.write_bytes(pcapng(packets))
            result = inspect(path, tablet)
            self.assertFalse(result["bidirectional_tablet_ipv4"])
            self.assertEqual(result["counts"]["ipv4_out"], 2)
            self.assertEqual(result["validated_report_modes"], [{"mode": 1, "name": "relay", "count": 1}])
            self.assertTrue(result["reports_match_video_ack_peer"])
            text = json.dumps(result)
            for private in (tablet, remote, "198.51.100.1", "TEST", "SSD@cs2-network."):
                self.assertNotIn(private, text)
            packets.append(udp_packet(remote, tablet, 3333, 1111, data_frame(1, 0, b"synthetic-private-payload")))
            path.write_bytes(pcapng(packets))
            result = inspect(path, tablet)
            self.assertTrue(result["bidirectional_tablet_ipv4"])
            self.assertEqual(result["counts"]["ipv4_in"], 1)
            self.assertNotIn("synthetic-private-payload", json.dumps(result))

            direct_report = bytearray(report)
            direct_report[31] = 0
            packets[1] = udp_packet(tablet, "203.0.113.20", 1111, 32100,
                                    frame(0xF9, protect(bytes(direct_report), b"SSD@cs2-network.")))
            path.write_bytes(pcapng(packets))
            result = inspect(path, tablet)
            self.assertEqual(result["validated_report_modes"], [{"mode": 0, "name": "p2p", "count": 1}])

            failed_report = DeviceId("TEST", 1, "ABC").to_bytes() + struct.pack(">i", -24) + b"\0" * 60
            packets[1] = udp_packet(tablet, "203.0.113.20", 1111, 32100,
                                    frame(0xF9, protect(failed_report, b"SSD@cs2-network.")))
            path.write_bytes(pcapng(packets))
            result = inspect(path, tablet)
            self.assertEqual(result["validated_report_modes"], [])
            self.assertEqual(result["connection_failure_reports"], [{"result_code": -24, "count": 1}])
            self.assertNotIn("unsupported_or_malformed_udp_frame", result["counts"])


if __name__ == "__main__":
    unittest.main()
