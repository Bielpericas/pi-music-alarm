"""Reproducción de alarmas en Spotify, usando SpotifyClient.

`SpotifyAlarmPlayer.play(uri)` hace el flujo completo de una alarma Spotify:
dispositivo seleccionado -> transferir -> reproducir el contenido. Nunca lanza
excepciones: devuelve True si Spotify aceptó la orden y False si algo falló
(el motivo queda en el log), para que quien lo llame use el WAV de respaldo.
"""
import logging

import db
from spotify_client import SpotifyError

logger = logging.getLogger("alarms")

DEVICE_ID_KEY = "spotify_device_id"
DEVICE_NAME_KEY = "spotify_device_name"


class SpotifyAlarmPlayer:
    def __init__(self, client, database):
        self.client = client
        self.database = database

    def play(self, uri):
        try:
            if not self.client.is_configured:
                raise SpotifyError("Spotify no está configurado (.env).")
            device_id = db.read_setting(self.database, DEVICE_ID_KEY)
            if not device_id:
                raise SpotifyError("No hay ningún dispositivo de Spotify seleccionado.")
            self.client.transfer_playback(device_id, play=False)
            self.client.play(device_id, uri=uri)
        except Exception as exc:  # SpotifyError, ValueError o cualquier imprevisto
            logger.warning("Spotify falló (%s): %s", uri, exc)
            return False
        logger.info("Spotify reproduciendo %s", uri)
        return True
