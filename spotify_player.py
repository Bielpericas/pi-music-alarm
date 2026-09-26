"""Reproducción de alarmas en Spotify, usando SpotifyClient.

`SpotifyAlarmPlayer.play(uri)` hace el flujo completo de una alarma Spotify:
dispositivo seleccionado -> transferir -> reproducir el contenido. Nunca lanza
excepciones: devuelve True si Spotify aceptó la orden y False si algo falló
(el motivo queda en el log), para que quien lo llame use el WAV de respaldo.
`stop()` pausa el dispositivo donde empezó a sonar.
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
        self._device_id = None  # dispositivo donde empezó a sonar la última alarma

    def play(self, uri, volume=None):
        """Transferir -> (volumen inicial, si se indica) -> reproducir `uri`.

        Si fijar el volumen falla, se registra y se reproduce igualmente.
        """
        try:
            if not self.client.is_configured:
                raise SpotifyError("Spotify no está configurado (.env).")
            device_id = db.read_setting(self.database, DEVICE_ID_KEY)
            if not device_id:
                raise SpotifyError("No hay ningún dispositivo de Spotify seleccionado.")
            self.client.transfer_playback(device_id, play=False)
            if volume is not None:
                try:
                    self.client.set_volume(volume, device_id)
                except Exception as exc:
                    logger.warning("No se pudo fijar el volumen inicial (%s%%): %s", volume, exc)
            self.client.play(device_id, uri=uri)
        except Exception as exc:  # SpotifyError, ValueError o cualquier imprevisto
            logger.warning("Spotify falló (%s): %s", uri, exc)
            return False
        self._device_id = device_id
        logger.info("Spotify reproduciendo %s", uri)
        return True

    def stop(self):
        """Pausa el dispositivo donde sonó la alarma. Nunca lanza excepciones."""
        device_id = self._device_id
        if not device_id:
            return False
        try:
            self.client.pause(device_id)
        except Exception as exc:
            logger.warning("No se pudo pausar Spotify: %s", exc)
            return False
        logger.info("Spotify pausado")
        return True

    def set_volume(self, volume):
        """Volumen del dispositivo donde suena la alarma. Nunca lanza excepciones."""
        device_id = self._device_id
        if not device_id:
            return False
        try:
            self.client.set_volume(volume, device_id)
        except Exception as exc:
            logger.warning("No se pudo ajustar el volumen de Spotify a %s%%: %s", volume, exc)
            return False
        logger.debug("Volumen de Spotify: %s%%", volume)
        return True
