import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kilo_agent as ka


class FakeClient:
    def __init__(self):
        self.calls = []

    def ack(self, job_id):
        self.calls.append(("ack", job_id))
        return 200, {}

    def complete(self, job_id, status, result):
        self.calls.append(("complete", job_id, status, result))
        return 200, {}


class FailingClient(FakeClient):
    def ack(self, job_id):
        raise OSError("netz weg")


class FakeRunner:
    def __init__(self, code=0, summary="ok"):
        self.code = code
        self.summary = summary
        self.instructions = []

    def run(self, instruction):
        self.instructions.append(instruction)
        return self.code, self.summary


def make_cfg(tmpdir, **overrides):
    values = {
        "BASE_URL": "https://example.invalid",
        "AGENT_TOKEN": "T" * 48,
        "WORKSPACE": tmpdir,
        "KILO_EXE": "kilo.exe",
        "WEB_URL": "http://127.0.0.1:8765",
        "STATE_DIR": tmpdir,
    }
    values.update(overrides)
    return ka.AgentConfig(values)


def make_agent(tmpdir, code=0, summary="ok", **cfg_overrides):
    cfg = make_cfg(tmpdir, **cfg_overrides)
    client = FakeClient()
    runner = FakeRunner(code, summary)
    ledger = ka.Ledger(os.path.join(tmpdir, "executed.json"))
    return ka.Agent(cfg, client, runner, ledger), client, runner


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_values(self):
        with self.assertRaises(ka.ConfigError):
            ka.AgentConfig({"BASE_URL": "https://x"})

    def test_short_token(self):
        with self.assertRaises(ka.ConfigError):
            make_cfg(self.tmp.name, AGENT_TOKEN="kurz")

    def test_defaults(self):
        cfg = make_cfg(self.tmp.name)
        self.assertEqual(cfg.job_timeout, 1800)
        self.assertFalse(cfg.dry_run)


class ParseTests(unittest.TestCase):
    def test_job_event(self):
        lines = [b": stream open\n", b"\n", b"id: abc\n", b"event: job\n",
                 b'data: {"id":"abc"}\n', b"\n"]
        events = list(ka.parse_sse(lines))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "job")
        self.assertEqual(json.loads(events[0]["data"]), {"id": "abc"})

    def test_heartbeat_ignored(self):
        self.assertEqual(list(ka.parse_sse([b": ping\n", b"\n"])), [])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_flow(self):
        agent, client, runner = make_agent(self.tmp.name)
        job = {"id": "a" * 32, "instruction": "Baue X"}
        self.assertTrue(agent.handle_job(job))
        self.assertEqual(runner.instructions, ["Baue X"])
        self.assertEqual(client.calls[0], ("ack", "a" * 32))
        self.assertEqual(client.calls[1][2], "done")

    def test_failure_flow(self):
        agent, client, _ = make_agent(self.tmp.name, code=1, summary="Fehler")
        agent.handle_job({"id": "b" * 32, "instruction": "x"})
        self.assertEqual(client.calls[1][2], "failed")
        self.assertEqual(client.calls[1][3], "Fehler")

    def test_timeout_maps_to_failed(self):
        agent, client, _ = make_agent(self.tmp.name, code=124, summary="Timeout")
        agent.handle_job({"id": "c" * 32, "instruction": "x"})
        self.assertEqual(client.calls[1][2], "failed")

    def test_dedupe_by_ledger(self):
        agent, client, runner = make_agent(self.tmp.name)
        job = {"id": "d" * 32, "instruction": "einmal"}
        agent.handle_job(job)
        agent.handle_job(job)
        self.assertEqual(len(runner.instructions), 1)
        self.assertEqual(client.calls[-1][2], "done")
        self.assertIn("Ledger", client.calls[-1][3])

    def test_invalid_instruction_rejected(self):
        agent, client, runner = make_agent(self.tmp.name)
        agent.handle_job({"id": "e" * 32, "instruction": "x" * (ka.MAX_INSTRUCTION + 1)})
        self.assertEqual(runner.instructions, [])
        self.assertEqual(client.calls[-1][2], "failed")

    def test_missing_fields_rejected(self):
        agent, client, runner = make_agent(self.tmp.name)
        agent.handle_job({"id": "zu-kurz", "instruction": "x"})
        self.assertEqual(runner.instructions, [])

    def test_dry_run_skips_runner(self):
        agent, client, runner = make_agent(self.tmp.name, DRY_RUN="1")
        agent.handle_job({"id": "f" * 32, "instruction": "x"})
        self.assertEqual(runner.instructions, [])
        self.assertEqual(client.calls[1][2], "done")
        self.assertIn("Dry-Run", client.calls[1][3])

    def test_ack_failure_does_not_crash(self):
        cfg = make_cfg(self.tmp.name)
        ledger = ka.Ledger(os.path.join(self.tmp.name, "executed.json"))
        agent = ka.Agent(cfg, FailingClient(), FakeRunner(), ledger)
        self.assertTrue(agent.handle_job({"id": "1" * 32, "instruction": "x"}))


class RunnerTests(unittest.TestCase):
    def test_command_shape(self):
        runner = ka.KiloRunner("kilo.exe", "C:\\ws", 60, model="p/m")
        cmd = runner.command("Baue X")
        self.assertEqual(cmd[0], "kilo.exe")
        self.assertIn("run", cmd)
        self.assertIn("--auto", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--dir", cmd)
        self.assertIn("C:\\ws", cmd)
        self.assertEqual(cmd[-1], "Baue X")

    def test_command_without_model(self):
        runner = ka.KiloRunner("kilo.exe", "C:\\ws", 60)
        self.assertNotIn("--model", runner.command("x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

class FakeWebApp:
    def __init__(self, final_status="Fertig", answer="Antwort", delay=0.3):
        import http.server, threading
        self.payloads = []
        self.state = {"busy": False, "status": "Bereit", "project": "", "events": [], "session_id": ""}
        self.final_status = final_status
        self.answer = answer
        self.delay = delay
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/state":
                    self._send(200, outer.state)
                else:
                    self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                outer.payloads.append(payload)
                outer.state["busy"] = True
                outer.state["status"] = "Kilo Code arbeitet"
                threading.Thread(target=outer.finish, daemon=True).start()
                self._send(202, outer.state)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def finish(self):
        import time
        time.sleep(self.delay)
        self.state["events"].append({"id": 1, "kind": "markdown", "text": self.answer})
        self.state["busy"] = False
        self.state["status"] = self.final_status

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class WebRunnerTests(unittest.TestCase):
    def test_success(self):
        app = FakeWebApp()
        try:
            runner = ka.KiloWebRunner(f"http://127.0.0.1:{app.port}", "C:\\ws", 10, True, poll=0.05)
            code, summary = runner.run("Baue X")
            self.assertEqual(code, 0)
            self.assertIn("Antwort", summary)
            self.assertEqual(app.payloads[0]["prompt"], "Baue X")
            self.assertEqual(app.payloads[0]["project"], "C:\\ws")
            self.assertTrue(app.payloads[0]["auto_approve"])
        finally:
            app.stop()

    def test_failure_status(self):
        app = FakeWebApp(final_status="Fehlercode 2", answer="Fehler")
        try:
            runner = ka.KiloWebRunner(f"http://127.0.0.1:{app.port}", "C:\\ws", 10, True, poll=0.05)
            code, _ = runner.run("x")
            self.assertEqual(code, 2)
        finally:
            app.stop()

    def test_unreachable(self):
        runner = ka.KiloWebRunner("http://127.0.0.1:1", "C:\\ws", 2, True, poll=0.05)
        code, summary = runner.run("x")
        self.assertEqual(code, 1)
        self.assertIn("nicht erreichbar", summary)


