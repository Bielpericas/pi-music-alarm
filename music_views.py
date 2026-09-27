"""Rutas web de la biblioteca de música local: ver, subir y borrar pistas.

Todo pasa por MusicLibrary: estas rutas nunca construyen rutas de ficheros
ni ejecutan nada con el nombre de un fichero.
"""
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

import db
from music_library import EXTENSIONS, UploadError, too_big_message

bp = Blueprint("music", __name__, url_prefix="/music")


def library():
    return current_app.extensions["music_library"]


def max_bytes():
    return current_app.config["LOCAL_MUSIC_MAX_UPLOAD_MB"] * 1024 * 1024


@bp.get("/")
def index():
    tracks = library().tracks()
    usage = {track.name: db.alarms_using_track(track.name) for track in tracks}
    return render_template("music.html", tracks=tracks, usage=usage,
                           max_mb=current_app.config["LOCAL_MUSIC_MAX_UPLOAD_MB"],
                           accept=",".join(EXTENSIONS))


@bp.post("/upload")
def upload():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Elige un archivo MP3, OGG o WAV para subir.", "error")
        return redirect(url_for("music.index"))
    try:
        name = library().save_upload(upload.filename, upload.stream, max_bytes())
    except UploadError as exc:
        flash(str(exc), "error")
    else:
        flash(f"«{name}» añadida a la música local.")
    return redirect(url_for("music.index"))


@bp.post("/delete")
def delete():
    name = request.form.get("name", "")
    try:
        deleted = library().delete(name)
    except OSError:
        current_app.logger.exception("No se pudo borrar %r", name)
        flash(f"No se pudo borrar «{name}».", "error")
        return redirect(url_for("music.index"))
    if not deleted:
        flash("Ese archivo no está en la biblioteca.", "error")
    else:
        flash(f"«{name}» eliminada de la música local.")
    return redirect(url_for("music.index"))


@bp.app_errorhandler(RequestEntityTooLarge)
def too_large(exc):
    """Subida mayor que el límite: mensaje amable en vez de un 413 en blanco."""
    if request.blueprint != "music":
        return exc
    flash(too_big_message(max_bytes()), "error")
    return redirect(url_for("music.index"))
