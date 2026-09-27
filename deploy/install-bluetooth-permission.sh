#!/usr/bin/env bash
# Permite que pi-music-alarm pare y arranque el reproductor Bluetooth
# (bluealsa-aplay) sin sudo: instala una regla de polkit limitada a
# start/stop de esa unidad y a tu usuario.
#
# Uso (desde cualquier carpeta, con tu usuario normal, NO con sudo):
#   bash deploy/install-bluetooth-permission.sh
#
# Opcional: SERVICE_USER=otro_usuario BLUETOOTH_SERVICE=otro-servicio bash deploy/...
# Requiere polkit con reglas JavaScript (>= 0.106; Raspberry Pi OS Bookworm o posterior).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE="${BLUETOOTH_SERVICE:-bluealsa-aplay}"
[[ "$SERVICE" == *.* ]] || SERVICE="$SERVICE.service"
TEMPLATE="$APP_DIR/deploy/pi-music-alarm-bluetooth.rules.template"
TARGET="/etc/polkit-1/rules.d/50-pi-music-alarm-bluetooth.rules"

if [[ "$SERVICE_USER" == "root" ]]; then
    echo "Lanza el script con el usuario del servicio (sin sudo)." >&2
    exit 1
fi
if [[ ! -d /etc/polkit-1/rules.d ]]; then
    echo "No existe /etc/polkit-1/rules.d: instala polkitd (sudo apt install polkitd)." >&2
    exit 1
fi

echo "Instalando $TARGET"
echo "  usuario: $SERVICE_USER"
echo "  unidad:  $SERVICE (solo start y stop)"

sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@UNIT@|$SERVICE|g" "$TEMPLATE" \
    | sudo tee "$TARGET" > /dev/null
sudo chmod 644 "$TARGET"   # polkitd lee rules.d al momento, sin reiniciar nada

echo
echo "Listo. Prueba (sin sudo, no debe pedir contraseña):"
echo "  systemctl --no-ask-password stop $SERVICE && systemctl --no-ask-password start $SERVICE"
