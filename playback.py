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
- Bluetooth (bluetooth_audio.py): ALARMA > SPOTIFY > BLUETOOTH. Antes de
  sonar se para el reproductor Bluetooth (libera la tarjeta USB) y sigue
  parado mientras suene la alarma, también con el WAV de respaldo o si otra
  alarma la sustituye. STOP y +10 MIN lo devuelven; al volver a sonar un
  snooze se para otra vez. Un fallo aquí nunca impide que suene la alarma.
- Sonido local (alarmas locales y respaldo de Spotify): la pista de la
  biblioteca (music_library.py) elegida o una al azar, elegida de nuevo cada
  vez que la alarma empieza a sonar (también tras un snooze). Si no puede
  sonar, el WAV de emergencia. STOP, snooze, auto-stop, borrar o sustituir
  la alarma paran ambos (_stop_local).
- Auto-stop: si la alarma tiene `max_duration_minutes` (> 0), al empezar a
  sonar se programa un job que la para por el mismo camino que STOP (fade,
  Spotify/WAV, estado y Bluetooth). No es un snooze: no se reprograma nada.
  STOP, +10 MIN y una alarma nueva cancelan el job; al volver de un snooze se
  empieza un contador completo. Cada start() recibe un token nuevo y el job
  solo actúa si su token es el de la alarma que suena: un job antiguo nunca
  para una alarma posterior. Sin límite (0) no se programa nada.
- Una parada fallida se reintenta cinco veces, cada 30 s. La alarma sigue en
  stop_pending hasta confirmar silencio; los jobs viejos no afectan a otra
  reproducción. Un snooze rechazado conserva un margen fijo de dos minutos.
- Temporizador de sueño (sleep_timer.py): cada start() sube `generation` y
  llama a `on_alarm_start` antes de sonar nada, para anularlo. Es el único
  punto: scheduler, snooze y Probar pasan todos por start().
"""
import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from fade import fade_plan, start_fade

logger = logging.getLogger("alarms")

SNOOZE_MINUTES = 10
STOP_RETRY_SECONDS = 30
STOP_RETRY_LIMIT = 5
START_RETRY_MINUTES = 2
ALARM_FIELDS = ("id", "name", "time", "source", "spotify_uri")
VOLUME_FIELDS = ("volume_start", "volume_end", "fade_minutes")  # opcionales
DURATION_FIELD = "max_duration_minutes"  # opcional; 0 o ausente = sin límite
TRACK_FIELD = "local_track"  # opcional; pista de la biblioteca, None = aleatoria
OPTIONAL_FIELDS = VOLUME_FIELDS + (DURATION_FIELD, TRACK_FIELD)


def volume_settings(alarm):
    """(volumen_inicial, plan_del_fade) o (None, []) si la alarma no los tiene."""
    if any(alarm.get(key) is None for key in VOLUME_FIELDS):
        return None, []
    start, end = int(alarm["volume_start"]), int(alarm["volume_end"])
    seconds = int(alarm["fade_minutes"]) * 60
    if seconds <= 0:
        return end, []  # sin fade: directamente al volumen final
    return start, fade_plan(start, end, seconds)


def monitored_play(player, args, callback):
    """Usa el protocolo opcional de finalización; conserva los reproductores inyectados.

    play_monitored devuelve si arrancó. Si arrancó, notifica una sola vez desde
    otro hilo, fuera del lock del reproductor: True = final normal, False = fallo.
    Una parada solicitada no notifica. Los reproductores sin protocolo usan play().
    """
    if callback is not None and callable(getattr(type(player), "play_monitored", None)):
        return player.play_monitored(*args, on_finished=callback)
    return player.play(*args)


def play_alarm_sound(alarm, player=None, spotify=None, volume=None, interrupted=None,
                     music=None, on_finished=None):
    """Devuelve local, spotify, fallback, cancelled o failed (ningún sonido iniciado).

    Cadena: Spotify → música local (`music`: pista elegida o aleatoria de la
    biblioteca) → WAV de emergencia (`player.play()`).
    - Fuente "local": la música local; si no puede sonar, el WAV de emergencia.
    - Fuente "spotify": `spotify.play(uri)` (con `volume` inicial si se da);
      si falla o no hay Spotify, lo mismo que una alarma local. Si mientras
      tanto se pulsó STOP o snooze (`interrupted`), no suena nada: "cancelled".
    `volume` no se usa todavía con el sonido local.
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

    if music is not None:
        try:
            callback = (lambda ok: on_finished("music", ok)) if on_finished else None
            if monitored_play(music, (alarm,), callback):
                return outcome
        except Exception:
            logger.exception("La música local falló al disparar «%s»", name)
        logger.warning("Sin música local para «%s»: suena el WAV de emergencia", name)
    if player is not None:
        try:
            callback = (lambda ok: on_finished("emergency", ok)) if on_finished else None
            if monitored_play(player, (), callback):
                return outcome
        except Exception:
            logger.exception("El reproductor falló al disparar «%s»", name)
    logger.error("No se pudo iniciar ningún sonido para «%s»", name)
    return "failed"


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
    status: str = "playing"  # connecting, playing, failed, stop_pending, finished, cancelled

    @property
    def id(self):
        return self.alarm["id"]


@dataclass(frozen=True)
class PendingSnooze:
    alarm: dict
    run_at: datetime
    snoozes: int
    retry_until: Optional[datetime] = None


class StopResult(NamedTuple):
    active: ActiveAlarm   # la alarma que se ha parado
    silenced: bool        # False si no se pudo parar el sonido (p. ej. Spotify sin red)


class AlarmPlaybackManager:
    def __init__(self, player, spotify=None, schedule_once=None,
                 clock=datetime.now, snooze_minutes=SNOOZE_MINUTES, fader=None,
                 bluetooth=None, music=None, on_alarm_start=None):
        self.player = player                  # AudioPlayer local (WAV de emergencia)
        self.music = music                    # LocalMusic (biblioteca + ffmpeg) o None
        self.spotify = spotify                # SpotifyAlarmPlayer o None
        self.bluetooth = bluetooth            # BluetoothAudio o None (pause/resume)
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
        self._token = 0                       # sube en cada start(): identifica la reproducción
        self._auto_stop = None                # cancel() del auto-stop pendiente
        self._stop_retry = None
        self._stop_retry_attempts = 0
        self._stop_retry_auto = False
        self._stop_retry_ticket = 0
        # on_alarm_start(nombre): se llama en cada start() antes de sonar nada
        # (el temporizador de sueño se anula ahí; ver sleep_timer.py).
        self.on_alarm_start = on_alarm_start

    # --- Estado (lectura sin lock: son referencias a objetos inmutables) ---

    @property
    def active(self):
        return self._active

    @property
    def generation(self):
        """Sube cada vez que empieza una alarma (start). Solo lectura."""
        return self._token

    @property
    def pending_snoozes(self):
        return self._pending_view

    # --- Acciones ---

    def start(self, alarm, manual=False, snoozes=0):
        """Hace sonar `alarm`, sustituyendo a la que sonara. Devuelve cómo suena."""
        keys = set(alarm.keys())
        alarm = {key: alarm[key] for key in ALARM_FIELDS + OPTIONAL_FIELDS
                 if key in ALARM_FIELDS or key in keys}
        with self._lock:
            if self._active is not None and self._active.status == "stop_pending":
                logger.warning("No se inicia otra alarma mientras haya una parada pendiente")
                return "stop_pending"
            # La generación sube antes de avisar: un temporizador de sueño que se
            # esté creando a la vez ve el cambio y no se programa.
            self._token += 1
            self._cancel_stop_retry()
            self._stop_retry_attempts = 0
            self._stop_retry_auto = False
            token = self._token
            self._notify_alarm_start(alarm["name"])
            self._cancel_fade()  # el fade de la alarma anterior, si lo hubiera
            self._cancel_auto_stop()  # el de la anterior: la nueva tiene el suyo
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
            self._active = ActiveAlarm(alarm, started_at, "connecting", manual, snoozes,
                                       status="connecting")
            # Con la tarjeta libre antes de reproducir (Spotify, WAV o respaldo).
            self._pause_bluetooth()
            initial_volume, plan = volume_settings(alarm)
            try:
                via = play_alarm_sound(alarm, self.player, self.spotify,
                                       volume=initial_volume, interrupted=starting,
                                       music=self.music,
                                       on_finished=lambda kind, ok: self._local_ended(token, kind, ok))
            finally:
                self._starting = None
            # Si la anterior sonaba en Spotify y la nueva no la ha reemplazado
            # allí, se pausa. (Pausar y luego reproducir en Spotify podría
            # llegar desordenado, por eso no se pausa si la nueva es Spotify.)
            if previous is not None and previous.via == "spotify" and via != "spotify":
                self._stop_spotify()

            status = via if via in ("failed", "cancelled") else "playing"
            self._active = ActiveAlarm(alarm, started_at, via, manual, snoozes, status)
            if via == "spotify" and plan:
                self._start_fade(plan)
            if status == "playing":
                self._schedule_auto_stop(alarm, started_at, token)
            elif status == "failed":
                self._resume_bluetooth()
            return via

    def _local_ended(self, token, kind, ok):
        """Final del proceso, fuera de su lock. Ignora avisos de alarmas anteriores."""
        with self._lock:
            active = self._active
            if token != self._token or active is None or active.status != "playing":
                return
            if kind == "music":
                logger.warning("La música local terminó inesperadamente; se intenta el WAV")
                try:
                    started = self.player is not None and monitored_play(
                        self.player, (), lambda success: self._local_ended(token, "emergency", success))
                except Exception:
                    logger.exception("Falló el WAV de emergencia")
                    started = False
                if started:
                    return
                ok = False
            self._active = replace(active, status="finished" if ok else "failed")
            self._cancel_auto_stop()
            self._resume_bluetooth()
            if not ok:
                logger.error("La alarma «%s» se ha quedado sin sonido", active.alarm["name"])

    def stop(self):
        """Para la alarma que suena. Devuelve StopResult, o None si no sonaba nada."""
        self._interrupt_start()
        with self._lock:
            return self._finish()

    def _finish(self, auto=False):
        """STOP (o auto-stop): el único camino que termina una alarma. Con el lock."""
        active = self._active
        if active is None:
            return None
        self._cancel_fade()  # antes de pausar: que no suba el volumen tras STOP
        if active.status in ("failed", "finished", "cancelled"):
            silenced = True
        elif active.via == "spotify":
            silenced = self._stop_spotify()
        elif active.via in ("local", "fallback"):
            silenced = self._stop_local()
        else:
            silenced = True  # "cancelled": no llegó a sonar nada
        if not silenced:
            self._active = replace(active, status="stop_pending")
            self._stop_retry_auto = self._stop_retry_auto or auto
            self._cancel_auto_stop(log=False)
            self._schedule_stop_retry()
            logger.warning("Parada pendiente de «%s»: se reintentará automáticamente "
                           "con límite; también puedes pulsar STOP", active.alarm["name"])
            return StopResult(self._active, False)
        self._active = None
        self._cancel_stop_retry()
        self._cancel_auto_stop(log=not auto)
        if auto:
            logger.info("ALARMA DETENIDA POR AUTO-STOP: %s (duración máxima: %s min)",
                        active.alarm["name"], active.alarm.get(DURATION_FIELD))
        else:
            logger.info("ALARMA DETENIDA: %s", active.alarm["name"])
        self._resume_bluetooth()  # también en +10 MIN: Bluetooth libre hasta que vuelva
        return StopResult(active, silenced)

    def snooze(self, minutes=None):
        """Para la alarma y la vuelve a disparar dentro de N minutos.

        Devuelve el PendingSnooze creado, o None si no sonaba nada.
        """
        self._interrupt_start()
        with self._lock:
            result = self.stop()
            if result is None or not result.silenced:
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

    def _notify_alarm_start(self, name):
        """Anula el temporizador de sueño. Un fallo nunca impide que suene la alarma."""
        if self.on_alarm_start is None:
            return
        try:
            self.on_alarm_start(name)
        except Exception:
            logger.exception("Error al avisar del inicio de la alarma; suena igualmente")

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

    def _schedule_auto_stop(self, alarm, started_at, token):
        minutes = int(alarm.get(DURATION_FIELD) or 0)
        if minutes <= 0:
            logger.info("«%s» sin duración máxima: no se programa auto-stop", alarm["name"])
            return
        run_at = started_at + timedelta(minutes=minutes)
        try:
            self._auto_stop = self.schedule_once(run_at, lambda: self._fire_auto_stop(token))
        except Exception:
            logger.exception("No se pudo programar el auto-stop; la alarma sigue sonando")
            self._auto_stop = None
            return
        logger.info("Auto-stop programado en %s min (a las %s) para «%s»",
                    minutes, run_at.strftime("%H:%M"), alarm["name"])

    def _fire_auto_stop(self, token):
        """Job del auto-stop. Solo para la alarma si sigue siendo la misma reproducción."""
        # Sin _interrupt_start(): si está arrancando otra alarma, un job viejo no
        # debe cortarla. El lock espera a que termine y el token decide.
        with self._lock:
            if self._active is None or token != self._token:
                logger.info("Auto-stop antiguo ignorado: ya no suena esa alarma")
                return None
            self._auto_stop = None  # ya se está ejecutando: no hay nada que cancelar
            return self._finish(auto=True)

    def _cancel_auto_stop(self, log=True):
        cancel, self._auto_stop = self._auto_stop, None
        if cancel is None:
            return
        try:
            cancel()
        except Exception:
            pass  # el job ya se había ejecutado o no existe; el token lo neutraliza
        if log:
            logger.info("Auto-stop cancelado")

    def _schedule_stop_retry(self):
        if self._stop_retry is not None or self._stop_retry_attempts >= STOP_RETRY_LIMIT:
            return
        token = self._token
        self._stop_retry_ticket += 1
        ticket = self._stop_retry_ticket
        run_at = self._clock() + timedelta(seconds=STOP_RETRY_SECONDS)
        try:
            self._stop_retry = self.schedule_once(
                run_at, lambda: self._fire_stop_retry(token, ticket))
        except Exception:
            logger.exception("No se pudo programar el reintento de STOP; usa STOP manual")

    def _fire_stop_retry(self, token, ticket):
        with self._lock:
            if (token != self._token or ticket != self._stop_retry_ticket or
                    self._stop_retry is None or self._active is None or
                    self._active.status != "stop_pending"):
                return None
            self._stop_retry = None
            self._stop_retry_ticket += 1  # un callback duplicado no vuelve a actuar
            self._stop_retry_attempts += 1
            result = self._finish(auto=self._stop_retry_auto)
            if result is not None and not result.silenced and self._stop_retry_attempts >= STOP_RETRY_LIMIT:
                logger.error("Agotados los %s reintentos de STOP para «%s»; "
                             "se mantiene la parada pendiente, usa STOP manual",
                             STOP_RETRY_LIMIT, result.active.alarm["name"])
            return result

    def _cancel_stop_retry(self):
        cancel, self._stop_retry = self._stop_retry, None
        self._stop_retry_ticket += 1
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass  # callback en vuelo neutralizado por su ticket y generación

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

    def _schedule(self, alarm, run_at, snoozes, retry_until=None):
        self._cancel_pending(alarm["id"])
        pending = PendingSnooze(alarm, run_at, snoozes, retry_until)
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
            now = self._clock()
            deadline = pending.retry_until or pending.run_at + timedelta(minutes=START_RETRY_MINUTES)
            if now > deadline:
                logger.error("Snooze de «%s» no iniciado: agotado el margen de recuperación",
                             pending.alarm["name"])
                return
            result = self.start(pending.alarm, snoozes=pending.snoozes)
            if result == "stop_pending":
                retry_at = now + timedelta(seconds=STOP_RETRY_SECONDS)
                if retry_at <= deadline:
                    try:
                        self._schedule(pending.alarm, retry_at, pending.snoozes, deadline)
                    except Exception:
                        logger.exception("No se pudo reprogramar el snooze rechazado de «%s»",
                                         pending.alarm["name"])
                    else:
                        logger.warning("Snooze de «%s» pendiente de recuperar STOP", pending.alarm["name"])
                else:
                    logger.error("Snooze de «%s» no iniciado: agotado el margen de recuperación",
                                 pending.alarm["name"])

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

    def _pause_bluetooth(self):
        if self.bluetooth is None:
            return
        try:
            self.bluetooth.pause()
        except Exception:
            logger.exception("No se pudo pausar Bluetooth; la alarma suena igualmente")

    def _resume_bluetooth(self):
        if self.bluetooth is None:
            return
        try:
            self.bluetooth.resume()
        except Exception:
            logger.exception("No se pudo devolver Bluetooth tras la alarma")

    def _stop_local(self):
        """Para la música local (ffmpeg) y el WAV de emergencia: pudo sonar cualquiera."""
        silenced = True
        if self.music is not None:
            try:
                silenced = self.music.stop() is not False
            except Exception:
                logger.exception("No se pudo parar la música local")
                silenced = False
        if self.player is not None:
            try:
                silenced = (self.player.stop() is not False) and silenced
            except Exception:
                logger.exception("No se pudo parar el sonido local")
                silenced = False
        return silenced

    def _stop_spotify(self):
        if self.spotify is None:
            return False
        try:
            return bool(self.spotify.stop())
        except Exception:
            logger.exception("No se pudo pausar Spotify")
            return False
