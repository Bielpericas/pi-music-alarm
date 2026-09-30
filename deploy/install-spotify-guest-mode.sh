#!/usr/bin/env bash
# Run as the existing Groove service user, without sudo. Root code is installed
# outside the writable checkout; no sudoers and no broad systemd permissions.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
if [[ "$SERVICE_USER" == root || ! "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ ]]; then
    echo "Usa el usuario no-root del servicio Groove." >&2
    exit 1
fi
id "$SERVICE_USER" > /dev/null
if [[ ! -d /etc/polkit-1/rules.d ]]; then
    echo "Instala polkitd: sudo apt install polkitd" >&2
    exit 1
fi
ACTUAL_USER="$(systemctl show pi-music-alarm.service --property=User --value)"
if [[ "$ACTUAL_USER" != "$SERVICE_USER" ]]; then
    echo "SERVICE_USER debe coincidir con User de pi-music-alarm.service." >&2
    exit 1
fi
sudo -v
# All compatibility checks happen before changing Raspotify or stopping music.
# -I ignores PYTHONPATH and code from the working directory.
sudo /usr/bin/python3 -I "$APP_DIR/deploy/spotify-mode-helper.py" check
sudo install -d -o root -g root -m 700 /var/lib/groove-spotify
sudo install -d -o root -g root -m 755 /run/groove-spotify /run/systemd/system
sudo install -d -o root -g root -m 755 /usr/local/libexec
sudo install -o root -g root -m 644 "$APP_DIR/deploy/spotify-mode-helper.py" /usr/local/libexec/groove-spotify-mode
sudo install -o root -g root -m 644 "$APP_DIR/deploy/groove-spotify-mode@.service" /etc/systemd/system/groove-spotify-mode@.service
sudo install -o root -g root -m 644 "$APP_DIR/deploy/groove-spotify-boot.service" /etc/systemd/system/groove-spotify-boot.service
sudo install -d -m 755 /etc/systemd/system/raspotify.service.d /etc/systemd/system/pi-music-alarm.service.d
sudo install -o root -g root -m 644 "$APP_DIR/deploy/raspotify-guest-mode.conf" /etc/systemd/system/raspotify.service.d/80-groove-spotify.conf
sudo install -o root -g root -m 644 "$APP_DIR/deploy/pi-music-alarm-spotify-guest.conf" /etc/systemd/system/pi-music-alarm.service.d/80-groove-spotify.conf
# /run is rebuilt on every boot. tmpfiles creates root-owned parents before units.
printf '%s\n' 'd /run/groove-spotify 0755 root root -' 'd /run/systemd/system 0755 root root -' \
    | sudo tee /etc/tmpfiles.d/groove-spotify.conf > /dev/null
sed "s/@USER@/$SERVICE_USER/g" "$APP_DIR/deploy/pi-music-alarm-spotify-guest.rules.template" \
    | sudo tee /etc/polkit-1/rules.d/50-pi-music-alarm-spotify-guest.rules > /dev/null
sudo chmod 644 /etc/polkit-1/rules.d/50-pi-music-alarm-spotify-guest.rules
sudo systemctl stop pi-music-alarm.service
sudo systemctl stop raspotify.service
sudo systemctl daemon-reload
# Installer/upgrades use the same transactions as the UI. Backup is created once.
sudo systemctl restart groove-spotify-boot.service
sudo systemctl start raspotify.service
sudo systemctl start groove-spotify-mode@private.service
sudo systemctl start pi-music-alarm.service
echo "Instalado. Spotify invitados ya se puede gestionar desde Groove."
echo "Backup privado: /var/lib/groove-spotify/conf.original (root, 0600)."
