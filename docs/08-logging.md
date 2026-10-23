# Logging

Ziel: Fehleranalyse über alle Stationen mit einer **Job-ID**, ohne großen
Speicherbedarf. Kein zentraler Log-Server, keine zusätzlichen Container.

## Quellen

| Quelle | Wo | Format |
|---|---|---|
| Bridge | journald + `/media/ssd/logs/bridge/bridge.jsonl` | JSON je Zeile |
| Wake-Dienst | journald + `/media/ssd/logs/wake/wake.jsonl` | JSON je Zeile |
| RDC-Agent | journald (`rdc-agent`) | Text |
| Windows-Agent | `/media/ssd/logs/agent/agent.jsonl` (per HTTPS hochgeladen) | JSON je Zeile |
| KiloCodeWeb | `C:\Tool\logs\kiloweb.jsonl` (lokal) → per Tailer mitgeschickt | JSON je Zeile |

Der Agent schickt seine Logzeilen und die KiloCodeWeb-Events gebündelt
(alle 5 s, max. 50 Zeilen) an `POST /logs` der Bridge. Von dort landen sie in
`agent.jsonl`. Fällt der Pi aus, bleiben die lokalen Dateien auf A1459 erhalten.

## Speicherbedarf und Retention

| Bereich | Limit |
|---|---|
| journald | 200 MB gesamt, 50 MB runtime, 20 MB je Datei, 14 Tage (`/etc/systemd/journald.conf.d/10-limits.conf`) |
| `bridge.jsonl` | 2 MB × 3 Dateien |
| `wake.jsonl` | 2 MB × 3 Dateien |
| `agent.jsonl` | 2 MB × 3 Dateien |
| `kiloweb.jsonl` (A1459) | 2 MB × 3 Dateien |
| `agent.log` (A1459) | 1 MB × 3 Dateien |

Summe der Datei-Logs: ca. 30 MB. Rotation passiert beim Überschreiten der Größe
(`.1`, `.2`, `.3`).

## Abfragen

```bash
kilo-log --job fa80b3fd9c404606a04f135d206c92a3 --since 2h
kilo-log --since 30m --source bridge
kilo-log --since 1d --source agent --tail 100
kilo-log --since 15m --json

# dauerhaft mitlaufen (Verlauf + Live), Strg+C beendet
kilo-log --since 2h --tail 200 --follow
kilo-log --job fa80… --since 1d --follow
```

Ausgabe:

```
13:21:29 bridge  submit  job=fa80… created=True len=37 client=chatgpt-rdc
13:21:29 bridge  dispatch job=fa80… attempt=1
13:21:29 bridge  ack      job=fa80…
13:21:29 agent   [log] ANFRAGE — QUELLE: WEB-CHAT — JOB: fa80…
13:21:39 bridge  complete job=fa80… status=done
13:21:41 agent   job=fa80… done exit=0 dauer=10.3s
```

Weitere Wege:

```bash
journalctl -u kilo-bridge --since '1 hour ago' -o cat
journalctl -u wake -n 50
tail -f /media/ssd/logs/bridge/bridge.jsonl
```

## Dauerhafte Ansicht mit Scrollback

Ein normales Terminal-Fenster behält die Ausgabe, solange es offen ist; mit
`--follow` läuft sie weiter. Für langen Rückblick:

- **Windows Terminal**: Einstellungen → Profil → „Verlauf“ (Scrollback) auf
  z. B. 10 000 Zeilen; alternativ `wt` mit Standardpuffer.
- **PowerShell-Konsole**: Fenstertitel → Eigenschaften → Bildschirmpufferhöhe
  auf 9999.
- **tmux auf dem Pi** (empfohlen für Dauerbetrieb):

```bash
ssh rasp
tmux new -s logs
kilo-log --since 6h --follow
# Strg+B, D löst; "tmux attach -t logs" verbindet wieder
```

Die eigentliche Persistenz liegt in den Dateien und in journald – ein
geschlossenes Fenster verliert nichts.

## Was nicht geloggt wird

- Tokens, API-Keys, Authorization-Header
- vollständige Instruktionstexte der Aufträge (nur Länge und Job-ID)
- Kilo-Code-Ausgaben vollständig; KiloCodeWeb speichert Events gekürzt (max. 4000 Zeichen)

## Fehlerfall: Agent liefert keine Logs

1. `Get-Content "$env:LOCALAPPDATA\KiloAgent\state\agent.log" -Tail 20`
2. `Test-NetConnection holger80.dynv6.net -Port 443`
3. Auf dem Pi: `tail -n 5 /media/ssd/logs/agent/agent.jsonl`
4. Caddy-Route prüfen: `POST /logs` ohne Token muss 401 liefern.

## Deinstallation der Datei-Logs

```bash
sudo systemctl stop kilo-bridge wake
sudo rm -rf /media/ssd/logs/bridge /media/ssd/logs/wake /media/ssd/logs/agent
sudo rm -f /etc/systemd/journald.conf.d/10-limits.conf
sudo systemctl restart systemd-journald kilo-bridge wake
```

Die Logs sind reine Diagnosedaten; die Dienste funktionieren ohne sie weiter
(Schreibfehler werden ignoriert).
