"""Rutas web de la sección Spotify. No hacen HTTP: todo pasa por SpotifyClient."""
import secrets

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)

import db
from spotify_guest import GuestModeError
from spotify_player import DEVICE_ID_KEY, DEVICE_NAME_KEY
from spotify_client import (
    SEARCH_MIN_CHARS,
    SpotifyAuthError,
    SpotifyConnectionError,
    SpotifyError,
    SpotifyForbiddenError,
    SpotifyNotConfiguredError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
)

bp = Blueprint("spotify", __name__, url_prefix="/spotify")


def client():
    return current_app.extensions["spotify"]


def friendly_error(exc):
    """Traduce los errores de Spotify a un mensaje útil para la interfaz."""
    if isinstance(exc, SpotifyNotConfiguredError):
        return str(exc)
    if isinstance(exc, SpotifyAuthError):
        return f"{exc} Vuelve a pulsar «Conectar Spotify»."
    if isinstance(exc, SpotifyForbiddenError):
        return (f"{exc}. Hace falta Spotify Premium y que tu usuario esté en "
                "«User Management» de la app en el Dashboard.")
    if isinstance(exc, SpotifyNotFoundError):
        return (f"{exc}. Abre Spotify en el dispositivo, selecciónalo aquí y "
                "pulsa «Transferir».")
    if isinstance(exc, SpotifyRateLimitError):
        return str(exc)
    if isinstance(exc, SpotifyConnectionError):
        return str(exc)
    return f"Error de Spotify: {exc}"


def report(exc, action):
    current_app.logger.warning("Spotify (%s): %s", action, exc)
    flash(friendly_error(exc), "error")


@bp.get("/")
def index():
    session.setdefault("spotify_guest_csrf", secrets.token_urlsafe(32))
    spotify = client()
    devices, error = [], None
    connected = spotify.is_configured and spotify.is_connected()
    if connected:
        try:
            devices = spotify.get_devices()
        except SpotifyError as exc:
            error = friendly_error(exc)
            connected = spotify.is_connected()  # puede haberse desvinculado
    selected_id = db.get_setting(DEVICE_ID_KEY)
    return render_template(
        "spotify.html",
        configured=spotify.is_configured,
        connected=connected,
        devices=devices,
        active=next((d for d in devices if d.get("is_active")), None),
        selected_id=selected_id,
        selected_name=db.get_setting(DEVICE_NAME_KEY),
        selected_available=any(d.get("id") == selected_id for d in devices),
        redirect_uri=spotify.redirect_uri,
        error=error,
        guest=current_app.extensions["spotify_guest"].status(),
        guest_csrf=session["spotify_guest_csrf"],
    )


@bp.get("/guest/status")
def guest_status():
    response = jsonify(current_app.extensions["spotify_guest"].status())
    response.cache_control.no_store = True
    return response


@bp.post("/guest")
def guest_mode():
    expected = session.get("spotify_guest_csrf")
    token = request.form.get("csrf", "")
    if not expected or not secrets.compare_digest(expected, token):
        abort(400)
    if (set(request.form) != {"csrf", "enabled"}
            or any(len(request.form.getlist(key)) != 1 for key in request.form)
            or request.form["enabled"] not in {"true", "false"}):
        abort(400)
    active = current_app.extensions["playback"].active
    if active is not None and active.alarm["source"] == "spotify":
        flash("Espera a que termine la alarma de Spotify antes de cambiar el modo.", "error")
        return redirect(url_for("spotify.index"))
    try:
        state = current_app.extensions["spotify_guest"].set_enabled(request.form["enabled"] == "true")
    except GuestModeError as exc:
        flash(str(exc), "error")  # only fixed, sanitized messages from our manager
    else:
        flash("Spotify invitados activado." if state["enabled"] else "Spotify vuelve a tu cuenta.")
    return redirect(url_for("spotify.index"))


@bp.get("/connect")
def connect():
    state = secrets.token_urlsafe(16)
    session["spotify_state"] = state
    try:
        return redirect(client().get_authorize_url(state))
    except SpotifyError as exc:
        report(exc, "connect")
        return redirect(url_for("spotify.index"))


@bp.get("/callback")
def callback():
    expected = session.pop("spotify_state", None)
    if request.args.get("error"):
        flash(f"Spotify no autorizó la conexión: {request.args['error']}.", "error")
    elif not expected or request.args.get("state") != expected:
        flash("La respuesta de Spotify no coincide con la petición (state). "
              "Abre la app en http://127.0.0.1:5000 y vuelve a intentarlo.", "error")
    elif not request.args.get("code"):
        flash("Spotify no devolvió ningún código de autorización.", "error")
    else:
        try:
            client().exchange_code(request.args["code"])
            flash("Cuenta de Spotify vinculada.")
        except SpotifyError as exc:
            report(exc, "callback")
    return redirect(url_for("spotify.index"))


@bp.post("/disconnect")
def disconnect():
    client().disconnect()
    flash("Spotify desvinculado.")
    return redirect(url_for("spotify.index"))


@bp.post("/device")
def select_device():
    device_id = request.form.get("device_id", "").strip()
    if not device_id:
        flash("Dispositivo no válido.", "error")
    else:
        db.set_setting(DEVICE_ID_KEY, device_id)
        db.set_setting(DEVICE_NAME_KEY, request.form.get("device_name", "").strip()[:100])
        flash("Dispositivo seleccionado.")
    return redirect(url_for("spotify.index"))


def _selected_device():
    device_id = db.get_setting(DEVICE_ID_KEY)
    if not device_id:
        flash("Primero selecciona un dispositivo.", "error")
    return device_id


@bp.post("/transfer")
def transfer():
    device_id = _selected_device()
    if device_id:
        try:
            current_app.extensions["playback"].control_spotify("transfer", device_id)
            flash("Reproducción transferida al dispositivo seleccionado.")
        except SpotifyError as exc:
            report(exc, "transfer")
    return redirect(url_for("spotify.index"))


@bp.post("/play")
def play():
    device_id = _selected_device()
    if device_id:
        try:
            current_app.extensions["playback"].control_spotify("play", device_id)
            flash("Play enviado.")
        except SpotifyError as exc:
            report(exc, "play")
    return redirect(url_for("spotify.index"))


@bp.post("/pause")
def pause():
    device_id = _selected_device()
    if device_id:
        try:
            current_app.extensions["playback"].control_spotify("pause", device_id)
            flash("Pausa enviada.")
        except SpotifyError as exc:
            report(exc, "pause")
    return redirect(url_for("spotify.index"))


# --- Buscador del formulario de alarmas (JSON para fetch) -------------------
#
# Solo lectura y aislado: cualquier fallo se convierte en un JSON de error y
# nunca toca el scheduler, las alarmas ni el reproductor. El buscador es una
# comodidad: el enlace manual del formulario funciona siempre, así que los
# mensajes lo recuerdan. No se registran ni la búsqueda ni tokens/cabeceras.
#
# `search_available: false` indica un error que no se arregla reintentando
# (sin vincular, sin permiso, hay que volver a vincular...): el navegador deja
# de buscar en esa página. `action: "reconnect"` añade el enlace a Spotify.

MANUAL_HINT = "Puedes pegar un enlace de Spotify arriba."
SEARCH_UNAVAILABLE = f"La búsqueda de Spotify no está disponible. {MANUAL_HINT}"
RECONNECT = f"Vuelve a vincular Spotify para activar la búsqueda. {MANUAL_HINT}"
NOT_CONNECTED = "Conecta Spotify para usar el buscador. También puedes pegar un enlace directamente."

# Textos de Spotify en un 403 que indican permisos/scopes de la autorización.
_SCOPE_HINTS = ("scope",)
# ...o que la app/cuenta no tiene acceso a la API (modo desarrollo, allowlist,
# Premium del propietario de la app...). Volver a vincular no lo arregla.
_ACCESS_HINTS = ("not registered", "developer dashboard", "user management", "premium",
                 "not available", "not allowed", "restricted")


def _json_response(payload, status=200, retry_after=None):
    resp = jsonify(payload)
    resp.status_code = status
    resp.cache_control.no_store = True
    if retry_after:
        resp.headers["Retry-After"] = str(retry_after)
    return resp


def _json_error(code, message, status, retry_after=None, available=True, action=None):
    payload = {"ok": False, "error": code, "message": message, "search_available": available}
    if retry_after:
        payload["retry_after"] = retry_after
    if action:
        payload["action"] = action
    return _json_response(payload, status, retry_after)


def forbidden_kind(exc, spotify):
    """Clasifica un 403 de Spotify.

    - "reauth": falta un scope en la autorización guardada o Spotify lo dice.
    - "app_access": la app o la cuenta no tienen acceso a ese endpoint.
    - "forbidden": otro 403 (no se sabe más; no se culpa a la cuenta).
    """
    text = f"{exc.api_message or ''} {exc.reason or ''}".lower()
    try:
        missing = spotify.missing_scopes()
    except Exception:  # noqa: BLE001 - solo es para elegir el mensaje
        missing = set()
    if missing or any(hint in text for hint in _SCOPE_HINTS):
        return "reauth"
    if any(hint in text for hint in _ACCESS_HINTS):
        return "app_access"
    return "forbidden"


def _ui_error(exc, action):
    """SpotifyError (o fallo inesperado) -> respuesta JSON con mensaje amable."""
    if isinstance(exc, SpotifyForbiddenError):
        kind = forbidden_kind(exc, client())
        # Motivo de Spotify (sin tokens) para poder diagnosticarlo en el log.
        current_app.logger.warning("Spotify (%s): 403 %s · reason=%s · message=%s", action,
                                   kind, exc.reason, exc.api_message)
        if kind == "reauth":
            return _json_error("reauth", RECONNECT, 403, available=False, action="reconnect")
        return _json_error(kind, SEARCH_UNAVAILABLE, 403, available=False)
    current_app.logger.warning("Spotify (%s): %s", action, type(exc).__name__)
    if isinstance(exc, SpotifyNotConfiguredError):
        return _json_error("not_configured", SEARCH_UNAVAILABLE, 409, available=False)
    if isinstance(exc, SpotifyAuthError):
        # El cliente ya renovó el token una vez: no se insiste (sin bucles).
        return _json_error("reauth", RECONNECT, 401, available=False, action="reconnect")
    if isinstance(exc, SpotifyRateLimitError):
        return _json_error("rate_limited", f"Spotify pide esperar {exc.retry_after} s antes "
                           f"de volver a buscar. {MANUAL_HINT}", 429, exc.retry_after)
    if isinstance(exc, SpotifyNotFoundError):
        return _json_error("not_found", "Spotify no encuentra ese contenido.", 404)
    if isinstance(exc, SpotifyConnectionError):
        return _json_error("unavailable", f"No se pudo conectar con Spotify. {MANUAL_HINT}", 503)
    return _json_error("spotify_error", f"Spotify no respondió como se esperaba. {MANUAL_HINT}",
                       502)


def _require_linked():
    spotify = client()
    if not spotify.is_configured:
        return _json_error("not_configured", SEARCH_UNAVAILABLE, 409, available=False)
    if not spotify.is_connected():
        return _json_error("not_connected", NOT_CONNECTED, 409, available=False,
                           action="reconnect")
    return None


@bp.get("/search")
def search():
    """GET /spotify/search?q=... -> resultados normalizados agrupados por tipo."""
    query = " ".join(request.args.get("q", "").split())
    if len(query) < SEARCH_MIN_CHARS:
        return _json_error("short_query", f"Escribe al menos {SEARCH_MIN_CHARS} caracteres.",
                           400)
    try:
        blocked = _require_linked()
        if blocked:
            return blocked
        results = client().search(query)
    except Exception as exc:  # noqa: BLE001 - el buscador nunca debe romper nada
        return _ui_error(exc, "search")
    return _json_response({"ok": True, "query": query, "results": results})


@bp.get("/lookup")
def lookup():
    """GET /spotify/lookup?uri=... -> metadata de un URI/URL (alarmas antiguas)."""
    try:
        blocked = _require_linked()
        if blocked:
            return blocked
        item = client().get_item(request.args.get("uri", ""))
    except ValueError:
        return _json_error("invalid_uri", "No es un enlace válido de canción, álbum o "
                           "playlist de Spotify.", 400)
    except Exception as exc:  # noqa: BLE001
        return _ui_error(exc, "lookup")
    if item is None:
        return _json_error("not_found", "Spotify no encuentra ese contenido.", 404)
    return _json_response({"ok": True, "item": item})
