"""Acceso a SQLite con la librería estándar (sin ORM, para ahorrar RAM en la Pi)."""
import sqlite3
from pathlib import Path

import click
from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columnas añadidas después de la primera versión: (nombre, definición).
MIGRATIONS = [("last_triggered", "TEXT")]


def connect(database):
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(database):
    """Crea la tabla si no existe y añade columnas nuevas a BDs antiguas."""
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(database)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(alarms)")}
        for column, definition in MIGRATIONS:
            if column not in existing:
                conn.execute(f"ALTER TABLE alarms ADD COLUMN {column} {definition}")
        conn.commit()
    finally:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_db)
    init_db(app.config["DATABASE"])

    @app.cli.command("init-db")
    def init_db_command():
        """Crea las tablas si no existen."""
        init_db(app.config["DATABASE"])
        click.echo("Base de datos inicializada.")


def list_alarms():
    return get_db().execute("SELECT * FROM alarms ORDER BY time, name").fetchall()


def get_alarm(alarm_id):
    return get_db().execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()


def create_alarm(name, time, days):
    db = get_db()
    db.execute(
        "INSERT INTO alarms (name, time, days) VALUES (?, ?, ?)",
        (name, time, ",".join(str(d) for d in days)),
    )
    db.commit()


def toggle_alarm(alarm_id):
    db = get_db()
    cur = db.execute("UPDATE alarms SET enabled = 1 - enabled WHERE id = ?", (alarm_id,))
    db.commit()
    return cur.rowcount > 0


def delete_alarm(alarm_id):
    db = get_db()
    cur = db.execute("DELETE FROM alarms WHERE id = ?", (alarm_id,))
    db.commit()
    return cur.rowcount > 0


def get_setting(key, default=None):
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


# --- Consultas del scheduler (usan su propia conexión, fuera de peticiones HTTP) ---

def enabled_alarms_at(conn, hhmm):
    return conn.execute(
        "SELECT * FROM alarms WHERE enabled = 1 AND time = ?", (hhmm,)
    ).fetchall()


def claim_trigger(conn, alarm_id, minute_key):
    """Marca la alarma como disparada en `minute_key` ("YYYY-MM-DD HH:MM").

    Es atómico: solo devuelve True la primera vez para ese minuto, aunque haya
    dos procesos revisando a la vez. Las alarmas de "una vez" se desactivan.
    """
    cur = conn.execute(
        """UPDATE alarms
           SET last_triggered = ?,
               enabled = CASE WHEN days = '' THEN 0 ELSE enabled END
           WHERE id = ? AND enabled = 1
             AND (last_triggered IS NULL OR last_triggered <> ?)""",
        (minute_key, alarm_id, minute_key),
    )
    conn.commit()
    return cur.rowcount > 0
