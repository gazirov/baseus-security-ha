import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from baseus_s1.cloud import (CloudClient, body_signature, encrypt_password, find_wake_key, login_error_message,
                             json_bytes, require_xm, xm_signature)
from baseus_s1.common import AuthError, CameraError, redact
from baseus_s1.wake import wake_camera, wake_message, wake_reply


class ProtocolTests(unittest.TestCase):
    def test_login_error_shows_server_reason_without_credentials_or_response_data(self):
        value = {"code": 100207, "message": "Account user@example.invalid rejected. password=private-value\nTry again.",
                 "data": {"auth": "token-must-not-leak", "password": "private-value"}}
        message = login_error_message(value, "user@example.invalid", "private-value")
        self.assertIn("rejected", message)
        self.assertIn("Try again", message)
        for private in ("user@example.invalid", "private-value", "token-must-not-leak", "\n"):
            self.assertNotIn(private, message)
        self.assertEqual(login_error_message({"message": "", "msg": "Unknown account"}), "Unknown account")
        self.assertEqual(login_error_message({"msg": {"auth": "secret"}}), "сервер не передал текст ошибки")

    def test_signatures_match_independent_dotnet_vectors(self):
        body = b'{"type":0,"account":"test@example.invalid","password":"ciphertext"}'
        self.assertEqual(body_signature(body, 1700000000123),
                         "ipc#CElYvAkK#be96da25080495d2d59e654f1da1492723f51746")
        self.assertEqual(xm_signature(1700000000123), "f3f8c5c8cba2a8f2d9a37cfaa0209d75")

    def test_password_rsa_roundtrip_and_randomized_padding(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        public = key.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        with patch("baseus_s1.cloud.PUBLIC_KEY", base64.b64encode(public).decode()):
            a, b = encrypt_password("пароль"), encrypt_password("пароль")
        self.assertNotEqual(a, b)
        self.assertEqual(key.decrypt(base64.b64decode(a), padding.PKCS1v15()).decode(), "пароль")

    def test_login_uses_region_and_stores_only_session_not_password(self):
        areas = {"data": [{"shortName": "RU", "server": "EU", "tel": "7"}],
                 "serverList": [{"label": "EU", "servers": {"bsServer": "https://eu.example.invalid"}}]}
        http = Mock()
        http.request.side_effect = [
            SimpleNamespace(status_code=200, json=lambda: areas),
            SimpleNamespace(status_code=200, json=lambda: {"code": 200, "data": {"auth": "private-token"}}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            client = CloudClient({"country": "RU"}, path, http)
            client.login("account", "never-log-this")
            call = http.request.call_args
            self.assertEqual(call.args, ("POST", "https://eu.example.invalid/api/auth/account/login"))
            self.assertEqual(call.kwargs["params"], {"countryCode": "7"})
            wire_body = call.kwargs["data"]
            self.assertNotIn(b"never-log-this", wire_body)
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"]["sign"],
                             body_signature(wire_body, int(call.kwargs["headers"]["timestamp"])))
            state = json.loads(path.read_text())
            self.assertEqual(state["token"], "private-token")
            self.assertNotIn("never-log-this", path.read_text())
            self.assertNotIn("account", state)

    def test_failed_login_does_not_replace_session(self):
        http = Mock()
        http.request.return_value = SimpleNamespace(status_code=403)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            with self.assertRaises(AuthError):
                CloudClient({}, path, http).login("a", "b")
            self.assertFalse(path.exists())

    def test_xm_server_success_is_not_http_success(self):
        with self.assertRaises(AuthError):
            require_xm({"code": 100401, "result": False})
        with self.assertRaises(CameraError):
            require_xm({"code": 123, "result": False, "payload": {"host": "stale"}})

    def test_http_500_with_xm_auth_code_is_auth_error_without_secret_leak(self):
        for code in (401, 100401, "401", "100401"):
            http = Mock()
            http.request.return_value = SimpleNamespace(status_code=500, json=lambda: {
                "code": code, "message": "token=private-secret", "payload": {"auth": "private-secret"},
            })
            with tempfile.TemporaryDirectory() as tmp:
                client = CloudClient({}, Path(tmp) / "session.json", http)
                client.state = {"token": "existing-secret"}
                with self.assertRaises(AuthError) as error:
                    client._request("POST", "https://example.invalid")
                self.assertIn(str(code), str(error.exception))
                self.assertNotIn("secret", str(error.exception))
                self.assertEqual(client.state["token"], "existing-secret")
                self.assertEqual(http.request.call_count, 1)

    def test_unknown_or_nonjson_http_500_stays_generic_server_error(self):
        for value in ({"code": 500, "message": "secret"}, {"code": []}, [], None):
            http = Mock()
            http.request.return_value = SimpleNamespace(status_code=500, json=lambda: value)
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(CameraError) as error:
                    CloudClient({}, Path(tmp) / "session.json", http)._request("POST", "https://example.invalid")
                self.assertNotIsInstance(error.exception, AuthError)
                self.assertNotIn("secret", str(error.exception))
        http.request.return_value.json = Mock(side_effect=ValueError("secret"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CameraError) as error:
                CloudClient({}, Path(tmp) / "session.json", http)._request("POST", "https://example.invalid")
            self.assertNotIsInstance(error.exception, AuthError)

    def test_wake_key_never_taken_from_different_camera(self):
        devices = {"device_list": [
            {"sn": "other", "platform": {"wake_key": "wrong"}},
            {"device_sn": "mine", "platform": {"wake_key": "correct"}},
        ]}
        self.assertEqual(find_wake_key(devices, "mine"), "correct")
        with self.assertRaises(CameraError):
            find_wake_key(devices, "unknown")
        devices["device_list"].append({"sn": "mine", "wake_key": "conflicting"})
        with self.assertRaises(CameraError):
            find_wake_key(devices, "mine")

    def test_wake_key_in_live_device_extend_schema(self):
        value = {"device_list": [
            {"device_sn": "other", "device_extend": {"platform": {"wake_key": "wrong"}}},
            {"device_sn": "mine", "device_extend": {"platform": {"wake_key": "correct"}}},
        ]}
        self.assertEqual(find_wake_key(value, "mine"), "correct")

    def test_secrets_redacted_recursively(self):
        value = {"device_list": [{"platform": {"wake_key": "secret", "p2p_password": "secret"},
                                   "sn": "keep", "product_id": "keep"}], "Authorization": "secret"}
        text = json.dumps(redact(value))
        self.assertNotIn("secret", text)
        self.assertIn("keep", text)

    def test_stale_retained_and_unrelated_wake_responses_ignored(self):
        reply = {"seq": "10", "actionId": "WatchDevice", "result": True}
        self.assertIsNone(wake_reply(reply, "11", False))
        self.assertIsNone(wake_reply(reply, "10", True))
        self.assertIsNone(wake_reply({**reply, "actionId": "Other"}, "10", False))
        self.assertTrue(wake_reply(reply, "10", False))
        self.assertFalse(wake_reply({**reply, "result": False}, "10", False))

    def test_mqtt_waits_for_camera_reply_not_just_puback_and_cleans_up(self):
        settings = dict(host="mqtt.example.invalid", port=8883, client_id="client",
                        username="u", password="secret", publish="out", subscribe="in")
        for send_reply in (True, False):
            client = Mock()
            ok = SimpleNamespace(is_failure=False)
            client.loop_start.side_effect = lambda: client.on_connect(client, None, None, ok, None)

            def subscribe(*args, **kwargs):
                client.on_subscribe(client, None, 1, [ok], None)
                return 0, 1

            def publish(topic, payload, **kwargs):
                value = json.loads(payload)
                self.assertFalse(kwargs["retain"])
                self.assertEqual(value["payload"]["mode"], 1)
                if send_reply:
                    data = json_bytes({"seq": value["seq"], "actionId": "WatchDevice", "result": True})
                    client.on_message(client, None, SimpleNamespace(payload=data, retain=False))
                message = Mock()
                message.is_published.return_value = True
                return message

            client.subscribe.side_effect, client.publish.side_effect = subscribe, publish
            with patch("baseus_s1.wake.mqtt.Client", return_value=client):
                if send_reply:
                    wake_camera(settings, "sn", "product", "key", timeout=0.02)
                else:
                    with self.assertRaisesRegex(CameraError, "WatchDevice"):
                        wake_camera(settings, "sn", "product", "key", timeout=0.02)
            client.disconnect.assert_called_once()
            client.loop_stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
