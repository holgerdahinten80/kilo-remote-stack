# Tests

## Automatisierte Tests

```bash
python3 -m unittest discover -s wake/tests
python3 -m unittest discover -s bridge/tests
python3 -m unittest discover -s bridge/client/tests
```

| Suite | Anzahl | Abdeckung |
|---|---|---|
| Wake | 25 | Auth, Methoden, Body, Cooldown, Parallelität, VPN-Fehler/-Timeout, Relay-Fehler, Cleanup, Dry-Run |
| Bridge | 26 | Token-Rollen, Validierung, Idempotenz, Queue-Limit, Rate-Limit, Lease/Ack/Complete, Lease-Ablauf, Running-Timeout, Persistenz, SSE, keine Befehlsausführung |
| Client | 18 | Konfiguration, SSE-Parsing, Erfolg/Fehler/Timeout, Ledger-Dedupe, Dry-Run, Web-Runner, Kommandobau |

Ergebnis: **69 Tests, alle grün** (Stand 23.10.2026).

## Manuelle Abnahmen

### Wake-Endpunkt

| Prüfung | Ergebnis |
|---|---|
| `POST /wake` ohne Token | 401 |
| `POST /wake` mit Token, Ziel aus | `wake_sent` |
| Tunnel danach | getrennt, Host-Routen und `/etc/resolv.conf` unverändert |
| Ziel nach Wake | nach ~41 s per TCP 3389 erreichbar |
| Cooldown | zweiter Aufruf innerhalb 60 s → 429 |

### Queue und SSE

| Prüfung | Ergebnis |
|---|---|
| Einreichung als `rdc` | 202 mit Job-ID |
| SSE-Zustellung | `event: job` mit korrekter ID |
| `ack` → `complete` | Status `running` → `done` |
| Doppelter Idempotency-Key | gleiche ID, HTTP 200 |
| Shell-Syntax im Auftrag | gespeichert, **nicht** ausgeführt |
| Lease-Ablauf | erneute Zustellung (`attempt 2`), danach `done` |
| Client-Neustart | Ledger verhindert Doppelausführung |

### Gesamtkette inkl. Wake-on-LAN

| Zeit | Ereignis |
|---|---|
| 12:38 | PC aus, `POST /wake` → `wake_sent` |
| 12:39:30 | TCP 3389 erreichbar (45 s nach Wake) |
| 12:40 | Auto-Login `dahinten` (console) |
| 12:40:33 | `KiloCodeWeb` aktiv, Agent verbunden |
| 12:42:40 | Auftrag `b9183849…` eingereicht |
| 12:43:10 | `done`, `wol-test.txt` = „Auto-Login OK" |

### Modell-/Provider-Analyse

| Test | Ergebnis |
|---|---|
| Eingebauter Provider `schleissheimer`, Modell Charlie | Antwort als DSML-Text, keine Ausführung |
| Modelle `agent`, `auto`, `alpha`, `bravo` | dito |
| CLI 7.8.17 (npx) | dito |
| Direkte API `POST /v1/chat/completions` ohne Streaming | `finish_reason: tool_calls`, valider `write_file`-Call |
| Direkte API mit Streaming | 5 Tool-Deltas, `finish_reason: tool_calls` |
| Eigener Provider `schleissheimer-native` | echter `tool_use`, Datei korrekt angelegt |
| Zweites Modell (`delta`) über neuen Provider | `tool_use`, Datei korrekt angelegt |

## Nicht abgedeckt

- Automatisierte End-to-End-Tests über alle vier Systeme (nur manuell belegt)
- Dauerbetrieb über mehrere Tage, Speicher-/Logwachstum
- Windows-Update-Reboot ohne Anmeldung
- Fehlerszenario „Firmen-VPN bricht während des Wake-Aufrufs ab"
- Rotation der Tokens als geübter Ablauf
- Verhalten bei mehreren gleichzeitigen SSE-Clients (fachlich nicht vorgesehen)
