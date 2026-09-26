"""Scheduler de alarmas.

Un único job de APScheduler se ejecuta cada minuto (en el segundo 0), lee de
SQLite las alarmas activas para esa hora y día, y dispara las que tocan.
La BD es la única fuente de verdad: no hay que sincronizar jobs al crear,
editar o borrar alarmas, y tras un reinicio todo sigue funcionando.
"""
import atexit
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import db

logger = logging.getLogger("alarms")


def close_logging():
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def setup_logging(log_path):
    """Escribe en consola y en un fichero de log pequeño y rotativo."""
    close_logging()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [
        logging.StreamHandler(),
        # delay=True: el fichero solo se abre al escribir la primera línea.
        RotatingFileHandler(
            log_path, maxBytes=256_000, backupCount=2, encoding="utf-8", delay=True
        ),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def fire_alarm(name, manual=False):
    """Acción de la alarma. De momento solo lo deja en consola y en el log."""
    suffix = " (prueba manual)" if manual else ""
    logger.info("ALARMA ACTIVADA: %s%s", name, suffix)


def alarm_matches_day(alarm, now):
    days = alarm["days"]
    return not days or str(now.weekday()) in days.split(",")


def check_alarms(database, now=None):
    """Dispara las alarmas que tocan en el minuto `now`. Devuelve sus nombres."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    fired = []
    conn = db.connect(database)
    try:
        for alarm in db.enabled_alarms_at(conn, now.strftime("%H:%M")):
            if alarm_matches_day(alarm, now) and db.claim_trigger(
                conn, alarm["id"], minute_key
            ):
                fire_alarm(alarm["name"])
                fired.append(alarm["name"])
    finally:
        conn.close()
    return fired


def start_scheduler(app):
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        check_alarms,
        CronTrigger(second=0),
        args=[app.config["DATABASE"]],
        id="check_alarms",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info("Scheduler iniciado: revisando alarmas cada minuto.")
    return scheduler
