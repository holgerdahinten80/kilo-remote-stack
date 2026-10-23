#!/usr/bin/env python3
"""Kilo-Agent fuer den Firmen-PC.

Holt Auftraege per SSE von der Bridge, uebergibt sie an die Kilo-CLI und
quittiert Annahme und Ergebnis. Fuehrt selbst nur den fest konfigurierten
Kilo-Befehl aus - keine Shell, keine freien Parameter aus dem Netzwerk.
"""

import argparse
import json
import logging
import os
import random
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("kilo-agent")

MAX_INSTRUCTION = 8192
IS_WINDOWS = os.name == "nt"


class ConfigError(Exception):
    pass


class AgentConfig:
    def __init__(self, values):
        self.runner_mode = str(values.get("RUNNER", "web")).strip().lower()
        required = ["BASE_URL", "AGENT_TOKEN", "WORKSPACE"]
        if self.runner_mode == "web":
            required.append("WEB_URL")
        else:
            required.append("KILO_EXE")
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise ConfigError("fehlende Werte: " + ", ".join(missing))
        if self.runner_mode not in ("web", "cli"):
            raise ConfigError("RUNNER muss web oder cli sein")
        self.base_url = values["BASE_URL"].rstrip("/")
        self.token = values["AGENT_TOKEN"]
        if len(self.token) < 32:
            raise ConfigError("AGENT_TOKEN zu kurz")
        self.workspace = values["WORKSPACE"]
        self.kilo_exe = values.get("KILO_EXE")
        self.web_url = (values.get("WEB_URL") or "").rstrip("/")
        self.web_project = values.get("WEB_PROJECT") or self.workspace
        self.web_auto_approve = str(values.get("WEB_AUTO_APPROVE", "1")).lower() in ("1", "true", "yes")
        self.model = values.get("MODEL") or None
        self.job_timeout = int(values.get("JOB_TIMEOUT_SECONDS", "1800"))
        self.connect_timeout = int(values.get("CONNECT_TIMEOUT_SECONDS", "30"))
        self.state_dir = values.get("STATE_DIR") or os.path.join(
            os.path.expanduser("~"), ".kilo-agent"
        )
        self.dry_run = str(values.get("DRY_RUN", "0")).lower() in ("1", "true", "yes")
        self.backoff_max = int(values.get("BACKOFF_MAX_SECONDS", "60"))

    @classmethod
    def from_file(cls, path):
        with open(path, encoding="utf-8-sig") as handle:
            return cls(json.load(handle))


class Ledger:
    def __init__(self, path):
        self.path = path
        self.entries = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    self.entries = json.load(handle)
            except (OSError, json.JSONDecodeError):
                self.entries = {}

    def seen(self, job_id):
        return job_id in self.entries

    def record(self, job_id, exit_code):
        self.entries[job_id] = {"ts": time.time(), "exit": exit_code}
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.entries, handle)
        os.replace(tmp, self.path)


class KiloRunner:
    def __init__(self, exe, workspace, timeout, model=None):
        self.exe = exe
        self.workspace = workspace
        self.timeout = timeout
        self.model = model

    def command(self, instruction):
        cmd = [self.exe, "run", "--auto", "--format", "json", "--dir", self.workspace]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append(instruction)
        return cmd

    def run(self, instruction):
        cmd = self.command(instruction)
        kwargs = {
            "cwd": self.workspace,
            "capture_output": True,
            "text": True,
            "timeout": self.timeout,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000
        try:
            result = subprocess.run(cmd, **kwargs)
        except subprocess.TimeoutExpired:
            return 124, "Timeout: Kilo-Lauf abgebrochen"
        except OSError as exc:
            return 1, f"Kilo-Start fehlgeschlagen: {type(exc).__name__}"
        tail = (result.stdout or "").strip().splitlines()[-1:] or [""]
        summary = tail[0][:400]
        if result.returncode != 0 and not summary:
            summary = (result.stderr or "").strip().splitlines()[-1:] or [""]
            summary = summary[0][:400]
        return result.returncode, summary


class KiloWebRunner:
    def __init__(self, base_url, project, timeout, auto_approve=True, poll=1.0):
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.timeout = timeout
        self.auto_approve = auto_approve
        self.poll = poll

    def _state(self):
        with urllib.request.urlopen(self.base_url + "/api/state", timeout=10) as response:
            return json.loads(response.read())

    def _start(self, prompt):
        payload = json.dumps({
            "prompt": prompt,
            "project": self.project,
            "auto_approve": self.auto_approve,
        }).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/api/run", data=payload, method="POST")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def run(self, instruction):
        deadline = time.monotonic() + self.timeout
        try:
            state = self._state()
        except (OSError, ValueError):
            return 1, "KiloCodeWeb nicht erreichbar (127.0.0.1:8765)"
        while state.get("busy"):
            if time.monotonic() > deadline:
                return 124, "KiloCodeWeb dauerhaft beschaeftigt"
            time.sleep(self.poll)
            state = self._state()

        before = len(state.get("events") or [])
        previous_status = str(state.get("status", ""))
        try:
            self._start(instruction)
        except urllib.error.HTTPError as exc:
            return 1, f"KiloCodeWeb lehnte den Auftrag ab (HTTP {exc.code})"
        except (OSError, ValueError):
            return 1, "KiloCodeWeb nicht erreichbar (127.0.0.1:8765)"

        started = False
        grace_end = time.monotonic() + 10
        while True:
            time.sleep(self.poll)
            try:
                state = self._state()
            except (OSError, ValueError):
                if time.monotonic() > deadline:
                    return 124, "Timeout"
                continue
            if state.get("busy"):
                started = True
            elif started or str(state.get("status", "")) != previous_status:
                break
            if time.monotonic() > deadline:
                return 124, "Timeout"
            if not started and time.monotonic() > grace_end:
                return 1, "KiloCodeWeb hat den Auftrag nicht gestartet"

        status = str(state.get("status", ""))
        events = (state.get("events") or [])[before:]
        answer = ""
        for event in reversed(events):
            if event.get("kind") == "markdown" and event.get("text"):
                answer = event["text"]
                break
        summary = (answer or status).strip()[:400]
        match = re.search(r"Fehlercode\s+(\d+)", status)
        if match:
            code = int(match.group(1))
        elif status.lower().startswith("fertig"):
            code = 0
        else:
            code = 1
        return code, summary or status or "Unbekannter Status"


def parse_sse(lines):
    event = {"event": "message", "data": None, "id": None}
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line == "":
            if event["data"] is not None:
                yield event
            event = {"event": "message", "data": None, "id": None}
            continue
        if line.startswith(":"):
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field in ("event", "data", "id"):
            event[field] = value


class BridgeClient:
    def __init__(self, base_url, token, connect_timeout=30):
        self.base_url = base_url
        self.token = token
        self.timeout = connect_timeout

    def _request(self, path, payload=None, method=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method or ("POST" if data else "GET"))
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
            return response.status, json.loads(body) if body else {}

    def ack(self, job_id):
        return self._request(f"/jobs/{job_id}/ack", {})

    def complete(self, job_id, status, result):
        return self._request(f"/jobs/{job_id}/complete", {"status": status, "result": result})

    def stream(self):
        request = urllib.request.Request(self.base_url + "/jobs/stream")
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("Accept", "text/event-stream")
        response = urllib.request.urlopen(request, timeout=self.timeout)
        return response


class Agent:
    def __init__(self, cfg, client, runner, ledger):
        self.cfg = cfg
        self.client = client
        self.runner = runner
        self.ledger = ledger

    def handle_job(self, job):
        job_id = job.get("id")
        instruction = job.get("instruction")
        if not isinstance(job_id, str) or len(job_id) != 32:
            LOG.warning("ungueltige Job-ID ignoriert")
            return False
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > MAX_INSTRUCTION:
            LOG.warning("ungueltiger Auftrag %s abgelehnt", job_id)
            try:
                self.client.ack(job_id)
                self.client.complete(job_id, "failed", "Ungueltiger Auftrag")
            except Exception as exc:
                LOG.warning("Quittung fehlgeschlagen: %s", type(exc).__name__)
            return False

        if self.ledger.seen(job_id):
            LOG.info("job=%s bereits ausgefuehrt, nur quittieren", job_id)
            self._safe_ack(job_id)
            self._safe_complete(job_id, "done", "Bereits ausgefuehrt (Ledger)")
            return True

        self._safe_ack(job_id)
        started = time.monotonic()
        if self.cfg.dry_run:
            code, summary = 0, "Dry-Run: Kilo nicht gestartet"
        else:
            code, summary = self.runner.run(instruction)
        self.ledger.record(job_id, code)
        status = "done" if code == 0 else "failed"
        self._safe_complete(job_id, status, summary or f"Exit {code}")
        LOG.info("job=%s %s exit=%s dauer=%.1fs", job_id, status, code, time.monotonic() - started)
        return True

    def _safe_ack(self, job_id):
        try:
            self.client.ack(job_id)
        except Exception as exc:
            LOG.warning("ack %s fehlgeschlagen: %s", job_id, type(exc).__name__)

    def _safe_complete(self, job_id, status, result):
        try:
            self.client.complete(job_id, status, result)
        except Exception as exc:
            LOG.warning("complete %s fehlgeschlagen: %s", job_id, type(exc).__name__)

    def run_forever(self, once=False):
        delay = 1
        response = None
        while True:
            try:
                response = self.client.stream()
                LOG.info("SSE verbunden")
                delay = 1
                for event in parse_sse(response):
                    if event["event"] != "job":
                        continue
                    try:
                        job = json.loads(event["data"])
                    except json.JSONDecodeError:
                        LOG.warning("ungueltiges Job-JSON ignoriert")
                        continue
                    self.handle_job(job)
                    if once:
                        response.close()
                        return
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                LOG.warning("Verbindung verloren (%s), neu in %ss", type(exc).__name__, delay)
            except KeyboardInterrupt:
                return
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                    response = None
            time.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, self.cfg.backoff_max)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Kilo-Agent (Firmen-PC)")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    parser.add_argument("--once", action="store_true", help="einen Auftrag verarbeiten und beenden")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = AgentConfig.from_file(args.config)
    if args.dry_run:
        cfg.dry_run = True
    os.makedirs(cfg.state_dir, exist_ok=True)
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(os.path.join(cfg.state_dir, "agent.log"), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    ledger = Ledger(os.path.join(cfg.state_dir, "executed.json"))
    client = BridgeClient(cfg.base_url, cfg.token, cfg.connect_timeout)
    if cfg.runner_mode == "web":
        runner = KiloWebRunner(cfg.web_url, cfg.web_project, cfg.job_timeout, cfg.web_auto_approve)
    else:
        runner = KiloRunner(cfg.kilo_exe, cfg.workspace, cfg.job_timeout, cfg.model)
    agent = Agent(cfg, client, runner, ledger)
    if IS_WINDOWS:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    LOG.info("Agent startet (workspace=%s, dry_run=%s)", cfg.workspace, cfg.dry_run)
    agent.run_forever(once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
