"""Temporizador de sueño: para una reproducción manual al cabo de 15-60 min.

Es independiente del auto-stop de las alarmas (playback.py): el auto-stop
para una alarma que suena; el temporizador de sueño para música que el
usuario ha puesto a mano y **nunca** para una alarma.

Cada temporizador queda ligado, al crearlo, a una fuente concreta:
- "spotify": el dispositivo de Spotify activo en ese momento (su ID). Al
  vencer solo se pausa si ese mismo dispositivo sigue siendo el activo. No se
  transfiere, no se cambia de dispositivo ni se desvincula la cuenta.
- "bluetooth": un dispositivo conectado (su MAC validada). Al vencer solo se
  desconecta si sigue conectado. Nunca se olvida, ni se le quita la confianza,
  ni se toca el adaptador (sigue emparejado y de confianza: se puede volver a
  conectar como siempre).
Al vencer no se "adivina" qué suena: si la fuente ya no es la misma, se
registra y no se hace nada.

Un solo temporizador a la vez: crear otro sustituye al anterior.

Alarmas (prioridad absoluta):
- AlarmPlaybackManager.start() (el único punto por el que empieza cualquier
  alarma: scheduler, snooze o Probar) llama a `invalidate_for_alarm()` antes
  de hacer ningún ruido. El temporizador se cancela y su job queda muerto.
- Además, cada temporizador recuerda la "generación" de alarmas del
  momento en que se creó (`playback.generation`, que sube en cada start()).
  Al vencer, si ha empezado cualquier alarma desde entonces o hay una
  sonando, no hace nada, aunque la invalidación no hubiera llegado.
- El vencimiento hace su comprobación y su acción con el lock tomado, y
  `invalidate_for_alarm()` también lo coge: si coinciden en el tiempo, o
  bien la alarma invalida antes (y el vencimiento no hace nada) o bien la
  pausa termina antes de que la alarma empiece a sonar. Nunca después.
- No se puede crear un temporizador mientras suena una alarma.

Solo en memoria: si Groove se reinicia, el temporizador desaparece y no se
restaura (mejor eso que parar música a una hora que ya no corresponde).

Nunca lanza excepciones hacia el scheduler: los fallos (Spotify sin red,
dispositivo desaparecido, ya desconectado...) se registran y el temporizador
termina sin reintentos.
"""
import logging
from contextlib import nullcontext
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from bluetooth_manager import BluetoothError, normalize_mac
from playback import timer_schedule_once

logger = logging.getLogger("alarms")

ALLOWED_MINUTES = (15, 30, 45, 60)
SPOTIFY, BLUETOOTH = "spotify", "bluetooth"
SOURCES = (SPOTIFY, BLUETOOTH)
SOURCE_LABELS = {SPOTIFY: "Spotify", BLUETOOTH: "Bluetooth"}
DEVICES_TIMEOUT = 5      # s al comprobar el dispositivo de Spotify (al crear y al vencer)
RECENT_SECONDS = 600     # cuánto se muestra el resultado del último temporizador

# Cómo terminó un temporizador (para el log y la interfaz).
EXPIRED, CANCELLED, REPLACED, ALARM, SKIPPED, FAILED = (
    "expired", "cancelled", "replaced", "alarm", "skipped", "failed")


class SleepTimerError(Exception):
    """No se puede crear el temporizador. `str(exc)` es apto para la interfaz."""


class InvalidSleepTimer(SleepTimerError):
    """Datos no válidos (duración o fuente)."""


@dataclass(frozen=True)
class SleepTimer:
    generation: int          # identifica este temporizador (sube con cada uno)
    source: str              # "spotify" o "bluetooth"
    minutes: int
    started_at: datetime
    expires_at: datetime
    target_id: str           # ID del dispositivo de Spotify o MAC Bluetooth
    target_name: str
    alarm_generation: int    # generación de alarmas al crearlo

    @property
    def label(self):
        """"Spotify · Groove" o "Bluetooth · Redmi Note 11 Pro 5G"."""
        base = SOURCE_LABELS[self.source]
        return f"{base} · {self.target_name}" if self.target_name else base

    def remaining(self, now):
        return max(0, int((self.expires_at - now).total_seconds() + 0.999))

    def as_dict(self, now):
        return {"active": True, "id": self.generation, "source": self.source,
                "label": self.label, "target_name": self.target_name,
                "minutes": self.minutes,
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "expires_at": self.expires_at.isoformat(timespec="seconds"),
                "remaining": self.remaining(now)}


@dataclass(frozen=True)
class SleepTimerResult:
    """Cómo terminó el último temporizador."""
    outcome: str
    message: str
    ended_at: datetime
    source: str


class SleepTimerManager:
    """Crear, cancelar, consultar y hacer vencer el temporizador de sueño.

    `spotify`: SpotifyClient (el mismo de toda la app). `bluetooth`:
    BluetoothManager (bluetooth_manager.py). `playback`: AlarmPlaybackManager
    (solo se leen `active` y `generation`). `schedule_once(run_at, callback)
    -> cancel()`: APScheduler en la app (scheduler.date_job_scheduler).
    """

    def __init__(self, spotify=None, bluetooth=None, playback=None, schedule_once=None,
                 clock=datetime.now):
        self.spotify = spotify
        self.bluetooth = bluetooth
        self.playback = playback
        # Sin APScheduler (scheduler desactivado): el mismo respaldo que los snoozes.
        self.schedule_once = schedule_once or timer_schedule_once
        self._clock = clock
        # Protege el estado y también la acción del vencimiento (ver docstring).
        self._lock = threading.RLock()
        self._timer: Optional[SleepTimer] = None
        self._cancel_job = None
        self._generation = 0
        self._last: Optional[SleepTimerResult] = None

    # --- Consultas (sin red) ---

    @property
    def timer(self):
        return self._timer

    def status(self):
        """Estado para la interfaz y /sleep-timer/status."""
        with self._lock:
            now = self._clock()
            timer = self._timer
            state = timer.as_dict(now) if timer else {"active": False}
            last = self._last
            if last is not None and (now - last.ended_at).total_seconds() <= RECENT_SECONDS:
                state["last"] = {"outcome": last.outcome, "message": last.message,
                                 "source": last.source,
                                 "ended_at": last.ended_at.isoformat(timespec="seconds")}
            else:
                state["last"] = None
            state["allowed_minutes"] = list(ALLOWED_MINUTES)
            return state

    # --- Acciones ---

    def start(self, source, minutes, mac=None):
        """Crea el temporizador (sustituye al que hubiera). Devuelve SleepTimer.

        Lanza InvalidSleepTimer (duración/fuente) o InvalidMac si los datos no
        valen, y SleepTimerError si la fuente no está sonando o suena una alarma.
        """
        minutes = self._validate_minutes(minutes)
        if source not in SOURCES:
            raise InvalidSleepTimer("Fuente no válida para el temporizador.")
        if source == BLUETOOTH:
            mac = normalize_mac(mac)  # antes de hablar con BlueZ
        self._refuse_during_alarm()
        alarm_generation = self._alarm_generation()
        # Resolver la fuente habla con Spotify/BlueZ: se hace sin el lock para
        # que una alarma que empiece mientras tanto no tenga que esperar.
        if source == SPOTIFY:
            target_id, target_name = self._spotify_target()
        else:
            target_id, target_name = self._bluetooth_target(mac)

        with self._lock:
            # Si mientras tanto ha empezado una alarma, no se crea nada.
            if self._alarm_generation() != alarm_generation:
                raise SleepTimerError("Ha empezado a sonar una alarma: el temporizador no se "
                                      "ha programado.")
            self._refuse_during_alarm()
            if self._timer is not None:
                self._end(REPLACED, "Temporizador sustituido por uno nuevo.")
                logger.info("Sleep timer anterior sustituido por uno nuevo")
            self._generation += 1
            now = self._clock()
            timer = SleepTimer(self._generation, source, minutes, now,
                               now + timedelta(minutes=minutes), target_id, target_name,
                               alarm_generation)
            generation = timer.generation
            try:
                self._cancel_job = self.schedule_once(timer.expires_at,
                                                      lambda: self._fire(generation))
            except Exception as exc:
                logger.exception("No se pudo programar el sleep timer")
                raise SleepTimerError("No se pudo programar el temporizador.") from exc
            self._timer = timer
            if source == SPOTIFY:
                logger.info("Sleep timer iniciado: Spotify, %s min (dispositivo «%s», "
                            "hasta las %s)", minutes, target_name,
                            timer.expires_at.strftime("%H:%M"))
            else:
                logger.info("Sleep timer iniciado: Bluetooth (%s, %s), %s min (hasta las %s)",
                            target_name, target_id, minutes, timer.expires_at.strftime("%H:%M"))
            return timer

    def cancel(self):
        """Cancela el temporizador activo. Devuelve el cancelado o None."""
        with self._lock:
            timer = self._timer
            if timer is None:
                return None
            self._end(CANCELLED, "Temporizador cancelado.")
            logger.info("Sleep timer cancelado (%s)", timer.label)
            return timer

    def invalidate_for_alarm(self, alarm_name=None):
        """Empieza una alarma: el temporizador activo queda anulado. Nunca lanza.

        Espera a que termine un vencimiento en curso (lock compartido): así una
        pausa de Spotify nunca llega después de que la alarma empiece a sonar.
        """
        try:
            with self._lock:
                timer = self._timer
                if timer is None:
                    return False
                self._end(ALARM, "Temporizador anulado porque empezó una alarma.")
                logger.info("Sleep timer invalidado por alarma%s: %s (vencía a las %s)",
                            f" «{alarm_name}»" if alarm_name else "", timer.label,
                            timer.expires_at.strftime("%H:%M"))
                return True
        except Exception:
            logger.exception("Error al invalidar el sleep timer; la alarma sigue igual")
            return False

    # --- Vencimiento ---

    def _fire(self, generation):
        # Orden único: reproducción -> sleep timer, también al empezar alarma.
        gate = getattr(self.playback, "_lock", None)
        with gate if gate is not None else nullcontext():
            return self._fire_locked(generation)

    def _fire_locked(self, generation):
        """Job del scheduler. Nunca lanza: todo fallo se registra."""
        try:
            with self._lock:
                timer = self._timer
                if timer is None or timer.generation != generation:
                    logger.info("Sleep timer antiguo ignorado: ya no está activo")
                    return None
                self._cancel_job = None  # ya se está ejecutando
                if self._alarm_generation() != timer.alarm_generation or self._alarm_active():
                    self._end(ALARM, "Temporizador anulado porque empezó una alarma.")
                    logger.info("Sleep timer ignorado al vencer: ha sonado una alarma desde "
                                "que se programó; no se toca nada")
                    return ALARM
                if timer.source == SPOTIFY:
                    outcome, message = self._expire_spotify(timer)
                else:
                    outcome, message = self._expire_bluetooth(timer)
                self._end(outcome, message)
                return outcome
        except Exception:
            logger.exception("Error inesperado al vencer el sleep timer")
            try:
                with self._lock:
                    if self._timer is not None and self._timer.generation == generation:
                        self._end(FAILED, "El temporizador no pudo parar la reproducción.")
            except Exception:
                pass
            return FAILED

    def _expire_spotify(self, timer):
        try:
            devices = self.spotify.get_devices(timeout=DEVICES_TIMEOUT)
        except Exception as exc:
            logger.warning("Sleep timer finalizado sin pausar Spotify: no se pudo consultar "
                           "Spotify (%s)", exc)
            return FAILED, "No se pudo pausar Spotify (error de Spotify)."
        current = next((d for d in devices or () if d.get("id") == timer.target_id), None)
        if current is None or not current.get("is_active"):
            logger.info("Sleep timer finalizado sin tocar Spotify: «%s» ya no es el dispositivo "
                        "activo (la reproducción cambió)", timer.target_name)
            return SKIPPED, "La reproducción de Spotify cambió: no se ha tocado."
        try:
            self.spotify.pause(timer.target_id)
        except Exception as exc:
            logger.warning("Sleep timer finalizado sin pausar Spotify: %s", exc)
            return FAILED, "No se pudo pausar Spotify (error de Spotify)."
        logger.info("Sleep timer finalizado: Spotify pausado («%s»)", timer.target_name)
        if self.playback is not None and callable(getattr(type(self.playback), "manual_spotify_paused", None)):
            self.playback.manual_spotify_paused(timer.target_id)
        return EXPIRED, "Spotify pausado por el temporizador."

    def _expire_bluetooth(self, timer):
        try:
            device = self.bluetooth.device(timer.target_id)
        except BluetoothError as exc:
            logger.info("Sleep timer finalizado sin desconectar: %s (%s) ya no está entre los "
                        "dispositivos (%s)", timer.target_name, timer.target_id,
                        exc.detail or exc)
            return SKIPPED, "El dispositivo Bluetooth ya no estaba."
        if not device.connected:
            logger.info("Sleep timer finalizado: %s (%s) ya estaba desconectado",
                        device.name, device.mac)
            return SKIPPED, f"«{device.name}» ya estaba desconectado."
        try:
            self.bluetooth.disconnect(device.mac)  # solo desconectar: sigue emparejado
        except BluetoothError as exc:
            logger.warning("Sleep timer finalizado sin desconectar %s (%s): %s", device.name,
                           device.mac, exc.detail or exc)
            return FAILED, f"No se pudo desconectar «{device.name}»."
        logger.info("Sleep timer finalizado: dispositivo Bluetooth desconectado (%s, %s); "
                    "sigue emparejado y de confianza", device.name, device.mac)
        return EXPIRED, f"«{device.name}» desconectado por el temporizador."

    # --- Internos ---

    def _end(self, outcome, message):
        """Termina el temporizador activo (con el lock) y cancela su job."""
        timer, self._timer = self._timer, None
        cancel, self._cancel_job = self._cancel_job, None
        if cancel is not None:
            try:
                cancel()
            except Exception:
                pass  # el job ya se ejecutó o no existe: la generación lo neutraliza
        if timer is not None:
            self._last = SleepTimerResult(outcome, message, self._clock(), timer.source)

    @staticmethod
    def _validate_minutes(minutes):
        try:
            value = int(str(minutes).strip())
        except (TypeError, ValueError):
            value = None
        if value not in ALLOWED_MINUTES:
            raise InvalidSleepTimer("Duración no válida: elige 15, 30, 45 o 60 minutos.")
        return value

    def _alarm_generation(self):
        return getattr(self.playback, "generation", 0) if self.playback is not None else 0

    def _alarm_active(self):
        return self.playback is not None and self.playback.active is not None

    def _refuse_during_alarm(self):
        if self._alarm_active():
            raise SleepTimerError("Hay una alarma sonando: el temporizador de sueño no se "
                                  "puede usar ahora.")

    def _spotify_target(self):
        spotify = self.spotify
        if spotify is None or not spotify.is_configured or not spotify.is_connected():
            raise SleepTimerError("Spotify no está conectado.")
        try:
            devices = spotify.get_devices(timeout=DEVICES_TIMEOUT)
        except Exception as exc:
            logger.warning("Sleep timer: no se pudo consultar Spotify (%s)", exc)
            raise SleepTimerError("No se pudo consultar Spotify. Inténtalo de nuevo.") from exc
        active = next((d for d in devices or () if d.get("is_active") and d.get("id")), None)
        if active is None:
            raise SleepTimerError("No hay nada sonando en Spotify ahora mismo.")
        return active["id"], str(active.get("name") or "")[:100]

    def _bluetooth_target(self, mac):
        if self.bluetooth is None or not getattr(self.bluetooth, "available", False):
            raise SleepTimerError("Bluetooth no está disponible.")
        try:
            device = self.bluetooth.device(mac)
        except BluetoothError as exc:
            raise SleepTimerError(str(exc)) from exc
        if not device.connected:
            raise SleepTimerError(f"«{device.name}» no está conectado.")
        return device.mac, device.name
