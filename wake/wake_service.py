#!/usr/bin/env python3
"""Wake-on-LAN-Dienst mit genau einem Endpunkt: POST /wake."""

import hmac
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("wake")

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

DEFAULTS = {
    "WG_INTERFACE": "wg-firma",
    "TARGET_PORT": "3389",
    "DRY_RUN": "1",
    "COOLDOWN_SECONDS": "60",
    "VPN_WAIT_SECONDS": "20",
    "RELAY_TIMEOUT_SECONDS": "10",
    "PRECHECK_TARGET": "1",
    "LISTEN": "127.0.0.1:8891",
    "CONFIG_ENV": "/etc/wake/config.env",
    "LOG_DIR": "/media/ssd/logs",
    "LOG_MAX_BYTES": "2097152",
    "LOG_BACKUPS": "3",
}

REQUIRED = ("WAKE_TOKEN", "RELAY_URL", "TARGET_MAC", "TARGET_IP")


class ConfigError(Exception):
    pass


class WgError(Exception):
    pass


class RelayError(Exception):
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


class Config:
    def __init__(self, values):
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in values.items() if v is not None})
        missing = [k for k in REQUIRED if not merged.get(k)]
        if missing:
            raise ConfigError("fehlende Werte: " + ", ".join(missing))
        if not MAC_RE.match(merged["TARGET_MAC"]):
            raise ConfigError("TARGET_MAC ungueltig")
        url = urllib.parse.urlsplit(merged["RELAY_URL"])
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ConfigError("RELAY_URL ungueltig")
        if not merged["LISTEN"].count(":"):
            raise ConfigError("LISTEN muss host:port sein")

        self.token = merged["WAKE_TOKEN"]
        self.relay_url = merged["RELAY_URL"]
        self.relay_host = url.hostname
        self.relay_port = url.port or (443 if url.scheme == "https" else 80)
        self.target_mac = merged["TARGET_MAC"].upper()
        self.target_ip = merged["TARGET_IP"]
        self.target_port = int(merged["TARGET_PORT"])
        self.wg_interface = merged["WG_INTERFACE"]
        self.dry_run = merged["DRY_RUN"].strip().lower() in ("1", "true", "yes", "on")
        self.cooldown = int(merged["COOLDOWN_SECONDS"])
        self.vpn_wait = int(merged["VPN_WAIT_SECONDS"])
        self.relay_timeout = int(merged["RELAY_TIMEOUT_SECONDS"])
        self.precheck = merged["PRECHECK_TARGET"].strip().lower() in ("1", "true", "yes", "on")
        host, _, port = merged["LISTEN"].rpartition(":")
        self.listen_host = host or "127.0.0.1"
        self.listen_port = int(port)
        self.log_dir = merged["LOG_DIR"]
        self.log_max_bytes = int(merged["LOG_MAX_BYTES"])
        self.log_backups = int(merged["LOG_BACKUPS"])

    @classmethod
    def from_file(cls, path=None):
        path = path or os.environ.get("CONFIG_ENV") or DEFAULTS["CONFIG_ENV"]
        return cls(parse_env_file(path))


class WakeResult:
    def __init__(self, status, http_status, message, retry_after=None):
        self.status = status
        self.http_status = http_status
        self.message = message
        self.retry_after = retry_after

    def as_dict(self):
        data = {"status": self.status, "message": self.message}
        if self.retry_after is not None:
            data["retry_after"] = self.retry_after
        return data


class TcpProbe:
    def open(self, host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False


class WireGuardControl:
    WG_QUICK = "/usr/bin/wg-quick"

    def __init__(self, interface, relay_host, relay_port, probe=None):
        self.interface = interface
        self.relay_host = relay_host
        self.relay_port = relay_port
        self.probe = probe or TcpProbe()

    def interface_up(self):
        try:
            result = subprocess.run(
                ["ip", "-4", "link", "show", self.interface],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def route_ready(self):
        try:
            result = subprocess.run(
                ["ip", "-4", "route", "get", self.relay_host],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0 and f"dev {self.interface}" in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False

    def up(self):
        self._run("up")

    def down(self):
        self._run("down")

    def _run(self, action):
        try:
            result = subprocess.run(
                ["sudo", "-n", self.WG_QUICK, action, self.interface],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WgError(f"{action} fehlgeschlagen: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:200]
            raise WgError(f"{action} fehlgeschlagen: {detail or 'Exit ' + str(result.returncode)}")

    def wait_ready(self, seconds, interval=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.interface_up() and self.route_ready() and self.probe.open(
                self.relay_host, self.relay_port, timeout=1.5
            ):
                return True
            time.sleep(interval)
        return False


class RelayClient:
    def __init__(self, url, mac, timeout):
        self.url = url
        self.mac = mac
        self.timeout = timeout

    def wake(self):
        params = urllib.parse.urlencode({"mac": self.mac})
        target = f"{self.url}?{params}"
        request = urllib.request.Request(target, method="GET")
        request.add_header("User-Agent", "pi-wake-service/1.0")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status
        except Exception as exc:
            raise RelayError(f"Relay-Aufruf fehlgeschlagen: {type(exc).__name__}") from exc


class WakeController:
    def __init__(self, cfg, wg, relay, probe, clock=time.monotonic):
        self.cfg = cfg
        self.wg = wg
        self.relay = relay
        self.probe = probe
        self.clock = clock
        self._lock = threading.Lock()
        self._last = None

    def handle(self):
        if not self._lock.acquire(blocking=False):
            return WakeResult("busy", 409, "Ein Wake-Vorgang laeuft bereits")
        try:
            if self._last is not None:
                remaining = self.cfg.cooldown - (self.clock() - self._last)
                if remaining > 0:
                    return WakeResult(
                        "cooldown", 429, "Cooldown aktiv", retry_after=int(remaining) + 1
                    )
            self._last = self.clock()
            return self._execute()
        finally:
            self._lock.release()

    def _execute(self):
        started_by_us = False
        try:
            if not self.wg.interface_up():
                self.wg.up()
                started_by_us = True
            if not self.wg.wait_ready(self.cfg.vpn_wait):
                return WakeResult("vpn_timeout", 504, "VPN-Route nicht verfuegbar")
            if self.cfg.precheck and self.probe.open(
                self.cfg.target_ip, self.cfg.target_port, timeout=1.5
            ):
                return WakeResult("already_running", 200, "Ziel ist bereits erreichbar")
            if self.cfg.dry_run:
                return WakeResult("dry_run", 200, "Dry-Run: Relay-Aufruf uebersprungen")
            code = self.relay.wake()
            if not 200 <= code < 300:
                return WakeResult("relay_error", 502, f"Relay antwortete mit HTTP {code}")
            return WakeResult("wake_sent", 200, "Wake-on-LAN-Paket an Relay gesendet")
        except WgError as exc:
            LOG.error("wireguard: %s", exc)
            return WakeResult("vpn_failed", 502, "VPN-Verbindung fehlgeschlagen")
        except RelayError as exc:
            LOG.error("relay: %s", exc)
            return WakeResult("relay_error", 502, "Relay nicht erreichbar")
        finally:
            if started_by_us:
                try:
                    self.wg.down()
                except WgError as exc:
                    LOG.error("cleanup: %s", exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "wake-service/1.0"
    protocol_version = "HTTP/1.1"
    max_body = 1024

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/wake" or parsed.query:
            self._send(404, "not_found", "Unbekannter Endpunkt")
            return
        if not self._authorized():
            self._send(401, "unauthorized", "Nicht autorisiert")
            return
        if not self._body_is_empty():
            self._send(400, "bad_request", "Kein Request-Body erlaubt")
            return
        started = time.monotonic()
        result = self.server.controller.handle()
        LOG.info(
            "wake request remote=%s status=%s http=%s duration_ms=%d",
            self.client_address[0], result.status, result.http_status,
            int((time.monotonic() - started) * 1000),
        )
        self._send(result.http_status, result.status, result.message, result.retry_after)

    def do_GET(self):
        self._send(405, "method_not_allowed", "Nur POST /wake erlaubt")

    def do_PUT(self):
        self._send(405, "method_not_allowed", "Nur POST /wake erlaubt")

    def do_DELETE(self):
        self._send(405, "method_not_allowed", "Nur POST /wake erlaubt")

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.controller.cfg.token
        return hmac.compare_digest(header.encode("utf-8"), expected.encode("utf-8"))

    def _body_is_empty(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return False
        if length <= 0:
            return True
        if length > self.max_body:
            return False
        self.rfile.read(length)
        return False

    def _send(self, code, status, message, retry_after=None):
        body = json.dumps({"status": status, "message": message}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        LOG.debug("http: " + fmt, *args)


class WakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, controller):
        super().__init__(address, Handler)
        self.controller = controller


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(cfg):
    handlers = [logging.StreamHandler()]
    try:
        directory = os.path.join(cfg.log_dir, "wake")
        os.makedirs(directory, exist_ok=True)
        from logging.handlers import RotatingFileHandler

        handlers.append(
            RotatingFileHandler(
                os.path.join(directory, "wake.jsonl"),
                maxBytes=cfg.log_max_bytes,
                backupCount=cfg.log_backups,
                encoding="utf-8",
            )
        )
    except OSError:
        pass
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def build(cfg):
    probe = TcpProbe()
    wg = WireGuardControl(cfg.wg_interface, cfg.relay_host, cfg.relay_port, probe)
    relay = RelayClient(cfg.relay_url, cfg.target_mac, cfg.relay_timeout)
    controller = WakeController(cfg, wg, relay, probe)
    return WakeServer((cfg.listen_host, cfg.listen_port), controller)


def main():
    cfg = Config.from_file()
    configure_logging(cfg)
    server = build(cfg)
    LOG.info(
        "wake-service startet auf %s:%d (dry_run=%s, interface=%s)",
        cfg.listen_host, cfg.listen_port, cfg.dry_run, cfg.wg_interface,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
