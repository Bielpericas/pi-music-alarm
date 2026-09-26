"""App Flask del despertador: CRUD de alarmas + scheduler (sin audio todavía)."""
import os
import re
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from werkzeug.serving import is_running_from_reloader

import db
import scheduler

DAY_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_NAME_LEN = 50


def validate_alarm_form(form):
    """Devuelve (datos, errores) a partir del formulario."""
    errors = []
    name = form.get("name", "").strip()
    time = form.get("time", "").strip()
    raw_days = form.getlist("days")

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

    return {"name": name, "time": time, "days": days}, errors


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


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev"),
        DATABASE=str(Path(app.instance_path) / "alarms.db"),
        ALARM_LOG=str(Path(app.instance_path) / "alarms.log"),
        SCHEDULER_ENABLED=True,
    )
    if config:
        app.config.update(config)

    db.init_app(app)
    scheduler.setup_logging(app.config["ALARM_LOG"])
    app.jinja_env.filters["format_days"] = format_days

    # En modo debug Flask arranca dos procesos (vigilante + servidor); el
    # scheduler solo debe correr en el que sirve. Sin debug hay un solo proceso.
    if (
        app.config["SCHEDULER_ENABLED"]
        and not app.testing
        and (not app.debug or is_running_from_reloader())
    ):
        scheduler.start_scheduler(app)

    @app.get("/")
    def index():
        return render_template("index.html", alarms=db.list_alarms())

    @app.route("/alarms/new", methods=["GET", "POST"])
    def new_alarm():
        if request.method == "POST":
            data, errors = validate_alarm_form(request.form)
            if not errors:
                db.create_alarm(data["name"], data["time"], data["days"])
                flash(f"Alarma «{data['name']}» creada.")
                return redirect(url_for("index"))
            return render_template(
                "alarm_form.html", data=data, errors=errors, day_names=DAY_NAMES
            ), 400
        return render_template(
            "alarm_form.html",
            data={"name": "", "time": "07:00", "days": []},
            errors=[],
            day_names=DAY_NAMES,
        )

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
        scheduler.fire_alarm(alarm["name"], manual=True)
        flash(f"Alarma «{alarm['name']}» probada: mira la consola o instance/alarms.log.")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    # DEBUG en la config desde el principio para que create_app sepa que
    # habrá recargador y no arranque el scheduler dos veces.
    create_app({"DEBUG": True}).run(debug=True)
