# ioBroker/Node-RED ohne Admin-Abhängigkeit

Ziel: Der ioBroker-Admin (Web-UI, ~126 MB) muss nicht dauerhaft laufen. Node-RED
schreibt States deshalb nicht mehr über die WebSocket-Verbindung auf Port 8081,
sondern über die **simple-api** auf Port 8087.

## Vorher/Nachher

| | vorher | nachher |
|---|---|---|
| Node-RED → ioBroker | `node-red-contrib-iobroker` (WebSocket 8081) | 18 × `http request` an `127.0.0.1:8087` |
| Admin-Adapter | dauerhaft an (~126 MB) | aus, per Kommando einschaltbar |
| Objekt-States | `iobout` schrieb Objekte | simple-api schreibt Objekte als `{"val":…, "ack":false}` |

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
http://127.0.0.1:8087/set/javascript.0.Schalter.Command?value={{{payload}}}
```

`payload` ist bei den Schaltern ein Boolean (aus dem Subflow `ConvertToBool`).

## Command-Kanal

Das VSH-Gerät `Command` (DIMMER_SWITCH) liefert `{powerState, brightness}`.
Node-RED verpackt das als vollständiges State-Objekt und schreibt es in den
bestehenden State:

```js
msg.payload = encodeURIComponent(JSON.stringify({ val: msg.payload, ack: false }));
```

Ziel: `javascript.0.Schalter.Command` (Typ `object`).

`script.js.Alexa.Command` wertet den State ausschließlich über

```js
on({ id: "javascript.0.Schalter.Command" }, obj => { … })
```

aus. Es gibt **keinen** zusätzlichen State wie `0_userdata.0.command.id`.

| ID | Funktion |
|---|---|
| 1 | Saugen |
| 2 | Wischen |
| 3 | TV an |
| 4 | Video an |
| 5 | Admin – an/aus über `powerState` |
| 6 | Dienste (Sonarr+Radarr+NZBGet) – an/aus über `powerState` |
| 7 | RDC-Agent – an/aus über `powerState` |

Ein Kommando pro Ziel: `powerState=ON` schaltet ein, `OFF` schaltet aus
(`active = obj.state.val.powerState == "ON"`).

Aufruf über Alexa: dem Gerät `Command` die entsprechende Helligkeit in Prozent
zuweisen, z. B. 5 % = Admin an. Alternativ eine Alexa-Routine anlegen.

Direkt (Skript/curl):

```bash
curl -s "http://127.0.0.1:8087/set/javascript.0.Schalter.Command?value=$( \
  python3 -c 'import urllib.parse,json;print(urllib.parse.quote(json.dumps({"val":{"powerState":"ON","brightness":5},"ack":False})))')"
```

## Container-Steuerung

Die drei Container laufen nicht in ioBroker, deshalb gibt es einen kleinen
Helferdienst auf dem Pi:

| | |
|---|---|
| Programm | `/usr/local/bin/containerctl` (Python, stdlib) |
| Unit | `/etc/systemd/system/containerctl.service` (User `pi`, docker-Gruppe) |
| Bindung | `127.0.0.1:8894` |
| Allowlist | Container: `sonarr`, `radarr`, `nzbget`; Dienst: `rdc-agent` |
| Endpunkte | `GET /status`, `/start-all`, `/stop-all` (einzeln: `/start/<name>`, `/stop/<name>`) |
| Dienst-Endpunkte | `GET /service/status`, `/service/start/rdc-agent`, `/service/stop/rdc-agent` |
| sudoers | `/etc/sudoers.d/containerctl`: nur `systemctl start/stop rdc-agent` |

Ablauf: Alexa → VSH `Command` → Node-RED → `javascript.0.Schalter.Command` →
ioBroker-JavaScript → `curl http://127.0.0.1:8894/stop-all` → `docker stop`.

Getestet: ID 8 → alle drei `exited`; ID 7 → alle `running`. Fremde Container
werden abgelehnt (`{"error":"container not allowed"}`).

## Automatischer Neustart bei Speicherwachstum

`iobroker-restart-check` (systemd-Timer täglich 04:00) prüft die RSS des
js-controllers und startet den Container nur neu, wenn mehr als 500 MB belegt
sind. Ergebnis beim Test: 641 MB → Neustart → 102 MB.

```bash
systemctl list-timers iobroker-restart.timer
journalctl -t iobroker-restart -n 10
```

## Tests

| Prüfung | Ergebnis |
|---|---|
| `curl /set/...?value=true` (Boolean) | `{"value":true,"val":true}` |
| Objekt-State per simple-api | nur als `{"val":{…},"ack":false}` akzeptiert |
| ID 99 | Log `Invalid command!`, keine Aktion |
| ID 5 + ON / OFF | `admin.0 is running` / `is not running` |
| ID 6 + ON / OFF | alle drei `running` / `exited` |
| ID 7 + ON / OFF | `rdc-agent` `active` / `inactive` |
| Node-RED-Log auf 8081 | 0 Verbindungen |
| Bindung simple-api | `127.0.0.1:8087`, aus dem LAN nicht erreichbar |
| Flows | 18 `http request`, 0 `iob*`-Knoten |

## Speicher

| | vorher | nachher |
|---|---|---|
| verfügbar | 815 MB | ~1,6 GB (Admin aus, Container aus) |
| Swap belegt | 2047 MB | ~1,0 GB |

## Sicherheitshinweis

Die simple-api ist auf `127.0.0.1` gebunden und nur lokal erreichbar (Node-RED,
`lights.py`, `dispatcharr-lights.py`). Aus dem LAN ist Port 8087 geschlossen;
`auth` bleibt aus, weil kein entfernter Zugriff möglich ist.

## Rollback

```bash
# Node-RED-Flows
sudo docker stop nodered
sudo cp /media/ssd/nodered/flows.json.bak-<zeitstempel> /media/ssd/nodered/flows.json
sudo docker start nodered
```

Admin wieder dauerhaft aktivieren:

```bash
docker exec iobroker iobroker object set system.adapter.admin.0 common.enabled=true
```
