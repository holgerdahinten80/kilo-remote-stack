# Komponenten

## 1. Wake-Dienst (Raspberry Pi)

| Eigenschaft | Wert |
|---|---|
| Programm | `/usr/local/bin/wake-service.py` |
| Unit | `/etc/systemd/system/wake.service` |
| Benutzer | `waker` (Systembenutzer, keine weiteren Gruppen) |
| Bindung | `127.0.0.1:8891` |
| Konfiguration | `/etc/wake/config.env` (root:waker 0640) |
| sudo-Recht | `/etc/sudoers.d/wake`: nur `wg-quick up/down wg-firma` |
| VPN | `/etc/wireguard/wg-firma.conf` (root 0600) |
| HTTPS | Caddy: `POST /wake` |
Ablauf: Cooldown prüfen → Tunnel prüfen oder `wg-quick up` → Interface, Route und
TCP-Erreichbarkeit des Relays abwarten → Ziel per TCP vorprüfen → Relay-Aufruf →
Tunnel nur trennen, wenn selbst gestartet.

**Wrapper `wake-now`**: `/usr/local/bin/wake-now` (als `rdc` ausführbar) liest
`/etc/wake.token` (root:rdc 0640) und ruft den lokalen Dienst auf. Damit kann
ChatGPT das Wecken auslösen, ohne den Token zu kennen.

## 2. Kilo-Bridge (Raspberry Pi)

| Eigenschaft | Wert |
|---|---|
| Programm | `/usr/local/bin/kilo-bridge.py` |
| Unit | `/etc/systemd/system/kilo-bridge.service` |
| Benutzer | `bridge` (Systembenutzer) |
| Bindung | `127.0.0.1:8892` |
| Datenbank | `/var/lib/kilo-bridge/queue.db` (WAL) |
| Konfiguration | `/etc/kilo-bridge/config.env` (root:bridge 0640) |
| Submit-Token | `/etc/kilo-submit.token` (root:rdc 0640) |
| HTTPS | Caddy: `handle /jobs*` mit `flush_interval -1` |

Die Bridge führt nichts aus. Sie speichert, lease't und liefert Aufträge aus.

**Auto-Wake**: Wird ein Auftrag angelegt und ist kein Agent per SSE verbunden,
ruft die Bridge nach `WAKE_AFTER_SECONDS` (10 s) den lokalen Wake-Dienst auf
(`WAKE_URL`, `WAKE_TOKEN` in `/etc/kilo-bridge/config.env`). Cooldown 300 s.

**Telegram**: Bei `complete` sendet die Bridge eine Nachricht an den Bot
(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Details: `docs/10-telegram.md`.

## 3. `kilo-submit` (Raspberry Pi)
| Eigenschaft | Wert |
|---|---|
| Pfad | `/usr/local/bin/kilo-submit` |
| Ausführung | als Benutzer `rdc` (durch den RDC-Agenten) |
| Wirkung | `POST http://127.0.0.1:8892/jobs` mit `instruction` aus Argument/stdin |
| Grenzen | max. 8192 Zeichen, keine Ziel-/Kommando-Parameter |

## 4. RDC-Agent (Raspberry Pi)

| Eigenschaft | Wert |
|---|---|
| Laufzeit | Node.js 22.23.4 unter `/opt/node22` |
| Paket | `@wonderwhy-er/desktop-commander@0.3.1` (global, `--ignore-scripts`) |
| Unit | `/etc/systemd/system/rdc-agent.service` |
| Benutzer | `rdc` (kein sudo, keine docker-Gruppe) |
| Verbindung | ausgehend zu `mcp.desktopcommander.app` |
| Gerät | `raspberrypi2`, an ChatGPT-Konto gekoppelt |

Der Agent gibt ChatGPT Shell-Zugriff als `rdc`. Deshalb existiert der Wrapper
`kilo-submit` und der Submit-Token ist bewusst auf `POST /jobs` beschränkt.

## 5. Client auf dem Firmen-PC

| Eigenschaft | Wert |
|---|---|
| Programm | `%LOCALAPPDATA%\KiloAgent\kilo_agent.py` |
| Konfiguration | `%LOCALAPPDATA%\KiloAgent\config.json` (ACL nur `Dahinten`) |
| Zustand | `%LOCALAPPDATA%\KiloAgent\state\executed.json`, `agent.log` |
| Aufgabe | `KiloAgent`, Trigger „bei Anmeldung", `Interactive`, `RunLevel Limited` |
| Verbindung | ausgehend `https://holger80.dynv6.net/jobs/stream` (SSE) |
| Ausführung | `RUNNER=web` → `http://127.0.0.1:8765/api/run` |

Der Client hat kein Inbound-Listening und führt ausschließlich den fest
konfigurierten Kilo-Aufruf aus.

## 6. KiloCodeWeb (Firmen-PC)

| Eigenschaft | Wert |
|---|---|
| Pfad | `C:\Tool\KiloCodeWeb.py` |
| Aufgabe | lokale Web-UI, startet `kilo.exe run`, streamt JSON-Events |
| Bindung | `127.0.0.1:8765` |
| Aufgabe | `KiloCodeWeb`, Trigger „bei Anmeldung", `Interactive`, `RunLevel Limited` |
| Modellwahl | `KILO_MODEL` → VS-Code-`state.vscdb` (`recentModels`/`favoriteModels`) → Fallback Charlie |
| Provider-Mapping | `schleissheimer/*` → `schleissheimer-native/*` (OpenAI-kompatibel) |
| Backups | `KiloCodeWeb.py.bak-*` im selben Verzeichnis |
| Aufruf | `http://127.0.0.1:8765` oder Desktop-Verknüpfung „Kilo Code Web" |

Das Skript ist ein Server und öffnet selbst kein Fenster. `KiloCodeWeb-Start.cmd`
startet ihn bei Bedarf und öffnet die Seite im Standardbrowser.

Die gepatchte Fassung liegt zusätzlich im Repo unter `pc/KiloCodeWeb.py`.

## 7. Provider-Konfiguration (Firmen-PC)

`C:\Users\dahinten\.config\kilo\kilo.jsonc` definiert den Provider
`schleissheimer-native`:

```jsonc
{
  "provider": {
    "schleissheimer-native": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://ai.schleissheimer.de/v1", "apiKey": "<key>" },
      "models": {
        "schleissheimer/charlie": {
          "tool_call": true,
          "reasoning": true,
          "limit": { "context": 131072, "output": 8192 }
        }
      }
    }
  }
}
```

Alle schleissheimer-Modelle sind mit `tool_call: true` hinterlegt.

Vorlage ohne Key im Repo: `pc/provider.kilo.jsonc.example`.

## 8. Workspace (Firmen-PC)

`C:\Users\dahinten\kilo-work` mit `.kilo\kilo.jsonc`:

- `read`, `write`, `edit`, `glob`, `grep`: erlaubt
- `bash`: alles verboten außer `git`, `npm`, `node`, `python`, `dir`, `type`, `echo`
- `external_directory`: verboten

## 9. Caddy (Raspberry Pi)

```caddyfile
handle /wake {
    reverse_proxy localhost:8891
}

handle /jobs* {
    reverse_proxy localhost:8892 {
        flush_interval -1
    }
}
```

Beide Routen liegen im Site-Block `holger80.dynv6.net`; alle anderen Pfade gehen
unverändert an den bisherigen Fallback (`localhost:8080`).
