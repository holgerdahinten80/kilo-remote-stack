# Betrieb

## Raspberry Pi

### Dienste

```bash
sudo systemctl status wake.service kilo-bridge.service rdc-agent.service
sudo journalctl -u kilo-bridge -f
sudo journalctl -u wake -n 50
sudo systemctl restart kilo-bridge wake rdc-agent
```

### Konfiguration

| Datei | Änderung wirksam nach |
|---|---|
| `/etc/wake/config.env` | `sudo systemctl restart wake` |
| `/etc/kilo-bridge/config.env` | `sudo systemctl restart kilo-bridge` |
| `/etc/wireguard/wg-firma.conf` | nächster Wake-Aufruf |
| `/media/ssd/caddy/Caddyfile` | `docker exec caddy caddy reload --config /etc/caddy/Caddyfile` |

### Tokens rotieren

```bash
# neuen Submit-Token setzen (Bridge + Wrapper)
sudo sed -i "s/^SUBMIT_TOKEN=.*/SUBMIT_TOKEN=<neu>/" /etc/kilo-bridge/config.env
printf '%s\n' '<neu>' | sudo tee /etc/kilo-submit.token
sudo chown root:rdc /etc/kilo-submit.token
sudo chmod 640 /etc/kilo-submit.token

# neuen Agent-Token setzen (Bridge + Windows-Client)
sudo sed -i "s/^AGENT_TOKEN=.*/AGENT_TOKEN=<neu>/" /etc/kilo-bridge/config.env
sudo systemctl restart kilo-bridge
# danach config.json auf A1459 aktualisieren
```

### WoL testen

```bash
curl -sS -X POST https://holger80.dynv6.net/wake \
  -H "Authorization: Bearer $WAKE_TOKEN"
```

## Firmen-PC (A1459)

### Aufgaben

```powershell
Get-ScheduledTask -TaskName KiloAgent,KiloCodeWeb | Select-Object TaskName,State
Start-ScheduledTask -TaskName KiloAgent
Stop-ScheduledTask -TaskName KiloCodeWeb
```

Beide Aufgaben laufen als `SCHLEISSHEIMER\Dahinten`, `Interactive`,
`RunLevel Limited`.

### Logs

| Log | Pfad |
|---|---|
| Agent | `%LOCALAPPDATA%\KiloAgent\state\agent.log` |
| KiloCodeWeb | Kommunikationsprotokoll in der Web-UI `http://127.0.0.1:8765` |
| Kilo-CLI | Ausgabe der Web-UI / `--print-logs` |

### Modell wählen

1. In VS Code das gewünschte Modell auswählen (landet in `recentModels[0]`).
2. KiloCodeWeb übernimmt es beim nächsten Auftrag automatisch.
3. Ohne VS-Code-Auswahl greift `DEFAULT_MODEL` (Charlie).
4. Überschreiben per Umgebungsvariable `KILO_MODEL` (z. B. für Tests).

### Modell-/Provider-Config

`C:\Users\dahinten\.config\kilo\kilo.jsonc` – Änderungen greifen beim nächsten
CLI-Aufruf, kein Neustart nötig.

### KiloCodeWeb neu starten

```powershell
Stop-ScheduledTask -TaskName KiloCodeWeb
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'KiloCodeWeb\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName KiloCodeWeb
```

## Wake-on-LAN-Ablauf

1. `POST /wake` → Wake-Dienst baut `wg-firma` auf (falls nicht aktiv).
2. Relay `192.168.10.146` sendet das Magic Packet.
3. Dienst prüft optional TCP 3389 auf `192.168.50.56` und trennt das VPN.
4. A1459 bootet (ca. 45 s bis RDP-Port offen), Auto-Login meldet `Dahinten` an.
5. `KiloCodeWeb` und `KiloAgent` starten über die Anmelde-Trigger.
6. Der Agent verbindet sich per SSE; offene Aufträge werden sofort abgearbeitet.

Messwerte aus dem Abnahmetest: Boot 45 s, Auto-Login 12:40, Agent verbunden
12:40:33, Auftrag `done` nach 29,5 s.

## Rollback

### Pi – Bridge

```bash
sudo systemctl disable --now kilo-bridge
sudo rm -f /etc/systemd/system/kilo-bridge.service /etc/kilo-submit.token
sudo rm -rf /etc/kilo-bridge /var/lib/kilo-bridge
sudo rm -f /usr/local/bin/kilo-bridge.py /usr/local/bin/kilo-submit
sudo userdel bridge
sudo systemctl daemon-reload
```

Caddy-Block `/jobs*` entfernen und `caddy reload`.

### Pi – Wake

```bash
sudo systemctl disable --now wake
sudo rm -f /etc/systemd/system/wake.service /etc/sudoers.d/wake
sudo rm -rf /etc/wake /usr/local/bin/wake-service.py
sudo userdel waker
sudo systemctl daemon-reload
```

Caddy-Block `/wake` entfernen. `/etc/wireguard/wg-firma.conf` kann bestehen
bleiben; die Firmen-IT sollte den Peer bei Bedarf löschen.

### Pi – RDC-Agent

```bash
sudo systemctl disable --now rdc-agent
sudo rm -f /etc/systemd/system/rdc-agent.service
sudo rm -rf /opt/node22 /home/rdc
sudo userdel -r rdc
sudo systemctl daemon-reload
```

Gerät zusätzlich im RDC-Dashboard entfernen.

### A1459

```powershell
Unregister-ScheduledTask -TaskName KiloAgent -Confirm:$false
Unregister-ScheduledTask -TaskName KiloCodeWeb -Confirm:$false
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\KiloAgent"
```

`C:\Tool\KiloCodeWeb.py` aus dem jeweiligen `.bak-*` wiederherstellen und den
Provider-Block aus `~/.config/kilo/kilo.jsonc` entfernen.
