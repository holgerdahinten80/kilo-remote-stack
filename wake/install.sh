#!/bin/bash
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SERVICE_USER="waker"
CONF_DIR="/etc/wake"
CONF_FILE="$CONF_DIR/config.env"

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit sudo ausfuehren: sudo bash $0" >&2
    exit 1
fi

echo "== Benutzer =="
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "Benutzer $SERVICE_USER angelegt"
else
    echo "Benutzer $SERVICE_USER existiert"
fi

echo "== Programm =="
install -m 755 -o root -g root "$SRC/wake_service.py" /usr/local/bin/wake-service.py

echo "== Konfiguration =="
install -d -m 750 -o root -g "$SERVICE_USER" "$CONF_DIR"
if [ -f "$CONF_FILE" ]; then
    echo "$CONF_FILE existiert bereits, bleibt unveraendert"
else
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    sed "s|<zufaellig-erzeugtes-token>|$TOKEN|" "$SRC/config.example.env" > "$CONF_FILE"
    chown root:"$SERVICE_USER" "$CONF_FILE"
    chmod 640 "$CONF_FILE"
    echo "$CONF_FILE angelegt (Token zufaellig erzeugt)"
fi

echo "== Wrapper wake-now =="
if [ ! -f /etc/wake.token ]; then
    TOKEN="$(grep '^WAKE_TOKEN=' "$CONF_FILE" | cut -d= -f2-)"
    printf '%s\n' "$TOKEN" > /etc/wake.token
    chown root:rdc /etc/wake.token
    chmod 640 /etc/wake.token
    echo "/etc/wake.token angelegt"
fi
install -m 755 -o root -g root "$SRC/wake-now" /usr/local/bin/wake-now

echo "== sudoers =="
install -m 440 -o root -g root "$SRC/sudoers-wake" /etc/sudoers.d/wake
visudo -c -f /etc/sudoers.d/wake >/dev/null
echo "sudoers-Regel geprueft"

echo "== systemd =="
install -m 644 -o root -g root "$SRC/wake.service" /etc/systemd/system/wake.service
systemctl daemon-reload
systemctl enable wake.service >/dev/null
systemctl restart wake.service
sleep 2
systemctl --no-pager --full status wake.service | head -12

echo
echo "Fertig. Konfiguration: $CONF_FILE (DRY_RUN=1)"
echo "Token anzeigen: sudo grep '^WAKE_TOKEN=' $CONF_FILE"
