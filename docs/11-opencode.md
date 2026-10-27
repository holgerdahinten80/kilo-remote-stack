# opencode auf dem Pi

## Installation

| Punkt | Wert |
|---|---|
| Binary | `/home/pi/.opencode/bin/opencode` (v2.1.4) |
| PATH | `/home/pi/.bashrc` ergänzt `.opencode/bin` (gilt nur für Login-Shells) |
| Auth | `opencode auth list` → `DeepSeek … stored` |
| Modelle | `deepseek/deepseek-flash`, `deepseek/deepseek-v4-pro`, `opencode/deepseek-v4.1-flash-free` |
| Laufzeitbenutzer | `pi` (Auth und Config liegen in dessen Home) |

## Bekanntes Problem: `run` hängt ohne TTY

`opencode run "<prompt>"` blockiert, wenn kein Terminal (TTY) vorhanden ist –
also aus Skripten, Cron, SSH ohne PTY oder über einen Remote-Agenten. Es gibt
keine Fehlermeldung und keinen Exit; der Prozess bleibt stehen.

Nachgewiesen:

| Aufruf | Ergebnis |
|---|---|
| `timeout 25 opencode run …` (kein TTY) | hängt, Exit 124, keine Ausgabe |
| `opencode run --standalone …` | hängt ebenfalls |
| `opencode run --server http://… ` + `OPENCODE_PASSWORD` | hängt ebenfalls |
| `script -q -e -c "opencode run …" /dev/null` (**Pseudo-TTY**) | funktioniert, Exit 0 |
| dasselbe mit `--format json` | funktioniert, JSON-Events |

Der Server selbst startet korrekt (`opencode serve --port 0` meldet
`server listening on http://127.0.0.1:PORT` + Passwort) – nur der Client `run`
braucht das TTY.

## Lösung: `oc-run`

`/usr/local/bin/oc-run` startet opencode unter einem Pseudo-Terminal **und mit
`--standalone`**. Dadurch entsteht kein dauerhafter Hintergrunddienst; nach dem
Lauf bleiben keine Prozesse zurück (spart ~200 MB im Leerlauf).

```bash
oc-run "Erkläre kurz, was dieses Verzeichnis enthält."
oc-run --model deepseek/deepseek-flash "Sag OK"
oc-run --timeout 600 "Analysiere …"
echo "Sag OK" | oc-run           # Prompt über stdin
oc-run --json "Sag OK"           # komplette Eventliste als JSON
```

| Option | Bedeutung |
|---|---|
| `--model`, `-m` | Modell (Default `deepseek/deepseek-flash`, Env `OPENCODE_MODEL`) |
| `--timeout`, `-t` | Sekunden (Default 300), danach Exit 124 |
| `--json` | Roh-Events statt Text |

Exitcodes: `0` Erfolg, `1` Modell-/Providerfehler, `124` Timeout.
Fehler erscheinen als Text bzw. als `{"type":"error",…}`.

## Aufräumen

Hängende Prozesse (z. B. nach fehlendem TTY) belegen ~130 MB:

```bash
pkill -f '/home/pi/.opencode/bin/opencode'
```

## Speicher

opencode braucht rund 130–210 MB RSS. Ein normaler `opencode run` startet
zusätzlich einen Hintergrunddienst (`serve --service`), der auch nach dem Lauf
weiterläuft. `oc-run` verwendet `--standalone` und hinterlässt nichts.

Es überwacht das Arbeitsverzeichnis; aus `/` oder `/home` gestartet indiziert es
große Bäume und belastet den Pi spürbar. Deshalb immer in einem kleinen
Projektverzeichnis starten (z. B. `/home/pi/oc-test` oder das jeweilige Repo).

Messwerte nach der Umstellung: `available` ~955 MB, Swap 1,5 GB belegt
(zram), keine opencode-Prozesse im Leerlauf.

## Nicht vorgesehen

- opencode als Benutzer `rdc` (kein Auth, kein Home) – für ChatGPT-Aufträge ist
  weiterhin Kilo Code über `kilo-submit` vorgesehen.
- opencode parallel zu mehreren Sessions im selben Verzeichnis.
