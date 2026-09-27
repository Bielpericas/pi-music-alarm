"""Ayudas de presentación para las plantillas (sin lógica de alarmas).

Calculan textos y datos que solo sirven para pintar la interfaz: la próxima
alarma, cuánto falta, las letras de los días, etc. No tocan la base de datos
ni el scheduler.
"""
import math
from datetime import datetime, timedelta

from spotify_client import parse_spotify_uri, spotify_web_url

DAY_LETTERS = ("L", "M", "X", "J", "V", "S", "D")
DAY_FULL = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
SPOTIFY_KIND_LABELS = {"track": "Canción", "album": "Álbum", "playlist": "Playlist"}


def next_occurrence(alarm, now):
    """Próxima vez que sonará una alarma activa, o None si está desactivada.

    Misma regla que el scheduler: a su hora HH:MM, en sus días (vacío = el
    próximo día en que llegue esa hora). Un minuto ya empezado no cuenta.
    """
    if not alarm["enabled"]:
        return None
    hour, minute = (int(part) for part in alarm["time"].split(":"))
    days = {int(d) for d in alarm["days"].split(",")} if alarm["days"] else None
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for offset in range(8):
        candidate = today + timedelta(days=offset)
        if candidate <= now:
            continue
        if days is None or candidate.weekday() in days:
            return candidate
    return None


def next_alarm(alarms, now):
    """(alarma, datetime) de la próxima alarma que va a sonar, o (None, None)."""
    upcoming = [(when, alarm) for alarm in alarms
                if (when := next_occurrence(alarm, now)) is not None]
    if not upcoming:
        return None, None
    when, alarm = min(upcoming, key=lambda item: item[0])
    return alarm, when


def describe_day(when, now):
    """"Hoy", "Mañana" o "El jueves"."""
    delta = (when.date() - now.date()).days
    if delta == 0:
        return "Hoy"
    if delta == 1:
        return "Mañana"
    return f"El {DAY_FULL[when.weekday()]}"


def describe_countdown(when, now):
    """"Suena en 7 h 12 min" / "Suena en 5 min"."""
    minutes = max(1, math.ceil((when - now).total_seconds() / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"Suena en {hours} h {minutes} min"
    if hours:
        return f"Suena en {hours} h"
    return f"Suena en {minutes} min"


def sun_height(when, now):
    """0..1: cuánto ha "salido" el sol del horizonte (1 = la alarma es ya)."""
    remaining = (when - now).total_seconds() / 3600
    return round(max(0.0, min(1.0, 1 - remaining / 24)), 3)


def human_size(size):
    """Tamaño aproximado: "6.2 MB", "850 KB"."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def duration_label(minutes):
    """Texto del desplegable de duración máxima (0 = sin límite)."""
    if minutes == 0:
        return "Sin límite"
    return f"{minutes} minutos"


def fade_label(minutes):
    """Texto del desplegable de fade-in."""
    if minutes == 0:
        return "Sin subida: directo al volumen final"
    if minutes == 1:
        return "Sube durante 1 minuto"
    return f"Sube durante {minutes} minutos"


def spotify_kind(uri):
    """"track", "album" o "playlist" según el URI (None si no es válido)."""
    try:
        return parse_spotify_uri(uri).split(":")[1]
    except ValueError:
        return None


def spotify_kind_label(uri):
    """"Canción", "Álbum" o "Playlist": el tipo sale del URI, no de la metadata."""
    return SPOTIFY_KIND_LABELS.get(spotify_kind(uri), "Contenido")


def spotify_link(uri):
    """Enlace a open.spotify.com para un URI válido ("" si no lo es)."""
    return spotify_web_url(uri) if spotify_kind(uri) else ""


def selected_days(days_csv):
    return {int(d) for d in days_csv.split(",")} if days_csv else set()


def now():
    return datetime.now()
