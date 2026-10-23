# Wake-on-LAN-Dienst (Pi)

Kleiner Dienst mit genau einem Endpunkt: `POST /wake`. Er baut bei Bedarf die
Firmen-WireGuard-Verbindung `wg-firma` auf, ruft den von der IT betriebenen
WoL-Relay auf und trennt die Verbindung wieder, wenn er sie selbst gestartet hat.

## Datenfluss

```
Client --HTTPS--> Caddy (Let's Encrypt)
                  --> 127.0.0.1:8891  wake-service (Token-Auth, Cooldown, Lock)
                        --> wg-quick up wg-firma (nur wenn nicht aktiv)
                        --> HTTP GET http://192.168.10.146/wol.php?mac=<fix>
                        --> wg-quick down wg-firma (nur wenn selbst gestartet)
```

Die MAC-Adresse ist fest konfiguriert und kann nicht per HTTP uebergeben werden.
Der Dienst nimmt keine Parameter, keine Shell-Befehle und keine Zielangaben an.

## Statuscodes

| HTTP | Status | Bedeutung |
|---|---|---|
| 200 | `wake_sent` | Relay hat das Magic Packet angenommen |
| 200 | `already_running` | Ziel war bereits per TCP erreichbar, kein Wake |
| 200 | `dry_run` | Alles ausser dem Relay-Aufruf ausgefuehrt |
| 401 | `unauthorized` | Token fehlt oder falsch |
| 400 | `bad_request` | Request-Body nicht erlaubt |
| 404 | `not_found` | Unbekannter Pfad/Query |
| 405 | `method_not_allowed` | Nur POST /wake |
| 409 | `busy` | Ein Vorgang laeuft bereits |
| 429 | `cooldown` | Cooldown aktiv (`Retry-After`) |
| 502 | `vpn_failed` / `relay_error` | VPN-Aufbau bzw. Relay-Aufruf fehlgeschlagen |
| 504 | `vpn_timeout` | Interface/Route nach `VPN_WAIT_SECONDS` nicht bereit |

`wake_sent` belegt **nicht**, dass der PC gestartet ist – nur, dass das Relay das
Paket angenommen hat.

## Dateien

| Pfad | Inhalt |
|---|---|
| `/usr/local/bin/wake-service.py` | Programm |
| `/etc/wake/config.env` | Konfiguration inkl. Token (root:waker 0640) |
| `/etc/systemd/system/wake.service` | Unit |
| `/etc/sudoers.d/wake` | Nur `wg-quick up/down wg-firma` |
| `/etc/wireguard/wg-firma.conf` | VPN-Config (root 0600, bereits vorhanden) |

## Installation

```bash
scp -r wake rasp:/home/pi/
ssh rasp "cd /home/pi/wake && sudo bash install.sh"
sudo grep '^WAKE_TOKEN=' /etc/wake/config.env   # Token fuer den Client
```

Die Installation legt den Systembenutzer `waker`, die Konfiguration mit
zufaelligem Token und die systemd-Unit an. `DRY_RUN=1` ist voreingestellt:
Der komplette Ablauf inkl. VPN wird ausgefuehrt, nur der Relay-Aufruf entfaellt.

## Reverse Proxy (Caddy)

Im Block `holger80.dynv6.net` **vor** dem Fallback-`handle` einfuegen:

```
	handle /wake {
		reverse_proxy localhost:8891
	}
```

Danach `docker restart caddy`. Backup vorher:
`sudo cp /media/ssd/caddy/Caddyfile /media/ssd/caddy/Caddyfile.bak-$(date +%F)`.

## Betrieb

```bash
sudo systemctl status wake.service
sudo journalctl -u wake.service -f
sudo systemctl restart wake.service
```

Aufruf (Token nie in Logs/URLs, immer als Header):

```bash
curl -sS -X POST https://holger80.dynv6.net/wake \
  -H "Authorization: Bearer $TOKEN"
```

## Fehlersuche

| Symptom | Pruefung |
|---|---|
| 502 `vpn_failed` | `sudo wg-quick up wg-firma`, `sudo journalctl -u wake -n 50` |
| 504 `vpn_timeout` | `ip route get 192.168.10.146`, `ping 192.168.10.146` |
| 502 `relay_error` | `curl -m 5 -o /dev/null -w '%{http_code}' http://192.168.10.146/` |
| 429 | Cooldown abwarten (`COOLDOWN_SECONDS`) |
| Dienst startet nicht | `python3 -m py_compile /usr/local/bin/wake-service.py` |

## Rollback

```bash
sudo systemctl disable --now wake.service
sudo rm -f /etc/systemd/system/wake.service /etc/sudoers.d/wake
sudo rm -f /usr/local/bin/wake-service.py
sudo rm -rf /etc/wake
sudo userdel waker
sudo systemctl daemon-reload
```

Caddy-Aenderung zuruecknehmen (Backup zurueckkopieren) und `docker restart caddy`.
Der WireGuard-Tunnel bleibt davon unberuehrt; `/etc/wireguard/wg-firma.conf`
kann bei Bedarf separat geloescht werden.

## Tests

```bash
cd wake && python3 -m unittest discover -s tests -v
```

Abgedeckt: Token-Pruefung, falsche Methoden/Pfade/Bodies, Cooldown, parallele
Aufrufe, VPN-Fehler, VPN-Timeout, Relay-Fehler, Aufraeumen (nur selbst
gestarteter Tunnel wird beendet), Dry-Run.
