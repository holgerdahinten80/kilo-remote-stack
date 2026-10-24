import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge_service as bs

SUBMIT = "S" * 48
AGENT = "A" * 48


def make_config(tmpdir, **overrides):
    values = {
        "SUBMIT_TOKEN": SUBMIT,
        "AGENT_TOKEN": AGENT,
        "LISTEN": "127.0.0.1:0",
        "DB_PATH": os.path.join(tmpdir, "queue.db"),
        "MAX_INSTRUCTION": "100",
        "MAX_PENDING": "5",
        "LEASE_SECONDS": "120",
        "MAX_ATTEMPTS": "3",
        "HEARTBEAT_SECONDS": "15",
        "SUBMIT_RATE_PER_MIN": "10",
        "IDEMPOTENCY_TTL_DAYS": "7",
        "LOG_DIR": tmpdir,
        "LOG_MAX_BYTES": "2097152",
        "LOG_BACKUPS": "3",
    }
    values.update(overrides)
    return bs.Config(values)


class ConfigTests(unittest.TestCase):
    def test_missing_tokens(self):
        with self.assertRaises(bs.ConfigError):
            bs.Config({"SUBMIT_TOKEN": SUBMIT})

    def test_short_token(self):
        with self.assertRaises(bs.ConfigError):
            bs.Config({"SUBMIT_TOKEN": "kurz", "AGENT_TOKEN": AGENT})

    def test_token_roles(self):
        cfg = bs.Config({"SUBMIT_TOKEN": SUBMIT, "AGENT_TOKEN": AGENT, "LISTEN": "127.0.0.1:0"})
        self.assertEqual(cfg.token_role(SUBMIT), "submit")
        self.assertEqual(cfg.token_role(AGENT), "agent")
        self.assertIsNone(cfg.token_role("falsch"))
        self.assertIsNone(cfg.token_role(""))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = bs.Store(os.path.join(self.tmp.name, "q.db"), 5)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_get(self):
        job_id, created = self.store.add("Testauftrag", "unit")
        self.assertTrue(created)
        job = self.store.job(job_id)
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["instruction"], "Testauftrag")

    def test_idempotency(self):
        first, created = self.store.add("A", "unit", "key-1")
        second, created2 = self.store.add("B", "unit", "key-1")
        self.assertTrue(created)
        self.assertFalse(created2)
        self.assertEqual(first, second)

    def test_queue_full(self):
        for i in range(5):
            self.store.add(f"job {i}", "unit")
        with self.assertRaises(bs.QueueFull):
            self.store.add("zu viel", "unit")

    def test_lease_ack_complete(self):
        job_id, _ = self.store.add("Auftrag", "unit")
        job = self.store.take_next(120, 3)
        self.assertEqual(job["id"], job_id)
        self.assertEqual(self.store.job(job_id)["status"], "leased")
        self.assertTrue(self.store.set_status(job_id, "running", require_status=("leased",)))
        self.assertTrue(self.store.set_status(job_id, "done", result="ok",
                                             require_status=("running",)))
        self.assertEqual(self.store.job(job_id)["status"], "done")

    def test_lease_expiry_redelivers(self):
        job_id, _ = self.store.add("Auftrag", "unit")
        first = self.store.take_next(0, 3)
        time.sleep(0.05)
        second = self.store.take_next(120, 3)
        self.assertEqual(first["id"], job_id)
        self.assertEqual(second["id"], job_id)
        self.assertEqual(second["attempts"], 2)

    def test_max_attempts(self):
        job_id, _ = self.store.add("Auftrag", "unit")
        for _ in range(3):
            self.store.take_next(0, 3)
            time.sleep(0.02)
        self.assertIsNone(self.store.take_next(120, 3))
        self.assertEqual(self.store.job(job_id)["status"], "failed")

    def test_persistence(self):
        job_id, _ = self.store.add("bleibt", "unit")
        reopened = bs.Store(os.path.join(self.tmp.name, "q.db"), 5)
        self.assertIsNotNone(reopened.job(job_id))

    def test_instruction_is_not_interpreted(self):
        payload = "rm -rf /tmp/x; shutdown now"
        job_id, _ = self.store.add(payload, "unit")
        self.assertEqual(self.store.job(job_id)["instruction"], payload)

    def test_running_timeout_marks_failed(self):
        job_id, _ = self.store.add("Auftrag", "unit")
        self.store.take_next(120, 3)
        self.store.set_status(job_id, "running", require_status=("leased",),
                              lease_until=time.time() - 1)
        self.assertIsNone(self.store.take_next(120, 3))
        job = self.store.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Running-Timeout", job["result"])

    def test_ack_extends_lease(self):
        job_id, _ = self.store.add("Auftrag", "unit")
        self.store.take_next(1, 3)
        self.store.set_status(job_id, "running", require_status=("leased",),
                              lease_until=time.time() + 300)
        time.sleep(1.2)
        self.assertIsNone(self.store.take_next(1, 3))
        self.assertEqual(self.store.job(job_id)["status"], "running")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(self.tmp.name)
        self.server = bs.BridgeServer(self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def call(self, path, method="POST", token=SUBMIT, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://{self.host}:{self.port}{path}", data=data, method=method)
        if token:
            request.add_header("Authorization", "Bearer " + token)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}

    def test_missing_token(self):
        code, _ = self.call("/jobs", token=None, body={"instruction": "x"})
        self.assertEqual(code, 401)

    def test_wrong_token(self):
        code, _ = self.call("/jobs", token="X" * 48, body={"instruction": "x"})
        self.assertEqual(code, 401)

    def test_submit_requires_submit_token(self):
        code, _ = self.call("/jobs", token=AGENT, body={"instruction": "x"})
        self.assertEqual(code, 403)

    def test_stream_requires_agent_token(self):
        code, _ = self.call("/jobs/stream", method="GET", token=SUBMIT)
        self.assertEqual(code, 403)

    def test_submit_validation(self):
        self.assertEqual(self.call("/jobs", body={})[0], 400)
        self.assertEqual(self.call("/jobs", body={"instruction": "   "})[0], 400)
        self.assertEqual(self.call("/jobs", body={"instruction": "x" * 101})[0], 400)
        self.assertEqual(self.call("/jobs", body={"instruction": 5})[0], 400)

    def test_submit_accepts_and_returns_id(self):
        code, data = self.call("/jobs", body={"instruction": "Baue Feature X"})
        self.assertEqual(code, 202)
        self.assertEqual(len(data["id"]), 32)

    def test_idempotent_submit(self):
        code, first = self.call("/jobs", body={"instruction": "A"}, headers={"Idempotency-Key": "k1"})
        code2, second = self.call("/jobs", body={"instruction": "B"}, headers={"Idempotency-Key": "k1"})
        self.assertEqual(code, 202)
        self.assertEqual(code2, 200)
        self.assertEqual(first["id"], second["id"])

    def test_rate_limit(self):
        self.server.submit_limiter = bs.RateLimiter(2)
        self.assertEqual(self.call("/jobs", body={"instruction": "1"})[0], 202)
        self.assertEqual(self.call("/jobs", body={"instruction": "2"})[0], 202)
        self.assertEqual(self.call("/jobs", body={"instruction": "3"})[0], 429)

    def test_get_job_hides_instruction(self):
        _, data = self.call("/jobs", body={"instruction": "geheim"})
        code, body = self.call(f"/jobs/{data['id']}", method="GET")
        self.assertEqual(code, 200)
        self.assertNotIn("instruction", body)

    def test_unknown_job(self):
        code, _ = self.call("/jobs/" + "0" * 32, method="GET")
        self.assertEqual(code, 404)

    def test_complete_validation(self):
        _, data = self.call("/jobs", body={"instruction": "x"})
        code, _ = self.call(f"/jobs/{data['id']}/complete", token=AGENT, body={"status": "quatsch"})
        self.assertEqual(code, 400)

    def test_stream_delivers_job_and_ack_cycle(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        conn.request("GET", "/jobs/stream", headers={"Authorization": "Bearer " + AGENT})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type").split(";")[0], "text/event-stream")
        first = response.readline()
        self.assertIn(b"stream open", first)

        code, created = self.call("/jobs", body={"instruction": "Fuehre aus"})
        self.assertEqual(code, 202)

        event = None
        for _ in range(10):
            block = []
            while True:
                line = response.readline().decode()
                if line in ("\r\n", "\n", ""):
                    break
                block.append(line.strip())
            fields = dict(item.split(": ", 1) for item in block if ": " in item)
            if fields.get("event") == "job":
                event = fields
                break
        self.assertIsNotNone(event, "kein job-Event empfangen")
        self.assertEqual(event["event"], "job")
        job = json.loads(event["data"])
        self.assertEqual(job["id"], created["id"])

        code, _ = self.call(f"/jobs/{job['id']}/ack", token=AGENT, body={})
        self.assertEqual(code, 200)
        code, _ = self.call(f"/jobs/{job['id']}/complete", token=AGENT,
                            body={"status": "done", "result": "fertig"})
        self.assertEqual(code, 200)
        _, status = self.call(f"/jobs/{job['id']}", method="GET")
        self.assertEqual(status["status"], "done")
        conn.close()

    def test_ack_wrong_state(self):
        code, _ = self.call("/jobs/" + "1" * 32 + "/ack", token=AGENT, body={})
        self.assertEqual(code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakeWake:
    def __init__(self):
        import http.server
        import threading
        self.calls = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.calls += 1
                body = b'{"status": "wake_sent"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/wake"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class WakeTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.wake = FakeWake()
        self.cfg = make_config(
            self.tmp.name,
            WAKE_URL=self.wake.url,
            WAKE_TOKEN="W" * 48,
            WAKE_AFTER_SECONDS="0.05",
            WAKE_COOLDOWN_SECONDS="0",
        )
        self.server = bs.BridgeServer(self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        time.sleep(0.3)
        self.tmp.cleanup()
        self.wake.stop()

    def submit(self, text="Auftrag"):
        import urllib.request
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/jobs",
            data=json.dumps({"instruction": text}).encode(),
            method="POST",
        )
        request.add_header("Authorization", "Bearer " + SUBMIT)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status

    def test_auto_wake_without_client(self):
        self.assertEqual(self.submit(), 202)
        deadline = time.time() + 5
        while self.wake.calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.wake.calls, 1)

    def test_no_wake_when_client_connected(self):
        import http.client
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request("GET", "/jobs/stream", headers={"Authorization": "Bearer " + AGENT})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        response.readline()
        self.submit("mit Client")
        time.sleep(0.5)
        self.assertEqual(self.wake.calls, 0)
        conn.close()

    def test_wake_cooldown(self):
        self.server.cfg.wake_cooldown = 60
        self.submit("eins")
        deadline = time.time() + 5
        while self.wake.calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.submit("zwei")
        time.sleep(0.5)
        self.assertEqual(self.wake.calls, 1)

    def test_wake_failure_does_not_break_submit(self):
        self.server.cfg.wake_url = "http://127.0.0.1:1/wake"
        self.assertEqual(self.submit("egal"), 202)

    def test_wake_disabled(self):
        self.server.cfg.wake_url = ""
        self.assertEqual(self.submit("ohne wake"), 202)
        time.sleep(0.3)
        self.assertEqual(self.wake.calls, 0)




class LogIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cfg = make_config(self.tmp.name)
        self.server = bs.BridgeServer(self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.tmp.cleanup()

    def post(self, token, payload):
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/logs",
            data=json.dumps(payload).encode(),
            method="POST",
        )
        request.add_header("Authorization", "Bearer " + token)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_agent_token_required(self):
        code, _ = self.post(SUBMIT, {"lines": [{"msg": "x"}]})
        self.assertEqual(code, 403)

    def test_writes_lines(self):
        code, data = self.post(AGENT, {"client": "pc", "lines": [{"msg": "start"}, "rohzeile"]})
        self.assertEqual(code, 200)
        self.assertEqual(data["count"], 2)
        path = os.path.join(self.tmp.name, "agent", "agent.jsonl")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["client"], "pc")
        self.assertEqual(lines[1]["line"], "rohzeile")

    def test_invalid_lines(self):
        self.assertEqual(self.post(AGENT, {"lines": []})[0], 400)
        self.assertEqual(self.post(AGENT, {"lines": "x"})[0], 400)
        self.assertEqual(self.post(AGENT, {})[0], 400)

    def test_rotation(self):
        sink = bs.JsonlSink(os.path.join(self.tmp.name, "rot", "x.jsonl"), max_bytes=100, backups=2)
        for index in range(20):
            sink.write({"n": index, "text": "x" * 20})
        self.assertTrue(os.path.isfile(sink.path))
        self.assertTrue(os.path.isfile(sink.path + ".1"))




class FakeTelegram:
    def __init__(self):
        import http.server
        import threading
        self.messages = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                from urllib.parse import parse_qs
                length = int(self.headers.get("Content-Length", "0"))
                fields = parse_qs(self.rfile.read(length).decode())
                outer.messages.append(fields.get("text", [""])[0])
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tg = FakeTelegram()
        self.cfg = make_config(
            self.tmp.name,
            TELEGRAM_BOT_TOKEN="TOKEN",
            TELEGRAM_CHAT_ID="123",
            TELEGRAM_API_BASE=self.tg.base,
        )
        self.server = bs.BridgeServer(self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address[:2]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        time.sleep(0.2)
        self.tmp.cleanup()
        self.tg.stop()

    def test_notifier_send(self):
        notifier = bs.TelegramNotifier("TOKEN", "123", self.tg.base, 5)
        self.assertTrue(notifier.send("Hallo"))
        self.assertEqual(self.tg.messages[-1], "Hallo")

    def test_completion_notifies(self):
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/jobs",
            data=json.dumps({"instruction": "Test"}).encode(),
            method="POST",
        )
        request.add_header("Authorization", "Bearer " + SUBMIT)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5) as response:
            job_id = json.loads(response.read())["id"]
        self.server.store.set_status(job_id, "running", require_status=("pending", "leased"))
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/jobs/{job_id}/complete",
            data=json.dumps({"status": "done", "result": "fertig"}).encode(),
            method="POST",
        )
        request.add_header("Authorization", "Bearer " + AGENT)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5):
            pass
        deadline = time.time() + 5
        while not self.tg.messages and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.tg.messages)
        self.assertIn(job_id[:8], self.tg.messages[-1])
        self.assertIn("done", self.tg.messages[-1])
        self.assertIn("fertig", self.tg.messages[-1])

    def test_disabled_without_token(self):
        cfg = make_config(self.tmp.name, TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
        self.assertFalse(cfg.telegram_enabled)


