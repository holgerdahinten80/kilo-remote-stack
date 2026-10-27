# Kilo Remote Stack

Sprach-/Chat-basierte Aufträge an einen Firmen-PC: ChatGPT reicht über einen
MCP-Connector und einen Pi-Agenten einen Auftrag ein, der Pi verwaltet ihn in
einer persistenten Queue, und ein Client auf dem Firmen-PC führt ihn über die
Kilo-Code-CLI aus. Zusätzlich kann derselbe Pi den PC per Wake-on-LAN wecken.

## Komponenten

| Komponente | Ort | Aufgabe |
|---|---|---|
| `wake/` | Raspberry Pi | HTTPS-Endpunkt `POST /wake`: VPN aufbauen, WoL-Relay aufrufen, VPN trennen; Wrapper `wake-now` |
| `bridge/` | Raspberry Pi | Auftrags-Queue (SQLite) + SSE-Auslieferung, HTTP-API, Auto-Wake bei fehlendem Client |
| `bridge/client/` | Firmen-PC | ausgehender Agent: Queue abholen, Kilo Code ausführen, quittieren |
| `pc/` | Firmen-PC | Kopien der dort betriebenen Konfiguration (Web-UI, Workspace-Permissions, Provider-Beispiel) |
| `pc/KiloCodeWeb.py` | Firmen-PC | lokale Web-UI, startet die Kilo-CLI und zeigt die Kommunikation |
| Caddy | Raspberry Pi | TLS-Terminierung für `/wake` und `/jobs*` |

## Dokumentation

| Datei | Inhalt |
|---|---|
| [docs/01-architektur.md](docs/01-architektur.md) | Aufbau, Datenfluss, Diagramme |
| [docs/02-komponenten.md](docs/02-komponenten.md) | Dienste, Pfade, Benutzer, Ports |
| [docs/03-api.md](docs/03-api.md) | HTTP-API, SSE, Zustände, Codes |
| [docs/04-betrieb.md](docs/04-betrieb.md) | Start/Stop, Logs, Konfiguration, Rollback |
| [docs/05-sicherheit.md](docs/05-sicherheit.md) | Rechte, Tokens, Risiken, Rotation |
| [docs/06-tests.md](docs/06-tests.md) | Testmatrix und Ergebnisse |
| [docs/07-troubleshooting.md](docs/07-troubleshooting.md) | Bekannte Fehler und Lösungen |
| [docs/08-logging.md](docs/08-logging.md) | Zentrale Logs, Rotation, Abfragen per Job-ID |
| [docs/09-chatgpt-auftraege.md](docs/09-chatgpt-auftraege.md) | Formulierungen für ChatGPT, Regeln, Fehlerfälle |
| [docs/10-telegram.md](docs/10-telegram.md) | Telegram-Benachrichtigungen bei Auftragsende |
| [docs/11-opencode.md](docs/11-opencode.md) | opencode auf dem Pi: TTY-Problem, Wrapper `oc-run` |
| [docs/12-iobroker-node-red.md](docs/12-iobroker-node-red.md) | Node-RED über simple-api, Admin nur bei Bedarf |
| [docs/13-schalter-app.md](docs/13-schalter-app.md) | Schalter-PWA mit Admin-, Dienste- und RDC-Schalter |

## Dateien auf dem Firmen-PC

| Repo-Datei | Ziel auf A1459 |
|---|---|
| `pc/KiloCodeWeb.py` | `C:\Tool\KiloCodeWeb.py` |
| `pc/workspace.kilo.jsonc` | `C:\Users\dahinten\kilo-work\.kilo\kilo.jsonc` |
| `pc/provider.kilo.jsonc.example` | `C:\Users\dahinten\.config\kilo\kilo.jsonc` (Key eintragen) |
| `bridge/client/kilo_agent.py` | `%LOCALAPPDATA%\KiloAgent\kilo_agent.py` |
| `bridge/client/config.example.json` | `%LOCALAPPDATA%\KiloAgent\config.json` (Token eintragen) |

## Schnellstart

```bash
# Tests (lokal, ohne Pi)
python3 -m unittest discover -s wake/tests
python3 -m unittest discover -s bridge/tests
python3 -m unittest discover -s bridge/client/tests

# Auftrag einreichen und auf das Ergebnis warten
/usr/local/bin/kilo-submit --wait --timeout 180 'Lege im Workspace die Datei hallo.txt an.'

# Status später abfragen
/usr/local/bin/kilo-job-status <job-id>

# Rechner wecken (auf dem Pi, als Benutzer rdc)
/usr/local/bin/wake-now

# Logs zu einer Job-ID
/usr/local/bin/kilo-log --job <id> --since 2h

# dauerhaft mitlaufen (Verlauf + Live)
/usr/local/bin/kilo-log --since 2h --follow

# Auftrag einreichen (beliebiger Client mit Submit-Token)
curl -sS -X POST https://holger80.dynv6.net/jobs \
  -H "Authorization: Bearer $SUBMIT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"instruction":"Lege im Workspace die Datei hallo.txt an."}'
```

## Diagramme

Die Diagramme liegen als Mermaid-Quelltext in `docs/diagrams/` und sind in den
Dokumenten eingebettet. VS Code zeigt sie in der Markdown-Vorschau, GitHub
rendert sie direkt.
