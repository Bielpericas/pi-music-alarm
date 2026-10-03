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
algo falló (el motivo queda en el log). Si PLAY quedó incierto y la pausa falla,
start_stop_pending conserva el control antes de permitir el respaldo local.
`stop()` pausa el dispositivo donde empezó a sonar.
"""
import logging
import random
import threading
import time

import db
from startup_budget import StartupBudget, StartupCancelled, StartupExpired, budget_lock, current_budget
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
DEFAULT_START_TIMEOUT = 20
STOP_CLEANUP_TIMEOUT = 2

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
                 wait=None, ready_delays=READY_DELAYS, rng=None, before_play=None,
                 start_timeout=DEFAULT_START_TIMEOUT, clock=time.monotonic, service_lock=None):
        self.client = client
        self.database = database
        self.preferred_name = preferred_name  # si no hay nombre guardado (SPOTIFY_DEVICE_NAME)
        self.retry_delays = tuple(retry_delays)
        self.ready_delays = tuple(ready_delays)
        self._interrupted = threading.Event()
        # wait(segundos) -> True si se interrumpió durante la espera.
        self._wait = wait or self._interrupted.wait
        self._device_id = None  # dispositivo donde empezó a sonar la última alarma
        self._rng = rng or random.Random()  # inyectable en tests
        self.before_play = before_play
        self.start_timeout = max(1, float(start_timeout))
        self._clock = clock
        self.service_lock = service_lock or threading.RLock()
        self.start_stop_pending = False
        self._play_sent = False

    def interrupt(self):
        """Corta los reintentos en curso (lo llama STOP / snooze sin esperar)."""
        self._interrupted.set()

    def play_interruptible(self, uri, volume=None, interrupted=None):
        return self.play(uri, volume=volume, cancelled=interrupted)

    def play(self, uri, volume=None, cancelled=None):
        """Resolver dispositivo -> transferir -> (esperar a que esté activo) ->
        volumen inicial -> reproducir, con reintentos."""
        self._interrupted.clear()
        self.start_stop_pending = False
        self._play_sent = False
        budget = StartupBudget(self.start_timeout, self._interrupted, self._clock, cancelled)
        try:
            with budget.activate(), budget_lock(self.service_lock):
                if not self.client.is_configured:
                    raise SpotifyNotConfiguredError("Spotify no está configurado (.env).")
                if self.before_play is not None:
                    self.before_play()
                self._pause(0)
                device_id = self._start(uri, volume)
        except (PlaybackInterrupted, StartupCancelled):
            logger.info("Búsqueda del dispositivo de Spotify interrumpida")
            self._silence_uncertain_start()
            return False
        except StartupExpired:
            logger.warning("Spotify: agotado el plazo total de %s s; se intenta el respaldo local",
                           self.start_timeout)
            self._silence_uncertain_start()
            return False
        except Exception as exc:  # SpotifyError, ValueError o cualquier imprevisto
            logger.warning("Spotify falló (%s): %s", uri, exc)
            self._silence_uncertain_start()
            return False
        self._device_id = device_id
        logger.info("Spotify reproduciendo %s", uri)
        return True

    def _silence_uncertain_start(self):
        if not self._play_sent:
            return
        # Una orden ya enviada no se puede retirar de Spotify. Confirmar pausa
        # antes del fallback; si falla, conservar control y reintentos de STOP.
        try:
            with StartupBudget(STOP_CLEANUP_TIMEOUT, clock=self._clock).activate(), budget_lock(self.service_lock):
                self.start_stop_pending = not self.stop()
        except (StartupCancelled, StartupExpired):
            self.start_stop_pending = True
        if self.start_stop_pending:
            logger.warning("Arranque Spotify incierto: no se confirmó la pausa; parada pendiente")

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
        # Álbum / playlist: pista inicial aleatoria, elegida una vez por cada vez
        # que suena la alarma (los reintentos usan la misma; un snooze, otra).
        position = self._random_position(uri)
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
                self._play(device["id"], uri, position)
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

    def _random_position(self, uri):
        """Índice aleatorio de pista para álbumes y playlists, o None.

        Si no se puede saber cuántas pistas hay (playlist ajena, error de red,
        permisos...), None: se empieza por la primera, como siempre.
        """
        kind = uri.split(":")[1] if uri and uri.count(":") == 2 else ""
        if kind not in ("album", "playlist"):
            return None
        try:
            self._pause(0)
            total = self.client.get_track_count(uri)
        except (StartupCancelled, StartupExpired, PlaybackInterrupted):
            raise
        except Exception as exc:
            logger.warning("No se pudo saber cuántas pistas tiene %s (%s); "
                           "se empieza por la primera", uri, exc)
            return None
        if not isinstance(total, int) or isinstance(total, bool) or total < 1:
            logger.info("Número de pistas de %s desconocido; se empieza por la primera", uri)
            return None
        position = self._rng.randrange(total)
        logger.info("Inicio aleatorio: pista %d de %d", position + 1, total)
        return position

    def _play(self, device_id, uri, position):
        """Reproduce; con posición aleatoria si la hay. Si Spotify rechaza la
        posición (400), empieza por la primera pista en vez de fallar."""
        if position is None:
            self._send_play(device_id, uri)
            return
        try:
            self._send_play(device_id, uri, offset=position)
        except SpotifyError as exc:
            if exc.status != 400:
                raise
            logger.warning("Spotify rechazó la pista inicial %d (%s); se empieza por la primera",
                           position + 1, exc)
            self._send_play(device_id, uri)

    def _send_play(self, device_id, uri, **kwargs):
        self._pause(0)
        self._device_id = device_id
        previously_sent = self._play_sent
        self._play_sent = True
        try:
            self.client.play(device_id, uri=uri, **kwargs)
        except SpotifyError as exc:
            if exc.status in (400, 401, 403, 404, 429):
                self._play_sent = previously_sent  # el rechazo no aclara una orden anterior incierta
            raise
        self._pause(0)

    def _pause(self, seconds):
        """Espera interrumpible: STOP / snooze la cortan al momento."""
        budget = current_budget()
        if budget is not None:
            budget.remaining()
            if seconds:
                budget.wait(seconds, self._wait)
        elif seconds and self._wait(seconds):
            raise PlaybackInterrupted("interrumpido")
        if self._interrupted.is_set():
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
