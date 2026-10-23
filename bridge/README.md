# Kilo-Bridge (Pi)

Nimmt Kilo-Code-Aufträge per HTTPS an, hält sie in SQLite und liefert sie per
Server-Sent Events an den Client auf dem Firmen-PC. Die Bridge führt selbst
**nichts** aus und kennt keine Shell.

## Komponenten

```
ChatGPT (RDC-App)  →  Agent auf Pi (User rdc)  →  kilo-submit  →  Bridge :8892
                                                                     ↑ SSE
                                                        Kilo-Client (Firmen-PC)
```

## Dateien

| Pfad | Zweck |
|---|---|
| `/usr/local/bin/kilo-bridge.py` | Bridge |
| `/usr/local/bin/kilo-submit` | Wrapper für Auftragseinreichung (läuft als `rdc`) |
| `/etc/kilo-bridge/config.env` | Tokens + Limits (root:bridge 0640) |
| `/etc/kilo-submit.token` | Submit-Token für `rdc` (root:rdc 0640) |
| `/var/lib/kilo-bridge/queue.db` | SQLite-Queue (WAL) |
| `/etc/systemd/system/kilo-bridge.service` | Unit |

## API

| Methode | Pfad | Token | Zweck |
|---|---|---|---|
| POST | `/jobs` | Submit | Auftrag anlegen (`instruction`, optional `client`, Header `Idempotency-Key`) |
| GET | `/jobs/stream` | Agent | SSE-Stream, Heartbeat 15 s |
| POST | `/jobs/{id}/ack` | Agent | Annahme quittieren → `running` |
| POST | `/jobs/{id}/complete` | Agent | `{"status":"done"\|"failed","result":"…"}` |
| GET | `/jobs/{id}` | Submit/Agent | Status (ohne Instruction) |

Status: `pending → leased → running → done|failed`. Lease 120 s; abgelaufene
Leases werden erneut zugestellt (at-least-once, max. 5 Versuche).

### SSE-Format

```
id: <job-id>
event: job
data: {"id":"…","instruction":"…","created_at":…,"attempts":1}
```

Alle 15 s `: ping`. Nach Verbindungsabbruch einfach neu verbinden – offene und
abgelaufene Leases werden erneut geliefert. Der Client **muss** pro `id` ein
lokales Ledger führen und bereits ausgeführte Aufträge nur noch bestätigen,
nicht erneut ausführen.

### Beispiel

```bash
kilo-submit "Baue Feature X in Projekt Y"
# → {"http": 202, "status": "accepted", "id": "…"}
```

## Tokens

- Zwei getrennte Tokens: `SUBMIT_TOKEN` (nur `POST /jobs`, rate-limited) und
  `AGENT_TOKEN` (Stream + Quittungen).
- `rdc` kann bewusst nur den Submit-Token lesen, nicht `config.env`.
- Rotation: neuen Wert in `/etc/kilo-bridge/config.env` setzen und bei
  `submit.token` angleichen, dann `sudo systemctl restart kilo-bridge.service`.

## Betrieb

```bash
sudo systemctl status kilo-bridge
sudo journalctl -u kilo-bridge -f
sudo systemctl restart kilo-bridge
```

Logs enthalten Job-ID, Status, Länge und Client – keine Instruction-Inhalte,
keine Tokens.

## Caddy

```
handle /jobs* {
    reverse_proxy localhost:8892 {
        flush_interval -1
    }
}
```

Der Matcher muss `/jobs*` heißen, nicht `/jobs/*`, sonst fällt `POST /jobs`
auf den Fallback.

## Rollback

```bash
sudo systemctl disable --now kilo-bridge
sudo rm -f /etc/systemd/system/kilo-bridge.service /etc/kilo-submit.token
sudo rm -rf /etc/kilo-bridge /var/lib/kilo-bridge
sudo rm -f /usr/local/bin/kilo-bridge.py /usr/local/bin/kilo-submit
sudo userdel bridge
sudo systemctl daemon-reload
```

Caddy-Block entfernen, `caddy reload`. Der RDC-Agent und `/wake` sind davon
unberührt.

## Tests

```bash
cd bridge && python3 -m unittest discover -s tests -v
```

Abgedeckt: Token-Rollen, Validierung, Idempotenz, Queue-Limit, Rate-Limit,
Lease/Ack/Complete, Lease-Ablauf, Maximalversuche, Persistenz, SSE-Auslieferung,
keine Interpretation von Instruction-Inhalten.
