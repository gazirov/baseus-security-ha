import hashlib
import json
import unittest

from baseus_s1.common import CameraError
from baseus_s1.xm_p2p import (LOGIN_COMMAND, build_signal_packet, encrypt_signal, login_payload,
                              open_video_payload, parse_signal_header)


class XmP2PTests(unittest.TestCase):
    def test_login_payload_matches_android_md5_formula(self):
        payload = login_payload("admin", "p2p-secret", "user-id", timestamp_ms=1700000000123,
                                random_digits="012345")
        value = json.loads(payload)
        self.assertEqual(value["auth"], hashlib.md5(b"admin:012345:p2p-secret").hexdigest())
        self.assertEqual(value["timestamp"], 1700000000123)
        self.assertEqual(value["user_id"], "user-id")
        self.assertEqual(value["res"], 1)

    def test_signal_packet_roundtrip_header(self):
        packet = build_signal_packet(LOGIN_COMMAND, b'{"ok":true}', encrypted=True)
        header = parse_signal_header(packet)
        self.assertEqual(header.command, LOGIN_COMMAND)
        self.assertEqual(header.payload_size, 11)
        self.assertEqual((header.packet_count, header.packet_number), (1, 1))
        self.assertTrue(header.encrypted)
        self.assertEqual(packet[:4], bytes.fromhex("aa55aa55"))

    def test_encrypt_signal_is_deterministic_aes_ecb_with_pkcs7(self):
        self.assertEqual(encrypt_signal(b"test", "password").hex(), "4ee663a2f27c4e5de0021dfb5712a920")

    def test_video_payload_and_invalid_packet_are_checked(self):
        self.assertEqual(json.loads(open_video_payload(0, 1, 2)),
                         {"channel": 0, "streamType": 1, "sign": 2})
        with self.assertRaises(CameraError):
            parse_signal_header(b"short")


if __name__ == "__main__":
    unittest.main()
