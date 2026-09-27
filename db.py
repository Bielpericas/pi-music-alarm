"""Acceso a SQLite con la librería estándar (sin ORM, para ahorrar RAM en la Pi)."""
import sqlite3
from pathlib import Path

import click
from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Columnas añadidas después de la primera versión: (nombre, definición).
MIGRATIONS = [
    ("last_triggered", "TEXT"),
    ("source", "TEXT NOT NULL DEFAULT 'local'"),
    ("spotify_uri", "TEXT"),
    ("volume_start", "INTEGER NOT NULL DEFAULT 20"),
    ("volume_end", "INTEGER NOT NULL DEFAULT 60"),
    ("fade_minutes", "INTEGER NOT NULL DEFAULT 5"),
    # Las alarmas existentes adoptan el valor por defecto (30 min).
    ("max_duration_minutes", "INTEGER NOT NULL DEFAULT 30"),
    # Música local (alarmas locales y respaldo de Spotify). NULL = aleatoria.
    ("local_track", "TEXT"),
    # Metadata de Spotify para mostrar (buscador). Las alarmas antiguas quedan en
    # NULL y siguen sonando igual: la reproducción solo usa spotify_uri.
    ("spotify_name", "TEXT"),
    ("spotify_subtitle", "TEXT"),
]

# Valores por defecto del volumen (Spotify): 20 % -> 60 % en 5 minutos.
DEFAULT_VOLUME_START = 20
DEFAULT_VOLUME_END = 60
DEFAULT_FADE_MINUTES = 5
# Duración máxima de una alarma sonando antes del auto-stop (0 = sin límite).
DEFAULT_MAX_DURATION = 30


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


def create_alarm(name, time, days, source="local", spotify_uri=None,
                 volume_start=DEFAULT_VOLUME_START, volume_end=DEFAULT_VOLUME_END,
                 fade_minutes=DEFAULT_FADE_MINUTES, max_duration_minutes=DEFAULT_MAX_DURATION,
                 local_track=None, spotify_name=None, spotify_subtitle=None):
    db = get_db()
    db.execute(
        "INSERT INTO alarms (name, time, days, source, spotify_uri, volume_start,"
        " volume_end, fade_minutes, max_duration_minutes, local_track,"
        " spotify_name, spotify_subtitle)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, time, ",".join(str(d) for d in days), source, spotify_uri,
         volume_start, volume_end, fade_minutes, max_duration_minutes, local_track,
         spotify_name, spotify_subtitle),
    )
    db.commit()


def update_alarm(alarm_id, name, time, days, source="local", spotify_uri=None,
                 volume_start=DEFAULT_VOLUME_START, volume_end=DEFAULT_VOLUME_END,
                 fade_minutes=DEFAULT_FADE_MINUTES, max_duration_minutes=DEFAULT_MAX_DURATION,
                 local_track=None, spotify_name=None, spotify_subtitle=None):
    db = get_db()
    cur = db.execute(
        "UPDATE alarms SET name = ?, time = ?, days = ?, source = ?, spotify_uri = ?,"
        " volume_start = ?, volume_end = ?, fade_minutes = ?, max_duration_minutes = ?,"
        " local_track = ?, spotify_name = ?, spotify_subtitle = ? WHERE id = ?",
        (name, time, ",".join(str(d) for d in days), source, spotify_uri,
         volume_start, volume_end, fade_minutes, max_duration_minutes, local_track,
         spotify_name, spotify_subtitle, alarm_id),
    )
    db.commit()
    return cur.rowcount > 0


def alarms_using_track(name):
    """Nombres de las alarmas que tienen elegida la pista `name`."""
    rows = get_db().execute(
        "SELECT name FROM alarms WHERE local_track = ? ORDER BY time, name", (name,)).fetchall()
    return [row["name"] for row in rows]


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

def write_setting(database, key, value):
    conn = connect(database)
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def read_setting(database, key, default=None):
    conn = connect(database)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


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
