"""Rutas web de la sección Spotify. No hacen HTTP: todo pasa por SpotifyClient."""
import secrets

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for,
)

import db
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
    )


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
            client().transfer_playback(device_id, play=False)
            flash("Reproducción transferida al dispositivo seleccionado.")
        except SpotifyError as exc:
            report(exc, "transfer")
    return redirect(url_for("spotify.index"))


@bp.post("/play")
def play():
    device_id = _selected_device()
    if device_id:
        try:
            client().play(device_id)
            flash("Play enviado.")
        except SpotifyError as exc:
            report(exc, "play")
    return redirect(url_for("spotify.index"))


@bp.post("/pause")
def pause():
    device_id = _selected_device()
    if device_id:
        try:
            client().pause(device_id)
            flash("Pausa enviada.")
        except SpotifyError as exc:
            report(exc, "pause")
    return redirect(url_for("spotify.index"))


# --- Buscador del formulario de alarmas (JSON para fetch) -------------------
#
# Solo lectura y aislado: cualquier fallo se convierte en un JSON de error y
# nunca toca el scheduler, las alarmas ni el reproductor. No se registran ni la
# búsqueda ni tokens/cabeceras: solo el tipo de error.

def _json_response(payload, status=200, retry_after=None):
    resp = jsonify(payload)
    resp.status_code = status
    resp.cache_control.no_store = True
    if retry_after:
        resp.headers["Retry-After"] = str(retry_after)
    return resp


def _json_error(code, message, status, retry_after=None):
    payload = {"ok": False, "error": code, "message": message}
    if retry_after:
        payload["retry_after"] = retry_after
    return _json_response(payload, status, retry_after)


def _ui_error(exc, action):
    """SpotifyError (o fallo inesperado) -> respuesta JSON con mensaje amable."""
    current_app.logger.warning("Spotify (%s): %s", action, type(exc).__name__)
    if isinstance(exc, SpotifyNotConfiguredError):
        return _json_error("not_configured", "Spotify no está configurado en Groove.", 409)
    if isinstance(exc, SpotifyAuthError):
        return _json_error("auth", "La sesión de Spotify ha caducado. Vuelve a conectar "
                           "Spotify.", 401)
    if isinstance(exc, SpotifyForbiddenError):
        return _json_error("forbidden", "Spotify no permite esta búsqueda con tu cuenta.", 403)
    if isinstance(exc, SpotifyRateLimitError):
        return _json_error("rate_limited", f"Spotify pide esperar {exc.retry_after} s antes "
                           "de volver a buscar.", 429, exc.retry_after)
    if isinstance(exc, SpotifyNotFoundError):
        return _json_error("not_found", "Spotify no encuentra ese contenido.", 404)
    if isinstance(exc, SpotifyConnectionError):
        return _json_error("unavailable", "No se pudo conectar con Spotify. Comprueba la "
                           "conexión a Internet.", 503)
    return _json_error("spotify_error", "Spotify no respondió como se esperaba. Inténtalo "
                       "de nuevo.", 502)


def _require_linked():
    spotify = client()
    if not spotify.is_configured:
        return _json_error("not_configured", "Spotify no está configurado en Groove.", 409)
    if not spotify.is_connected():
        return _json_error("not_connected", "Conecta Spotify para buscar música desde "
                           "Groove.", 409)
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
