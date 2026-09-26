"""Reproducción de alarmas en Spotify, usando SpotifyClient.

`SpotifyAlarmPlayer.play(uri)` hace el flujo completo de una alarma Spotify:

1. Resolver el dispositivo (ver `choose_device`): el ID guardado si sigue
   disponible; si no, el dispositivo cuyo nombre coincide con el preferido
   (p. ej. "Groove"), y se guarda su ID nuevo. Nunca se usa "el dispositivo
   que estuviera sonando antes".
2. Transferir la reproducción a ese dispositivo.
3. Fijar el volumen inicial (si se indica).
4. Reproducir el contenido.

Si el dispositivo no aparece (Raspotify reiniciándose, aún no anunciado, red
inestable...), se reintenta unos segundos (RETRY_DELAYS). Cada reintento
repite el ciclo completo, volviendo a resolver el dispositivo.

Estado "frío" de Groove: tras reiniciar Raspotify el dispositivo aparece en
la lista, pero la primera orden puede fallar con 403 "Player command failed:
Restriction violated" (reason UNKNOWN) hasta que se activa. Ese 403 concreto
se trata como temporal y se reintenta; además, tras transferir se comprueba
(READY_DELAYS) que el dispositivo ya está activo y no restringido antes de
reproducir. Los demás 403 (Premium, permisos...) y los errores que no se
arreglan esperando (autenticación, configuración) no se reintentan.
`interrupt()` corta cualquier espera al momento (STOP / snooze).

Nunca lanza excepciones: devuelve True si Spotify aceptó la orden y False si
algo falló (el motivo queda en el log), para que se use el WAV de respaldo.
`stop()` pausa el dispositivo donde empezó a sonar.
"""
import logging
import threading

import db
from spotify_client import (
    SpotifyAuthError,
    SpotifyError,
    SpotifyForbiddenError,
    SpotifyNotConfiguredError,
    SpotifyRateLimitError,
)

logger = logging.getLogger("alarms")

DEVICE_ID_KEY = "spotify_device_id"
DEVICE_NAME_KEY = "spotify_device_name"

# Espera antes de cada intento: inmediato, +2 s, +4 s, +6 s (12 s como máximo).
RETRY_DELAYS = (0, 2, 4, 6)

# Tras transferir: comprobar que el dispositivo está activo y no restringido
# antes de reproducir (inmediato, +1 s, +1 s). Si no lo está, se prueba igual.
READY_DELAYS = (0, 1, 1)

# Errores que no se arreglan esperando unos segundos: no se reintentan.
# (Un 429 que llega hasta aquí ya trae un Retry-After largo: tampoco.)
NON_RETRYABLE = (SpotifyAuthError, SpotifyForbiddenError, SpotifyNotConfiguredError,
                 SpotifyRateLimitError)


class DeviceUnavailable(SpotifyError):
    """El dispositivo preferido no aparece (de momento). Se puede reintentar."""


class DeviceConflict(SpotifyError):
    """Varios dispositivos con el nombre preferido y ninguno es el guardado."""


class PlaybackInterrupted(SpotifyError):
    """STOP / snooze mientras se buscaba el dispositivo."""


def is_cold_start_restriction(exc):
    """403 "Restriction violated" con reason UNKNOWN (o sin reason): Groove
    recién arrancado que aún no acepta órdenes. Es temporal: se reintenta."""
    if not isinstance(exc, SpotifyForbiddenError):
        return False
    text = (getattr(exc, "api_message", None) or str(exc)).casefold()
    reason = (getattr(exc, "reason", None) or "UNKNOWN").upper()
    return "restriction violated" in text and reason == "UNKNOWN"


def is_retryable(exc):
    if is_cold_start_restriction(exc):
        return True
    return not isinstance(exc, (DeviceConflict,) + NON_RETRYABLE)


def is_ready(device):
    return bool(device.get("is_active")) and not device.get("is_restricted")


def same_name(a, b):
    return (a or "").strip().casefold() == (b or "").strip().casefold()


def choose_device(devices, saved_id, preferred_name):
    """Elige el dispositivo para la alarma entre los disponibles ahora.

    1. El del ID guardado, si sigue en la lista.
    2. Si no, el único cuyo nombre coincide con `preferred_name` (sin
       distinguir mayúsculas).
    Lanza DeviceUnavailable si no hay ninguno y DeviceConflict si hay varios
    con ese nombre (no se elige uno al azar).
    """
    usable = [d for d in devices if d.get("id")]
    for device in usable:
        if saved_id and device["id"] == saved_id:
            return device
    if not preferred_name:
        raise DeviceUnavailable(
            "el dispositivo guardado no está disponible y no se conoce su nombre")
    matches = [d for d in usable if same_name(d.get("name"), preferred_name)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise DeviceConflict(
            f"hay {len(matches)} dispositivos llamados «{preferred_name}» y ninguno es "
            "el guardado; no se elige uno al azar")
    names = ", ".join(f"«{d.get('name')}»" for d in usable) or "ninguno"
    raise DeviceUnavailable(f"«{preferred_name}» no aparece (disponibles: {names})")


class SpotifyAlarmPlayer:
    def __init__(self, client, database, preferred_name="", retry_delays=RETRY_DELAYS,
                 wait=None, ready_delays=READY_DELAYS):
        self.client = client
        self.database = database
        self.preferred_name = preferred_name  # si no hay nombre guardado (SPOTIFY_DEVICE_NAME)
        self.retry_delays = tuple(retry_delays)
        self.ready_delays = tuple(ready_delays)
        self._interrupted = threading.Event()
        # wait(segundos) -> True si se interrumpió durante la espera.
        self._wait = wait or self._interrupted.wait
        self._device_id = None  # dispositivo donde empezó a sonar la última alarma

    def interrupt(self):
        """Corta los reintentos en curso (lo llama STOP / snooze sin esperar)."""
        self._interrupted.set()

    def play(self, uri, volume=None):
        """Resolver dispositivo -> transferir -> (esperar a que esté activo) ->
        volumen inicial -> reproducir, con reintentos."""
        self._interrupted.clear()
        try:
            if not self.client.is_configured:
                raise SpotifyNotConfiguredError("Spotify no está configurado (.env).")
            device_id = self._start(uri, volume)
        except PlaybackInterrupted:
            logger.info("Búsqueda del dispositivo de Spotify interrumpida")
            return False
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

    # --- Resolución del dispositivo ---

    def _start(self, uri, volume):
        """Ciclo completo con reintentos. Devuelve el ID del dispositivo."""
        saved_id = db.read_setting(self.database, DEVICE_ID_KEY)
        saved_name = db.read_setting(self.database, DEVICE_NAME_KEY)
        name = saved_name or self.preferred_name
        if not saved_id and not name:
            raise SpotifyError("No hay ningún dispositivo de Spotify seleccionado.")

        label = f"«{name}»" if name else "el dispositivo guardado"
        attempts = len(self.retry_delays)
        last_error = None
        for attempt, delay in enumerate(self.retry_delays, 1):
            self._pause(delay)
            try:
                # Cada intento vuelve a resolver el dispositivo (ID guardado / nombre).
                device = choose_device(self.client.get_devices(), saved_id, name)
                self.client.transfer_playback(device["id"], play=False)
                self._remember(device, saved_id, saved_name)
                saved_id, saved_name = device["id"], device.get("name") or saved_name
                if not is_ready(device):
                    self._wait_until_ready(device["id"], label)
                if volume is not None:
                    try:
                        self.client.set_volume(volume, device["id"])
                    except Exception as exc:
                        logger.warning("No se pudo fijar el volumen inicial (%s%%): %s",
                                       volume, exc)
                self.client.play(device["id"], uri=uri)
                return device["id"]
            except PlaybackInterrupted:
                raise
            except SpotifyError as exc:
                if not is_retryable(exc):
                    logger.warning("Spotify: %s. No se reintenta.", exc)
                    raise
                last_error = exc
                if is_cold_start_restriction(exc):
                    logger.warning("%s aún no acepta órdenes (403 Restriction violated); "
                                   "intento %d/%d", label, attempt, attempts)
                else:
                    logger.warning("Buscando %s (intento %d/%d): %s",
                                   label, attempt, attempts, exc)
        raise DeviceUnavailable(
            f"{label} no está disponible tras {attempts} intentos: {last_error}")

    def _pause(self, seconds):
        """Espera interrumpible: STOP / snooze la cortan al momento."""
        if (seconds and self._wait(seconds)) or self._interrupted.is_set():
            raise PlaybackInterrupted("interrumpido")

    def _wait_until_ready(self, device_id, label):
        """Tras transferir, espera (poco) a que el dispositivo figure activo y no
        restringido. Si no llega a estarlo, se intenta reproducir igualmente."""
        for delay in self.ready_delays:
            self._pause(delay)
            current = next((d for d in self.client.get_devices() if d.get("id") == device_id),
                           None)
            if current is None:
                raise DeviceUnavailable(f"{label} ha desaparecido tras transferir")
            if is_ready(current):
                return
        if self.ready_delays:
            logger.info("%s aún no figura como activo; se intenta reproducir igualmente", label)

    def _remember(self, device, saved_id, saved_name):
        """Guarda el ID y el nombre actuales del dispositivo si han cambiado."""
        if device["id"] != saved_id:
            logger.info("Dispositivo «%s» encontrado por nombre; ID actualizado (%s -> %s)",
                        device.get("name"), saved_id, device["id"])
            db.write_setting(self.database, DEVICE_ID_KEY, device["id"])
        if device.get("name") and device.get("name") != saved_name:
            db.write_setting(self.database, DEVICE_NAME_KEY, device["name"])
