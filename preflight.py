"""Pre-flight: health checks unos minutos antes de cada alarma.

Cada alarma activa tiene un job de APScheduler `preflight:<id>` que se
ejecuta ALARM_PREFLIGHT_MINUTES (5) minutos antes de su próxima hora. El
pre-flight ejecuta los health checks (health.py), escribe un resumen en el
log y guarda el informe como "último diagnóstico". Nada más:

- No cambia la alarma ni su job: las alarmas las sigue disparando el job
  `check_alarms` del scheduler, que no sabe nada del pre-flight. Aunque el
  pre-flight falle entero (o no llegue a ejecutarse), la alarma suena.
- No arranca, para ni repara nada, ni reproduce audio.

Programación (`sync()`): la base de datos es la fuente de verdad. sync()
calcula, para cada alarma activa, cuándo toca su pre-flight y deja los jobs
`preflight:*` exactamente así: crea los que faltan, mueve los que cambiaron
de hora y borra los de alarmas borradas o desactivadas (sin huérfanos). Se
llama:
- al arrancar Groove (los jobs viven en memoria y se rehacen desde la BD);
- justo después de crear, editar, activar/desactivar o borrar una alarma;
- cada minuto (job `preflight-sync`, en el segundo 30): programa la
  siguiente vez de las alarmas recurrentes tras sonar, retira el de las de
  "una vez" y corrige cualquier cambio de la hora del sistema (la Pi no
  tiene reloj con pila y la hora puede llegar tarde por NTP).

Si a la próxima alarma le quedan menos de 5 minutos, no hay pre-flight para
esa vez (nunca se ejecuta con retraso): la alarma suena con normalidad y el
de la siguiente vez se programa cuando toque.
"""
import logging
import threading
from datetime import datetime, timedelta

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import db
import ui
from health import OK, WARNING

logger = logging.getLogger("alarms")

JOB_PREFIX = "preflight:"
SYNC_JOB_ID = "preflight-sync"
DEFAULT_MINUTES = 5

# Checks que se registran según la fuente de la alarma (en este orden).
LOCAL_CHECKS = ("audio", "local_music", "ffmpeg", "emergency", "bluetooth", "scheduler")
SPOTIFY_CHECKS = ("audio", "spotify", "raspotify") + LOCAL_CHECKS[1:]


def job_id(alarm_id):
    return f"{JOB_PREFIX}{alarm_id}"


def relevant_checks(alarm):
    return SPOTIFY_CHECKS if alarm["source"] == "spotify" else LOCAL_CHECKS


class PreflightScheduler:
    def __init__(self, database, checker, library=None, minutes=DEFAULT_MINUTES,
                 clock=datetime.now):
        self.database = database
        self.checker = checker            # HealthChecker (run(trigger) -> HealthReport)
        self.library = library            # MusicLibrary: ¿existe la pista elegida?
        self.minutes = max(0, int(minutes))
        self.clock = clock
        self.scheduler = None             # BackgroundScheduler; sin él, sync() no hace nada
        self._lock = threading.Lock()     # sync() desde rutas y desde el scheduler

    @property
    def enabled(self):
        return self.minutes > 0

    def start(self, scheduler):
        """Engancha el pre-flight al scheduler: jobs iniciales + revisión cada minuto."""
        self.scheduler = scheduler
        scheduler.add_job(self.sync, CronTrigger(second=30), id=SYNC_JOB_ID,
                          max_instances=1, coalesce=True, misfire_grace_time=30,
                          replace_existing=True)
        self.sync()
        if self.enabled:
            logger.info("Pre-flight activado: %s min antes de cada alarma.", self.minutes)
        else:
            logger.info("Pre-flight desactivado (ALARM_PREFLIGHT_MINUTES=0).")

    # --- Programación ---

    def planned(self, now=None):
        """{alarm_id: (alarma, hora_de_la_alarma)} de las alarmas activas."""
        now = now or self.clock()
        conn = db.connect(self.database)
        try:
            alarms = [dict(row) for row in conn.execute("SELECT * FROM alarms WHERE enabled = 1")]
        finally:
            conn.close()
        plan = {}
        for alarm in alarms:
            when = ui.next_occurrence(alarm, now)
            if when is not None:
                plan[alarm["id"]] = (alarm, when)
        return plan

    def sync(self):
        """Deja los jobs preflight:* como dice la BD. Nunca lanza excepciones."""
        if self.scheduler is None:
            return
        with self._lock:
            try:
                self._sync()
            except Exception:
                logger.exception("No se pudieron actualizar los pre-flight "
                                 "(las alarmas no se ven afectadas)")

    def _sync(self):
        now = self.clock()
        plan = self.planned(now) if self.enabled else {}
        existing = {job.id: job for job in self.scheduler.get_jobs()
                    if job.id.startswith(JOB_PREFIX)}

        for alarm_id, (alarm, when) in plan.items():
            jid = job_id(alarm_id)
            run_at = when - timedelta(minutes=self.minutes)
            kwargs = {"alarm_id": alarm_id, "alarm_at": when.isoformat(timespec="minutes")}
            job = existing.pop(jid, None)
            if job is not None and job.kwargs == kwargs:
                continue  # ya está bien (aunque esté a punto de ejecutarse)
            if run_at <= now:
                # Menos de N minutos para la alarma: sin pre-flight esta vez.
                if job is not None:
                    self._remove(jid, alarm_id, "la alarma suena en menos de "
                                                f"{self.minutes} min")
                continue
            try:
                self.scheduler.add_job(
                    self.run, DateTrigger(run_date=run_at), id=jid, kwargs=kwargs,
                    replace_existing=True, misfire_grace_time=60, coalesce=True,
                )
            except Exception:
                logger.exception("No se pudo programar el pre-flight de la alarma %s", alarm_id)
                continue
            logger.info("Pre-flight %s para «%s» (alarma %s): %s, antes de la alarma de las %s",
                        "reprogramado" if job is not None else "programado",
                        alarm["name"], alarm_id, run_at.strftime("%d/%m %H:%M"),
                        when.strftime("%H:%M"))

        # Lo que queda no corresponde a ninguna alarma activa: huérfanos.
        for jid in existing:
            self._remove(jid, jid[len(JOB_PREFIX):], "la alarma ya no está activa")

    def _remove(self, jid, alarm_id, reason):
        try:
            self.scheduler.remove_job(jid)
        except Exception:
            return  # ya se había ejecutado o no existe
        logger.info("Pre-flight cancelado para la alarma %s: %s", alarm_id, reason)

    # --- Ejecución ---

    def run(self, alarm_id, alarm_at=None):
        """Job del pre-flight: checks + resumen en el log. Nunca lanza excepciones
        y nunca toca la alarma: esta suena a su hora pase lo que pase aquí."""
        try:
            self._run(alarm_id, alarm_at)
        except Exception:
            logger.exception("Pre-flight alarma %s: error inesperado. La alarma sonará "
                             "igualmente a su hora.", alarm_id)

    def _run(self, alarm_id, alarm_at):
        conn = db.connect(self.database)
        try:
            row = conn.execute("SELECT * FROM alarms WHERE id = ?", (alarm_id,)).fetchone()
        finally:
            conn.close()
        if row is None or not row["enabled"]:
            logger.info("Pre-flight alarma %s omitido: la alarma ya no está activa", alarm_id)
            return
        alarm = dict(row)
        try:
            report = self.checker.run(trigger=f"Pre-flight de «{alarm['name']}»")
        except Exception:
            logger.exception("Pre-flight alarma %s: no se pudieron ejecutar los checks. "
                             "La alarma sonará igualmente a su hora.", alarm_id)
            return
        for level, line in summarize(alarm, report, self._local_music_ready(alarm, report)):
            logger.log(level, "Pre-flight alarma %s: %s", alarm_id, line)

    def _local_music_ready(self, alarm, report):
        """¿Puede sonar la música local de esta alarma? (texto o None)."""
        if report.status_of("local_music") != OK or report.status_of("ffmpeg") != OK:
            return None
        track = alarm.get("local_track")
        if track and self.library is not None:
            return f"música local («{track}»)" if self.library.path(track) else None
        return f"música local ({report.get('local_music').summary})"


def summarize(alarm, report, local_ready):
    """Líneas del log del pre-flight: [(nivel, texto)].

    1. Estado de cada check relevante: "audio=ok spotify=warning ...".
    2. Qué pasará: si falla Spotify o la música local, qué respaldo queda.
    """
    checks = relevant_checks(alarm)
    statuses = " ".join(f"{cid}={report.status_of(cid)}" for cid in checks)
    lines = [(logging.INFO, f"«{alarm['name']}» {alarm['time']} ({alarm['source']}): {statuses}")]

    problems = []
    for cid in checks:
        check = report.get(cid)
        if check and check.status != OK:
            problem = f"{cid}={check.status} ({check.summary})"
            if check.details:
                problem += ": " + "; ".join(" ".join(str(d).split()) for d in check.details)
            problems.append(problem)
    emergency_ready = report.status_of("emergency") == OK
    fallback = local_ready or ("WAV de emergencia" if emergency_ready else None)

    if report.status_of("audio") not in (OK, WARNING):
        lines.append((logging.WARNING, "la salida de audio USB no está disponible: puede que "
                                       "no se oiga nada. La alarma se intentará igualmente."))
    if alarm["source"] == "spotify":
        spotify_ok = report.status_of("spotify") == OK and report.status_of("raspotify") == OK
        if not spotify_ok:
            if fallback:
                lines.append((logging.WARNING,
                              f"Spotify puede fallar; hay respaldo: {fallback}."))
            else:
                lines.append((logging.WARNING, "Spotify puede fallar y NO hay respaldo "
                                               "(ni música local ni WAV de emergencia)."))
    elif not local_ready:
        if emergency_ready:
            lines.append((logging.WARNING, "sin música local disponible: sonará el WAV "
                                           "de emergencia."))
        else:
            lines.append((logging.WARNING, "sin música local ni WAV de emergencia: puede "
                                           "que no suene nada."))
    if problems:
        lines.append((logging.WARNING, "problemas: " + "; ".join(problems)))
    else:
        lines.append((logging.INFO, "todo listo."))
    return lines
