# Schalter-App (vormals Lichter)

Kleine PWA auf dem Pi, die ioBroker-Schalter und drei System-Schalter bedient.

| | |
|---|---|
| Datei | `/usr/local/bin/dispatcharr-lights.py` (Repo: `docs/tools/lights.py`) |
| Unit | `lights.service` (User `pi`) |
| Bindung | `127.0.0.1:8890` |
| URL | `https://holger80.dynv6.net/lights/` (Caddy) bzw. `http://192.168.178.30:8890/lights/` im LAN |
| Auth | Token aus `/usr/local/share/dispatcharr/zap.token` (im HTML eingebettet, Query `?token=`) |

## System-Schalter

Die drei Einträge stehen zusätzlich zu den Lampen in der Liste:

| Anzeige | Quelle | Ein/Aus |
|---|---|---|
| ioBroker Admin | `0_userdata.0.iobroker.admin` | simple-api `set` → JavaScript schaltet `admin.0` |
| Dienste | `containerctl` `/status` | `start-all` / `stop-all` (Sonarr, Radarr, NZBGet) |
| RDC-Agent | `containerctl` `/service/status` | `service/start|stop/rdc-agent` |
| Jellyfin | `containerctl` `/status` | `/start/jellyfin`, `/stop/jellyfin` |
| Dispatcharr | `containerctl` `/status` | `/start/dispatcharr`, `/stop/dispatcharr` |

Status wird beim Laden aus den echten Diensten gelesen, nicht aus einem Cache.

## Endpunkte

| Pfad | Zweck |
|---|---|
| `GET /lights/` | PWA |
| `GET /lights/list?token=…` | Geräte + System-Schalter mit aktuellem Wert |
| `POST /lights/set?id=…&value=…&token=…` | schalten (System-IDs: `__admin__`, `__services__`, `__rdc__`) |
| `POST /lights/say?token=…` | Ansage an alle Echos |
| `POST /lights/query?token=…` | Alexa-Geräte neu abfragen |

## Test

| Prüfung | Ergebnis |
|---|---|
| Titel | `Schalter` |
| System-Items in `/list` | `__admin__`, `__services__`, `__rdc__` mit `val` |
| `__admin__` true/false | `admin.0 is running` / `is not running` |
| `__services__` true/false | alle drei `running` / `exited` |
| `__rdc__` true/false | `active` / `inactive` |
| `__jellyfin__` true/false | `running` / `exited` |
| `__dispatcharr__` true/false | `running` / `exited` |

## Deploy/Rollback

```bash
# Deploy
sudo install -m 755 -o root -g root /home/pi/lights-new.py /usr/local/bin/dispatcharr-lights.py
sudo systemctl restart lights

# Rollback
sudo cp -p /usr/local/bin/dispatcharr-lights.py.bak-<zeitstempel> /usr/local/bin/dispatcharr-lights.py
sudo systemctl restart lights
```

Die Route `/lights/*` in Caddy bleibt unverändert; die PWA-Installation auf dem
Handy zeigt nach dem Neuladen „Schalter".
