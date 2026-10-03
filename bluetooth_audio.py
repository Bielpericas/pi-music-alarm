"""Reproductor Bluetooth (BlueALSA) y prioridad de las alarmas.

La Pi recibe audio Bluetooth con `bluealsa-aplay`, que abre la misma tarjeta
USB que aplay y Raspotify. Prioridad: ALARMA > SPOTIFY > BLUETOOTH. Antes de
que suene una alarma se para el servicio (libera la tarjeta) y tras STOP o
+10 MIN se vuelve a arrancar.

Este módulo es el único que habla con systemd. Solo hace `start` / `stop` del
reproductor: nunca desconecta ni desempareja dispositivos, ni toca BlueZ.

Permisos: se ejecuta `systemctl --no-ask-password` como el usuario del
servicio (sin sudo, que además `NoNewPrivileges` bloquearía). Una regla de
polkit concede a ese usuario solo start/stop de esta unidad (ver README).

Reglas:
- Nunca lanza excepciones; devuelve si se confirmó la parada o restauración.
- Solo se vuelve a arrancar si lo paró Groove: si el usuario lo tenía parado,
  sigue parado.
- `pause()` es idempotente: si una alarma sustituye a otra, Bluetooth sigue
  parado sin arrancarse entre medias.
"""
import logging
import subprocess
import sys
import threading
from startup_budget import current_budget

logger = logging.getLogger("alarms")

DEFAULT_SERVICE = "bluealsa-aplay"
SYSTEMCTL_TIMEOUT = 8  # segundos; stop espera a que el proceso termine
ACTIVE_STATES = ("active", "activating", "reloading", "deactivating")
DISABLED_VALUES = ("", "none", "off", "false", "0")


def unit_name(service):
    """"bluealsa-aplay" -> "bluealsa-aplay.service" (así lo ve polkit)."""
    service = service.strip()
    return service if "." in service else f"{service}.service"


class BluetoothAudio:
    """Para y rearranca el servicio del reproductor Bluetooth vía systemctl."""

    def __init__(self, service=DEFAULT_SERVICE, run=subprocess.run,
                 timeout=SYSTEMCTL_TIMEOUT):
        self.unit = unit_name(service)
        self._run = run
        self.timeout = timeout
        self._paused = False  # True si lo ha parado Groove y hay que devolverlo
        self._restore_needed = False  # conservar intención incluso si start/stop falla
        self._lock = threading.Lock()

    @property
    def paused(self):
        return self._paused

    @property
    def restore_pending(self):
        return self._restore_needed

    def is_active(self):
        """True / False, o None si no se ha podido saber."""
        result = self._systemctl("is-active", quiet=True)
        if result is None:
            return None
        state = (result.stdout or "").strip()
        if state in ACTIVE_STATES:
            return True
        if state:  # inactive, failed, unknown...
            return False
        return None

    def pause(self):
        """True solo si la salida Bluetooth se confirmó libre."""
        with self._lock:
            active = self.is_active()
            if active is False:
                self._paused = self._restore_needed
                logger.info("Bluetooth (%s) no estaba activo: nada que pausar", self.unit)
                return True
            if active is True:
                self._restore_needed = True
            # Activo, o estado desconocido: se intenta parar igualmente.
            self._systemctl("stop")
            if self.is_active() is False:
                self._paused = True
                logger.info("Bluetooth pausado por alarma (%s detenido)", self.unit)
                return True
            self._paused = False
            logger.warning("No se pudo detener %s; la salida Bluetooth no está confirmada libre",
                           self.unit)
            return False

    def resume(self):
        """Vuelve a arrancar el reproductor si lo paró Groove."""
        with self._lock:
            if not self._restore_needed:
                self._paused = False
                return True  # nada que restaurar; no arrancar lo que ya estaba parado
            self._systemctl("start")
            state = self._systemctl("is-active", quiet=True)
            if state is not None and (state.stdout or "").strip() in {"active", "reloading"}:
                self._paused = False
                self._restore_needed = False
                logger.info("Bluetooth disponible de nuevo (%s arrancado)", self.unit)
                return True
            logger.warning("No se pudo volver a arrancar %s. Arráncalo con: "
                           "sudo systemctl start %s", self.unit, self.unit)
            return False

    def _systemctl(self, verb, quiet=False):
        """Ejecuta systemctl; devuelve el CompletedProcess o None si ni se pudo lanzar."""
        command = ["systemctl", "--no-ask-password", verb, self.unit]
        try:
            budget = current_budget()
            timeout = budget.timeout(self.timeout) if budget is not None else self.timeout
            result = self._run(command, capture_output=True, text=True,
                               timeout=timeout, check=False)
        except FileNotFoundError:
            logger.warning("systemctl no está disponible: no se gestiona Bluetooth")
            return None
        except subprocess.TimeoutExpired:
            logger.warning("systemctl %s %s tardó más de %s s", verb, self.unit, self.timeout)
            return None
        except Exception:
            logger.exception("Error al ejecutar systemctl %s %s", verb, self.unit)
            return None
        if result.returncode != 0 and not quiet:
            message = (result.stderr or "").strip()
            logger.warning("systemctl %s %s terminó con código %s: %s",
                           verb, self.unit, result.returncode, message)
        return result


class NoBluetooth:
    """Sin gestión de Bluetooth (Windows, tests o BLUETOOTH_SERVICE=none)."""

    unit = None
    paused = False
    restore_pending = False

    def pause(self):
        return True

    def resume(self):
        return True


def create_bluetooth(config, platform=None):
    """Construye el gestor según la configuración (BLUETOOTH_SERVICE)."""
    service = str(config.get("BLUETOOTH_SERVICE", DEFAULT_SERVICE))
    platform = platform or sys.platform
    if service.strip().lower() in DISABLED_VALUES or not platform.startswith("linux"):
        return NoBluetooth()
    return BluetoothAudio(service)
