"""App Flask del despertador: CRUD de alarmas + scheduler + audio local + Spotify."""
import os
import re
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from werkzeug.serving import is_running_from_reloader

import db
import scheduler
import spotify_views
from audio_player import create_player
from spotify_client import create_spotify_client, parse_spotify_uri
from spotify_player import SpotifyAlarmPlayer

DEFAULT_SOUND = Path(__file__).parent / "sounds" / "alarm.wav"
DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_NAME_LEN = 50
SOURCES = ("local", "spotify")


def validate_alarm_form(form):
    """Devuelve (datos, errores) a partir del formulario."""
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
    if source == "spotify":
        try:
            spotify_uri = parse_spotify_uri(spotify_uri)
        except ValueError as exc:
            errors.append(str(exc))
    else:
        spotify_uri = None  # las alarmas locales no guardan URI

    data = {"name": name, "time": time, "days": days,
            "source": source, "spotify_uri": spotify_uri}
    return data, errors


def describe_source(alarm):
    """Texto corto para la lista: "Local" o "Spotify · playlist"."""
    if alarm["source"] == "spotify" and alarm["spotify_uri"]:
        return f"Spotify · {alarm['spotify_uri'].split(':')[1]}"
    return "Local"


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


def create_app(config=None, player=None, spotify=None):
    """Crea la app. `player` y `spotify` permiten inyectar dobles (mocks) en tests."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        DATABASE=str(Path(app.instance_path) / "alarms.db"),
        ALARM_LOG=str(Path(app.instance_path) / "alarms.log"),
        SCHEDULER_ENABLED=True,
        AUDIO_BACKEND=os.environ.get("AUDIO_BACKEND", "local"),
        SOUND_PATH=os.environ.get("ALARM_SOUND", str(DEFAULT_SOUND)),
        ALSA_DEVICE=os.environ.get("ALSA_DEVICE", ""),  # solo Linux (aplay -D)
        SPOTIFY_CLIENT_ID=os.environ.get("SPOTIFY_CLIENT_ID", ""),
        SPOTIFY_CLIENT_SECRET=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        SPOTIFY_REDIRECT_URI=os.environ.get(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5000/spotify/callback"
        ),
    )
    if config:
        app.config.update(config)

    db.init_app(app)
    scheduler.setup_logging(app.config["ALARM_LOG"])
    app.jinja_env.filters["format_days"] = format_days
    app.extensions["audio_player"] = player or create_player(app.config)
    app.extensions["spotify"] = spotify or create_spotify_client(app.config)
    app.extensions["spotify_alarm"] = SpotifyAlarmPlayer(
        app.extensions["spotify"], app.config["DATABASE"]
    )
    app.jinja_env.filters["describe_source"] = describe_source
    app.register_blueprint(spotify_views.bp)

    # En modo debug Flask arranca dos procesos (vigilante + servidor); el
    # scheduler solo debe correr en el que sirve. Sin debug hay un solo proceso.
    if (
        app.config["SCHEDULER_ENABLED"]
        and not app.testing
        and (not app.debug or is_running_from_reloader())
    ):
        scheduler.start_scheduler(
            app, app.extensions["audio_player"], app.extensions["spotify_alarm"]
        )

    def render_form(data, errors=(), status=200, alarm_id=None):
        return render_template(
            "alarm_form.html", data=data, errors=errors, day_names=DAY_NAMES,
            alarm_id=alarm_id,
        ), status

    @app.get("/")
    def index():
        return render_template("index.html", alarms=db.list_alarms())

    @app.route("/alarms/new", methods=["GET", "POST"])
    def new_alarm():
        if request.method == "POST":
            data, errors = validate_alarm_form(request.form)
            if errors:
                return render_form(form_echo(data), errors, 400)
            db.create_alarm(data["name"], data["time"], data["days"],
                            data["source"], data["spotify_uri"])
            flash(f"Alarma «{data['name']}» creada.")
            return redirect(url_for("index"))
        return render_form({"name": "", "time": "07:00", "days": [],
                            "source": "local", "spotify_uri": ""})

    @app.route("/alarms/<int:alarm_id>/edit", methods=["GET", "POST"])
    def edit_alarm(alarm_id):
        alarm = db.get_alarm(alarm_id)
        if alarm is None:
            abort(404)
        if request.method == "POST":
            data, errors = validate_alarm_form(request.form)
            if errors:
                return render_form(form_echo(data), errors, 400, alarm_id)
            db.update_alarm(alarm_id, data["name"], data["time"], data["days"],
                            data["source"], data["spotify_uri"])
            flash(f"Alarma «{data['name']}» guardada.")
            return redirect(url_for("index"))
        days = [int(d) for d in alarm["days"].split(",")] if alarm["days"] else []
        return render_form({"name": alarm["name"], "time": alarm["time"], "days": days,
                            "source": alarm["source"],
                            "spotify_uri": alarm["spotify_uri"] or ""}, alarm_id=alarm_id)

    def form_echo(data):
        """Si hay errores, se vuelve a mostrar lo que escribió el usuario."""
        return {**data, "spotify_uri": request.form.get("spotify_uri", "").strip()}

    @app.post("/alarms/<int:alarm_id>/toggle")
    def toggle_alarm(alarm_id):
        if not db.toggle_alarm(alarm_id):
            abort(404)
        return redirect(url_for("index"))

    @app.post("/alarms/<int:alarm_id>/delete")
    def delete_alarm(alarm_id):
        if not db.delete_alarm(alarm_id):
            abort(404)
        flash("Alarma eliminada.")
        return redirect(url_for("index"))

    @app.post("/alarms/<int:alarm_id>/test")
    def test_alarm(alarm_id):
        alarm = db.get_alarm(alarm_id)
        if alarm is None:
            abort(404)
        # Exactamente el mismo flujo que usa el scheduler.
        outcome = scheduler.fire_alarm(
            alarm, manual=True,
            player=app.extensions["audio_player"],
            spotify=app.extensions["spotify_alarm"],
        )
        messages = {
            "local": "sonando el WAV local",
            "spotify": "reproduciendo en Spotify",
            "fallback": "Spotify falló, sonando el WAV local (motivo en instance/alarms.log)",
        }
        flash(f"Alarma «{alarm['name']}» probada: {messages[outcome]}.",
              "error" if outcome == "fallback" else "message")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    # Carga .env (SPOTIFY_CLIENT_ID, etc.). `flask run` lo hace solo.
    from dotenv import load_dotenv

    load_dotenv()
    # DEBUG en la config desde el principio para que create_app sepa que
    # habrá recargador y no arranque el scheduler dos veces.
    create_app({"DEBUG": True}).run(debug=True)
