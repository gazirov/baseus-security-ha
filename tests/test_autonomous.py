import io
import json
from pathlib import Path
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from baseus_s1.autonomous import Camera, CaptureFailure, DirectCapture, VideoDecoder, _successful_reply, annexb_codec, battery_metadata, cameras_from_response, capture_all
from baseus_s1.common import CameraError
from baseus_s1.ppcs import ack_frame, data_frame, frame, parse_data, parse_frame
from baseus_s1.ppcs_discovery import Address, DeviceId, punch_packet
from baseus_s1.ppcs_udp import ReceiveTimeout, UdpSession, protect, validate_source_options
from baseus_s1.xm_p2p import build_signal_packet, encrypt_signal
from baseus_s1.xm_stream import XmStream, decrypt_payload


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(output, format="JPEG")
    return output.getvalue()


def row(sn="camera1"):
    return {"device_sn": sn, "product_id": "product1", "device_name": "Camera",
            "online_status": 0, "device_info": {"camera_on": 1},
            "device_extend": {"platform": {"device_did": "TEST-123-ABC",
                "service_string": "synthetic", "p2p_password": "secret"}}}


class FakeSocket:
    def __init__(self, events):
        self.events, self.sent, self.closed = list(events), [], False

    def settimeout(self, _):
        pass

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvfrom(self, _):
        if self.events:
            return self.events.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True


class AutonomousTests(unittest.TestCase):
    def test_battery_metadata_validates_percent_and_labels_cloud_source(self):
        for value in (0, 96, 100, -1, 101, True, "96", None, {"secret": 96}):
            device = row()
            device["device_info"]["battery"] = value
            camera = cameras_from_response({"device_list": [device]})[0]
            valid = type(value) is int and 0 <= value <= 100
            self.assertEqual(battery_metadata(camera), {
                "battery_percent": value if valid else None,
                "battery_source": "cloud_device_info" if valid else None})
        for info in (None, [], {}):
            device["device_info"] = info
            camera = cameras_from_response({"device_list": [device]})[0]
            self.assertIsNone(battery_metadata(camera)["battery_percent"])

    def test_battery_in_success_error_and_disabled_results_without_extra_requests(self):
        from unittest.mock import MagicMock
        devices = [row("ok"), row("error"), row("disabled")]
        for device in devices:
            device["device_info"]["battery"] = 96
        devices[2]["device_info"]["camera_on"] = 0
        client = MagicMock()
        client.devices.return_value = {"device_list": devices}
        def capture(camera):
            if camera.sn == "error":
                raise CameraError("synthetic secret")
            return jpeg()
        with tempfile.TemporaryDirectory() as directory:
            results = capture_all(client, Path(directory), capture=capture, attempts=1)
        self.assertEqual([r["status"] for r in results], ["ok", "error", "disabled"])
        self.assertTrue(all(r["battery_percent"] == 96 for r in results))
        self.assertTrue(all(r["battery_source"] == "cloud_device_info" for r in results))
        self.assertNotIn("secret", json.dumps(results))
        client.devices.assert_called_once_with()
        client.login.assert_not_called()

    def test_skip_frames_counts_only_decoded_images_and_keeps_session_cleanup(self):
        from unittest.mock import MagicMock
        session = MagicMock()
        session.__enter__.return_value = session
        login = build_signal_packet(1, encrypt_signal(b'{"result":"ok"}', "secret"), encrypted=True)
        wire = bytearray(build_signal_packet(0, encrypt_signal(b"video", "secret"), encrypted=True))
        wire[5] = 2
        session.recv.side_effect = [(0, login)] + [(1, bytes(wire))] * 4
        decoder = MagicMock()
        decoder.feed.side_effect = [None, jpeg(), jpeg(), jpeg()]
        camera = cameras_from_response({"device_list": [row()]})[0]
        result = DirectCapture(connect=lambda *a, **k: session, decoder_factory=lambda: decoder, skip_frames=2)(camera)
        self.assertTrue(result.startswith(b"\xff\xd8"))
        self.assertEqual(decoder.feed.call_count, 4)
        session.__exit__.assert_called_once()
        for value in (-1, 301, True, 1.5, "10"):
            with self.assertRaises(CameraError):
                DirectCapture(skip_frames=value)

    def test_cli_cameras_includes_cloud_battery(self):
        import baseus_autonomous
        from unittest.mock import MagicMock
        device = row()
        device["device_info"]["battery"] = 96
        client = MagicMock()
        client.state = {"token": "synthetic-token"}
        client.devices.return_value = {"device_list": [device]}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            baseus_autonomous, "CloudClient", return_value=client
        ), patch.object(baseus_autonomous, "configure_logging"), patch("sys.stdout", output):
            code = baseus_autonomous.main(["cameras", "--state", directory])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["battery_percent"], 96)
        self.assertNotIn("secret", output.getvalue())
        client.login.assert_not_called()

    def test_skip_frames_keeps_original_deadline_and_does_not_return_early_image(self):
        from unittest.mock import MagicMock
        session = MagicMock()
        session.__enter__.return_value = session
        login = build_signal_packet(1, encrypt_signal(b'{"result":"ok"}', "secret"), encrypted=True)
        wire = bytearray(build_signal_packet(0, encrypt_signal(b"video", "secret"), encrypted=True))
        wire[5] = 2
        session.recv.side_effect = lambda _: (0, login) if session.send.call_count == 1 else (1, bytes(wire))
        decoder = MagicMock()
        decoder.feed.return_value = jpeg()
        camera = cameras_from_response({"device_list": [row()]})[0]
        ticks = iter(x / 10 for x in range(1000))
        with patch("baseus_s1.autonomous.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaises(CaptureFailure) as error:
                DirectCapture(connect=lambda *a, **k: session, decoder_factory=lambda: decoder,
                              skip_frames=300, timeout=2)(camera)
        self.assertEqual((error.exception.stage, error.exception.code), ("decode", "timeout"))
        self.assertGreater(decoder.feed.call_count, 0)
        self.assertLess(decoder.feed.call_count, 300)
        session.__exit__.assert_called_once()

    def test_explicit_interface_is_socket_local_and_uses_network_order(self):
        from unittest.mock import MagicMock
        primary, route = MagicMock(), MagicMock()
        primary.getsockname.return_value = ("192.168.88.36", 23000)
        route.__enter__.return_value = route
        route.getsockname.return_value = ("192.168.88.36", 24000)
        answers = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.0.2.10", 32100))]
        with patch("baseus_s1.ppcs_udp.sys.platform", "win32"), patch("baseus_s1.ppcs_udp.socket.getaddrinfo", return_value=answers), patch(
            "baseus_s1.ppcs_udp.socket.socket", side_effect=[primary, route]
        ), patch.object(UdpSession, "_handshake", return_value=("192.0.2.20", 1234)) as handshake:
            session = UdpSession.connect("TEST-1-ABC", "BBCNFFAHOMMK", bind_address="192.168.88.36", interface_index=5)
            for sock in (primary, route):
                sock.setsockopt.assert_called_once_with(socket.IPPROTO_IP, getattr(socket, "IP_UNICAST_IF", 31), b"\0\0\0\x05")
                sock.bind.assert_called_once_with(("192.168.88.36", 0))
            self.assertEqual(handshake.call_args.args[3], Address("192.168.88.36", 23000))
            route.__exit__.assert_called_once()
            session.close()
            primary.close.assert_called_once()

    def test_invalid_source_options_rejected_and_no_silent_fallback(self):
        from unittest.mock import MagicMock
        for address in ("bad-secret", "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.2.1/24", True):
            with self.assertRaises(CameraError) as error:
                validate_source_options(address)
            self.assertNotIn("secret", str(error.exception))
        for index in (0, -1, True, 0x100000000):
            with self.assertRaises(CameraError):
                validate_source_options(interface_index=index)
        with patch("baseus_s1.ppcs_udp.sys.platform", "linux"):
            with self.assertRaises(CameraError):
                validate_source_options(interface_index=5)
        primary = MagicMock()
        primary.setsockopt.side_effect = OSError("secret")
        answers = [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", ("192.0.2.10", 32100))]
        with patch("baseus_s1.ppcs_udp.sys.platform", "win32"), patch("baseus_s1.ppcs_udp.socket.getaddrinfo", return_value=answers), patch(
            "baseus_s1.ppcs_udp.socket.socket", return_value=primary
        ), patch.object(UdpSession, "_handshake") as handshake:
            with self.assertRaises(CameraError) as error:
                UdpSession.connect("TEST-1-ABC", "BBCNFFAHOMMK", interface_index=5)
            self.assertNotIn("secret", str(error.exception))
        handshake.assert_not_called()
        primary.close.assert_called_once()

    def test_cloud_schema_duplicates_and_secret_representations(self):
        cameras = cameras_from_response({"total": 1, "device_list": [row()]})
        self.assertNotIn("secret", repr(cameras))
        for response in ({}, {"total": 2, "device_list": [row()]},
                         {"device_list": [row(), row()]}, {"device_list": [{}]}):
            with self.assertRaises(CameraError):
                cameras_from_response(response)

    def test_batch_isolation_safe_paths_and_no_payload_leaks(self):
        class Client:
            def devices(self):
                return {"total": 3, "device_list": [row("bad"), row("../escape"), row("last")]}

        def capture(camera):
            if camera.sn == "bad":
                raise CameraError("server leaked secret")
            return jpeg()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = capture_all(Client(), root, capture=capture)
            self.assertEqual([r["status"] for r in results], ["error", "ok", "ok"])
            self.assertNotIn("secret", json.dumps(results))
            self.assertEqual(len(list(root.glob("*.jpg"))), 2)
            self.assertEqual(Path(results[1]["image"]).parent, root)

    def test_disabled_camera_not_woken_and_invalid_jpeg_not_saved(self):
        off = row("off")
        off["device_info"]["camera_on"] = 0
        class Client:
            def devices(self):
                return {"device_list": [off, row()]}
        with tempfile.TemporaryDirectory() as directory:
            results = capture_all(Client(), Path(directory), capture=lambda _: b"not jpeg")
            self.assertEqual([r["status"] for r in results], ["disabled", "error"])
            self.assertFalse(list(Path(directory).glob("*.jpg")))

    def test_transport_protection(self):
        plain = bytes(range(256))
        for key in (b"", b"test", b"synthetic-long-key"):
            self.assertEqual(protect(protect(plain, key), key, decrypt=True), plain)
        self.assertEqual(protect(b"", b"test"), b"")

    def test_login_rejection_and_contradictory_success(self):
        for payload in (b'{"ret":1}', b'{"result":true,"code":1}', b'{"result":false,"ret":0}', b'{"unknown":0}', b'[]',
                        b'{"result":"ok","errno":1}', b'{"result":"failed","ret":0}',
                        b'{"result":"ok","errno":"1"}', b'{"result":"ok","errno":"bad"}',
                        b'{"result":null,"ret":0}', b'{"result":{},"ret":0}'):
            with self.assertRaises(CameraError):
                _successful_reply(payload)
        _successful_reply(b'{"ret":0}')
        _successful_reply(b'{"result":true}')
        _successful_reply(b'{"result":"ok"}')
        _successful_reply(b'{"result":"ok","errno":0}')
        _successful_reply(b'{"result":"ok","errno":"0"}')

    def test_handshake_deadline_and_encrypted_messages(self):
        class Clock:
            now = 0
            def __call__(self):
                self.now += 0.01
                return self.now
        server = ("192.0.2.1", 32100)
        sock = FakeSocket([])
        with self.assertRaises(CameraError):
            UdpSession._handshake(sock, {server}, DeviceId("TEST", 1, "ABC"), Address("192.0.2.2", 12), b"key", 8, clock=Clock())
        self.assertLessEqual(len(sock.sent), 20)
        self.assertEqual(parse_frame(protect(sock.sent[0][0], b"key", decrypt=True)).message_type, 0)

    def test_punch_timer_is_independent_of_discovery_and_status_is_logged(self):
        class Clock:
            now = 0
            def __call__(self):
                return self.now
        clock = Clock()
        server = ("192.0.2.1", 32100)
        candidate = Address("192.0.2.2", 20000)
        class TimedSocket(FakeSocket):
            def sendto(self, data, address):
                self.sent.append((data, address, clock.now))
            def recvfrom(self, size):
                clock.now += 0.1
                return super().recvfrom(size)
        sock = TimedSocket([(frame(0x21, b"\xfe\0\0\0"), server),
                            (frame(0x01, Address("198.51.100.1", 12000).to_bytes()), server),
                            (frame(0x40, candidate.to_bytes()), server)])
        with self.assertLogs("baseus_s1.ppcs_udp", level="DEBUG") as logs:
            with self.assertRaises(CameraError):
                UdpSession._handshake(sock, {server}, DeviceId("TEST", 1, "ABC"), Address("192.0.2.5", 10000), b"", 2.2, clock=clock)
        hello = [time for data, _, time in sock.sent if parse_frame(data).message_type == 0]
        requests = [time for data, _, time in sock.sent if parse_frame(data).message_type == 0x20]
        punches = [time for data, address, time in sock.sent if parse_frame(data).message_type == 0x41 and address == (candidate.ip, candidate.port)]
        self.assertEqual(len(hello), 1)
        self.assertEqual(len(requests), 3)
        self.assertGreaterEqual(len(punches), 4)
        self.assertTrue(all(b - a <= 0.61 for a, b in zip(punches, punches[1:])))
        text = "\n".join(logs.output)
        self.assertIn("status=254", text)
        self.assertIn("public_mappings=1", text)
        self.assertNotIn(candidate.ip, text)
        self.assertNotIn("198.51.100.1", text)

    def test_direct_backend_denied_login_closes_session(self):
        class Session:
            closed = False
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.closed = True
            def send(self, *_):
                pass
            def recv(self, _):
                return 0, build_signal_packet(1, encrypt_signal(b'{"ret":1}', "secret"), encrypted=True)
        session = Session()
        camera = cameras_from_response({"device_list": [row()]})[0]
        with self.assertRaises(CameraError):
            DirectCapture(connect=lambda *a, **k: session, decoder_factory=lambda: None)(camera)
        self.assertTrue(session.closed)

    def test_cli_login_uses_environment_and_closes_client(self):
        import baseus_autonomous
        class Client:
            state, closed = {}, False
            def login(self, account, password):
                self.received = (account, password)
            def close(self):
                self.closed = True
        client = Client()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(baseus_autonomous, "CloudClient", return_value=client), patch.object(baseus_autonomous, "configure_logging"), patch.dict(
                "os.environ", {"BASEUS_ACCOUNT": "synthetic-account", "BASEUS_PASSWORD": "synthetic-password"}
            ), patch("sys.stdout", output):
                code = baseus_autonomous.main(["login", "--state", directory])
        self.assertEqual(code, 0)
        self.assertEqual(client.received, ("synthetic-account", "synthetic-password"))
        self.assertTrue(client.closed)
        self.assertNotIn("synthetic", output.getvalue())

    def test_cli_never_implicitly_logs_in(self):
        import baseus_autonomous
        from baseus_s1.common import AuthError
        from unittest.mock import MagicMock
        for token in (None, "synthetic-token"):
            client = MagicMock()
            client.state = {"token": token}
            client.devices.side_effect = AuthError("rejected")
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(baseus_autonomous, "CloudClient", return_value=client), patch.object(
                    baseus_autonomous, "configure_logging"
                ), patch("sys.stderr", io.StringIO()):
                    code = baseus_autonomous.main(["cameras", "--state", directory])
            self.assertEqual(code, 1)
            client.login.assert_not_called()
            client.close.assert_called_once()

    def test_capture_logging_and_failure_metadata_do_not_echo_secrets(self):
        camera = cameras_from_response({"device_list": [row()]})[0]
        def fail(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 60)
            raise CameraError("untrusted secret token payload")
        with self.assertLogs("baseus_s1.autonomous", level="DEBUG") as logs:
            with self.assertRaises(CaptureFailure) as error:
                DirectCapture(connect=fail, timeout=180, connect_timeout=60, decoder_factory=lambda: None)(camera)
        self.assertEqual(error.exception.stage, "connect")
        for secret in ("untrusted", "secret", "token", "payload"):
            self.assertNotIn(secret, str(logs.output) + str(error.exception))
        class Client:
            def devices(self):
                return {"device_list": [row()]}
        with tempfile.TemporaryDirectory() as directory:
            results = capture_all(Client(), Path(directory), capture=lambda _: (_ for _ in ()).throw(CaptureFailure("video", "timeout")), attempts=1)
        self.assertEqual(results[0]["failure_stage"], "video")
        self.assertEqual(results[0]["error_code"], "timeout")

    def test_retries_rewake_and_pause_then_stop_on_success(self):
        from unittest.mock import MagicMock
        device = row()
        device["device_extend"]["platform"]["wake_key"] = "synthetic-wake-key"
        client = MagicMock()
        client.devices.return_value = {"device_list": [device]}
        client.mqtt_config.return_value = {}
        events = []
        count = 0
        def backend(camera):
            nonlocal count
            events.append("capture")
            count += 1
            if count < 3:
                raise CaptureFailure("connect", "timeout")
            return jpeg()
        with tempfile.TemporaryDirectory() as directory, patch(
            "baseus_s1.autonomous.wake_camera", side_effect=lambda *a, **k: events.append("wake")
        ) as wake_mock, patch("baseus_s1.autonomous.time.sleep", side_effect=lambda delay: events.append(("pause", delay))):
            results = capture_all(client, Path(directory), capture=backend, wake=True)
            self.assertEqual(len(list(Path(directory).glob("*.jpg"))), 1)
        self.assertEqual(events, ["wake", "capture", ("pause", 20), "wake", "capture", ("pause", 20), "wake", "capture"])
        self.assertEqual(wake_mock.call_count, 3)
        client.login.assert_not_called()
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["attempts"], 3)
        self.assertNotIn("error", results[0])
        self.assertNotIn("synthetic-wake-key", json.dumps(results))

    def test_exhausted_retries_continue_next_camera_without_wake(self):
        class Client:
            def devices(self):
                return {"device_list": [row("bad"), row("good")]}
        calls = []
        def backend(camera):
            calls.append(camera.sn)
            if camera.sn == "bad":
                raise CaptureFailure("connect", "transport_error")
            return jpeg()
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep") as sleep_mock, patch("baseus_s1.autonomous.wake_camera") as wake_mock:
            results = capture_all(Client(), Path(directory), capture=backend, attempts=2, retry_delay=5)
        self.assertEqual(calls, ["bad", "bad", "good"])
        sleep_mock.assert_called_once_with(5)
        wake_mock.assert_not_called()
        self.assertEqual([r["attempts"] for r in results], [2, 1])
        self.assertEqual([r["status"] for r in results], ["error", "ok"])

    def test_nontransient_failures_auth_and_save_are_never_retried(self):
        from baseus_s1.common import AuthError
        from unittest.mock import MagicMock
        client = MagicMock()
        client.devices.return_value = {"device_list": [row()]}
        for error in (CaptureFailure("xm_login", "login_rejected"), CaptureFailure("decode", "invalid_frame"), CameraError("secret failure")):
            backend = MagicMock(side_effect=error)
            with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep") as sleep_mock:
                results = capture_all(client, Path(directory), capture=backend)
            backend.assert_called_once()
            sleep_mock.assert_not_called()
            self.assertEqual(results[0]["attempts"], 1)
        backend = MagicMock(side_effect=AuthError("denied"))
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep") as sleep_mock:
            with self.assertRaises(AuthError):
                capture_all(client, Path(directory), capture=backend)
        backend.assert_called_once()
        sleep_mock.assert_not_called()
        backend = MagicMock(return_value=jpeg())
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.atomic_write", side_effect=OSError("disk")), patch("baseus_s1.autonomous.time.sleep") as sleep_mock:
            results = capture_all(client, Path(directory), capture=backend)
        backend.assert_called_once()
        sleep_mock.assert_not_called()
        self.assertEqual(results[0]["error_stage"], "save")

    def test_retry_settings_rejected_before_cloud_call(self):
        from unittest.mock import MagicMock
        for attempts, delay in ((0, 20), (4, 20), (True, 20), (2, -1), (2, float("nan")), (2, float("inf")), (2, 301), (2, True)):
            client = MagicMock()
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(CameraError):
                    capture_all(client, Path(directory), attempts=attempts, retry_delay=delay)
            client.devices.assert_not_called()

    def test_each_capture_retry_uses_and_closes_a_new_session(self):
        from unittest.mock import MagicMock
        sessions = [MagicMock() for _ in range(3)]
        for session in sessions:
            session.__enter__.return_value = session
            session.recv.side_effect = OSError("synthetic network failure")
        connect = MagicMock(side_effect=sessions)
        backend = DirectCapture(connect=connect, decoder_factory=lambda: None)
        client = MagicMock()
        client.devices.return_value = {"device_list": [row()]}
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep"):
            results = capture_all(client, Path(directory), capture=backend)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(results[0]["attempts"], 3)
        for session in sessions:
            session.__exit__.assert_called_once()

    def test_transient_wake_timeout_retried_but_rejection_is_not(self):
        from unittest.mock import MagicMock
        device = row()
        device["device_extend"]["platform"]["wake_key"] = "synthetic-wake-key"
        client = MagicMock()
        client.devices.return_value = {"device_list": [device]}
        backend = MagicMock(return_value=jpeg())
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep") as pause, patch(
            "baseus_s1.autonomous.wake_camera", side_effect=[CameraError("Нет коррелированного ответа WatchDevice; пробуждение не подтверждено"), None]
        ) as wake_mock:
            results = capture_all(client, Path(directory), capture=backend, wake=True)
        self.assertEqual(wake_mock.call_count, 2)
        pause.assert_called_once_with(20)
        backend.assert_called_once()
        self.assertEqual(results[0]["attempts"], 2)
        backend.reset_mock()
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep") as pause, patch(
            "baseus_s1.autonomous.wake_camera", side_effect=CameraError("WatchDevice: сервер отклонил пробуждение")
        ) as wake_mock:
            results = capture_all(client, Path(directory), capture=backend, wake=True)
        self.assertEqual(wake_mock.call_count, 1)
        pause.assert_not_called()
        backend.assert_not_called()
        self.assertEqual(results[0]["error_stage"], "wake")

    def test_curated_udp_errors_are_retryable_and_cancel_stops_retries(self):
        from unittest.mock import MagicMock
        for message in ("PPCS: ошибка DNS/socket", "PPCS: ошибка отправки UDP", "PPCS: ошибка приёма UDP", "PPCS peer закрыл соединение"):
            backend = DirectCapture(connect=MagicMock(side_effect=CameraError(message)), decoder_factory=lambda: None)
            camera = cameras_from_response({"device_list": [row()]})[0]
            with self.assertRaises(CaptureFailure) as error:
                backend(camera)
            self.assertEqual(error.exception.code, "transport_error")
        client = MagicMock()
        client.devices.return_value = {"device_list": [row()]}
        capture = MagicMock(side_effect=CaptureFailure("connect", "timeout"))
        with tempfile.TemporaryDirectory() as directory, patch("baseus_s1.autonomous.time.sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                capture_all(client, Path(directory), capture=capture)
        capture.assert_called_once()

    def test_xm_heartbeat_runs_while_peer_is_silent_and_deadline_is_preserved(self):
        class Clock:
            now = 0
            def __call__(self):
                self.now += 0.5
                return self.now
        class Session:
            def __init__(self):
                self.sent, self.deadlines = [], []
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def send(self, _, data):
                self.sent.append(data)
            def recv(self, deadline):
                self.deadlines.append(deadline)
                raise ReceiveTimeout("poll timeout")
        clock, session = Clock(), Session()
        camera = cameras_from_response({"device_list": [row()]})[0]
        with patch("baseus_s1.autonomous.time.monotonic", clock):
            with self.assertRaises(CaptureFailure) as error:
                DirectCapture(connect=lambda *a, **k: session, timeout=10, decoder_factory=lambda: None)(camera)
        self.assertEqual(error.exception.code, "timeout")
        self.assertEqual(error.exception.stage, "xm_login")
        self.assertGreater(len(session.sent), 1)
        self.assertEqual(int.from_bytes(session.sent[1][14:16], "little"), 2001)
        self.assertTrue(all(deadline <= 12 for deadline in session.deadlines))

    def test_session_timeout_is_bounded_and_socket_closed(self):
        class Clock:
            now = 0
            def __call__(self):
                self.now += 0.05
                return self.now
        sock = FakeSocket([])
        with UdpSession(sock, ("192.0.2.1", 1234), clock=Clock()) as session:
            with self.assertRaises(CameraError):
                session.recv(1)
        self.assertTrue(sock.closed)

    def test_handshake_ignores_unadvertised_peer_and_wrong_did(self):
        device = DeviceId("TEST", 123, "ABC")
        server, peer, attacker = ("192.0.2.1", 32100), ("192.0.2.2", 20000), ("192.0.2.3", 20000)
        sock = FakeSocket([(punch_packet(device, ready=True), attacker),
                           (frame(0x40, Address(*peer).to_bytes()), server),
                           (punch_packet(DeviceId("TEST", 124, "ABC"), ready=True), peer),
                           (punch_packet(device), peer),
                           (punch_packet(device, ready=True), peer)])
        result = UdpSession._handshake(sock, {server}, device, Address("192.0.2.5", 1234), b"", 1)
        self.assertEqual(result, peer)
        self.assertTrue(all(addr != attacker for _, addr in sock.sent))

    def test_session_reorder_ack_foreign_peer_and_close(self):
        peer = ("192.0.2.1", 1234)
        sock = FakeSocket([(data_frame(1, 0, b"bad"), ("192.0.2.2", 1234)),
                           (data_frame(1, 1, b"b"), peer), (data_frame(1, 0, b"a"), peer)])
        with UdpSession(sock, peer) as session:
            self.assertEqual(session.recv(time.monotonic() + 1), (1, b"ab"))
        self.assertTrue(sock.closed)
        self.assertEqual([parse_frame(x).message_type for x, _ in sock.sent], [0xD1, 0xD1, 0xF0])

    def test_real_local_udp_session(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.bind(("127.0.0.1", 0))
            with UdpSession(client, server.getsockname()) as session:
                session.send(0, b"request")
                server.settimeout(1)
                raw, address = server.recvfrom(65535)
                self.assertEqual(parse_data(parse_frame(raw)).payload, b"request")
                server.sendto(ack_frame(0, [0]), address)
                server.sendto(data_frame(1, 0, b"answer"), address)
                self.assertEqual(session.recv(time.monotonic() + 1), (1, b"answer"))

    def test_xm_stream_partial_fragmented_decryption(self):
        payload = encrypt_signal(b"some video bytes", "secret")
        first = build_signal_packet(2, payload[:16], encrypted=True, packet_count=2, packet_number=1)
        second = build_signal_packet(2, payload[16:], encrypted=True, packet_count=2, packet_number=2)
        stream = XmStream("secret")
        self.assertEqual(stream.feed(first[:10]), [])
        self.assertEqual(stream.feed(first[10:]), [])
        self.assertEqual(stream.feed(second)[0].payload, b"some video bytes")
        with self.assertRaises(CameraError):
            decrypt_payload(payload, "wrong", 1)

    def test_xm_bad_header_and_gap_rejected(self):
        with self.assertRaises(CameraError):
            XmStream("secret").feed(b"x" * 24)
        with self.assertRaises(CameraError):
            XmStream("secret").feed(build_signal_packet(1, b"x", packet_count=2, packet_number=2))

    def test_camera_version_zero_control_reply_is_supported(self):
        wire = bytearray(build_signal_packet(1, encrypt_signal(b'{"ret":0}', "secret"), encrypted=True))
        wire[4], wire[5] = 0x01, 0
        stream = XmStream("secret")
        packet = stream.feed(bytes(wire))[0]
        self.assertEqual((packet.model, packet.command, packet.encryption, packet.payload), (0, 1, 1, b'{"ret":0}'))
        wire[4] = 0x21
        with self.assertRaises(CameraError):
            XmStream("secret").feed(bytes(wire))

    def test_direct_backend_login_video_and_session_cleanup(self):
        class Session:
            sent, closed = [], False
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.closed = True
            def send(self, channel, data):
                self.sent.append((channel, data))
            def recv(self, _):
                if len(self.sent) == 1:
                    wire = bytearray(build_signal_packet(1, encrypt_signal(b'{"result":"ok"}', "secret"), encrypted=True))
                    wire[4], wire[5] = 0x01, 0
                    return 0, bytes(wire)
                wire = bytearray(build_signal_packet(0, encrypt_signal(b"video", "secret"), encrypted=True))
                wire[5] = 2
                wire[14] = 0x10
                return 1, bytes(wire)
        class Decoder:
            def feed(self, codec, data):
                self.assert_data = (codec, data)
                return jpeg()
        session = Session()
        camera = cameras_from_response({"device_list": [row()]})[0]
        result = DirectCapture(connect=lambda *a, **k: session, decoder_factory=Decoder)(camera)
        self.assertTrue(result.startswith(b"\xff\xd8"))
        self.assertTrue(session.closed)
        commands = [XmStream("secret").feed(w)[0] for _, w in session.sent]
        login = json.loads(commands[0].payload)
        video = json.loads(commands[1].payload)
        self.assertEqual(login["username"], camera.sn)
        self.assertEqual(video["camera_sn"], camera.sn)
        self.assertIn("videoKeepAlive_2", video)

    def test_pyav_real_h264_to_jpeg(self):
        try:
            import av
        except ImportError:
            self.skipTest("optional PyAV not installed")
        encoder = av.CodecContext.create("libx264", "w")
        encoder.width = encoder.height = 32
        encoder.pix_fmt = "yuv420p"
        encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
        frame = av.VideoFrame.from_image(Image.new("RGB", (32, 32), "green"))
        encoded = b"".join(bytes(p) for p in encoder.encode(frame))
        encoded += b"".join(bytes(p) for p in encoder.encode(None))
        decoder = VideoDecoder()
        image = decoder.feed("h264", encoded + encoded)
        self.assertIsNotNone(image)
        self.assertTrue(image.startswith(b"\xff\xd8"))

    def test_annexb_detection_uses_parameter_sets_not_header_hint(self):
        self.assertEqual(annexb_codec(b"\0\0\0\1\x40\x01\x01\0\0\1\x42\x01"), "hevc")
        self.assertEqual(annexb_codec(b"\0\0\1\x67\x64\0\0\1\x68\x01"), "h264")
        self.assertIsNone(annexb_codec(b"\0\0\1\x02\x01"))
        self.assertIsNone(annexb_codec(b"\0\0\1"))
        with self.assertRaises(CameraError):
            annexb_codec(b"\0\0\1\x67\x64\0\0\1\x40\x01")

    def test_hevc_parameter_sets_override_incorrect_h264_hint(self):
        from unittest.mock import MagicMock
        decoder = VideoDecoder.__new__(VideoDecoder)
        decoder._codec, decoder._context, decoder._bytes = None, None, 0
        decoder._av = MagicMock()
        # No parse/decode output needed to check codec choice at the boundary.
        decoder._av.CodecContext.create.return_value.parse.return_value = []
        decoder.feed("h264", b"\0\0\0\1\x40\x01\x01")
        decoder._av.CodecContext.create.assert_called_once_with("hevc", "r")
        self.assertEqual(decoder._codec, "hevc")

    def test_pyav_real_hevc_to_jpeg_with_wrong_header_hint(self):
        try:
            import av
        except ImportError:
            self.skipTest("optional PyAV not installed")
        if "libx265" not in av.codecs_available:
            self.skipTest("optional HEVC encoder not installed")
        encoder = av.CodecContext.create("libx265", "w")
        encoder.width = encoder.height = 64
        encoder.pix_fmt = "yuv420p"
        encoder.thread_count = 1
        encoder.options = {"preset": "ultrafast", "tune": "zerolatency",
                           "x265-params": "log-level=error:pools=none:frame-threads=1"}
        picture = av.VideoFrame.from_image(Image.new("RGB", (64, 64), "green"))
        encoded = b"".join(bytes(p) for p in encoder.encode(picture))
        encoded += b"".join(bytes(p) for p in encoder.encode(None))
        decoder = VideoDecoder()
        image = decoder.feed("h264", encoded + encoded)
        self.assertEqual(decoder._codec, "hevc")
        self.assertIsNotNone(image)
        self.assertTrue(image.startswith(b"\xff\xd8"))


if __name__ == "__main__":
    unittest.main()
