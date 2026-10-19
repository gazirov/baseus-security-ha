import unittest

from baseus_s1.common import CameraError
from baseus_s1.ppcs import parse_frame
from baseus_s1.ppcs_discovery import (
    Address, DeviceId, decode_service_bytes, discovery_address,
    p2p_request, parse_address, parse_device_id, parse_service_string, punch_packet, request_ack_status,
)


class DiscoveryTests(unittest.TestCase):
    def test_request_ack_status_is_not_peer_ready(self):
        from baseus_s1.ppcs import frame
        for status in (0, 1, 252, 253, 254, 255):
            self.assertEqual(request_ack_status(parse_frame(frame(0x21, bytes([status, 0, 0, 0])))), status)
        for kind, payload in ((0x21, b""), (0x21, b"\0\1\0\0"), (0x42, b"\0" * 4)):
            with self.assertRaises(CameraError):
                request_ack_status(parse_frame(frame(kind, payload)))

    def test_decoder_short_vectors(self):
        self.assertEqual(decode_service_bytes("AA"), b"p")
        self.assertEqual(decode_service_bytes("AAAA"), b"p\x10")
        self.assertEqual(decode_service_bytes("AA:key"), b"p")

    def test_service_hosts_and_hidden_key(self):
        service = parse_service_string("BBCNFFAHOMMK:synthetic-key")
        self.assertEqual(service.hosts, ("a", "b", "c"))
        self.assertEqual(service.transport_key, b"synthetic-key")
        self.assertNotIn("synthetic", repr(service))
        self.assertEqual(parse_service_string("BBCNFFAHOMMK").transport_key, b"")
        with self.assertRaises(CameraError):
            parse_service_string("BBCNFFAHOMMK:" + "x" * 21)

    def test_invalid_service_strings_never_echo_input(self):
        for value in ("", "A", "AZ", "AА", "A" * 2048, "AA:secret"):
            with self.assertRaises(CameraError) as cm:
                parse_service_string(value)
            self.assertNotIn("secret", str(cm.exception))

    def test_id_formats_and_safe_representation(self):
        device = DeviceId.from_string("TEST-123456-ABC")
        self.assertEqual(device, DeviceId.from_string("TEST123456ABC"))
        wire = bytes.fromhex("54455354000000000001e2404142430000000000")
        self.assertEqual(device.to_bytes(), wire)
        self.assertEqual(parse_device_id(wire), device)
        self.assertNotIn("TEST", repr(device))
        for bad in ("123", "TEST-9999999999-ABC", "TEST-1-ABC/", "TÉST1ABC"):
            with self.assertRaises(CameraError):
                DeviceId.from_string(bad)

    def test_address_endianness(self):
        address = Address("192.168.88.42", 16332)
        wire = bytes.fromhex("0002cc3f2a58a8c00000000000000000")
        self.assertEqual(address.to_bytes(), wire)
        self.assertEqual(parse_address(wire), address)
        self.assertEqual(discovery_address(parse_frame(b"\xf1\x40\x00\x10" + wire)), address)
        with self.assertRaises(CameraError):
            parse_address(wire[:-1])

    def test_request_and_punch(self):
        device = DeviceId("TEST", 1, "ABC")
        address = Address("192.0.2.1", 1234)
        request = parse_frame(p2p_request(device, address))
        self.assertEqual((request.message_type, len(request.payload)), (0x20, 36))
        self.assertEqual(parse_device_id(request.payload[:20]), device)
        self.assertEqual(parse_address(request.payload[20:]), address)
        self.assertEqual(parse_frame(punch_packet(device)).message_type, 0x41)
        self.assertEqual(parse_frame(punch_packet(device, ready=True)).message_type, 0x42)
        with self.assertRaises(CameraError):
            parse_device_id(device.to_bytes()[:-1] + b"x")


if __name__ == "__main__":
    unittest.main()
