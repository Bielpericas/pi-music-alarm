#!/usr/bin/env bash
# Instala pi-music-alarm como servicio systemd que arranca con la Raspberry.
#
# Uso (desde cualquier carpeta, con tu usuario normal, NO con sudo):
#   bash deploy/install-service.sh
#
# Opcional: SERVICE_USER=otro_usuario bash deploy/install-service.sh
#
# Rellena la plantilla con tu usuario y la carpeta real del repositorio, la
# copia a /etc/systemd/system/ (pide la contraseña de sudo) y la activa.
set -euo pipefail

SERVICE_NAME="pi-music-alarm"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
TEMPLATE="$APP_DIR/deploy/$SERVICE_NAME.service.template"
TARGET="/etc/systemd/system/$SERVICE_NAME.service"

if [[ "$SERVICE_USER" == "root" ]]; then
    echo "No ejecutes el servicio como root. Lanza el script con tu usuario (sin sudo)." >&2
    exit 1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "No existe $APP_DIR/.venv. Crea el entorno virtual primero (ver README)." >&2
    exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "Aviso: no existe $APP_DIR/.env (cp .env.example .env y rellénalo)." >&2
fi

echo "Instalando $TARGET"
echo "  usuario: $SERVICE_USER:$SERVICE_GROUP"
echo "  carpeta: $APP_DIR"

sed -e "s|@USER@|$SERVICE_USER|g" \
    -e "s|@GROUP@|$SERVICE_GROUP|g" \
    -e "s|@APP_DIR@|$APP_DIR|g" \
    "$TEMPLATE" | sudo tee "$TARGET" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo
echo "Listo. Estado:"
sudo systemctl --no-pager --lines=5 status "$SERVICE_NAME" || true
