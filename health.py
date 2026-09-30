"""Health checks de Groove: la página Diagnóstico y el pre-flight de las alarmas.

`HealthChecker.run()` ejecuta todas las comprobaciones y devuelve un
`HealthReport` con un `CheckResult` por componente (id, nombre, estado
ok / warning / error, resumen corto y detalles opcionales).

Reglas:
- Solo lectura. Nunca se arranca ni para un servicio, no se toca Bluetooth
  (ni emparejamiento ni visibilidad), no se reproduce audio, no se escribe en
  la base de datos y no se toca la reproducción de Spotify. (La única
  escritura posible es la renovación normal del token de Spotify, la misma
  que hace cualquier petición a Spotify.)
- Un check que falla (excepción o tiempo agotado) da un resultado "error";
  nunca rompe los demás ni la página. Las trazas van al log, no a la web.
- Comandos y peticiones con timeouts cortos. Los checks corren en paralelo y
  el conjunto tiene un presupuesto total (`total_timeout`).
- Nada sensible en los resultados: ni tokens, ni client secret, ni variables
  de entorno, ni rutas completas (solo nombres de fichero o de servicio).
- Algunos resultados llevan un `code` machine-readable para que otros módulos
  (el pre-flight) reaccionen sin interpretar textos. Aquí nunca se actúa.

Estados:
- ok: funciona como se espera.
- warning: funciona a medias, no está configurado o no se puede comprobar
  aquí, y hay alternativa (biblioteca vacía, Spotify sin vincular, Groove no
  aparece en Spotify...).
- error: el componente debería funcionar y no funciona (servicio parado,
  archivo que falta, API caída), o no se ha podido comprobar (timeout, fallo
  al consultar systemd).
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import db
import ui
from audio_player import DEFAULT_MUSIC_DEVICE
from bluetooth_audio import DISABLED_VALUES, unit_name
from spotify_client import (
    SpotifyAuthError,
    SpotifyConnectionError,
    SpotifyError,
    SpotifyForbiddenError,
    SpotifyRateLimitError,
)
from spotify_player import (
    DEVICE_ID_KEY,
    DEVICE_NAME_KEY,
    DeviceConflict,
    DeviceUnavailable,
    choose_device,
)

logger = logging.getLogger("alarms")

OK, WARNING, ERROR = "ok", "warning", "error"
SEVERITY = {OK: 0, WARNING: 1, ERROR: 2}

COMMAND_TIMEOUT = 3      # s por comando (systemctl, ffmpeg -version, bluetoothctl)
SPOTIFY_TIMEOUT = 5      # s para GET /me/player/devices
TOTAL_TIMEOUT = 12       # s para el conjunto de checks
DEFAULT_RASPOTIFY_SERVICE = "raspotify"
DEFAULT_BLUEALSA_SERVICE = "bluealsa"
ASOUND_DIR = "/proc/asound"
MAX_TEXT = 160           # los mensajes de error se recortan

# CheckResult.code: Spotify responde bien, pero el dispositivo de las alarmas
# no está en /me/player/devices (no es auth, red, 429, permisos ni conflicto).
SPOTIFY_DEVICE_MISSING = "device_missing"

# Orden en la página (y en el log del pre-flight).
CHECK_ORDER = ("audio", "spotify", "raspotify", "local_music", "ffmpeg", "emergency",
               "bluetooth", "scheduler")
CHECK_NAMES = {
    "audio": "Audio USB",
    "spotify": "Spotify",
    "raspotify": "Raspotify",
    "local_music": "Música local",
    "ffmpeg": "ffmpeg",
    "emergency": "WAV de emergencia",
    "bluetooth": "Bluetooth",
    "scheduler": "Scheduler",
}


@dataclass(frozen=True)
class CheckResult:
    id: str
    name: str
    status: str                  # OK, WARNING o ERROR
    summary: str                 # una línea: "Disponible", "4 pistas"...
    details: tuple = ()          # líneas cortas y seguras (opcional)
    code: str = None             # motivo machine-readable (p. ej. SPOTIFY_DEVICE_MISSING)

    def as_dict(self):
        return {"id": self.id, "name": self.name, "status": self.status,
                "summary": self.summary, "details": list(self.details)}


@dataclass(frozen=True)
class HealthReport:
    checked_at: datetime
    trigger: str                 # "Manual", "Pre-flight de «Trabajo»"...
    results: tuple = field(default_factory=tuple)

    def get(self, check_id):
        return next((r for r in self.results if r.id == check_id), None)

    def status_of(self, check_id):
        result = self.get(check_id)
        return result.status if result else ERROR

    @property
    def status(self):
        """El peor estado de todos los checks."""
        return max((r.status for r in self.results), key=SEVERITY.get, default=OK)

    def count(self, status):
        return sum(1 for r in self.results if r.status == status)

    def as_dict(self):
        return {"checked_at": self.checked_at.isoformat(timespec="seconds"),
                "trigger": self.trigger, "status": self.status,
                "checks": [r.as_dict() for r in self.results]}


def result(check_id, status, summary, *details, code=None):
    return CheckResult(check_id, CHECK_NAMES.get(check_id, check_id), status, summary,
                       tuple(d for d in details if d), code)


def short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def plural(count, one, many):
    return f"{count} {one if count == 1 else many}"


# --- systemd (solo consultas) ---------------------------------------------

class CommandFailed(Exception):
    """No se pudo ejecutar el comando (no existe, tiempo agotado...)."""


class CommandTimedOut(CommandFailed):
    """El comando superó su timeout; permite reintentos selectivos."""


def run_command(run, command, timeout=COMMAND_TIMEOUT):
    """Ejecuta `command` sin shell ni stdin. Devuelve CompletedProcess o lanza CommandFailed."""
    try:
        return run(command, capture_output=True, text=True, timeout=timeout,
                   check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CommandFailed(f"{command[0]} no está instalado") from None
    except subprocess.TimeoutExpired:
        raise CommandTimedOut(f"{command[0]} tardó más de {timeout} s") from None
    except OSError as exc:
        raise CommandFailed(f"no se pudo ejecutar {command[0]}: {exc.strerror or exc}") from None


def service_state(run, service, timeout=COMMAND_TIMEOUT):
    """Estado de systemd ("active", "inactive", "failed"...). Lanza CommandFailed."""
    completed = run_command(run, ["systemctl", "--no-ask-password", "is-active",
                                  unit_name(service)], timeout)
    state = (completed.stdout or "").strip().splitlines()
    if not state:
        raise CommandFailed("systemctl no devolvió ningún estado")
    return state[0].strip()


STATE_LABELS = {"active": "Activo", "inactive": "Detenido", "failed": "Ha fallado",
                "activating": "Arrancando", "deactivating": "Deteniéndose",
                "reloading": "Recargando"}


def state_label(state):
    return STATE_LABELS.get(state, f"Estado: {state}")


# --- ALSA (sin abrir el dispositivo) --------------------------------------

_CARD_RE = re.compile(r"CARD=([^,]+)")
_DEV_RE = re.compile(r"DEV=(\d+)")
_HW_RE = re.compile(r"^(?:plug)?hw:([^,=]+)(?:,(\d+))?")
_CARDS_LINE_RE = re.compile(r"^\s*(\d+)\s+\[([^\]]+?)\s*\]")


def parse_alsa_device(device):
    """("Device", 0) a partir de "plughw:CARD=Device,DEV=0" o "hw:1,0"; None si no se sabe."""
    device = (device or "").strip()
    card = _CARD_RE.search(device)
    if card:
        dev = _DEV_RE.search(device)
        return card.group(1), int(dev.group(1)) if dev else 0
    hw = _HW_RE.match(device)
    if hw:
        return hw.group(1), int(hw.group(2) or 0)
    return None


def list_cards(asound):
    """Ids de las tarjetas de /proc/asound/cards, p. ej. ["vc4hdmi", "Device"]."""
    try:
        text = (asound / "cards").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [m.group(2) for line in text.splitlines() if (m := _CARDS_LINE_RE.match(line))]


# --- Checker ---------------------------------------------------------------

class HealthChecker:
    """Ejecuta los checks y recuerda el último informe (en memoria).

    Todo lo externo es inyectable para los tests: `run` (subprocess.run),
    `which`, `platform`, `asound_dir`, `clock` y los objetos de la app.
    """

    def __init__(self, config, *, database, library=None, spotify=None, bluetooth=None,
                 playback=None, scheduler=None, run=subprocess.run, which=shutil.which,
                 platform=None, asound_dir=ASOUND_DIR, clock=datetime.now,
                 total_timeout=TOTAL_TIMEOUT):
        self.config = config
        self.database = database
        self.library = library
        self.spotify = spotify
        self.bluetooth = bluetooth
        self.playback = playback
        self.scheduler = scheduler        # BackgroundScheduler o None (se asigna al arrancar)
        self._run = run
        self._which = which
        self.platform = platform or sys.platform
        self.asound = Path(asound_dir)
        self._clock = clock
        self.total_timeout = total_timeout
        self._lock = threading.Lock()     # un diagnóstico a la vez
        self._last = None

    @property
    def last(self):
        """Último informe (manual, de la página o de un pre-flight), o None."""
        return self._last

    @property
    def is_linux(self):
        return self.platform.startswith("linux")

    def checks(self):
        return {
            "audio": self.check_audio,
            "spotify": self.check_spotify,
            "raspotify": self.check_raspotify,
            "local_music": self.check_local_music,
            "ffmpeg": self.check_ffmpeg,
            "emergency": self.check_emergency,
            "bluetooth": self.check_bluetooth,
            "scheduler": self.check_scheduler,
        }

    def run(self, trigger="Manual"):
        """Ejecuta todos los checks (en paralelo) y guarda el informe. Nunca lanza."""
        with self._lock:
            checks = self.checks()
            results = {}
            pool = ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="health")
            try:
                futures = {cid: pool.submit(self._safe, cid, fn) for cid, fn in checks.items()}
                deadline = time.monotonic() + self.total_timeout
                for cid, future in futures.items():
                    try:
                        results[cid] = future.result(timeout=max(0.0, deadline - time.monotonic()))
                    except FutureTimeout:
                        logger.warning("Diagnóstico: el check %s tardó más de %s s", cid,
                                       self.total_timeout)
                        results[cid] = result(cid, ERROR, "Sin respuesta",
                                              f"La comprobación tardó más de {self.total_timeout} s.")
            finally:
                # No se espera a un check colgado: su hilo termina solo (timeouts).
                pool.shutdown(wait=False, cancel_futures=True)
            ordered = tuple(results[cid] for cid in CHECK_ORDER if cid in results)
            report = HealthReport(self._clock(), trigger, ordered)
            self._last = report
            return report

    def _safe(self, check_id, check):
        try:
            return check()
        except Exception as exc:
            logger.exception("Diagnóstico: el check %s falló", check_id)
            return result(check_id, ERROR, "No se pudo comprobar",
                          f"Error inesperado ({type(exc).__name__}). Detalles en el log.")

    # --- Scheduler ---

    def check_scheduler(self):
        sched = self.scheduler
        if sched is None:
            if not self.config.get("SCHEDULER_ENABLED", True):
                return result("scheduler", WARNING, "Desactivado",
                              "SCHEDULER_ENABLED está desactivado: las alarmas no sonarán.")
            return result("scheduler", ERROR, "No iniciado",
                          "Las alarmas no sonarán hasta que Groove arranque el scheduler.")
        # 0 = parado, 1 = funcionando, 2 = en pausa (APScheduler).
        state = getattr(sched, "state", 1 if getattr(sched, "running", False) else 0)
        if state == 0:
            return result("scheduler", ERROR, "Parado", "Las alarmas no sonarán.")
        if sched.get_job("check_alarms") is None:
            return result("scheduler", ERROR, "Sin job de alarmas",
                          "Falta el job que revisa las alarmas cada minuto.")
        preflights = sum(1 for job in sched.get_jobs() if job.id.startswith("preflight:"))

        conn = db.connect(self.database)
        try:
            alarms = [dict(row) for row in conn.execute("SELECT * FROM alarms")]
        finally:
            conn.close()
        enabled = [a for a in alarms if a["enabled"]]
        now = self._clock()
        upcoming, when = ui.next_alarm(enabled, now)
        next_line = (f"Próxima alarma: {when:%H:%M} ({ui.describe_day(when, now).lower()})"
                     f" · «{upcoming['name']}»" if upcoming else "Sin próximas alarmas")
        status, summary = (WARNING, "En pausa") if state == 2 else (OK, "Funcionando")
        return result("scheduler", status, summary, next_line,
                      f"{plural(len(enabled), 'alarma activa', 'alarmas activas')}"
                      f" · {plural(preflights, 'pre-flight programado', 'pre-flight programados')}")

    # --- Audio USB (ALSA) ---

    def check_audio(self):
        if self.config.get("AUDIO_BACKEND", "local") != "local":
            return result("audio", WARNING, "Audio desactivado", "AUDIO_BACKEND no es «local».")
        device = self.config.get("ALSA_DEVICE") or DEFAULT_MUSIC_DEVICE
        if not self.is_linux:
            return result("audio", WARNING, "No comprobable aquí",
                          "La salida ALSA solo se comprueba en la Raspberry (Linux).")
        parsed = parse_alsa_device(device)
        if parsed is None:
            return result("audio", WARNING, "No comprobable",
                          f"No se puede verificar «{device}» sin reproducir sonido.")
        card, dev = parsed
        if not self.asound.is_dir():
            return result("audio", ERROR, "ALSA no disponible",
                          "No hay tarjetas de sonido registradas en el sistema.")
        # Solo se leen ficheros de /proc/asound: no se abre el dispositivo, así
        # que no suena nada ni se molesta a una alarma que esté sonando.
        card_dir = self.asound / (f"card{card}" if card.isdigit() else card)
        cards = list_cards(self.asound)
        if not card_dir.is_dir():
            return result("audio", ERROR, "Tarjeta no encontrada",
                          f"«{card}» no aparece. ¿Está conectada la tarjeta USB?",
                          f"Tarjetas detectadas: {', '.join(cards) or 'ninguna'}.")
        pcm = card_dir / f"pcm{dev}p"
        if not pcm.is_dir():
            return result("audio", ERROR, "Sin salida de audio",
                          f"La tarjeta «{card}» no tiene salida DEV={dev}.")
        try:
            status = (pcm / "sub0" / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            status = ""
        if "RUNNING" in status:
            usage = "En uso ahora mismo (reproduciendo)."
        elif status.strip() == "closed":
            usage = "Libre."
        else:
            usage = ""
        return result("audio", OK, "Disponible", f"Salida: {device}", usage)

    # --- ffmpeg ---

    def check_ffmpeg(self):
        binary = self.config.get("FFMPEG_BINARY") or "ffmpeg"
        name = Path(binary).name
        if self.config.get("AUDIO_BACKEND", "local") != "local":
            return result("ffmpeg", WARNING, "Audio desactivado", "AUDIO_BACKEND no es «local».")
        found = self._which(binary)
        if not found:
            if not self.is_linux:
                return result("ffmpeg", WARNING, "No instalado",
                              "En este equipo la música local no se usa (solo en la Raspberry).")
            return result("ffmpeg", ERROR, "No instalado",
                          f"No se encuentra «{name}». Instálalo con: sudo apt install -y ffmpeg")
        for attempt in range(2):
            try:
                completed = run_command(self._run, [found, "-hide_banner", "-version"])
                break
            except CommandTimedOut as exc:
                if attempt == 1:
                    return result("ffmpeg", ERROR, "No se puede ejecutar", short(exc),
                                  "Timeout persistente tras un único reintento (2 intentos).")
                logger.warning("Diagnóstico: ffmpeg -version: %s; se reintenta una sola vez",
                               short(exc))
            except CommandFailed as exc:
                return result("ffmpeg", ERROR, "No se puede ejecutar", short(exc))
        if completed.returncode != 0:
            return result("ffmpeg", ERROR, "No se puede ejecutar",
                          f"«{name} -version» terminó con código {completed.returncode}.")
        first = ((completed.stdout or "").strip().splitlines() or [""])[0]
        match = re.search(r"version\s+(\S+)", first)
        return result("ffmpeg", OK, f"Versión {match.group(1)}" if match else "Disponible")

    # --- Biblioteca local ---

    def check_local_music(self):
        if self.library is None:
            return result("local_music", WARNING, "No disponible")
        root = Path(self.library.root)
        if not root.exists():
            parent = root.parent
            if parent.is_dir() and os.access(parent, os.W_OK):
                return result("local_music", WARNING, "Carpeta sin crear",
                              "Se creará al subir música. Mientras, suena el WAV de emergencia.")
            return result("local_music", ERROR, "Carpeta no disponible",
                          "No existe la carpeta de música y no se puede crear.")
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            return result("local_music", ERROR, "Carpeta no legible",
                          "Groove no puede leer la carpeta de música.")
        tracks = self.library.tracks()
        writable = "" if os.access(root, os.W_OK) else \
            "La carpeta es de solo lectura: no se podrá subir música."
        if not tracks:
            return result("local_music", WARNING, "Biblioteca vacía",
                          "Las alarmas locales sonarán con el WAV de emergencia.", writable)
        return result("local_music", WARNING if writable else OK,
                      plural(len(tracks), "pista", "pistas"), writable)

    # --- WAV de emergencia ---

    def check_emergency(self):
        if self.config.get("AUDIO_BACKEND", "local") != "local":
            return result("emergency", WARNING, "Audio desactivado",
                          "AUDIO_BACKEND no es «local».")
        path = Path(self.config.get("SOUND_PATH") or "")
        name = path.name or "(sin configurar)"
        if not path.is_file():
            return result("emergency", ERROR, "Falta el archivo",
                          f"No se encuentra «{name}». Sin él, si todo lo demás falla no sonará nada.",
                          "Genera uno con: python tools/make_test_sound.py")
        if not os.access(path, os.R_OK):
            return result("emergency", ERROR, "No se puede leer", f"Sin permiso de lectura en «{name}».")
        try:
            with wave.open(str(path), "rb") as wav:
                rate, frames = wav.getframerate(), wav.getnframes()
        except (wave.Error, EOFError, OSError):
            return result("emergency", ERROR, "WAV no válido",
                          f"«{name}» no es un WAV PCM que aplay pueda reproducir.")
        seconds = frames / rate if rate else 0
        return result("emergency", OK, "Listo", f"{name} · {seconds:.1f} s · {rate} Hz")

    # --- Raspotify ---

    def check_raspotify(self):
        service = str(self.config.get("RASPOTIFY_SERVICE", DEFAULT_RASPOTIFY_SERVICE))
        if service.strip().lower() in DISABLED_VALUES:
            return result("raspotify", WARNING, "No comprobado", "RASPOTIFY_SERVICE=none.")
        if not self.is_linux:
            return result("raspotify", WARNING, "No comprobable aquí",
                          "Raspotify solo se comprueba en la Raspberry (Linux).")
        unit = unit_name(service)
        try:
            state = service_state(self._run, service)
        except CommandFailed as exc:
            return result("raspotify", ERROR, "No se pudo consultar", short(exc))
        if state == "active":
            return result("raspotify", OK, "Activo", unit)
        if state in ("activating", "reloading"):
            return result("raspotify", WARNING, state_label(state), unit)
        return result("raspotify", ERROR, state_label(state),
                      f"{unit}: las alarmas Spotify usarán la música local o el WAV de emergencia.")

    # --- Spotify ---

    def check_spotify(self):
        client = self.spotify
        if client is None:
            return result("spotify", WARNING, "No disponible")
        if not client.is_configured:
            return result("spotify", WARNING, "No configurado",
                          "Faltan SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET en .env.")
        if not client.is_connected():
            return result("spotify", WARNING, "Sin cuenta vinculada",
                          "Vincula la cuenta en la página Spotify.")
        try:
            devices = client.get_devices(timeout=SPOTIFY_TIMEOUT)
        except SpotifyAuthError:
            return result("spotify", ERROR, "Sesión no válida",
                          "Spotify rechaza los tokens: vuelve a pulsar «Conectar Spotify».")
        except SpotifyConnectionError as exc:
            return result("spotify", ERROR, "Sin conexión con Spotify", short(exc))
        except SpotifyRateLimitError as exc:
            return result("spotify", WARNING, "Spotify pide esperar", short(exc))
        except SpotifyForbiddenError:
            return result("spotify", ERROR, "Acceso denegado",
                          "Hace falta Spotify Premium y tu usuario en «User Management».")
        except SpotifyError as exc:
            status = f"HTTP {exc.status}" if exc.status else "sin respuesta válida"
            return result("spotify", ERROR, "Error de la API", f"Spotify respondió con {status}.")

        saved_id = db.read_setting(self.database, DEVICE_ID_KEY)
        saved_name = db.read_setting(self.database, DEVICE_NAME_KEY)
        name = saved_name or self.config.get("SPOTIFY_DEVICE_NAME") or ""
        label = f"«{name}»" if name else "El dispositivo guardado"
        if not saved_id and not name:
            return result("spotify", WARNING, "Conectado",
                          "No hay dispositivo elegido para las alarmas.")
        try:
            # Misma regla que usan las alarmas, pero sin guardar nada ni transferir.
            choose_device(devices, saved_id, name)
        except DeviceConflict:
            return result("spotify", WARNING, "Conectado",
                          f"Hay varios dispositivos llamados {label}.")
        except DeviceUnavailable:
            # Spotify responde bien pero el dispositivo no está en la lista: el
            # único caso que el pre-flight intenta arreglar reiniciando Raspotify.
            return result("spotify", WARNING, "Conectado",
                          f"{label} no aparece ahora en Spotify.", code=SPOTIFY_DEVICE_MISSING)
        return result("spotify", OK, "Conectado", f"{label} disponible")

    # --- Bluetooth ---

    def check_bluetooth(self):
        if not self.is_linux:
            return result("bluetooth", WARNING, "No comprobable aquí",
                          "BlueALSA solo se comprueba en la Raspberry (Linux).")
        bluealsa = str(self.config.get("BLUEALSA_SERVICE", DEFAULT_BLUEALSA_SERVICE))
        player = str(self.config.get("BLUETOOTH_SERVICE", "bluealsa-aplay"))
        services = [s for s in (bluealsa, player) if s.strip().lower() not in DISABLED_VALUES]
        if not services:
            return result("bluetooth", WARNING, "No gestionado",
                          "BLUEALSA_SERVICE y BLUETOOTH_SERVICE están desactivados.")
        try:
            states = {s: service_state(self._run, s) for s in services}
        except CommandFailed as exc:
            return result("bluetooth", ERROR, "No se pudo consultar", short(exc))
        lines = [f"{unit_name(s)}: {state_label(st).lower()}" for s, st in states.items()]

        if bluealsa in states and states[bluealsa] != "active":
            return result("bluetooth", ERROR, "BlueALSA detenido", *lines)
        if player in states and states[player] != "active":
            if getattr(self.bluetooth, "paused", False):
                return result("bluetooth", OK, "Pausado por la alarma",
                              "Groove lo vuelve a arrancar con STOP o +10 MIN.", *lines)
            return result("bluetooth", ERROR, "Reproductor detenido",
                          "El audio Bluetooth no sonará por la Raspberry.", *lines)
        connected = self._connected_devices()
        if connected is not None:
            lines.append(plural(connected, "dispositivo conectado", "dispositivos conectados")
                         if connected else "Ningún dispositivo conectado")
        return result("bluetooth", OK, "BlueALSA activo", *lines)

    def _connected_devices(self):
        """Nº de dispositivos conectados (bluetoothctl, solo lectura) o None si no se sabe."""
        try:
            completed = run_command(self._run, ["bluetoothctl", "devices", "Connected"])
        except CommandFailed:
            return None
        if completed.returncode != 0:
            return None
        return sum(1 for line in (completed.stdout or "").splitlines()
                   if line.startswith("Device "))
