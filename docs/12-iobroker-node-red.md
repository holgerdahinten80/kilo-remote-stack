# ioBroker/Node-RED ohne Admin-Abhängigkeit

Ziel: Der ioBroker-Admin (Web-UI, 126 MB) muss nicht dauerhaft laufen. Node-RED
schreibt States deshalb nicht mehr über die WebSocket-Verbindung auf Port 8081,
sondern über die **simple-api** auf Port 8087.

## Vorher/Nachher

| | vorher | nachher |
|---|---|---|
| Node-RED → ioBroker | `node-red-contrib-iobroker` (WebSocket 8081) | 18 × `http request` an `127.0.0.1:8087` |
| Admin-Adapter | dauerhaft an (126 MB) | aus, per Kommando einschaltbar |
| Objekt-States | `iobout` konnte Objekte schreiben | simple-api kann **nur** Primitive; Command läuft über eine Zahl |

## Aufbau

```
Alexa → VSH-Gerät → Node-RED → http request
                                  → http://127.0.0.1:8087/set/<state>?value=<v>
                                      → ioBroker (javascript.0 / alexa2.0)
```

Beispiel-URLs (aus den Flows):

```
http://127.0.0.1:8087/set/javascript.0.Schalter.Kueche?value={{{payload}}}
http://127.0.0.1:8087/set/alexa2.0.Smart-Home-Devices.<id>.powerState?value={{{payload}}}
http://127.0.0.1:8087/set/0_userdata.0.command.id?value={{{payload}}}
```

`payload` ist bei den Schaltern ein Boolean (aus dem Subflow `ConvertToBool`).

## Command-Kanal

Das VSH-Gerät `Command` (DIMMER_SWITCH) liefert `{powerState, brightness}`.
simple-api kann keine Objekt-States schreiben, deshalb wandelt Node-RED das in
eine Zahl um und schreibt `0_userdata.0.command.id`.

`script.js.Alexa.Command` wertet diese Zahl aus:

| ID | Funktion |
|---|---|
| 1 | Saugen |
| 2 | Wischen |
| 3 | TV an |
| 4 | Video an |
| **5** | **ioBroker-Admin einschalten** |
| **6** | **ioBroker-Admin ausschalten** |
| **7** | **Sonarr + Radarr + NZBGet einschalten** |
| **8** | **Sonarr + Radarr + NZBGet ausschalten** |

Aufruf über Alexa: dem Gerät `Command` die Helligkeit 5 % bzw. 6 % zuweisen
(oder eine Alexa-Routine „iobroker admin an" → „Command auf 5 %").

## Container-Steuerung

Die drei Container laufen nicht in ioBroker, deshalb gibt es einen kleinen
Helferdienst auf dem Pi:

| | |
|---|---|
| Programm | `/usr/local/bin/containerctl` (Python, stdlib) |
| Unit | `/etc/systemd/system/containerctl.service` (User `pi`, docker-Gruppe) |
| Bindung | `127.0.0.1:8894` |
| Allowlist | `sonarr`, `radarr`, `nzbget` – keine freien Containernamen |
| Endpunkte | `GET /status`, `/start-all`, `/stop-all` (einzeln: `/start/<name>`, `/stop/<name>`) |

Ablauf: Alexa → VSH `Command` → Node-RED → `0_userdata.0.command.id` →
ioBroker-JavaScript → `curl http://127.0.0.1:8894/stop-all` → `docker stop`.

Getestet: `command.id=8` → alle drei `exited`; `command.id=7` → alle `running`.
Der Helfer lehnt andere Container ab (`{"error":"container not allowed"}`).

## Tests

| Prüfung | Ergebnis |
|---|---|
| `curl /set/...?value=true` (Boolean) | `{"value":true,"val":true}` |
| Objekt-State per simple-api | abgelehnt (`forbidden properties`) → deshalb Zahl |
| `command.id=99` | Log `Invalid command!` (keine Aktion) |
| `command.id=5` | `admin.0 is running` |
| `command.id=6` | `admin.0 is not running` |
| Node-RED-Log auf 8081 | 0 Verbindungen |
| Bindung simple-api | `127.0.0.1:8087`, aus dem LAN nicht erreichbar |
| Flows | 18 `http request`, 0 `iob*`-Knoten |

## Speicher

| | vorher | nachher |
|---|---|---|
| verfügbar | 815 MB | ~1,24 GB |
| Swap belegt | 2047 MB | ~1,3 GB |

## Sicherheitshinweis

Die simple-api ist auf **`127.0.0.1`** gebunden und damit nur lokal erreichbar
(Node-RED, `lights.py`, `dispatcharr-lights.py`). Aus dem LAN ist Port 8087
geschlossen; `auth` bleibt aus, weil kein entfernter Zugriff mehr möglich ist.

```bash
# Prüfen
ss -lntp | grep 8087                 # muss 127.0.0.1:8087 zeigen
curl -s 127.0.0.1:8087/get/0_userdata.0.iobroker.admin
```

## Rollback

```bash
# Node-RED-Flows
sudo docker stop nodered
sudo cp /media/ssd/nodered/flows.json.bak-<zeitstempel> /media/ssd/nodered/flows.json
sudo docker start nodered
```

Skript `script.js.Alexa.Command`: Listener auf `0_userdata.0.command.id` und die
Funktionen `AdminAn`/`AdminAus` entfernen (oder Objekt aus dem ioBroker-Backup
zurückholen). Admin wieder dauerhaft aktivieren:

```bash
docker exec iobroker iobroker object set system.adapter.admin.0 common.enabled=true
```
