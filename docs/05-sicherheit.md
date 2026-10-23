# Sicherheit

## Grundsätze

- Kein Dienst nimmt frei wählbare Kommandos, Pfade, IPs oder MACs aus HTTP-Anfragen an.
- Alle Geheimnisse liegen außerhalb des Repos, mit restriktiven Dateirechten.
- Jede Komponente läuft mit minimalen Rechten und eigenem Benutzer.
- Logs enthalten keine Tokens, keine Instruction-Inhalte und keine API-Keys.

## Rechte-Modell

| Komponente | Benutzer | Rechte |
|---|---|---|
| Wake-Dienst | `waker` | nur `wg-quick up/down wg-firma` per sudoers |
| Bridge | `bridge` | Schreibzugriff nur auf `/var/lib/kilo-bridge` |
| RDC-Agent | `rdc` | Shell als `rdc`, kein sudo, keine docker-Gruppe |
| Windows-Client | `Dahinten` | normaler Benutzer, keine Adminrechte |
| Kilo Code | `Dahinten` | Workspace-Permissions beschränken Tools |

`pi` ist Mitglied der `docker`-Gruppe und damit faktisch root-äquivalent –
deshalb läuft der RDC-Agent bewusst nicht als `pi`.

## Tokens

| Token | Rechte | Ablage |
|---|---|---|
| Submit | nur `POST /jobs`, rate-limited | `/etc/kilo-submit.token` (root:rdc 0640) |
| Agent | Stream, Ack, Complete, Status | `/etc/kilo-bridge/config.env` (root:bridge 0640) |
| Wake | nur `POST /wake` | `/etc/wake/config.env` (root:waker 0640); für `rdc` zusätzlich `/etc/wake.token` (0640) |

Die Bridge speichert Digests, vergleicht mit `hmac.compare_digest` und protokolliert
weder Token noch Payload.

Bekannte Einschränkung: Der RDC-Agent kann als `rdc` den Submit-Token lesen. Das
ist unvermeidbar, wenn ChatGPT den Wrapper aufrufen soll; der Token erlaubt
deshalb ausschließlich das Einreichen von Aufträgen.

## Was der Dienst bewusst nicht kann

- keine Shell-Ausführung durch die Bridge
- keine freien Parameter im Auftrag (nur `instruction` als Text)
- kein Inbound-Port im Firmennetz
- keine Remote-Shell auf dem Firmen-PC – der Client startet ausschließlich
  `kilo run` im festgelegten Workspace

## Grenzen, die technisch nicht erzwingbar sind

- **Kilo Code handelt mit Benutzerrechten.** Ein Auftrag kann alles tun, was
  `Dahinten` darf, soweit die Permission-Config es zulässt. Ein harter
  „nur dieses Projekt"-Zwang ist nur über Windows-ACLs, Kilo-Permissions und
  Unternehmensrichtlinien erreichbar.
- **Prompt-Injection** im Auftragstext ist möglich. Deshalb: festes Schema,
  Längenlimit, Client-seitige Prüfung, restriktive Workspace-Permissions.
- **Auto-Login** speichert die Anmeldedaten auf der Maschine; wer physisch
  Zugriff hat, erhält eine aktive Sitzung. Alternative: S4U-Task ohne
  gespeichertes Passwort.
- **RDC-Relay** ist ein Drittanbieter-Dienst (Beta) mit Shell-Zugriff als `rdc`.
  Datenschutz-/Policy-Frage, unabhängig von diesem Stack.

## API-Key des LLM-Providers

Der Key liegt in `C:\Users\dahinten\.config\kilo\kilo.jsonc`
(ACL nur `Dahinten`) und zusätzlich in `~/.local/share/kilo/auth.json`.
Er wird nie vom Pi oder von der Bridge gelesen. Nach einer Rotation beide
Stellen aktualisieren.

## Checkliste vor Produktivbetrieb

- [ ] Firmen-IT hat den WireGuard-Peer und den RDC-Einsatz freigegeben
- [ ] API-Key rotiert und dokumentiert abgelegt
- [ ] Submit-/Agent-/Wake-Token sind individuell und ≥ 32 Zeichen
- [ ] `kilo-work` enthält keine vertraulichen Dateien
- [ ] Auto-Login ist policy-konform (sonst S4U)
- [ ] Backups von `Caddyfile` und `KiloCodeWeb.py` vorhanden
- [ ] Logrotation/Größe von `agent.log` beobachtet
