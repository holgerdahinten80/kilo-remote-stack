import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wake_service as ws


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeProbe:
    def __init__(self, result=False):
        self.result = result
        self.calls = []

    def open(self, host, port, timeout=1.5):
        self.calls.append((host, port))
        return self.result


class FakeWG:
    def __init__(self, interface_up=False, ready=True, up_error=None):
        self._interface_up = interface_up
        self.ready = ready
        self.up_error = up_error
        self.up_calls = 0
        self.down_calls = 0
        self.block = None
        self.entered = threading.Event()

    def interface_up(self):
        return self._interface_up

    def up(self):
        self.up_calls += 1
        if self.block is not None:
            self.entered.set()
            self.block.wait(5)
        if self.up_error:
            raise ws.WgError("up failed")
        self._interface_up = True

    def down(self):
        self.down_calls += 1
        self._interface_up = False

    def wait_ready(self, seconds, interval=0.5):
        return self.ready


class FakeRelay:
    def __init__(self, code=200, error=None):
        self.code = code
        self.error = error
        self.calls = 0

    def wake(self):
        self.calls += 1
        if self.error:
            raise ws.RelayError("relay down")
        return self.code


def make_config(**overrides):
    values = {
        "WAKE_TOKEN": "test-token",
        "RELAY_URL": "http://192.168.10.146/wol.php",
        "TARGET_MAC": "BC:E9:2F:9B:EB:DC",
        "TARGET_IP": "192.168.50.56",
        "TARGET_PORT": "3389",
        "DRY_RUN": "0",
        "COOLDOWN_SECONDS": "60",
        "VPN_WAIT_SECONDS": "1",
        "RELAY_TIMEOUT_SECONDS": "1",
        "PRECHECK_TARGET": "1",
        "LISTEN": "127.0.0.1:0",
    }
    values.update(overrides)
    return ws.Config(values)


def make_controller(cfg=None, wg=None, relay=None, probe=None, clock=None):
    cfg = cfg or make_config()
    wg = wg or FakeWG()
    relay = relay or FakeRelay()
    probe = probe or FakeProbe()
    clock = clock or FakeClock()
    return ws.WakeController(cfg, wg, relay, probe, clock), wg, relay, probe, clock


class ConfigTests(unittest.TestCase):
    def test_missing_value_rejected(self):
        with self.assertRaises(ws.ConfigError):
            ws.Config({"RELAY_URL": "http://x/wol.php"})

    def test_invalid_mac_rejected(self):
        with self.assertRaises(ws.ConfigError):
            make_config(TARGET_MAC="nicht-gueltig")

    def test_invalid_relay_scheme_rejected(self):
        with self.assertRaises(ws.ConfigError):
            make_config(RELAY_URL="ftp://example/wol.php")

    def test_defaults_applied(self):
        cfg = make_config()
        self.assertEqual(cfg.wg_interface, "wg-firma")
        self.assertFalse(cfg.dry_run)

    def test_env_file_parser(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("# comment\nWAKE_TOKEN = abc \nRELAY_URL=http://x/y\n")
            path = handle.name
        try:
            values = ws.parse_env_file(path)
            self.assertEqual(values["WAKE_TOKEN"], "abc")
            self.assertEqual(values["RELAY_URL"], "http://x/y")
        finally:
            os.unlink(path)


class ControllerTests(unittest.TestCase):
    def test_tunnel_started_and_stopped(self):
        controller, wg, relay, probe, _ = make_controller()
        result = controller.handle()
        self.assertEqual(result.status, "wake_sent")
        self.assertEqual(wg.up_calls, 1)
        self.assertEqual(wg.down_calls, 1)
        self.assertEqual(relay.calls, 1)

    def test_existing_tunnel_not_stopped(self):
        wg = FakeWG(interface_up=True)
        controller, wg, relay, _, _ = make_controller(wg=wg)
        result = controller.handle()
        self.assertEqual(result.status, "wake_sent")
        self.assertEqual(wg.up_calls, 0)
        self.assertEqual(wg.down_calls, 0)

    def test_dry_run_skips_relay(self):
        cfg = make_config(DRY_RUN="1")
        controller, wg, relay, _, _ = make_controller(cfg=cfg)
        result = controller.handle()
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(relay.calls, 0)
        self.assertEqual(wg.down_calls, 1)

    def test_already_running_skips_relay(self):
        probe = FakeProbe(result=True)
        controller, wg, relay, _, _ = make_controller(probe=probe)
        result = controller.handle()
        self.assertEqual(result.status, "already_running")
        self.assertEqual(relay.calls, 0)
        self.assertEqual(wg.down_calls, 1)

    def test_cooldown(self):
        controller, _, relay, _, clock = make_controller()
        first = controller.handle()
        second = controller.handle()
        self.assertEqual(first.status, "wake_sent")
        self.assertEqual(second.http_status, 429)
        self.assertEqual(second.retry_after, 61)
        self.assertEqual(relay.calls, 1)
        clock.now += 61
        third = controller.handle()
        self.assertEqual(third.status, "wake_sent")
        self.assertEqual(relay.calls, 2)

    def test_vpn_timeout_cleans_up(self):
        wg = FakeWG(ready=False)
        controller, wg, relay, _, _ = make_controller(wg=wg)
        result = controller.handle()
        self.assertEqual(result.status, "vpn_timeout")
        self.assertEqual(result.http_status, 504)
        self.assertEqual(wg.down_calls, 1)
        self.assertEqual(relay.calls, 0)

    def test_vpn_error(self):
        wg = FakeWG(up_error=True)
        controller, wg, _, _, _ = make_controller(wg=wg)
        result = controller.handle()
        self.assertEqual(result.status, "vpn_failed")
        self.assertEqual(result.http_status, 502)
        self.assertEqual(wg.down_calls, 0)

    def test_relay_error(self):
        relay = FakeRelay(error=True)
        controller, wg, _, _, _ = make_controller(relay=relay)
        result = controller.handle()
        self.assertEqual(result.status, "relay_error")
        self.assertEqual(result.http_status, 502)
        self.assertEqual(wg.down_calls, 1)

    def test_relay_non_2xx(self):
        relay = FakeRelay(code=500)
        controller, _, _, _, _ = make_controller(relay=relay)
        result = controller.handle()
        self.assertEqual(result.status, "relay_error")
        self.assertEqual(result.http_status, 502)

    def test_parallel_request_rejected(self):
        wg = FakeWG()
        controller, wg, _, _, _ = make_controller(wg=wg)
        release = threading.Event()
        wg.block = release

        first = {}

        def run():
            first["result"] = controller.handle()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(wg.entered.wait(3))
        second = controller.handle()
        release.set()
        thread.join(5)
        self.assertEqual(second.status, "busy")
        self.assertEqual(second.http_status, 409)
        self.assertEqual(first["result"].status, "wake_sent")

    def test_cooldown_not_applied_to_busy_request(self):
        clock = FakeClock()
        controller, _, _, _, _ = make_controller(clock=clock)
        self.assertEqual(controller.handle().status, "wake_sent")
        self.assertEqual(controller.handle().status, "cooldown")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.controller, self.wg, self.relay, _, _ = make_controller(cfg=self.cfg)
        self.server = ws.WakeServer(("127.0.0.1", 0), self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, path, method="POST", token="test-token", body=None):
        if token is None:
            request = urllib.request.Request(self.base + path, data=body, method=method)
        else:
            request = urllib.request.Request(
                self.base + path, data=body, method=method,
                headers={"Authorization": "Bearer " + token},
            )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_missing_token(self):
        code, data = self.request("/wake", token=None)
        self.assertEqual(code, 401)
        self.assertEqual(data["status"], "unauthorized")
        self.assertEqual(self.relay.calls, 0)

    def test_wrong_token(self):
        code, _ = self.request("/wake", token="falsch")
        self.assertEqual(code, 401)

    def test_valid_request(self):
        code, data = self.request("/wake")
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "wake_sent")

    def test_get_not_allowed(self):
        code, data = self.request("/wake", method="GET")
        self.assertEqual(code, 405)

    def test_unknown_path(self):
        code, data = self.request("/other")
        self.assertEqual(code, 404)

    def test_query_rejected(self):
        code, _ = self.request("/wake?mac=00:11:22:33:44:55")
        self.assertEqual(code, 404)

    def test_body_rejected(self):
        code, data = self.request("/wake", body=b'{"cmd": "reboot"}')
        self.assertEqual(code, 400)
        self.assertEqual(self.relay.calls, 0)

    def test_cooldown_response(self):
        self.request("/wake")
        code, data = self.request("/wake")
        self.assertEqual(code, 429)
        self.assertEqual(data["status"], "cooldown")

    def test_no_token_in_response(self):
        code, data = self.request("/wake", token="falsch")
        self.assertNotIn("test-token", json.dumps(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
