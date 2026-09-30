"""Unprivileged Spotify guest-mode manager (fixed systemd instances, no sudo).

SQLite stores only a boolean. The root helper owns Raspotify configuration and
publishes a sanitized observation of its live process. No credential access here.
"""
import json
import logging
from pathlib import Path
import subprocess
import sys
import threading

import db

logger = logging.getLogger(__name__)
SETTING = "spotify_guest_mode"
HELPER = Path("/usr/local/libexec/groove-spotify-mode")
STATUS = Path("/run/groove-spotify/status.json")
ERRORS = {None, "change_failed_rolled_back", "rollback_failed"}


class GuestModeError(Exception):
    pass


def unavailable(busy=False):
    return {"available": False, "enabled": None, "active": False, "busy": busy}


class NoSpotifyGuest:
    available = False

    def status(self):
        return unavailable()

    def reconcile(self):
        pass

    def set_enabled(self, enabled):
        raise GuestModeError("El control de Spotify invitados no está instalado.")

    def before_alarm(self):
        pass


class SpotifyGuest:
    available = True

    def __init__(self, database, run=subprocess.run, status_path=STATUS):
        self.database = database
        self.run = run
        self.status_path = status_path
        self._lock = threading.Lock()

    def _action(self, mode):
        if mode not in {"guest", "private", "status"}:
            raise ValueError("invalid mode")
        try:
            result = self.run(["systemctl", "--no-ask-password", "start",
                               f"groove-spotify-mode@{mode}.service"],
                              capture_output=True, text=True, timeout=200)
        except (OSError, subprocess.SubprocessError):
            raise GuestModeError("No se pudo comprobar o cambiar el modo de Spotify.") from None
        if result.returncode:
            # Never forward stderr/stdout: systemd may include sensitive diagnostics.
            raise GuestModeError("No se pudo cambiar el modo de Spotify. Comprueba su estado.")

    def _observe(self):
        self._action("status")
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
            if (raw.get("enabled") is not None and type(raw["enabled"]) is not bool
                    or type(raw.get("active")) is not bool or raw.get("error") not in ERRORS):
                raise ValueError()
            return {"available": True, "enabled": raw.get("enabled"),
                    "active": raw["active"], "busy": False}
        except (OSError, ValueError, TypeError, AttributeError):
            raise GuestModeError("No se pudo confirmar el estado real de Spotify.") from None

    def status(self):
        if not self._lock.acquire(blocking=False):
            return {"available": True, "enabled": None, "active": False, "busy": True}
        try:
            return self._observe()
        except GuestModeError:
            return {"available": True, "enabled": None, "active": False, "busy": False}
        finally:
            self._lock.release()

    def _apply(self, enabled):
        self._action("guest" if enabled else "private")
        state = self._observe()
        if state["enabled"] is not enabled or not state["active"]:
            raise GuestModeError("Raspotify no ha confirmado el cambio. Comprueba su estado.")
        return state

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if not self._lock.acquire(blocking=False):
            raise GuestModeError("Hay un cambio de Spotify en curso. Espera unos segundos.")
        try:
            try:
                observed = self._observe()
            except GuestModeError:
                if enabled:
                    raise
                observed = {"enabled": None}
            if enabled and observed["enabled"] is None:
                raise GuestModeError("Primero restaura el modo privado para activar invitados.")
            previous = observed["enabled"] is True
            state = self._apply(enabled)
            try:
                db.write_setting(self.database, SETTING, "true" if enabled else "false")
            except Exception:
                # Persistence is part of the transaction. Undo an unrecorded change.
                self._apply(previous)
                raise GuestModeError("No se pudo guardar el modo de Spotify; se ha restaurado el anterior.") from None
            return state
        finally:
            self._lock.release()

    def reconcile(self):
        """Called before the scheduler starts. Missing/corrupt state means private."""
        with self._lock:
            try:
                wanted = db.read_setting(self.database, SETTING) == "true"
            except Exception:
                wanted = False
            try:
                self._apply(wanted)
            except GuestModeError:
                logger.warning("Spotify invitados: reconciliación fallida; intentando modo privado.")
                try:
                    self._apply(False)
                    db.write_setting(self.database, SETTING, "false")
                except Exception:
                    logger.error("Spotify invitados: no se pudo confirmar el modo privado.")

    def before_alarm(self):
        """Spotify alarms reclaim the primary account; local alarms need no changes."""
        with self._lock:
            state = self._observe()
            if state["enabled"] is False and state["active"]:
                return
            self._apply(False)
            try:
                db.write_setting(self.database, SETTING, "false")
            except Exception:
                # Audio can safely use the restored primary account even if the
                # settings database cannot be updated. No raw exception in logs.
                logger.error("Spotify invitados: no se pudo guardar el modo privado de la alarma.")
            logger.info("Spotify invitados: modo privado restaurado para la alarma.")


def create_spotify_guest(config):
    service = config.get("RASPOTIFY_SERVICE", "raspotify")
    if not sys.platform.startswith("linux") or not HELPER.is_file() or service not in {"raspotify", "raspotify.service"}:
        return NoSpotifyGuest()
    return SpotifyGuest(config["DATABASE"])
