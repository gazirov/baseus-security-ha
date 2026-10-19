import unittest

from baseus_s1.common import CameraError
from baseus_s1.ppcs import ReceiveWindow, SendWindow, ack_frame, data_frame, frame, parse_ack, parse_data, parse_frame


class PpcsTests(unittest.TestCase):
    def test_wire_vectors(self):
        self.assertEqual(data_frame(1, 258, b"abc").hex(), "f1d00007d1010102616263")
        self.assertEqual(ack_frame(1, [258, 65535]).hex(), "f1d10008d10100020102ffff")
        self.assertEqual(parse_ack(parse_frame(ack_frame(1, [258]))), (1, (258,)))
        data = parse_data(parse_frame(data_frame(1, 258, b"abc")))
        self.assertEqual((data.channel, data.sequence, data.payload), (1, 258, b"abc"))
        self.assertNotIn("abc", repr(data))

    def test_declared_length_separates_vendor_trailer(self):
        packet = parse_frame(ack_frame(0, [1]) + b"trailer")
        self.assertEqual(parse_ack(packet), (0, (1,)))
        self.assertEqual(packet.trailer, b"trailer")
        self.assertNotIn("trailer", repr(packet))

    def test_invalid_packets(self):
        for wire in (b"", b"xxxx", bytes.fromhex("f1d00008d1000000")):
            with self.assertRaises(CameraError):
                parse_frame(wire)
        with self.assertRaises(CameraError):
            parse_ack(parse_frame(frame(0xD1, bytes.fromhex("d10000020001"))))
        with self.assertRaises(CameraError):
            data_frame(8, 0, b"")

    def test_reorder_duplicates_gaps_and_wrap(self):
        window = ReceiveWindow(start=65534)
        self.assertEqual(window.feed(0, b"c"), b"")
        self.assertEqual(window.feed(65534, b"a"), b"a")
        self.assertEqual(window.feed(65534, b"a"), b"")
        self.assertEqual(window.feed(65535, b"b"), b"bc")
        self.assertEqual(window.expected, 1)
        self.assertEqual(window.buffered_bytes, 0)

    def test_limits_and_conflicting_retransmission(self):
        window = ReceiveWindow(max_packets=4, max_bytes=3)
        window.feed(1, b"abc")
        self.assertEqual(window.feed(1, b"abc"), b"")
        with self.assertRaises(CameraError):
            window.feed(1, b"xyz")
        with self.assertRaises(CameraError):
            window.feed(2, b"x")
        with self.assertRaises(CameraError):
            window.feed(4, b"")

    def test_sender_fragmentation_ack_and_retry_limit(self):
        window = SendWindow(0)
        wires = window.queue(b"a" * 1025, 0)
        self.assertEqual([len(parse_data(parse_frame(w)).payload) for w in wires], [1024, 1])
        self.assertEqual(window.retransmit(0.1), [])
        self.assertEqual(window.retransmit(0.4, max_attempts=2), wires)
        self.assertEqual(window.acknowledge(parse_frame(ack_frame(0, [0, 0, 10]))), 1)
        with self.assertRaises(CameraError):
            window.retransmit(1, max_attempts=2)
        self.assertEqual(window.acknowledge(parse_frame(ack_frame(0, [1]))), 1)
        self.assertEqual(window.retransmit(2), [])

    def test_sender_full_window_is_atomic_and_wrong_channel_rejected(self):
        window = SendWindow(0, max_packets=1)
        with self.assertRaises(CameraError):
            window.queue(b"a" * 1025, 0)
        self.assertEqual(window.next_sequence, 0)
        window.queue(b"a", 0)
        with self.assertRaises(CameraError):
            window.acknowledge(parse_frame(ack_frame(1, [0])))
        self.assertEqual(window.acknowledge(parse_frame(ack_frame(0, [0]))), 1)


if __name__ == "__main__":
    unittest.main()
