"""App Flask del despertador: CRUD de alarmas + scheduler + audio local + Spotify."""
import os
import re
from pathlib import Path

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, send_from_directory, url_for,
)
from werkzeug.serving import is_running_from_reloader

import db
import diagnostics_views
import music_views
import scheduler
import spotify_views
import ui
from audio_player import create_music_player, create_player
from bluetooth_audio import DEFAULT_SERVICE as DEFAULT_BLUETOOTH_SERVICE, NoBluetooth, create_bluetooth
from health import DEFAULT_BLUEALSA_SERVICE, DEFAULT_RASPOTIFY_SERVICE, HealthChecker
from music_library import LocalMusic, MusicLibrary
from playback import AlarmPlaybackManager
from preflight import DEFAULT_MINUTES as DEFAULT_PREFLIGHT_MINUTES, PreflightScheduler
from spotify_client import (
    MAX_META_NAME, MAX_META_SUBTITLE, clean_text, create_spotify_client, parse_spotify_uri,
)
from spotify_player import RETRY_DELAYS, SpotifyAlarmPlayer

DEFAULT_SOUND = Path(__file__).parent / "sounds" / "alarm.wav"
DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_NAME_LEN = 50
SOURCES = ("local", "spotify")
MAX_FADE_MINUTES = 30
FADE_CHOICES = (0, 1, 2, 3, 5, 10, 15, 20, 30)  # opciones del desplegable
DURATION_CHOICES = (15, 30, 45, 60, 0)  # duración máxima (auto-stop); 0 = sin límite


def _int_field(form, key, default, low, high, label, errors):
    """Entero del formulario entre low y high; si falta, `default`."""
    raw = form.get(key, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{label} debe ser un número.")
        return default
    if not low <= value <= high:
        errors.append(f"{label} debe estar entre {low} y {high}.")
    return value


def validate_alarm_form(form, tracks=(), keep_track=None):
    """Devuelve (datos, errores) a partir del formulario.

    `tracks`: pistas de la biblioteca. `keep_track`: la pista que ya tenía la
    alarma, que se puede conservar aunque ahora no esté (se avisa en el formulario).
    """
    errors = []
    name = form.get("name", "").strip()
    time = form.get("time", "").strip()
    raw_days = form.getlist("days")
    source = form.get("source", "local")
    spotify_uri = form.get("spotify_uri", "").strip()

    if not name:
        errors.append("El nombre es obligatorio.")
    elif len(name) > MAX_NAME_LEN:
        errors.append(f"El nombre no puede superar {MAX_NAME_LEN} caracteres.")

    if not TIME_RE.match(time):
        errors.append("La hora debe tener formato HH:MM.")

    try:
        days = sorted({int(d) for d in raw_days})
    except ValueError:
        days = []
        errors.append("Días no válidos.")
    if any(d < 0 or d > 6 for d in days):
        errors.append("Días no válidos.")

    if source not in SOURCES:
        errors.append("Fuente de sonido no válida.")
        source = "local"
    spotify_name = spotify_subtitle = None
    if source == "spotify":
        try:
            spotify_uri = parse_spotify_uri(spotify_uri)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            spotify_name, spotify_subtitle = spotify_metadata(form, spotify_uri)
    else:
        spotify_uri = None  # las alarmas locales no guardan URI

    # Volumen: se guarda siempre (modelo simple), pero de momento solo lo usa Spotify.
    volume_start = _int_field(form, "volume_start", db.DEFAULT_VOLUME_START, 0, 100,
                              "El volumen inicial", errors)
    volume_end = _int_field(form, "volume_end", db.DEFAULT_VOLUME_END, 0, 100,
                            "El volumen final", errors)
    fade_minutes = _int_field(form, "fade_minutes", db.DEFAULT_FADE_MINUTES, 0,
                              MAX_FADE_MINUTES, "La duración del fade-in", errors)
    if volume_start > volume_end:
        errors.append("El volumen inicial no puede ser mayor que el final.")
    max_duration = _int_field(form, "max_duration_minutes", db.DEFAULT_MAX_DURATION, 0, 60,
                              "La duración máxima", errors)
    if max_duration not in DURATION_CHOICES:
        errors.append("Elige una duración máxima de la lista.")
        max_duration = db.DEFAULT_MAX_DURATION

    # Música local: solo un nombre de la biblioteca (nunca una ruta). Vacío = aleatoria.
    local_track = form.get("local_track", "").strip() or None
    if local_track is not None and local_track not in tracks and local_track != keep_track:
        errors.append("La música local elegida no está en la biblioteca.")
        local_track = None

    data = {"name": name, "time": time, "days": days,
            "source": source, "spotify_uri": spotify_uri,
            "spotify_name": spotify_name, "spotify_subtitle": spotify_subtitle,
            "volume_start": volume_start, "volume_end": volume_end,
            "fade_minutes": fade_minutes, "max_duration_minutes": max_duration,
            "local_track": local_track}
    return data, errors


def spotify_metadata(form, spotify_uri):
    """(nombre, subtítulo) que manda el buscador para `spotify_uri`, saneados.

    Solo es texto para mostrar: la reproducción usa únicamente el URI validado.
    Se descarta si no corresponde a ese URI (p. ej. se eligió un resultado y
    luego se pegó otro enlace a mano) o si viene vacía.
    """
    try:
        meta_uri = parse_spotify_uri(form.get("spotify_meta_uri", ""))
    except ValueError:
        return None, None
    name = clean_text(form.get("spotify_name", ""), MAX_META_NAME)
    if meta_uri != spotify_uri or not name:
        return None, None
    return name, clean_text(form.get("spotify_subtitle", ""), MAX_META_SUBTITLE) or None


def describe_volume(alarm):
    """Texto para la lista: "Volumen: 20 → 60 % · 5 min"."""
    start, end, minutes = alarm["volume_start"], alarm["volume_end"], alarm["fade_minutes"]
    if minutes == 0 or start == end:
        return f"Volumen: {end} %"
    return f"Volumen: {start} → {end} % · {minutes} min"


def describe_source(alarm):
    """Texto corto para la lista: "Local", "Spotify · Discovery" o, si no hay
    metadata guardada, "Spotify · playlist"."""
    if alarm["source"] == "spotify" and alarm["spotify_uri"]:
        name = alarm["spotify_name"] if "spotify_name" in alarm.keys() else None
        return f"Spotify · {name or alarm['spotify_uri'].split(':')[1]}"
    return "Local"


def playback_key(active, snoozes):
    """Huella del estado de reproducción: si cambia, la página se recarga."""
    parts = [f"{active.id}@{active.started_at.isoformat()}@{active.via}" if active else "-"]
    parts += [f"{p.alarm['id']}@{p.run_at.isoformat()}" for p in snoozes]
    return "|".join(parts)


def format_days(days_csv):
    if not days_csv:
        return "Una vez"
    days = [int(d) for d in days_csv.split(",")]
    if days == list(range(7)):
        return "Todos los días"
    if days == list(range(5)):
        return "Lunes a viernes"
    if days == [5, 6]:
        return "Fin de semana"
    return ", ".join(DAY_NAMES[d] for d in days)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def create_app(config=None, player=None, spotify=None, bluetooth=None, music=None):
    """Crea la app. `player`, `spotify`, `bluetooth` y `music` permiten inyectar
    dobles (mocks) en tests."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        DATABASE=str(Path(app.instance_path) / "alarms.db"),
        ALARM_LOG=str(Path(app.instance_path) / "alarms.log"),
        SCHEDULER_ENABLED=True,
        AUDIO_BACKEND=os.environ.get("AUDIO_BACKEND", "local"),
        SOUND_PATH=os.environ.get("ALARM_SOUND", str(DEFAULT_SOUND)),
        ALSA_DEVICE=os.environ.get("ALSA_DEVICE", ""),  # solo Linux (aplay -D y ffmpeg)
        # Biblioteca de música local (instance/music/, junto a la BD): ffmpeg la
        # reproduce por ALSA_DEVICE (o plughw:CARD=Device,DEV=0 si está vacío).
        FFMPEG_BINARY=os.environ.get("FFMPEG_BINARY", "ffmpeg"),
        LOCAL_MUSIC_MAX_UPLOAD_MB=_env_int("LOCAL_MUSIC_MAX_UPLOAD_MB", 50),
        # Servicio systemd del reproductor Bluetooth que se para mientras suena
        # una alarma ("none" = no gestionar Bluetooth). Ver bluetooth_audio.py.
        BLUETOOTH_SERVICE=os.environ.get("BLUETOOTH_SERVICE", DEFAULT_BLUETOOTH_SERVICE),
        # Diagnóstico (solo consulta su estado): BlueALSA y Raspotify.
        BLUEALSA_SERVICE=os.environ.get("BLUEALSA_SERVICE", DEFAULT_BLUEALSA_SERVICE),
        RASPOTIFY_SERVICE=os.environ.get("RASPOTIFY_SERVICE", DEFAULT_RASPOTIFY_SERVICE),
        # Minutos antes de cada alarma en que se ejecuta el pre-flight (0 = desactivado).
        ALARM_PREFLIGHT_MINUTES=_env_int("ALARM_PREFLIGHT_MINUTES", DEFAULT_PREFLIGHT_MINUTES),
        SPOTIFY_CLIENT_ID=os.environ.get("SPOTIFY_CLIENT_ID", ""),
        SPOTIFY_CLIENT_SECRET=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        # Nombre del dispositivo de las alarmas si aún no se ha elegido ninguno
        # en la página Spotify (p. ej. "Groove", el nombre de Raspotify).
        SPOTIFY_DEVICE_NAME=os.environ.get("SPOTIFY_DEVICE_NAME", ""),
        SPOTIFY_REDIRECT_URI=os.environ.get(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/spotify/callback"
        ),
    )
    if config:
        app.config.update(config)
    app.config.setdefault("MUSIC_DIR", str(Path(app.config["DATABASE"]).parent / "music"))
    # Límite de la petición entera: el fichero más el margen del multipart.
    # music_views vuelve a comprobar el tamaño exacto del fichero al guardarlo.
    if app.config.get("MAX_CONTENT_LENGTH") is None:  # Flask lo trae a None (sin límite)
        app.config["MAX_CONTENT_LENGTH"] = (app.config["LOCAL_MUSIC_MAX_UPLOAD_MB"] + 1) * 1024 * 1024

    db.init_app(app)
    scheduler.setup_logging(app.config["ALARM_LOG"])
    app.jinja_env.filters["format_days"] = format_days
    app.extensions["audio_player"] = player or create_player(app.config)
    library = MusicLibrary(app.config["MUSIC_DIR"])
    library.ensure_dir()
    app.extensions["music_library"] = library
    if music is None and not app.testing:  # en tests nunca se lanza ffmpeg
        track_player = create_music_player(app.config)
        music = LocalMusic(library, track_player) if track_player else None
    app.extensions["music"] = music
    app.extensions["spotify"] = spotify or create_spotify_client(app.config)
    # En tests nunca se toca systemd salvo que se inyecte un doble.
    app.extensions["bluetooth"] = bluetooth or (
        NoBluetooth() if app.testing else create_bluetooth(app.config))
    app.extensions["spotify_alarm"] = SpotifyAlarmPlayer(
        app.extensions["spotify"], app.config["DATABASE"],
        preferred_name=app.config["SPOTIFY_DEVICE_NAME"],
        retry_delays=app.config.get("SPOTIFY_RETRY_DELAYS", RETRY_DELAYS),
    )
    app.jinja_env.filters["describe_source"] = describe_source
    app.jinja_env.filters["describe_volume"] = describe_volume
    app.jinja_env.filters["fade_label"] = ui.fade_label
    app.jinja_env.filters["duration_label"] = ui.duration_label
    app.jinja_env.filters["human_size"] = ui.human_size
    app.jinja_env.filters["selected_days"] = ui.selected_days
    app.jinja_env.filters["spotify_kind"] = ui.spotify_kind
    app.jinja_env.filters["spotify_kind_label"] = ui.spotify_kind_label
    app.jinja_env.filters["spotify_link"] = ui.spotify_link
    app.register_blueprint(spotify_views.bp)
    app.register_blueprint(music_views.bp)
    app.register_blueprint(diagnostics_views.bp)
    # Estado de la alarma que suena (STOP / +10 MIN). Ver playback.py.
    playback = AlarmPlaybackManager(
        app.extensions["audio_player"], app.extensions["spotify_alarm"],
        bluetooth=app.extensions["bluetooth"], music=app.extensions["music"],
    )
    app.extensions["playback"] = playback

    # Health checks (página Diagnóstico y pre-flight). Solo observan. Ver health.py.
    health = HealthChecker(
        app.config, database=app.config["DATABASE"], library=library,
        spotify=app.extensions["spotify"], bluetooth=app.extensions["bluetooth"],
        playback=playback,
    )
    app.extensions["health"] = health
    preflight = PreflightScheduler(app.config["DATABASE"], health, library=library,
                                   minutes=app.config["ALARM_PREFLIGHT_MINUTES"])
    app.extensions["preflight"] = preflight
    app.extensions["scheduler"] = None

    # En modo debug Flask arranca dos procesos (vigilante + servidor); el
    # scheduler solo debe correr en el que sirve. Sin debug hay un solo proceso.
    if (
        app.config["SCHEDULER_ENABLED"]
        and not app.testing
        and (not app.debug or is_running_from_reloader())
    ):
        background = scheduler.start_scheduler(app, playback)
        # Los snoozes usan el mismo APScheduler (jobs en memoria).
        playback.schedule_once = scheduler.date_job_scheduler(background)
        app.extensions["scheduler"] = health.scheduler = background
        # Pre-flight: jobs preflight:<id>, independientes del job de las alarmas.
        preflight.start(background)

    @app.context_processor
    def inject_playback():
        """Estado de reproducción para todas las páginas (aviso de alarma
        sonando y comprobación de conexión desde cualquier pantalla)."""
        active, snoozes = playback.active, playback.pending_snoozes
        return {
            "playback_active": active,
            "playback_key": playback_key(active, snoozes),
            "day_letters": ui.DAY_LETTERS,
            "day_full": ui.DAY_FULL,
        }

    def render_form(data, errors=(), status=200, alarm_id=None):
        return render_template(
            "alarm_form.html", data=data, errors=errors, day_names=DAY_NAMES,
            alarm_id=alarm_id, fade_choices=FADE_CHOICES, duration_choices=DURATION_CHOICES,
            tracks=library.names(),  # se lee cada vez: lo subido aparece sin reiniciar
            # Sin red: solo mira si hay tokens guardados. El buscador lo necesita.
            spotify_connected=spotify_linked(),
        ), status

    def spotify_linked():
        spotify = app.extensions["spotify"]
        try:
            return bool(spotify.is_configured and spotify.is_connected())
        except Exception:  # el formulario nunca debe fallar por Spotify
            app.logger.exception("No se pudo consultar el estado de Spotify")
            return False

    def alarm_fields(data):
        return (data["name"], data["time"], data["days"], data["source"],
                data["spotify_uri"], data["volume_start"], data["volume_end"],
                data["fade_minutes"], data["max_duration_minutes"], data["local_track"],
                data["spotify_name"], data["spotify_subtitle"])

    @app.get("/")
    def index():
        active, snoozes = playback.active, playback.pending_snoozes
        alarms = db.list_alarms()
        now = ui.now()
        upcoming, when = ui.next_alarm(alarms, now)
        return render_template(
            "index.html", alarms=alarms, active=active, snoozes=snoozes,
            upcoming=upcoming,
            upcoming_day=ui.describe_day(when, now) if when else None,
            upcoming_countdown=ui.describe_countdown(when, now) if when else None,
            sun_height=ui.sun_height(when, now) if when else 0,
        )

    # --- PWA: manifest y service worker servidos desde la raíz ---

    static_dir = Path(app.root_path) / "static"

    @app.get("/manifest.webmanifest")
    def manifest():
        resp = send_from_directory(static_dir, "manifest.webmanifest",
                                   mimetype="application/manifest+json")
        resp.cache_control.no_cache = True
        return resp

    @app.get("/sw.js")
    def service_worker():
        # En la raíz para que su alcance sea toda la app; sin caché para que
        # los cambios del service worker lleguen enseguida.
        resp = send_from_directory(static_dir, "sw.js", mimetype="text/javascript")
        resp.cache_control.no_cache = True
        resp.headers["Service-Worker-Allowed"] = "/"
        return resp

    @app.route("/alarms/new", methods=["GET", "POST"])
    def new_alarm():
        if request.method == "POST":
            data, errors = validate_alarm_form(request.form, library.names())
            if errors:
                return render_form(form_echo(data), errors, 400)
            db.create_alarm(*alarm_fields(data))
            preflight.sync()
            flash(f"Alarma «{data['name']}» creada.")
            return redirect(url_for("index"))
        return render_form({"name": "", "time": "07:00", "days": [],
                            "source": "local", "spotify_uri": "",
                            "spotify_name": None, "spotify_subtitle": None,
                            "volume_start": db.DEFAULT_VOLUME_START,
                            "volume_end": db.DEFAULT_VOLUME_END,
                            "fade_minutes": db.DEFAULT_FADE_MINUTES,
                            "max_duration_minutes": db.DEFAULT_MAX_DURATION,
                            "local_track": None})

    @app.route("/alarms/<int:alarm_id>/edit", methods=["GET", "POST"])
    def edit_alarm(alarm_id):
        alarm = db.get_alarm(alarm_id)
        if alarm is None:
            abort(404)
        if request.method == "POST":
            data, errors = validate_alarm_form(request.form, library.names(),
                                               keep_track=alarm["local_track"])
            if errors:
                return render_form(form_echo(data), errors, 400, alarm_id)
            db.update_alarm(alarm_id, *alarm_fields(data))
            preflight.sync()
            flash(f"Alarma «{data['name']}» guardada.")
            return redirect(url_for("index"))
        days = [int(d) for d in alarm["days"].split(",")] if alarm["days"] else []
        return render_form({"name": alarm["name"], "time": alarm["time"], "days": days,
                            "source": alarm["source"],
                            "spotify_uri": alarm["spotify_uri"] or "",
                            "spotify_name": alarm["spotify_name"],
                            "spotify_subtitle": alarm["spotify_subtitle"],
                            "volume_start": alarm["volume_start"],
                            "volume_end": alarm["volume_end"],
                            "fade_minutes": alarm["fade_minutes"],
                            "max_duration_minutes": alarm["max_duration_minutes"],
                            "local_track": alarm["local_track"]},
                           alarm_id=alarm_id)

    def form_echo(data):
        """Si hay errores, se vuelve a mostrar lo que escribió el usuario."""
        return {**data, "spotify_uri": request.form.get("spotify_uri", "").strip()}

    @app.post("/alarms/<int:alarm_id>/toggle")
    def toggle_alarm(alarm_id):
        if not db.toggle_alarm(alarm_id):
            abort(404)
        preflight.sync()
        return redirect(url_for("index"))

    @app.post("/alarms/<int:alarm_id>/delete")
    def delete_alarm(alarm_id):
        if not db.delete_alarm(alarm_id):
            abort(404)
        playback.forget(alarm_id)  # si sonaba o estaba pospuesta, se cancela
        preflight.sync()
        flash("Alarma eliminada.")
        return redirect(url_for("index"))

    @app.post("/alarms/<int:alarm_id>/test")
    def test_alarm(alarm_id):
        alarm = db.get_alarm(alarm_id)
        if alarm is None:
            abort(404)
        # Exactamente el mismo flujo que usa el scheduler.
        outcome = playback.start(alarm, manual=True)
        messages = {
            "local": "sonando el WAV local",
            "spotify": "reproduciendo en Spotify",
            "fallback": "Spotify falló, sonando el WAV local (motivo en instance/alarms.log)",
        }
        flash(f"Alarma «{alarm['name']}» probada: {messages[outcome]}.",
              "error" if outcome == "fallback" else "message")
        return redirect(url_for("index"))

    # --- Alarma sonando: STOP y +10 MIN (la lógica vive en playback.py) ---

    @app.post("/playback/stop")
    def stop_alarm():
        result = playback.stop()
        if result is None:
            flash("No hay ninguna alarma sonando.")
        elif not result.silenced:
            flash(f"Alarma «{result.active.alarm['name']}» detenida, pero no se pudo "
                  "parar el sonido (mira instance/alarms.log).", "error")
        else:
            flash(f"Alarma «{result.active.alarm['name']}» detenida.")
        return redirect(url_for("index"))

    @app.post("/playback/snooze")
    def snooze_alarm():
        pending = playback.snooze()
        if pending is None:
            flash("No hay ninguna alarma sonando.")
        else:
            flash(f"Alarma «{pending.alarm['name']}» pospuesta hasta las "
                  f"{pending.run_at.strftime('%H:%M')}.")
        return redirect(url_for("index"))

    @app.post("/playback/snooze/<int:alarm_id>/cancel")
    def cancel_snooze(alarm_id):
        if playback.cancel_snooze(alarm_id):
            flash("Snooze cancelado: la alarma no volverá a sonar hasta su próxima hora.")
        return redirect(url_for("index"))

    @app.get("/playback/state")
    def playback_state():
        """Estado para que la página se refresque sola cuando empieza a sonar."""
        active, snoozes = playback.active, playback.pending_snoozes
        return jsonify(
            key=playback_key(active, snoozes),
            active=None if active is None else {
                "id": active.id,
                "name": active.alarm["name"],
                "started_at": active.started_at.isoformat(timespec="seconds"),
                "via": active.via,
            },
            snoozes=[{"id": p.alarm["id"], "name": p.alarm["name"],
                      "run_at": p.run_at.isoformat(timespec="seconds")}
                     for p in snoozes],
        )

    return app


if __name__ == "__main__":
    # Carga .env (SPOTIFY_CLIENT_ID, etc.). `flask run` lo hace solo.
    from dotenv import load_dotenv

    load_dotenv()
    # DEBUG en la config desde el principio para que create_app sepa que
    # habrá recargador y no arranque el scheduler dos veces.
    create_app({"DEBUG": True}).run(debug=True)
