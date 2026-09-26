"""Alarma que está sonando ahora: start, stop y snooze.

`AlarmPlaybackManager` es el único que sabe qué alarma suena, desde cuándo y
por dónde (Spotify, WAV local o WAV como respaldo de Spotify). El scheduler,
el botón Probar y los botones STOP / +10 MIN solo hablan con él.

Concurrencia (hilos de APScheduler + hilos de waitress):
- start/stop/snooze/forget se serializan con un único lock. Si Spotify tarda
  en arrancar, un STOP pulsado a la vez espera a que termine y luego la para.
- El estado se publica como objetos inmutables (`active`, `pending_snoozes`):
  las páginas lo leen sin coger el lock, así que nunca se quedan esperando.

Reglas:
- Si se dispara una alarma mientras suena otra, la anterior se para y suena
  la nueva ("la última gana").
- STOP es idempotente: sin alarma sonando no hace nada.
- Los snoozes viven solo en memoria: si la app se reinicia, se pierden (mejor
  eso que sonar a una hora incorrecta). Las alarmas normales no se tocan.
- Volumen (solo Spotify): se fija el volumen inicial antes de reproducir y un
  fade-in (fade.py) lo sube hasta el final. STOP, snooze o una alarma nueva
  cancelan el fade en curso; cada vez que la alarma empieza a sonar, empieza
  un fade nuevo desde el volumen inicial.
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from fade import fade_plan, start_fade

logger = logging.getLogger("alarms")

SNOOZE_MINUTES = 10
ALARM_FIELDS = ("id", "name", "time", "source", "spotify_uri")
VOLUME_FIELDS = ("volume_start", "volume_end", "fade_minutes")  # opcionales


def volume_settings(alarm):
    """(volumen_inicial, plan_del_fade) o (None, []) si la alarma no los tiene."""
    if any(alarm.get(key) is None for key in VOLUME_FIELDS):
        return None, []
    start, end = int(alarm["volume_start"]), int(alarm["volume_end"])
    seconds = int(alarm["fade_minutes"]) * 60
    if seconds <= 0:
        return end, []  # sin fade: directamente al volumen final
    return start, fade_plan(start, end, seconds)


def play_alarm_sound(alarm, player=None, spotify=None, volume=None, interrupted=None):
    """Hace sonar la alarma y devuelve "local", "spotify", "fallback" o "cancelled".

    - Fuente "local": `player.play()` (el WAV). `volume` no se usa todavía.
    - Fuente "spotify": `spotify.play(uri)` (con `volume` inicial si se da);
      si falla o no hay Spotify, el WAV. Si mientras tanto se pulsó STOP o
      snooze (`interrupted`), no suena nada: "cancelled".
    Nunca lanza excepciones: los fallos se registran.
    """
    name = alarm["name"]
    if alarm["source"] == "spotify":
        if spotify is not None and alarm["spotify_uri"]:
            try:
                uri = alarm["spotify_uri"]
                started = spotify.play(uri) if volume is None else spotify.play(uri, volume=volume)
                if started:
                    return "spotify"
            except Exception:
                logger.exception("Spotify falló al disparar «%s»", name)
        if interrupted is not None and interrupted.is_set():
            logger.info("Arranque de «%s» cancelado antes de sonar", name)
            return "cancelled"
        logger.warning("Usando el sonido local como respaldo para «%s»", name)
        outcome = "fallback"
    else:
        outcome = "local"

    if player is not None:
        try:
            player.play()
        except Exception:
            logger.exception("El reproductor falló al disparar «%s»", name)
    return outcome


def timer_schedule_once(run_at, callback):
    """Programa `callback` para `run_at` con un hilo Timer. Devuelve cancel().

    Solo se usa si no hay APScheduler (p. ej. con el scheduler desactivado).
    """
    delay = max(0.0, (run_at - datetime.now()).total_seconds())
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer.cancel


@dataclass(frozen=True)
class ActiveAlarm:
    alarm: dict           # copia de la alarma (id, name, time, source, spotify_uri)
    started_at: datetime
    via: str              # "local", "spotify", "fallback"; "connecting" mientras se
                          # busca el dispositivo; "cancelled" si STOP llegó antes de sonar
    manual: bool = False  # disparada con Probar
    snoozes: int = 0      # cuántas veces se ha pospuesto ya

    @property
    def id(self):
        return self.alarm["id"]


@dataclass(frozen=True)
class PendingSnooze:
    alarm: dict
    run_at: datetime
    snoozes: int


class StopResult(NamedTuple):
    active: ActiveAlarm   # la alarma que se ha parado
    silenced: bool        # False si no se pudo parar el sonido (p. ej. Spotify sin red)


class AlarmPlaybackManager:
    def __init__(self, player, spotify=None, schedule_once=None,
                 clock=datetime.now, snooze_minutes=SNOOZE_MINUTES, fader=None):
        self.player = player                  # AudioPlayer local (WAV)
        self.spotify = spotify                # SpotifyAlarmPlayer o None
        self.schedule_once = schedule_once or timer_schedule_once
        self.fader = fader or start_fade      # fader(set_volume, plan) -> objeto con cancel()
        self._fade = None                     # fade-in en curso (solo uno a la vez)
        self.snooze_minutes = snooze_minutes
        self._clock = clock
        self._lock = threading.RLock()
        self._active: Optional[ActiveAlarm] = None
        self._starting = None                 # Event del start en curso (para interrumpirlo)
        self._pending = {}                    # alarm_id -> (PendingSnooze, cancel)
        self._pending_view = ()

    # --- Estado (lectura sin lock: son referencias a objetos inmutables) ---

    @property
    def active(self):
        return self._active

    @property
    def pending_snoozes(self):
        return self._pending_view

    # --- Acciones ---

    def start(self, alarm, manual=False, snoozes=0):
        """Hace sonar `alarm`, sustituyendo a la que sonara. Devuelve cómo suena."""
        keys = set(alarm.keys())
        alarm = {key: alarm[key] for key in ALARM_FIELDS + VOLUME_FIELDS
                 if key in ALARM_FIELDS or key in keys}
        with self._lock:
            self._cancel_fade()  # el fade de la alarma anterior, si lo hubiera
            suffix = " (prueba manual)" if manual else " (pospuesta)" if snoozes else ""
            logger.info("ALARMA ACTIVADA: %s%s", alarm["name"], suffix)

            # Si esta misma alarma tenía un snooze pendiente, queda obsoleto.
            self._cancel_pending(alarm["id"])
            previous = self._active
            if previous is not None:
                logger.info("«%s» sustituye a «%s», que estaba sonando",
                            alarm["name"], previous.alarm["name"])
                if previous.via != "spotify":
                    self._stop_local()
            started_at = self._clock()
            starting = threading.Event()
            self._starting = starting
            if alarm["source"] == "spotify":
                # Visible (con STOP) mientras se busca el dispositivo y se reintenta.
                self._active = ActiveAlarm(alarm, started_at, "connecting", manual, snoozes)
            initial_volume, plan = volume_settings(alarm)
            try:
                via = play_alarm_sound(alarm, self.player, self.spotify,
                                       volume=initial_volume, interrupted=starting)
            finally:
                self._starting = None
            # Si la anterior sonaba en Spotify y la nueva no la ha reemplazado
            # allí, se pausa. (Pausar y luego reproducir en Spotify podría
            # llegar desordenado, por eso no se pausa si la nueva es Spotify.)
            if previous is not None and previous.via == "spotify" and via != "spotify":
                self._stop_spotify()

            self._active = ActiveAlarm(alarm, started_at, via, manual, snoozes)
            if via == "spotify" and plan:
                self._start_fade(plan)
            return via

    def stop(self):
        """Para la alarma que suena. Devuelve StopResult, o None si no sonaba nada."""
        self._interrupt_start()
        with self._lock:
            active = self._active
            if active is None:
                return None
            self._active = None
            self._cancel_fade()  # antes de pausar: que no suba el volumen tras STOP
            if active.via == "spotify":
                silenced = self._stop_spotify()
            elif active.via in ("local", "fallback"):
                silenced = self._stop_local()
            else:
                silenced = True  # "cancelled": no llegó a sonar nada
            logger.info("ALARMA DETENIDA: %s", active.alarm["name"])
            return StopResult(active, silenced)

    def snooze(self, minutes=None):
        """Para la alarma y la vuelve a disparar dentro de N minutos.

        Devuelve el PendingSnooze creado, o None si no sonaba nada.
        """
        self._interrupt_start()
        with self._lock:
            result = self.stop()
            if result is None:
                return None
            active = result.active
            run_at = self._clock() + timedelta(minutes=minutes or self.snooze_minutes)
            pending = self._schedule(active.alarm, run_at, active.snoozes + 1)
            logger.info("ALARMA POSPUESTA: %s hasta las %s",
                        active.alarm["name"], run_at.strftime("%H:%M"))
            return pending

    def cancel_snooze(self, alarm_id):
        """Cancela el snooze pendiente de una alarma. Devuelve True si había uno."""
        with self._lock:
            had_snooze = alarm_id in self._pending
            self._cancel_pending(alarm_id)
            if had_snooze:
                logger.info("Snooze cancelado para la alarma %s", alarm_id)
            return had_snooze

    def forget(self, alarm_id):
        """La alarma se ha borrado: cancela su snooze y la para si está sonando."""
        with self._lock:
            self._cancel_pending(alarm_id)
            if self._active is not None and self._active.id == alarm_id:
                self.stop()

    # --- Internos ---

    def _interrupt_start(self):
        """Sin coger el lock: si un start está buscando el dispositivo de
        Spotify (reintentos), lo corta al momento para que STOP/snooze no
        tengan que esperar hasta 12 s."""
        starting = self._starting
        if starting is None:
            return
        starting.set()
        if self.spotify is not None:
            try:
                self.spotify.interrupt()
            except Exception:
                logger.exception("No se pudo interrumpir la búsqueda del dispositivo")

    def _start_fade(self, plan):
        try:
            self._fade = self.fader(self.spotify.set_volume, plan)
        except Exception:
            logger.exception("No se pudo iniciar el fade-in; la alarma sigue sonando")
            self._fade = None

    def _cancel_fade(self):
        fade, self._fade = self._fade, None
        if fade is not None:
            fade.cancel()

    def _schedule(self, alarm, run_at, snoozes):
        self._cancel_pending(alarm["id"])
        pending = PendingSnooze(alarm, run_at, snoozes)
        cancel = self.schedule_once(run_at, lambda: self._fire_snooze(pending))
        self._pending[alarm["id"]] = (pending, cancel)
        self._publish_pending()
        return pending

    def _fire_snooze(self, pending):
        with self._lock:
            entry = self._pending.get(pending.alarm["id"])
            if entry is None or entry[0] is not pending:
                return  # cancelado o sustituido por otro snooze mientras tanto
            del self._pending[pending.alarm["id"]]
            self._publish_pending()
            self.start(pending.alarm, snoozes=pending.snoozes)

    def _cancel_pending(self, alarm_id):
        entry = self._pending.pop(alarm_id, None)
        if entry is not None:
            try:
                entry[1]()
            except Exception:
                pass  # el job ya se había ejecutado o no existe
            self._publish_pending()

    def _publish_pending(self):
        self._pending_view = tuple(
            sorted((p for p, _ in self._pending.values()), key=lambda p: p.run_at)
        )

    def _stop_local(self):
        if self.player is None:
            return True
        try:
            self.player.stop()
            return True
        except Exception:
            logger.exception("No se pudo parar el sonido local")
            return False

    def _stop_spotify(self):
        if self.spotify is None:
            return False
        try:
            return bool(self.spotify.stop())
        except Exception:
            logger.exception("No se pudo pausar Spotify")
            return False
