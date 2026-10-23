# Troubleshooting

## Caddy

**`POST /jobs` liefert 404, `GET /jobs/stream` funktioniert**

Der Matcher muss `handle /jobs*` heißen. `handle /jobs/*` matcht `/jobs` ohne
Slash nicht, die Anfrage fällt auf den Fallback (nginx) und endet in 404.

**SSE kommt erst nach Minuten an**

`flush_interval -1` im `reverse_proxy`-Block setzen und Caddy neu laden
(`caddy reload`, kein Container-Neustart nötig).

## Bridge

**Auftrag bleibt lange `leased`**

Ein Client hat die Verbindung nach der Zustellung verloren. Nach
`LEASE_SECONDS` (120 s) wird der Auftrag erneut zugestellt. Das ist normal und
führt dank Ledger nicht zu Doppelausführung.

**Auftrag bleibt `running`**

Der Client ist nach dem `ack` abgestürzt. Nach `RUNNING_TIMEOUT_SECONDS`
(3600 s) markiert die Bridge den Auftrag als `failed` („Running-Timeout").

**`507 queue_full`**

Mehr als 100 offene Aufträge. Erst abarbeiten, dann erneut einreichen.

**`429 rate_limited`**

Mehr als 10 Einreichungen pro Minute (`SUBMIT_RATE_PER_MIN`).

**`rdc` bekommt „Permission denied" auf den Token**

Der Submit-Token muss unter `/etc/kilo-submit.token` liegen, **nicht** in
`/etc/kilo-bridge/` – dieses Verzeichnis ist 0750 und für `rdc` nicht
durchquerbar.

## Wake-Dienst

**`502 vpn_failed`**

```bash
sudo wg-quick up wg-firma
sudo journalctl -u wake -n 50
```

Meist ein abgelaufener Peer oder ein falscher Endpoint.

**`504 vpn_timeout`**

Route oder Relay nicht erreichbar:

```bash
ip route get 192.168.10.146
ping -c 3 192.168.10.146
```

**`200 already_running`, obwohl der PC aus ist**

Der Precheck prüft TCP 3389. Antwortet ein anderes Gerät oder ein Proxy, wird
falsch positiv erkannt. `PRECHECK_TARGET=0` setzen oder Ziel prüfen.

## Windows-Client

**Kein Auftrag wird abgearbeitet**

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'kilo_agent\.py' }
Get-Content "$env:LOCALAPPDATA\KiloAgent\state\agent.log" -Tail 20
```

Der Agent läuft nur bei angemeldetem Benutzer (Interactive-Task). Nach einem
Logoff/Neustart startet er erst wieder bei der Anmeldung – mit Auto-Login
automatisch.

**`agent.log` fehlt**

Ältere Versionen loggten nur nach stdout, das unter `pythonw` verloren geht.
Aktuelle Version schreibt zusätzlich in die Datei.

## KiloCodeWeb / Kilo Code

**Antwort enthält `<｜DSML｜ …>` oder Codeblöcke, Datei wird nicht angelegt**

Der eingebaute Provider `schleissheimer` sendet keine Tool-Definitionen. Lösung:
Provider `schleissheimer-native` (OpenAI-kompatibel, `tool_call: true`) und
Mapping in `KiloCodeWeb.py`. Prüfen:

```powershell
Select-String "C:\Tool\KiloCodeWeb.py" -Pattern "MODEL_PROVIDER_MAP|DEFAULT_MODEL"
```

**`HTTP 402 Add credits`**

Kilo-Gateway-Modelle (`kilo/*`) benötigen Guthaben. Entweder Guthaben aufladen
oder ein Modell der direkt hinterlegten Provider verwenden.

**`Google Generative AI API key is missing`**

Der Google-Provider erwartet `GOOGLE_GENERATIVE_AI_API_KEY`; vorhanden ist nur
`GEMINI_API_KEY`.

**Zwei Instanzen, veralteter Code wird ausgeführt**

Windows erlaubt mit `SO_REUSEADDR` Doppelbindungen. `KiloCodeWeb.py` setzt
`ThreadingHTTPServer.allow_reuse_address = False`; beim Neustart zusätzlich alle
Prozesse mit `KiloCodeWeb.py` in der Kommandozeile beenden.

**`/usr/bin/env: Permission denied` nach dem Kopieren einer Datei**

Die Datei hat ein UTF-8-BOM vor dem Shebang. Ohne BOM neu schreiben
(`UTF8Encoding($false)` bzw. `newline=""` in Python).

**Kilo-CLI nutzt das falsche Modell**

Reihenfolge: `KILO_MODEL` (Env) → VS-Code-`state.vscdb` → `DEFAULT_MODEL`.
Den tatsächlichen Aufruf zeigt das Kommunikationsprotokoll der Web-UI unter
„CLI-Befehl".
