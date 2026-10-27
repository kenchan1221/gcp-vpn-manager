import base64
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests
from app import App
from cloud import Cloud, CloudNetworkError, public_ip
from diagnostics import DailyLogs, redact
from settings import Node, Settings, SettingsStore, crypt, normalize_conf, secure_directory
from tunnels import Controller, wireguard_path


def profile():
    # Synthetic test-only key material, not credentials for any deployed peer.
    key = base64.b64encode(bytes(range(32))).decode()
    return (
        f"[Interface]\nPrivateKey = {key}\nAddress = 10.77.0.2/32\nDNS = 1.1.1.1\n\n"
        f"[Peer]\nPublicKey = {key}\nAllowedIPs = 0.0.0.0/0, ::/0\n"
        "Endpoint = 203.0.113.7:51820\nPersistentKeepalive = 25\n"
    )


def node():
    return Node.new("Example", "asia-east1-b", "vpn-example", profile())


def settings(count=1):
    nodes = [Node.new(f"Example {i}", "asia-east1-b", f"vpn-{i}", profile()) for i in range(count)]
    return Settings("example-project", "adc", nodes)


def vm(status="RUNNING", ip="203.0.113.9"):
    return {"status": status, "networkInterfaces": [{"accessConfigs": [{"natIP": ip}]}]}


def response(body, status=200):
    result = Mock()
    result.status_code = status
    result.json.return_value = body
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


class SettingsTests(unittest.TestCase):
    def test_one_server_is_enough(self):
        settings().validate()

    def test_twenty_servers_work(self):
        settings(20).validate()

    def test_invalid_server_counts(self):
        for count in (0, 21):
            with self.assertRaises(ValueError):
                settings(count).validate()

    def test_duplicate_vm_rejected(self):
        config = settings(2)
        config.nodes[1].instance = config.nodes[0].instance
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            config.validate()

    def test_invalid_names_prevent_path_or_command_injection(self):
        for field, value in (("id", "../key"), ("zone", "x;cmd"), ("instance", "../vm")):
            value_node = node()
            setattr(value_node, field, value)
            with self.assertRaises(ValueError):
                value_node.validate()

    def test_public_endpoint_replaced_but_port_retained(self):
        text = normalize_conf(profile().replace(":51820", ":54024"))
        self.assertIn("Endpoint = {IP}:54024", text)
        self.assertNotIn("203.0.113.7", text)
        self.assertEqual(normalize_conf(text), text)

    def test_bom_and_comment_import(self):
        self.assertEqual(normalize_conf("\ufeff# comment\n" + profile()), normalize_conf(profile()))

    def test_reject_hooks_extra_peers_and_duplicates(self):
        for content in (
            profile() + "PostUp = echo dangerous\n",
            profile() + "[Peer]\n",
            profile().replace("DNS = 1.1.1.1", "DNS = 1.1.1.1\nDNS = 8.8.8.8"),
            profile().replace("Endpoint = 203.0.113.7:51820", "Endpoint = 203.0.113.7:65536"),
        ):
            with self.assertRaises(ValueError):
                normalize_conf(content)

    def test_reject_malformed_key_address_and_route(self):
        for content in (
            profile().replace("Address = 10.77.0.2/32", "Address = invalid"),
            profile().replace("AllowedIPs = 0.0.0.0/0, ::/0", "AllowedIPs = invalid"),
            profile().replace(base64.b64encode(bytes(range(32))).decode(), "secret"),
        ):
            with self.assertRaises(ValueError):
                normalize_conf(content)

    def test_adc_does_not_keep_old_key(self):
        config = settings()
        config.service_account = {"secret": "not-real"}
        with self.assertRaises(ValueError):
            config.validate()

    def test_key_cannot_redirect_token_to_untrusted_host(self):
        config = settings()
        config.auth = "service_account"
        config.service_account = {"type": "service_account", "token_uri": "https://untrusted.invalid/token"}
        with self.assertRaisesRegex(ValueError, "official"):
            config.validate()

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_real_dpapi_roundtrip_persists_across_store_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = settings(2)
            SettingsStore(path).save(config)
            raw = (path / "settings.dat").read_bytes()
            self.assertNotIn(b"PrivateKey", raw)
            self.assertNotIn(b"example-project", raw)
            reloaded = SettingsStore(path).load()
            self.assertEqual(reloaded, config)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_wrong_dpapi_blob_is_not_silently_reset(self):
        with self.assertRaises(OSError):
            crypt(b"invalid-blob", decrypt=True)

    def test_missing_settings_triggers_first_run(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(SettingsStore(Path(directory)).load())

    def test_failed_encrypt_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            original = path / "settings.dat"
            original.write_bytes(b"previous")
            store = SettingsStore(path, encode=Mock(side_effect=OSError("encryption failed")))
            with self.assertRaises(OSError):
                store.save(settings())
            self.assertEqual(original.read_bytes(), b"previous")

    def test_version_mismatch_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "settings.dat").write_text('{"version": 99}')
            with self.assertRaisesRegex(ValueError, "version"):
                SettingsStore(path, decode=lambda data: data).load()

    @unittest.skipUnless(os.name == "nt", "Windows ACL")
    def test_private_directory_acl_can_be_created_and_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            secure_directory(path)
            SettingsStore(path).save(settings())
            self.assertEqual(SettingsStore(path).load().project, "example-project")
            result = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                                    errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("(I)", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_service_account_key_is_encrypted_and_can_sign_locally(self):
        import rsa
        from google.oauth2 import service_account
        _, private = rsa.newkeys(1024)
        config = settings()
        config.auth = "service_account"
        config.service_account = {
            "type": "service_account",
            "project_id": "example-project",
            "client_email": "test@example-project.iam.gserviceaccount.com",
            "private_key": private.save_pkcs1().decode(),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory))
            store.save(config)
            raw = store.path.read_bytes()
            self.assertNotIn(b"PRIVATE KEY", raw)
            restored = store.load()
            credentials = service_account.Credentials.from_service_account_info(restored.service_account)
            self.assertTrue(credentials.sign_bytes(b"local-test-only"))


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.config = settings()
        self.session = Mock()
        self.session.request.return_value = response(vm())
        self.factory = Mock(return_value=self.session)
        self.cloud = Cloud(self.config, session_factory=self.factory, sleep=Mock())
        self.node = self.config.nodes[0]

    def test_endpoint_uses_user_project_and_node(self):
        self.cloud.get(self.node)
        url = self.session.request.call_args.args[1]
        self.assertEqual(url, "https://compute.googleapis.com/compute/v1/projects/"
                         "example-project/zones/asia-east1-b/instances/vpn-0")

    def test_transient_read_rebuilds_session(self):
        self.session.request.side_effect = [requests.ConnectionError("reset"), response(vm())]
        self.assertEqual(self.cloud.get(self.node)["status"], "RUNNING")
        self.session.close.assert_called_once()
        self.assertEqual(self.factory.call_count, 2)

    def test_persistent_network_error_is_bounded(self):
        self.session.request.side_effect = requests.ConnectionError("DNS failed")
        with self.assertRaises(CloudNetworkError):
            self.cloud.get(self.node)
        self.assertEqual(self.session.request.call_count, 3)

    def test_permission_error_is_not_retried(self):
        self.session.request.return_value = response({}, 403)
        with self.assertRaises(requests.HTTPError):
            self.cloud.get(self.node)
        self.session.request.assert_called_once()

    def test_certificate_error_is_not_retried(self):
        self.session.request.side_effect = requests.exceptions.SSLError("certificate")
        with self.assertRaises(requests.exceptions.SSLError):
            self.cloud.get(self.node)
        self.session.request.assert_called_once()

    def test_transient_server_error_retried(self):
        first = response({}, 503)
        self.session.request.side_effect = [first, response(vm())]
        self.cloud.get(self.node)
        first.close.assert_called_once()
        self.assertEqual(self.session.request.call_count, 2)

    def test_start_post_is_not_blindly_retried(self):
        self.session.request.side_effect = requests.Timeout("may have reached server")
        with self.assertRaises(CloudNetworkError):
            self.cloud.request("POST", "zones/asia-east1-b/instances/vpn-0/start")
        self.session.request.assert_called_once()

    def test_start_running_vm_does_not_mutate_cloud(self):
        self.assertEqual(self.cloud.start(self.node, Mock()), "203.0.113.9")
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args[0], "GET")

    def test_start_waits_for_operation_and_returns_new_ip(self):
        self.session.request.side_effect = [
            response(vm("TERMINATED", "")),
            response({"name": "op-1", "status": "PENDING"}),
            response({"name": "op-1", "status": "DONE"}),
            response(vm()),
        ]
        self.assertEqual(self.cloud.start(self.node, Mock()), "203.0.113.9")
        self.assertEqual(self.session.request.call_args_list[1].args[0], "POST")
        self.assertIn("requestId", self.session.request.call_args_list[1].kwargs["params"])

    def test_operation_error_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "QUOTA"):
            self.cloud.wait_operation(self.node, {
                "status": "DONE", "error": {"errors": [{"code": "QUOTA", "message": "full"}]},
            }, 9999999999)

    def test_no_external_ip_is_explicit_error(self):
        self.session.request.return_value = response(vm(ip=""))
        with self.assertRaisesRegex(RuntimeError, "external IPv4"):
            self.cloud.start(self.node, Mock())

    def test_stop_waits_and_verifies_terminated(self):
        self.session.request.side_effect = [
            response(vm()), response({"status": "DONE"}), response(vm("TERMINATED", "")),
        ]
        self.cloud.stop(self.node, Mock())
        self.assertEqual(self.session.request.call_count, 3)

    def test_stop_does_not_claim_success_when_running(self):
        self.session.request.side_effect = [response(vm()), response({"status": "DONE"}), response(vm())]
        with self.assertRaisesRegex(RuntimeError, "not yet confirmed"):
            self.cloud.stop(self.node, Mock())

    def test_ip_can_be_on_later_interface(self):
        value = vm()
        value["networkInterfaces"].insert(0, {"accessConfigs": []})
        self.assertEqual(public_ip(value), "203.0.113.9")


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = settings()
        self.node = self.config.nodes[0]
        self.cloud = Mock()
        self.cloud.start.return_value = "203.0.113.9"
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.controller = Controller(self.config, self.cloud, Path(self.temp.name),
                                     run=self.run, sleep=Mock(), probe=Mock())
        self.report = Mock()

    def test_start_only_does_not_touch_local_tunnels(self):
        self.controller.start_only(self.node, self.report)
        self.run.assert_not_called()
        self.cloud.start.assert_called_once()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_connect_removes_old_tunnel_before_cloud_and_preserves_runtime_for_reboot(self):
        steps = Mock()
        self.controller.disconnect = steps.disconnect
        self.cloud.start = steps.start
        steps.start.return_value = "203.0.113.9"
        self.controller.local_tunnels = Mock(return_value={self.node.id: "RUNNING"})
        with patch("tunnels.wireguard_path", return_value=Path("wireguard.exe")):
            self.controller.connect(self.node, self.report)
        self.assertLess([call[0] for call in steps.mock_calls].index("disconnect"),
                        [call[0] for call in steps.mock_calls].index("start"))
        runtime = Path(self.temp.name) / f"{self.node.id}.conf"
        self.assertIn("Endpoint = 203.0.113.9:51820", runtime.read_text())
        self.assertEqual(self.controller.probe.call_count, 2)
        self.cloud.stop.assert_not_called()

    def test_disconnect_never_stops_cloud(self):
        runtime = Path(self.temp.name) / f"{self.node.id}.conf"
        runtime.write_text("generated profile", encoding="utf-8")
        self.controller.local_tunnels = Mock(side_effect=[{self.node.id: "RUNNING"}, {}])
        with patch("tunnels.wireguard_path", return_value=Path("wireguard.exe")):
            self.controller.disconnect(self.report)
        self.cloud.stop.assert_not_called()
        self.assertFalse(runtime.exists())
        self.assertEqual(self.run.call_args.args[0][-2:], ["/uninstalltunnelservice", self.node.id])

    def test_failed_disconnect_keeps_service_profile(self):
        runtime = Path(self.temp.name) / f"{self.node.id}.conf"
        runtime.write_text("generated profile", encoding="utf-8")
        self.controller.local_tunnels = Mock(return_value={self.node.id: "RUNNING"})
        self.run.return_value = subprocess.CompletedProcess([], 5, "", "access denied")
        with patch("tunnels.wireguard_path", return_value=Path("wireguard.exe")):
            with self.assertRaisesRegex(RuntimeError, "access denied"):
                self.controller.disconnect(self.report)
        self.assertTrue(runtime.exists())

    def test_stop_refuses_active_local_tunnel(self):
        self.controller.local_tunnels = Mock(return_value={self.node.id: "STOPPED"})
        with self.assertRaisesRegex(RuntimeError, "Disconnect"):
            self.controller.stop(self.node, self.report)
        self.cloud.stop.assert_not_called()

    def test_only_configured_services_are_queried(self):
        self.run.return_value = subprocess.CompletedProcess([], 1060, "", "")
        self.assertEqual(self.controller.local_tunnels(), {})
        self.assertEqual(self.run.call_args.args[0], ["sc.exe", "query", "WireGuardTunnel$" + self.node.id])

    def test_local_state_error_is_not_disconnected(self):
        self.run.return_value = subprocess.CompletedProcess([], 5, "", "access denied")
        with self.assertRaisesRegex(RuntimeError, "access denied"):
            self.controller.local_tunnels()

    def test_network_recovery_has_deadline(self):
        now = [0]
        self.controller.clock = lambda: now[0]
        self.controller.sleep = lambda delay: now.__setitem__(0, now[0] + delay)
        self.controller.probe.side_effect = OSError("DNS unavailable")
        with self.assertRaises(CloudNetworkError):
            self.controller.wait_network(self.report)
        self.assertEqual(now[0], 30)
        self.assertEqual(self.controller.probe.call_count, 15)

    def test_x86_can_find_native_program_files(self):
        with patch.dict(os.environ, {"ProgramW6432": "native-programs", "ProgramFiles": "x86-programs"}):
            with patch.object(Path, "is_file", return_value=True):
                self.assertEqual(wireguard_path(), Path("native-programs") / "WireGuard" / "wireguard.exe")


class LogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 27, 12, 0)
        self.errors = Mock()
        self.handler = DailyLogs(Path(self.temp.name) / "logs", self.errors,
                                 clock=lambda: self.now.timestamp())
        self.addCleanup(self.handler.close)

    def record(self, message):
        return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)

    def test_exact_calendar_retention_and_scope(self):
        for age in range(10):
            day = (self.now - timedelta(days=age)).date()
            (self.handler.directory / f"vpn-{day}-1.log").touch()
        other = self.handler.directory / "notes.log"
        other.touch()
        self.handler.cleanup()
        for age in range(10):
            day = (self.now - timedelta(days=age)).date()
            self.assertEqual((self.handler.directory / f"vpn-{day}-1.log").exists(), age < 7)
        self.assertTrue(other.exists())

    def test_rollover_and_unicode_persistence(self):
        old = self.handler.path()
        self.now += timedelta(days=7)
        self.handler.handle(self.record("測試日誌"))
        self.assertFalse(old.exists())
        self.assertIn("測試日誌", self.handler.path().read_text(encoding="utf-8"))

    def test_idle_cleanup(self):
        old = self.handler.path()
        self.now += timedelta(days=7)
        self.handler.maintain()
        self.assertFalse(old.exists())

    def test_log_failure_notifies_user(self):
        with patch.object(Path, "open", side_effect=PermissionError("disk failure")):
            self.handler.handle(self.record("test"))
        self.errors.assert_called_once()

    def test_cleanup_failure_does_not_stop_logging(self):
        (self.handler.directory / "vpn-2020-01-01-1.log").touch()
        self.now += timedelta(hours=2)
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            self.handler.handle(self.record("still recording"))
        self.errors.assert_called_once()
        self.assertIn("still recording", self.handler.path().read_text(encoding="utf-8"))

    def test_secrets_redacted(self):
        key = base64.b64encode(bytes(range(32))).decode()
        text = redact(f'PrivateKey = {key}\nAuthorization: Bearer sample-token\n'
                      '{"access_token":"sample-access"}')
        self.assertNotIn(key, text)
        self.assertNotIn("sample-token", text)
        self.assertNotIn("sample-access", text)


class WorkerTests(unittest.TestCase):
    def test_success_followed_by_refresh_failure_is_not_failed_action(self):
        app = App.__new__(App)
        app.busy = False
        app.status = Mock()
        app.controls = Mock()
        app.progress = Mock()
        app.events = queue.Queue()
        app.refresh = Mock(side_effect=RuntimeError("DNS"))
        threads = []
        real_thread = threading.Thread

        def launch(**kwargs):
            value = real_thread(**kwargs)
            threads.append(value)
            return value

        with patch("app.threading.Thread", side_effect=launch):
            app.begin("Connect", lambda: app.report("connected"))
            threads[0].join(timeout=5)
        self.assertFalse(threads[0].is_alive())
        events = []
        while not app.events.empty():
            events.append(app.events.get_nowait())
        self.assertIn(("done", None), events)
        self.assertFalse(any(kind == "error" for kind, _ in events))
        self.assertTrue(any("refresh failed" in str(value) for _, value in events))


if __name__ == "__main__":
    unittest.main()
