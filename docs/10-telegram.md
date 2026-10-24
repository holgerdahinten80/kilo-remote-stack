# Telegram-Benachrichtigungen

Die Bridge schickt bei Abschluss eines Auftrags eine Nachricht an Telegram.
ChatGPT selbst kann das nicht – die Meldung kommt direkt vom Pi und funktioniert
damit auch, wenn der Chat längst geschlossen ist.

## Nachrichten

| Ereignis | Nachricht |
|---|---|
| Auftrag fertig | `✅ Kilo-Auftrag bed6dfd9… done (12.4 s)` + Ergebnistext |
| Auftrag fehlgeschlagen | `❌ Kilo-Auftrag … failed (… s)` + Grund |
| optional Annahme | `📥 Kilo-Auftrag … angenommen` (`TELEGRAM_ON_SUBMIT=1`) |

Der Ergebnistext wird auf `TELEGRAM_RESULT_CHARS` (Standard 400) gekürzt.
Der Versand läuft asynchron; ein Telegram-Fehler beeinflusst den Auftrag nicht.

## Konfiguration

`/etc/kilo-bridge/config.env` (root:bridge 0640):

```ini
TELEGRAM_BOT_TOKEN=<BotFather-Token>
TELEGRAM_CHAT_ID=<Chat-ID>
TELEGRAM_ON_SUBMIT=0
TELEGRAM_RESULT_CHARS=400
TELEGRAM_TIMEOUT_SECONDS=10
TELEGRAM_API_BASE=https://api.telegram.org
```

Ohne Token oder Chat-ID ist die Funktion automatisch deaktiviert.

## Einrichtung

1. `@BotFather` → `/newbot` → Token notieren.
2. Den Bot im Chat **einmal anschreiben** (`/start`), sonst verweigert Telegram
   den Versand.
3. Chat-ID ermitteln: eigene ID über `@userinfobot`, bei Gruppen die ID mit
   führendem Minus.
4. Werte in die Config eintragen, `sudo systemctl restart kilo-bridge`.

## Testen

```bash
sudo systemctl restart kilo-bridge
# Testnachricht direkt aus der Bridge-Config:
sudo python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("bridge", "/usr/local/bin/kilo-bridge.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cfg = module.Config.from_file()
notifier = module.TelegramNotifier(cfg.telegram_token, cfg.telegram_chat,
                                   cfg.telegram_api_base, cfg.telegram_timeout)
print(notifier.send("Test vom Kilo-Remote-Stack"))
PY
```

Oder einen echten Auftrag einreichen und die Nachricht abwarten.

## Sicherheit

- Token liegt ausschließlich in `/etc/kilo-bridge/config.env` und wird nicht
  geloggt (geprüft: 0 Treffer in `bridge.jsonl`).
- Fehler werden nur als Ausnahmetyp protokolliert, nie mit URL oder Token.
- Bei Verdacht auf Kompromittierung: BotFather → `/revoke` → neuen Token in die
  Config, `restart`.
- Wer den Bot kennt, kann ihn anschreiben, aber ohne Token nichts senden.

## Deaktivieren

```bash
sudo sed -i 's/^TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN=/' /etc/kilo-bridge/config.env
sudo systemctl restart kilo-bridge
```
