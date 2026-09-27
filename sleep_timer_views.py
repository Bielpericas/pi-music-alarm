"""Rutas del temporizador de sueño. La lógica vive en sleep_timer.py.

- GET  /sleep-timer/status  estado en memoria (sin red ni comandos).
- POST /sleep-timer/start   source (spotify|bluetooth), minutes (15/30/45/60)
                            y mac si es Bluetooth.
- POST /sleep-timer/cancel

Los formularios vuelven a la página de origen (`return_to`, de una lista
cerrada: nunca una URL que mande el navegador) con un aviso. Con
`Accept: application/json` responden JSON.
"""
from flask import Blueprint, current_app, flash, jsonify, redirect, request, url_for

from bluetooth_manager import InvalidMac
from sleep_timer import InvalidSleepTimer, SleepTimerError

bp = Blueprint("sleep_timer", __name__, url_prefix="/sleep-timer")

# return_to -> endpoint. Cualquier otro valor vuelve a la página principal.
RETURN_TO = {"index": "index", "spotify": "spotify.index", "bluetooth": "bluetooth.index"}


def manager():
    return current_app.extensions["sleep_timer"]


def _wants_json():
    return request.accept_mimetypes.best == "application/json"


def _reply(message, ok=True, status=200):
    if _wants_json():
        resp = jsonify(ok=ok, message=message, state=manager().status())
        resp.status_code = status
        resp.cache_control.no_store = True
        return resp
    flash(message, "message" if ok else "error")
    endpoint = RETURN_TO.get(request.form.get("return_to", ""), "index")
    return redirect(url_for(endpoint))


@bp.get("/status")
def status():
    resp = jsonify(manager().status())
    resp.cache_control.no_store = True
    return resp


@bp.post("/start")
def start():
    form = request.form
    try:
        timer = manager().start(form.get("source", ""), form.get("minutes", ""),
                                mac=form.get("mac") or None)
    except InvalidMac:
        return _reply("Dispositivo Bluetooth no válido.", ok=False, status=400)
    except InvalidSleepTimer as exc:
        return _reply(str(exc), ok=False, status=400)
    except SleepTimerError as exc:
        return _reply(str(exc), ok=False, status=409)
    return _reply(f"Temporizador de sueño: {timer.label} se apagará en {timer.minutes} min "
                  f"(a las {timer.expires_at.strftime('%H:%M')}).")


@bp.post("/cancel")
def cancel():
    if manager().cancel() is None:
        return _reply("No había ningún temporizador de sueño activo.")
    return _reply("Temporizador de sueño cancelado.")
