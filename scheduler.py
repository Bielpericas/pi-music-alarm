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
from apscheduler.triggers.date import DateTrigger

import db
from playback import play_alarm_sound

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


def fire_alarm(alarm, manual=False, player=None, spotify=None):
    """Registra la alarma y la hace sonar, sin estado de "alarma activa".

    Se mantiene por compatibilidad; la app usa AlarmPlaybackManager.start(),
    que además recuerda qué suena para STOP / +10 MIN.
    Devuelve lo que ha sonado: "local", "spotify" o "fallback".
    """
    suffix = " (prueba manual)" if manual else ""
    logger.info("ALARMA ACTIVADA: %s%s", alarm["name"], suffix)
    return play_alarm_sound(alarm, player, spotify)


def alarm_matches_day(alarm, now):
    days = alarm["days"]
    return not days or str(now.weekday()) in days.split(",")


def check_alarms(database, now=None, player=None, spotify=None, manager=None):
    """Dispara las alarmas que tocan en el minuto `now`. Devuelve sus nombres.

    Con `manager` (AlarmPlaybackManager) la alarma queda como "activa" para
    STOP / +10 MIN; sin él, solo suena (así la usan algunos tests).

    APScheduler ejecuta este job en su pool de hilos: si Spotify tarda (las
    peticiones tienen timeout), no se bloquea el bucle del scheduler.
    """
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    minute_key = now.strftime("%Y-%m-%d %H:%M")
    fired = []
    conn = db.connect(database)
    try:
        for alarm in db.enabled_alarms_at(conn, now.strftime("%H:%M")):
            if alarm_matches_day(alarm, now) and db.claim_trigger(
                conn, alarm["id"], minute_key
            ):
                if manager is not None:
                    manager.start(alarm)
                else:
                    fire_alarm(alarm, player=player, spotify=spotify)
                fired.append(alarm["name"])
    finally:
        conn.close()
    return fired


def start_scheduler(app, manager):
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        check_alarms,
        CronTrigger(second=0),
        args=[app.config["DATABASE"]],
        kwargs={"manager": manager},
        id="check_alarms",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info("Scheduler iniciado: revisando alarmas cada minuto.")
    return scheduler


def date_job_scheduler(scheduler, misfire_grace_time=60):
    """Devuelve schedule_once(run_at, callback) -> cancel() usando APScheduler.

    Para los snoozes, el auto-stop y el temporizador de sueño. El jobstore por
    defecto es en memoria: si la app se reinicia, los jobs pendientes se
    pierden (a propósito). `misfire_grace_time=None`: el job se ejecuta
    aunque llegue tarde.
    """
    def schedule_once(run_at, callback):
        job = scheduler.add_job(
            callback, DateTrigger(run_date=run_at), misfire_grace_time=misfire_grace_time
        )
        return job.remove

    return schedule_once
