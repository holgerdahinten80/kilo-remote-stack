#!/usr/bin/env python3
"""Auftrags-Bridge fuer Kilo-Code-Auftraege.

Nimmt Auftraege per HTTP entgegen, haelt sie in SQLite und liefert sie per
Server-Sent-Events an den Firmen-PC-Client aus. Fuehrt selbst nichts aus.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import urllib.request

LOG = logging.getLogger("bridge")

DEFAULTS = {
    "LISTEN": "127.0.0.1:8892",
    "DB_PATH": "/var/lib/kilo-bridge/queue.db",
    "MAX_INSTRUCTION": "8192",
    "MAX_PENDING": "100",
    "LEASE_SECONDS": "120",
    "RUNNING_TIMEOUT_SECONDS": "3600",
    "MAX_ATTEMPTS": "5",
    "HEARTBEAT_SECONDS": "15",
    "SUBMIT_RATE_PER_MIN": "10",
    "IDEMPOTENCY_TTL_DAYS": "7",
    "WAKE_URL": "",
    "WAKE_TOKEN": "",
    "WAKE_AFTER_SECONDS": "10",
    "WAKE_COOLDOWN_SECONDS": "300",
    "CONFIG_ENV": "/etc/kilo-bridge/config.env",
}

REQUIRED = ("SUBMIT_TOKEN", "AGENT_TOKEN")

STATUS_PENDING = "pending"
STATUS_LEASED = "leased"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

JOB_PATH = re.compile(r"^/jobs/([0-9a-f]{32})(/ack|/complete)?$")


class ConfigError(Exception):
    pass


def parse_env_file(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Config:
    def __init__(self, values):
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in values.items() if v is not None})
        missing = [k for k in REQUIRED if not merged.get(k)]
        if missing:
            raise ConfigError("fehlende Werte: " + ", ".join(missing))
        if len(merged["SUBMIT_TOKEN"]) < 32 or len(merged["AGENT_TOKEN"]) < 32:
            raise ConfigError("Tokens muessen mindestens 32 Zeichen haben")
        host, _, port = merged["LISTEN"].rpartition(":")
        if not host or not port.isdigit():
            raise ConfigError("LISTEN muss host:port sein")

        self.listen_host = host
        self.listen_port = int(port)
        self.db_path = merged["DB_PATH"]
        self.max_instruction = int(merged["MAX_INSTRUCTION"])
        self.max_pending = int(merged["MAX_PENDING"])
        self.lease_seconds = int(merged["LEASE_SECONDS"])
        self.running_timeout = int(merged["RUNNING_TIMEOUT_SECONDS"])
        self.max_attempts = int(merged["MAX_ATTEMPTS"])
        self.heartbeat_seconds = int(merged["HEARTBEAT_SECONDS"])
        self.submit_rate = int(merged["SUBMIT_RATE_PER_MIN"])
        self.idempotency_ttl = int(merged["IDEMPOTENCY_TTL_DAYS"]) * 86400
        self.wake_url = merged["WAKE_URL"]
        self.wake_token = merged["WAKE_TOKEN"]
        self.wake_after = float(merged["WAKE_AFTER_SECONDS"])
        self.wake_cooldown = float(merged["WAKE_COOLDOWN_SECONDS"])
        if self.wake_url:
            if not self.wake_url.startswith(("http://", "https://")):
                raise ConfigError("WAKE_URL muss http(s) sein")
            if len(self.wake_token) < 32:
                raise ConfigError("WAKE_TOKEN fehlt oder ist zu kurz")
        self.submit_digest = digest(merged["SUBMIT_TOKEN"])
        self.agent_digest = digest(merged["AGENT_TOKEN"])

    @classmethod
    def from_file(cls, path=None):
        path = path or os.environ.get("CONFIG_ENV") or DEFAULTS["CONFIG_ENV"]
        return cls(parse_env_file(path))

    def token_role(self, presented):
        if not presented:
            return None
        candidate = digest(presented)
        if hmac.compare_digest(candidate, self.submit_digest):
            return "submit"
        if hmac.compare_digest(candidate, self.agent_digest):
            return "agent"
        return None


class Store:
    def __init__(self, path, max_pending=100):
        self.path = path
        self.max_pending = max_pending
        self._init_lock = threading.Lock()
        self._ensure_schema()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self):
        with self._init_lock:
            conn = self.connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        status TEXT NOT NULL,
                        instruction TEXT NOT NULL,
                        client TEXT,
                        idempotency_key TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        lease_until REAL,
                        acked_at REAL,
                        completed_at REAL,
                        result TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency
                        ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def add(self, instruction, client, idempotency_key=None):
        now = time.time()
        job_id = uuid.uuid4().hex
        conn = self.connect()
        try:
            if idempotency_key:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row:
                    return row["id"], False
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN (?, ?, ?)",
                (STATUS_PENDING, STATUS_LEASED, STATUS_RUNNING),
            ).fetchone()["n"]
            if pending >= self.max_pending:
                raise QueueFull()
            conn.execute(
                """INSERT INTO jobs
                   (id, created_at, updated_at, status, instruction, client, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, now, now, STATUS_PENDING, instruction, client, idempotency_key),
            )
            try:
                conn.commit()
            except sqlite3.IntegrityError:
                if idempotency_key:
                    row = conn.execute(
                        "SELECT id FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                    ).fetchone()
                    if row:
                        return row["id"], False
                raise
            return job_id, True
        finally:
            conn.close()

    def take_next(self, lease_seconds, max_attempts):
        now = time.time()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE jobs SET status = ?, updated_at = ?, completed_at = ?, result = ?
                   WHERE status = ? AND lease_until IS NOT NULL AND lease_until < ?""",
                (STATUS_FAILED, now, now, "Running-Timeout", STATUS_RUNNING, now),
            )
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE status = ?
                      OR (status = ? AND lease_until < ?)
                   ORDER BY created_at
                   LIMIT 1""",
                (STATUS_PENDING, STATUS_LEASED, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            if row["attempts"] >= max_attempts:
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, completed_at = ?, result = ? WHERE id = ?",
                    (STATUS_FAILED, now, now, "Maximalversuche erreicht", row["id"]),
                )
                conn.commit()
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, attempts = attempts + 1, lease_until = ?, updated_at = ? WHERE id = ?",
                (STATUS_LEASED, now + lease_seconds, now, row["id"]),
            )
            conn.commit()
            return {
                "id": row["id"],
                "instruction": row["instruction"],
                "created_at": row["created_at"],
                "attempts": row["attempts"] + 1,
            }
        finally:
            conn.close()

    def job(self, job_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_status(self, job_id, status, result=None, require_status=None, lease_until=None):
        now = time.time()
        conn = self.connect()
        try:
            if require_status:
                row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    return False
                if row["status"] not in require_status:
                    return False
            if status in (STATUS_DONE, STATUS_FAILED):
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, completed_at = ?, result = COALESCE(?, result), lease_until = NULL WHERE id = ?",
                    (status, now, now, result, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ?, acked_at = ?, result = COALESCE(?, result), lease_until = COALESCE(?, lease_until) WHERE id = ?",
                    (status, now, now, result, lease_until, job_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()

    def purge_old(self, ttl_seconds):
        cutoff = time.time() - ttl_seconds
        conn = self.connect()
        try:
            conn.execute(
                "DELETE FROM jobs WHERE status IN (?, ?) AND completed_at < ?",
                (STATUS_DONE, STATUS_FAILED, cutoff),
            )
            conn.commit()
        finally:
            conn.close()


class QueueFull(Exception):
    pass


class RateLimiter:
    def __init__(self, limit, window=60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits = []
        self._lock = threading.Lock()

    def allow(self):
        now = self.clock()
        with self._lock:
            self._hits = [hit for hit in self._hits if now - hit < self.window]
            if len(self._hits) >= self.limit:
                return False
            self._hits.append(now)
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = "kilo-bridge/1.0"
    protocol_version = "HTTP/1.1"
    max_body = 16384

    def do_GET(self):
        path = urlsplit(self.path).path
        role = self._role()
        if role is None:
            self._json(401, "unauthorized", "Nicht autorisiert")
            return
        if path == "/jobs/stream":
            if role != "agent":
                self._json(403, "forbidden", "Agent-Token erforderlich")
                return
            self._stream()
            return
        match = JOB_PATH.match(path)
        if match and match.group(2) is None:
            job = self.server.store.job(match.group(1))
            if job is None:
                self._json(404, "not_found", "Unbekannter Auftrag")
                return
            self._json(200, "ok", "Status", extra=self._public(job))
            return
        self._json(404, "not_found", "Unbekannter Endpunkt")

    def do_POST(self):
        path = urlsplit(self.path).path
        role = self._role()
        if role is None:
            self._json(401, "unauthorized", "Nicht autorisiert")
            return

        if path == "/jobs":
            if role != "submit":
                self._json(403, "forbidden", "Submit-Token erforderlich")
                return
            self._submit()
            return

        match = JOB_PATH.match(path)
        if match:
            if role != "agent":
                self._json(403, "forbidden", "Agent-Token erforderlich")
                return
            job_id, action = match.group(1), match.group(2)
            if action == "/ack":
                self._ack(job_id)
                return
            if action == "/complete":
                self._complete(job_id)
                return
        self._json(404, "not_found", "Unbekannter Endpunkt")

    def _submit(self):
        if not self.server.submit_limiter.allow():
            self._json(429, "rate_limited", "Zu viele Auftraege", extra={"retry_after": 60})
            return
        payload = self._read_json()
        if payload is None:
            return
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            self._json(400, "bad_request", "instruction fehlt")
            return
        instruction = instruction.strip()
        if len(instruction) > self.server.cfg.max_instruction:
            self._json(400, "bad_request", "instruction zu lang")
            return
        client = payload.get("client")
        if client is not None and (not isinstance(client, str) or len(client) > 64):
            self._json(400, "bad_request", "client ungueltig")
            return
        key = self.headers.get("Idempotency-Key")
        if key is not None and (not key.strip() or len(key) > 128):
            self._json(400, "bad_request", "Idempotency-Key ungueltig")
            return
        try:
            job_id, created = self.server.store.add(instruction, client, key)
        except QueueFull:
            self._json(507, "queue_full", "Queue ist voll")
            return
        LOG.info(
            "submit job=%s created=%s len=%d client=%s",
            job_id, created, len(instruction), client or "-",
        )
        if created:
            self.server.maybe_wake()
        self._json(202 if created else 200, "accepted" if created else "duplicate",
                   "Auftrag angenommen" if created else "Auftrag existiert bereits",
                   extra={"id": job_id})

    def _ack(self, job_id):
        payload = self._read_json(allow_empty=True)
        if payload is None:
            return
        ok = self.server.store.set_status(
            job_id, STATUS_RUNNING,
            require_status=(STATUS_LEASED, STATUS_PENDING, STATUS_RUNNING),
            lease_until=time.time() + self.server.cfg.running_timeout,
        )
        if not ok:
            self._json(409, "not_acked", "Auftrag nicht im passenden Status")
            return
        LOG.info("ack job=%s", job_id)
        self._json(200, "acknowledged", "Auftrag angenommen", extra={"id": job_id})

    def _complete(self, job_id):
        payload = self._read_json()
        if payload is None:
            return
        status = payload.get("status")
        result = payload.get("result")
        if status not in (STATUS_DONE, STATUS_FAILED):
            self._json(400, "bad_request", "status muss done oder failed sein")
            return
        if result is not None and (not isinstance(result, str) or len(result) > 2000):
            self._json(400, "bad_request", "result ungueltig")
            return
        ok = self.server.store.set_status(
            job_id, status, result=result,
            require_status=(STATUS_RUNNING, STATUS_LEASED),
        )
        if not ok:
            self._json(409, "not_completed", "Auftrag nicht im passenden Status")
            return
        LOG.info("complete job=%s status=%s", job_id, status)
        self._json(200, "ok", "Auftrag abgeschlossen", extra={"id": job_id, "status": status})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        cfg = self.server.cfg
        self.server.client_connected()
        LOG.info("stream open remote=%s", self.client_address[0])
        last_beat = time.monotonic()
        try:
            self.wfile.write(b": stream open\n\n")
            self.wfile.flush()
            while True:
                try:
                    job = self.server.store.take_next(cfg.lease_seconds, cfg.max_attempts)
                except sqlite3.Error as exc:
                    LOG.warning("store error im stream: %s", exc)
                    break
                if job is not None:
                    data = json.dumps(job, ensure_ascii=False)
                    self.wfile.write(f"id: {job['id']}\nevent: job\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    LOG.info("dispatch job=%s attempt=%d", job["id"], job["attempts"])
                if time.monotonic() - last_beat >= cfg.heartbeat_seconds:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.info("stream closed remote=%s", self.client_address[0])
        finally:
            self.server.client_disconnected()

    def _role(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return self.server.cfg.token_role(header[7:].strip())

    def _read_json(self, allow_empty=False):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, "bad_request", "Content-Length ungueltig")
            return None
        if length > self.max_body:
            self._json(413, "too_large", "Request zu gross")
            return None
        if length == 0:
            if allow_empty:
                return {}
            self._json(400, "bad_request", "JSON-Body erforderlich")
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, "bad_request", "Ungueltiges JSON")
            return None
        if not isinstance(payload, dict):
            self._json(400, "bad_request", "JSON-Objekt erwartet")
            return None
        return payload

    def _public(self, job):
        return {
            "id": job["id"],
            "status": job["status"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "attempts": job["attempts"],
            "result": job["result"],
        }

    def _json(self, code, status, message, extra=None):
        body = {"status": status, "message": message}
        if extra:
            body.update(extra)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        LOG.debug("http: " + fmt, *args)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg, store=None):
        super().__init__((cfg.listen_host, cfg.listen_port), Handler)
        self.cfg = cfg
        self.store = store or Store(cfg.db_path, cfg.max_pending)
        self.submit_limiter = RateLimiter(cfg.submit_rate)
        self._last_purge = 0.0
        self._clients = 0
        self._clients_lock = threading.Lock()
        self._wake_lock = threading.Lock()
        self._last_wake = 0.0
        threading.Thread(target=self._purge_loop, daemon=True).start()

    def client_connected(self):
        with self._clients_lock:
            self._clients += 1

    def client_disconnected(self):
        with self._clients_lock:
            self._clients = max(0, self._clients - 1)

    def clients(self):
        with self._clients_lock:
            return self._clients

    def maybe_wake(self, wait=False):
        if not self.cfg.wake_url or self.clients() > 0:
            return False
        if wait:
            return self._wake_after_delay()
        threading.Thread(target=self._wake_after_delay, daemon=True).start()
        return True

    def _wake_after_delay(self):
        if not self._wake_lock.acquire(blocking=False):
            return False
        try:
            time.sleep(self.cfg.wake_after)
            if self.clients() > 0:
                LOG.info("auto-wake nicht noetig: Client verbunden")
                return False
            now = time.time()
            if now - self._last_wake < self.cfg.wake_cooldown:
                LOG.info("auto-wake im Cooldown")
                return False
            self._last_wake = now
            request = urllib.request.Request(self.cfg.wake_url, method="POST")
            request.add_header("Authorization", "Bearer " + self.cfg.wake_token)
            with urllib.request.urlopen(request, timeout=120) as response:
                LOG.info("auto-wake: HTTP %s", response.status)
            return True
        except Exception as exc:
            LOG.warning("auto-wake fehlgeschlagen: %s", type(exc).__name__)
            return False
        finally:
            self._wake_lock.release()

    def _purge_loop(self):
        while True:
            time.sleep(3600)
            try:
                self.store.purge_old(self.cfg.idempotency_ttl)
            except Exception as exc:
                LOG.warning("purge failed: %s", exc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_file()
    server = BridgeServer(cfg)
    LOG.info("kilo-bridge startet auf %s:%d (db=%s)", cfg.listen_host, cfg.listen_port, cfg.db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
