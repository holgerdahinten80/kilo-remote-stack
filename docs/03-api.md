# API

Basis: `https://holger80.dynv6.net`

## Authentifizierung

Alle Endpunkte außer der internen Health-Prüfung verlangen einen Bearer-Token:

```
Authorization: Bearer <token>
```

Es gibt zwei getrennte Tokens:

| Token | Ablage | Rechte |
|---|---|---|
| Submit-Token | `/etc/kilo-submit.token` (Pi), Client/`kilo-submit` | nur `POST /jobs` |
| Agent-Token | `/etc/kilo-bridge/config.env` (Pi), Windows-Client | Stream, Ack, Complete, Status |

Die Bridge speichert nur SHA-256-Digests und vergleicht zeitkonstant.

## Aufträge

### `POST /jobs`

```json
{ "instruction": "Lege im Workspace die Datei hallo.txt an.", "client": "chatgpt-rdc" }
```

Optional: Header `Idempotency-Key: <max 128 Zeichen>`.

Ist kein Agent per SSE verbunden, löst die Bridge nach 10 s automatisch den
Wake-Dienst aus (Cooldown 300 s). Der Auftrag bleibt bis zur Zustellung `pending`.

| Code | Status | Bedeutung |
|---|---|---|
| 202 | `accepted` | neuer Auftrag, `id` in der Antwort |
| 200 | `duplicate` | Idempotency-Key bereits bekannt, gleiche `id` |
| 400 | `bad_request` | fehlende/zu lange `instruction`, ungültiges JSON |
| 401 | `unauthorized` | Token fehlt/falsch |
| 403 | `forbidden` | Agent-Token statt Submit-Token |
| 413 | `too_large` | Body > 16 KB |
| 429 | `rate_limited` | mehr als 10 Aufträge/Minute |
| 507 | `queue_full` | ≥ 100 offene Aufträge |

### `GET /jobs/{id}`

Liefert Status ohne `instruction`:

```json
{ "id": "…", "status": "done", "created_at": 1792748136.5,
  "updated_at": 1792748150.1, "attempts": 1, "result": "…" }
```

### `GET /jobs/stream` (SSE, Agent-Token)

```
Content-Type: text/event-stream
```

```
: stream open

id: 685a916568cb4de092591b4f6db95211
event: job
data: {"id":"…","instruction":"…","created_at":1792748136.5,"attempts":1}

: ping
```

- Heartbeat alle 15 s (`: ping`).
- Nach Verbindungsabbruch einfach neu verbinden; offene und abgelaufene Leases
  werden erneut geliefert (at-least-once).
- Der Client führt ein lokales Ledger (`executed.json`) und führt dieselbe
  Job-ID nicht zweimal aus.

### `POST /jobs/{id}/ack` (Agent-Token)

Bestätigt die Annahme, Status `running`, Lease 3600 s (`RUNNING_TIMEOUT_SECONDS`).

### `POST /jobs/{id}/complete` (Agent-Token)

```json
{ "status": "done", "result": "Kurzbeschreibung" }
```

`status` ist `done` oder `failed`; `result` max. 2000 Zeichen.

## Zustände und Zeiten

| Parameter | Wert | Wirkung |
|---|---|---|
| `LEASE_SECONDS` | 120 | Zeit bis ein zugestellter, nicht bestätigter Auftrag erneut geliefert wird |
| `RUNNING_TIMEOUT_SECONDS` | 3600 | danach wird `running` als `failed` („Running-Timeout") markiert |
| `MAX_ATTEMPTS` | 5 | danach `failed` |
| `COOLDOWN_SECONDS` (Wake) | 60 | Mindestabstand zwischen Wake-Aufrufen |
| `IDEMPOTENCY_TTL_DAYS` | 7 | Aufbewahrung abgeschlossener Aufträge |

## Wake

### `POST /wake`

Leerer Body, Submit-Token nicht erforderlich – der Wake-Dienst nutzt seinen
eigenen Token aus `/etc/wake/config.env`.

| Code | Status | Bedeutung |
|---|---|---|
| 200 | `wake_sent` | Relay hat das Paket angenommen |
| 200 | `already_running` | Ziel per TCP bereits erreichbar |
| 200 | `dry_run` | Ablauf ohne Relay-Aufruf |
| 401 | `unauthorized` | Token fehlt/falsch |
| 409 | `busy` | Vorgang läuft bereits |
| 429 | `cooldown` | Cooldown aktiv (`Retry-After`) |
| 502 | `vpn_failed` / `relay_error` | VPN bzw. Relay fehlgeschlagen |
| 504 | `vpn_timeout` | Interface/Route nicht rechtzeitig bereit |

`wake_sent` bedeutet **nicht**, dass der PC gestartet ist – nur, dass das Relay
das Magic Packet angenommen hat.
