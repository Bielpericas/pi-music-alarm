"""Recuperación acotada al arrancar sin router y al volver la conexión con Spotify."""
import logging
import subprocess
import sys
import threading
import time

from apscheduler.triggers.interval import IntervalTrigger

import db
from bluetooth_audio import DISABLED_VALUES, unit_name
from spotify_client import SpotifyConnectionError, SpotifyError
from spotify_player import DEVICE_ID_KEY, DEVICE_NAME_KEY, DeviceUnavailable, choose_device
from startup_budget import StartupBudget, StartupExpired, current_budget

logger = logging.getLogger("alarms")
JOB_ID = "spotify-network-recovery"
INTERVAL_SECONDS = 30
RECOVERY_LIMIT = 3
RECOVERY_COOLDOWN = 60
PROBE_TIMEOUT = 10


class SpotifyNetworkRecovery:
    def __init__(self, database, client, playback, alarm_player, guests, service="raspotify",
                 preferred_name="", enabled=True, platform=None, run=subprocess.run,
                 clock=time.monotonic):
        self.database = database
        self.client = client
        self.playback = playback
        self.alarm_player = alarm_player
        self.guests = guests
        self.service = service
        self.preferred_name = preferred_name
        self.enabled = (enabled and (platform or sys.platform).startswith("linux") and
                        str(service).strip().lower() not in DISABLED_VALUES)
        self.run = run
        self.clock = clock
        self._lock = threading.Lock()
        self._armed = True  # también cubre una caída anterior al arranque de Groove
        self._offline = False
        self._online_samples = 0
        self._attempts = 0
        self._next_attempt = 0
        self.last_status = "Esperando comprobación" if self.enabled else "Desactivada"

    def start(self, scheduler):
        if self.enabled:
            scheduler.add_job(self.check, IntervalTrigger(seconds=INTERVAL_SECONDS), id=JOB_ID,
                              max_instances=1, coalesce=True, misfire_grace_time=15,
                              replace_existing=True)
            logger.info("Recuperación de Raspotify tras arranque/caída de red activada (cada %s s)",
                        INTERVAL_SECONDS)

    def _command(self, *args):
        budget = current_budget()
        return self.run(["systemctl", "--no-ask-password", *args], capture_output=True,
                        text=True, timeout=budget.timeout(5))

    def check(self):
        if not self.enabled or not self._lock.acquire(blocking=False):
            return
        try:
            with StartupBudget(PROBE_TIMEOUT, clock=self.clock).activate():
                self._check()
        except SpotifyConnectionError:
            if not self._offline:
                logger.info("Raspotify: conexión con Spotify no disponible; se espera a que vuelva")
                self._attempts = 0
            self._offline = True
            self._armed = True
            self._online_samples = 0
            self.last_status = "Esperando conexión con Spotify"
        except StartupExpired:
            self.last_status = "Comprobación agotada; sin reinicio adicional"
        except SpotifyError:
            # Auth, Premium, 429 y 5xx no prueban que haya vuelto la red.
            self._online_samples = 0
            self.last_status = "Comprobación Spotify no válida; sin reinicio"
        except (OSError, subprocess.SubprocessError):
            self.last_status = "No se pudo comprobar o reiniciar Raspotify"
            logger.warning("Recuperación de Raspotify: operación de systemd no confirmada")
        except Exception:
            self.last_status = "Error de recuperación; ver log"
            logger.exception("Error en recuperación de Raspotify; las alarmas conservan su respaldo")
        finally:
            self._lock.release()

    def _check(self):
        if not self.client.is_configured or not self.client.is_connected():
            self.last_status = "Spotify sin configurar o vincular"
            return
        # Una lectura de la API confirma Internet/Spotify, no solo asociación Wi-Fi.
        devices = self.client.get_devices(timeout=3)
        self._online_samples += 1
        if self._offline:
            logger.info("Raspotify: conexión con Spotify restablecida; comprobando Groove")
            self._offline = False
        if self.playback.active is not None or getattr(self.playback, "manual_spotify_device", None) is not None:
            self.last_status = "Recuperación aplazada: alarma activa"
            return
        saved_id = db.read_setting(self.database, DEVICE_ID_KEY)
        name = db.read_setting(self.database, DEVICE_NAME_KEY) or self.preferred_name
        if not saved_id and not name:
            self.last_status = "Selecciona el dispositivo Spotify de Groove"
            return
        try:
            choose_device(devices, saved_id, name)
            present = True
        except DeviceUnavailable:
            present = False
        # DeviceConflict sale como SpotifyError: nunca escoger ni reiniciar al azar.
        state = self._command("is-active", unit_name(self.service)).stdout.strip()
        if state == "active" and present:
            if self._attempts:
                logger.info("Raspotify recuperado: Groove vuelve a aparecer en Spotify")
            self._armed = False
            self._attempts = 0
            self.last_status = "Groove disponible; sin reinicio"
            return
        if state not in {"active", "failed", "inactive"}:
            self.last_status = "Raspotify en transición; se espera"
            return
        if not self._armed:
            self.last_status = "Sin episodio de arranque/caída de red; sin reinicio"
            return
        if self._attempts >= RECOVERY_LIMIT:
            if self.last_status != "Agotados los tres intentos; revisa Diagnóstico":
                logger.warning("Recuperación de Raspotify: agotados los tres intentos; "
                               "revisa Diagnóstico y permisos. No se reinicia indefinidamente")
            self.last_status = "Agotados los tres intentos; revisa Diagnóstico"
            return
        if self._online_samples < 2 or self.clock() < self._next_attempt:
            self.last_status = "Esperando conexión estable o pausa entre intentos"
            return
        if self._command("is-enabled", unit_name(self.service)).stdout.strip() not in {"enabled", "enabled-runtime"}:
            self.last_status = "Raspotify no habilitado; sin reinicio"
            return
        self._restart_when_idle()

    def _restart_when_idle(self):
        # Mismo orden de locks que el arranque. No esperar si alguien controla audio.
        if not self.playback._lock.acquire(blocking=False):
            self.last_status = "Recuperación aplazada: reproducción ocupada"
            return
        try:
            if (self.playback.active is not None or
                    getattr(self.playback, "manual_spotify_device", None) is not None or
                    not self.alarm_player.service_lock.acquire(blocking=False)):
                self.last_status = "Recuperación aplazada: alarma o mantenimiento Spotify"
                return
            try:
                guest_lock = getattr(self.guests, "_lock", None)
                if guest_lock is not None and not guest_lock.acquire(blocking=False):
                    self.last_status = "Recuperación aplazada: cambio de invitados"
                    return
                try:
                    if self.guests.available:
                        state = self.guests._observe()
                        if state["enabled"] is not False:
                            self.last_status = "Recuperación aplazada: invitados o identidad desconocida"
                            return
                    # Contar incluso un rechazo por permisos: no insistir indefinidamente.
                    self._attempts += 1
                    self._next_attempt = self.clock() + RECOVERY_COOLDOWN
                    logger.warning("Raspotify: red disponible pero Groove no está listo; "
                                   "reinicio de %s (%s/%s)", unit_name(self.service),
                                   self._attempts, RECOVERY_LIMIT)
                    result = self._command("restart", unit_name(self.service))
                    self.last_status = ("Reinicio solicitado; pendiente de comprobar Groove" if result.returncode == 0
                                        else "Reinicio rechazado; comprueba permiso de polkit")
                    if result.returncode:
                        logger.warning("Recuperación de Raspotify: reinicio rechazado; "
                                       "comprueba deploy/install-raspotify-permission.sh")
                finally:
                    if guest_lock is not None:
                        guest_lock.release()
            finally:
                self.alarm_player.service_lock.release()
        finally:
            self.playback._lock.release()
