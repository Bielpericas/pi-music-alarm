"""Pre-flight (preflight.py): programación de jobs preflight:* y resumen en el log.

Se usa un BackgroundScheduler real arrancado en pausa: los jobs se crean,
mueven y borran de verdad, pero nunca se ejecutan solos. El reloj es fijo y
los health checks son dobles (nada de systemctl, ffmpeg, ALSA ni Spotify).
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from apscheduler.schedulers.background import BackgroundScheduler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from health import ERROR, OK, WARNING, CheckResult, HealthReport  # noqa: E402
from music_library import MusicLibrary  # noqa: E402
from preflight import SYNC_JOB_ID, PreflightScheduler, job_id  # noqa: E402

MONDAY_0700 = datetime(2026, 9, 28, 7, 0)
WEEKDAYS = ["0", "1", "2", "3", "4"]


def block_real_processes(test):
    for target in ("Popen", "run"):
        guard = mock.patch.object(subprocess, target,
                                  side_effect=AssertionError("los tests no ejecutan procesos"))
        guard.start()
        test.addCleanup(guard.stop)


def report(**statuses):
    """HealthReport con los estados indicados (el resto, ok)."""
    summaries = {"local_music": "4 pistas"}
    ids = ("audio", "spotify", "raspotify", "local_music", "ffmpeg", "emergency", "bluetooth",
           "scheduler")
    return HealthReport(MONDAY_0700, "test", tuple(
        CheckResult(cid, cid, statuses.get(cid, OK), summaries.get(cid, "x")) for cid in ids))


class FakeChecker:
    def __init__(self, result=None, error=None):
        self.result = result or report()
        self.error = error
        self.triggers = []

    def run(self, trigger="Manual"):
        self.triggers.append(trigger)
        if self.error:
            raise self.error
        return self.result


def alarm_rows(database):
    conn = sqlite3.connect(database)
    try:
        return [tuple(row) for row in conn.execute("SELECT * FROM alarms ORDER BY id")]
    finally:
        conn.close()


def paused_scheduler(test):
    sched = BackgroundScheduler(daemon=True)
    sched.start(paused=True)  # jobs reales que nunca se ejecutan solos
    test.addCleanup(sched.shutdown, wait=False)
    return sched


def preflight_jobs(sched):
    return {job.id: job.next_run_time.replace(tzinfo=None)
            for job in sched.get_jobs() if job.id.startswith("preflight:")}


class PreflightSchedulingTest(unittest.TestCase):
    """A través de las rutas de la app, como lo usa Groove."""

    def setUp(self):
        block_real_processes(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(scheduler.close_logging)
        self.db_path = os.path.join(tmp.name, "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path,
                               "ALARM_LOG": os.path.join(tmp.name, "a.log")},
                              player=mock.Mock(spec=AudioPlayer))
        self.client = self.app.test_client()
        self.now = MONDAY_0700
        self.sched = paused_scheduler(self)
        self.preflight = self.app.extensions["preflight"]
        self.preflight.clock = lambda: self.now
        self.preflight.checker = FakeChecker()
        self.preflight.start(self.sched)

    def create(self, **overrides):
        data = {"name": "Trabajo", "time": "07:30", "days": WEEKDAYS}
        data.update(overrides)
        self.assertEqual(self.client.post("/alarms/new", data=data).status_code, 302)

    def test_default_is_five_minutes(self):
        self.assertEqual(self.app.config["ALARM_PREFLIGHT_MINUTES"], 5)
        self.assertEqual(self.preflight.minutes, 5)

    def test_env_setting(self):
        with mock.patch.dict(os.environ, {"ALARM_PREFLIGHT_MINUTES": "10"}):
            app = create_app({"TESTING": True, "DATABASE": self.db_path,
                              "ALARM_LOG": os.path.join(os.path.dirname(self.db_path), "b.log")},
                             player=mock.Mock(spec=AudioPlayer))
        self.assertEqual(app.extensions["preflight"].minutes, 10)

    def test_start_adds_periodic_sync_job(self):
        job = self.sched.get_job(SYNC_JOB_ID)
        self.assertIsNotNone(job)
        self.assertIn("second='30'", str(job.trigger))

    def test_create_schedules_preflight(self):
        with self.assertLogs("alarms", "INFO") as logs:
            self.create()
        self.assertEqual(preflight_jobs(self.sched), {"preflight:1": datetime(2026, 9, 28, 7, 25)})
        self.assertEqual(self.sched.get_job("preflight:1").kwargs,
                         {"alarm_id": 1, "alarm_at": "2026-09-28T07:30"})
        self.assertIn("Pre-flight programado para «Trabajo» (alarma 1)", "\n".join(logs.output))
        # Solo jobs del pre-flight: nada más se crea ni se modifica.
        self.assertEqual({job.id for job in self.sched.get_jobs()}, {SYNC_JOB_ID, "preflight:1"})

    def test_edit_reschedules_the_same_job(self):
        self.create()
        self.client.post("/alarms/1/edit", data={"name": "Trabajo", "time": "08:00",
                                                 "days": WEEKDAYS})
        self.assertEqual(preflight_jobs(self.sched), {"preflight:1": datetime(2026, 9, 28, 7, 55)})

    def test_edit_days_moves_to_the_next_matching_day(self):
        self.create()
        self.client.post("/alarms/1/edit", data={"name": "Finde", "time": "07:30",
                                                 "days": ["5", "6"]})
        self.assertEqual(preflight_jobs(self.sched), {"preflight:1": datetime(2026, 10, 3, 7, 25)})

    def test_delete_removes_preflight(self):
        self.create()
        with self.assertLogs("alarms", "INFO") as logs:
            self.client.post("/alarms/1/delete")
        self.assertEqual(preflight_jobs(self.sched), {})
        self.assertIn("Pre-flight cancelado para la alarma 1", "\n".join(logs.output))

    def test_disable_and_enable(self):
        self.create()
        self.client.post("/alarms/1/toggle")
        self.assertEqual(preflight_jobs(self.sched), {})
        self.client.post("/alarms/1/toggle")
        self.assertEqual(preflight_jobs(self.sched), {"preflight:1": datetime(2026, 9, 28, 7, 25)})

    def test_disabled_alarm_never_gets_preflight(self):
        self.create()
        self.client.post("/alarms/1/toggle")
        self.preflight.sync()
        self.assertEqual(preflight_jobs(self.sched), {})

    def test_each_alarm_has_its_own_job(self):
        self.create()
        self.create(name="Tarde", time="18:00", days=[])
        self.assertEqual(preflight_jobs(self.sched), {
            "preflight:1": datetime(2026, 9, 28, 7, 25),
            "preflight:2": datetime(2026, 9, 28, 17, 55),
        })

    def test_alarm_inside_the_window_has_no_preflight(self):
        self.now = datetime(2026, 9, 28, 7, 27)
        self.create()  # suena a las 07:30: quedan 3 minutos
        self.assertEqual(preflight_jobs(self.sched), {})

    def test_alarm_exactly_at_the_limit_has_no_preflight(self):
        self.now = datetime(2026, 9, 28, 7, 25)
        self.create()
        self.assertEqual(preflight_jobs(self.sched), {})

    def test_alarm_inside_the_window_still_rings(self):
        self.now = datetime(2026, 9, 28, 7, 27)
        self.create()
        manager = mock.Mock()
        fired = scheduler.check_alarms(self.db_path, datetime(2026, 9, 28, 7, 30), manager=manager)
        self.assertEqual(fired, ["Trabajo"])
        manager.start.assert_called_once()

    def test_recurring_alarm_gets_next_preflight_after_ringing(self):
        self.create()
        self.now = datetime(2026, 9, 28, 7, 30, 30)  # ya ha sonado; el job de sync corre en :30
        self.preflight.sync()
        self.assertEqual(preflight_jobs(self.sched), {"preflight:1": datetime(2026, 9, 29, 7, 25)})

    def test_one_time_alarm_loses_its_preflight_after_ringing(self):
        self.create(days=[])
        scheduler.check_alarms(self.db_path, datetime(2026, 9, 28, 7, 30), manager=mock.Mock())
        self.now = datetime(2026, 9, 28, 7, 30, 30)
        self.preflight.sync()
        self.assertEqual(preflight_jobs(self.sched), {})

    def test_due_job_is_not_removed_by_a_concurrent_sync(self):
        self.create()
        self.now = datetime(2026, 9, 28, 7, 25)  # el pre-flight está a punto de ejecutarse
        self.preflight.sync()
        self.assertIn("preflight:1", preflight_jobs(self.sched))

    def test_orphan_jobs_are_removed(self):
        self.sched.add_job(print, "date", run_date=datetime(2026, 9, 28, 9, 0), id="preflight:99")
        self.preflight.sync()
        self.assertEqual(preflight_jobs(self.sched), {})
        self.assertIsNotNone(self.sched.get_job(SYNC_JOB_ID))  # su propio job sigue

    def test_restart_rebuilds_jobs_from_the_database(self):
        self.create()
        self.create(name="Tarde", time="18:00")
        new_sched = paused_scheduler(self)  # Groove reiniciado: jobstore vacío
        restarted = PreflightScheduler(self.db_path, FakeChecker(), clock=lambda: self.now)
        restarted.start(new_sched)
        self.assertEqual(set(preflight_jobs(new_sched)), {"preflight:1", "preflight:2"})

    def test_restart_inside_the_window_does_not_run_late(self):
        self.create()
        new_sched = paused_scheduler(self)
        checker = FakeChecker()
        restarted = PreflightScheduler(self.db_path, checker,
                                       clock=lambda: datetime(2026, 9, 28, 7, 27))
        restarted.start(new_sched)
        self.assertEqual(preflight_jobs(new_sched), {})
        self.assertEqual(checker.triggers, [])

    def test_zero_minutes_disables_preflight(self):
        self.create()
        self.preflight.minutes = 0
        self.preflight.sync()
        self.assertEqual(preflight_jobs(self.sched), {})

    def test_sync_never_breaks_the_routes(self):
        self.preflight.database = os.path.join(os.path.dirname(self.db_path), "no", "existe.db")
        with self.assertLogs("alarms", "ERROR"):
            self.create()
        self.assertEqual(len(alarm_rows(self.db_path)), 1)

    def test_without_scheduler_sync_does_nothing(self):
        self.preflight.scheduler = None
        self.create()  # p. ej. en tests o con SCHEDULER_ENABLED=False
        self.assertEqual(preflight_jobs(self.sched), {})


class PreflightRunTest(unittest.TestCase):
    def setUp(self):
        block_real_processes(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = os.path.join(tmp.name, "test.db")
        db.init_db(self.db_path)
        self.music = Path(tmp.name) / "music"
        self.music.mkdir()
        self.checker = FakeChecker()
        self.preflight = PreflightScheduler(self.db_path, self.checker,
                                            library=MusicLibrary(self.music),
                                            clock=lambda: MONDAY_0700)

    def add_alarm(self, source="spotify", enabled=1, track=None, days="0,1,2,3,4"):
        conn = sqlite3.connect(self.db_path)
        uri = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M" if source == "spotify" else None
        cur = conn.execute("INSERT INTO alarms (name, time, days, enabled, source, spotify_uri,"
                           " local_track) VALUES ('Trabajo', '07:30', ?, ?, ?, ?, ?)",
                           (days, enabled, source, uri, track))
        conn.commit()
        conn.close()
        return cur.lastrowid

    def logs(self, alarm_id):
        with self.assertLogs("alarms", "INFO") as captured:
            self.preflight.run(alarm_id, "2026-09-28T07:30")
        return "\n".join(captured.output)

    def test_logs_statuses_of_relevant_checks(self):
        self.checker.result = report(spotify=WARNING)
        output = self.logs(self.add_alarm())
        self.assertIn("Pre-flight alarma 1: «Trabajo» 07:30 (spotify): audio=ok spotify=warning "
                      "raspotify=ok local_music=ok ffmpeg=ok emergency=ok bluetooth=ok "
                      "scheduler=ok", output)
        self.assertEqual(self.checker.triggers, ["Pre-flight de «Trabajo»"])

    def test_spotify_failure_mentions_local_fallback(self):
        self.checker.result = report(spotify=ERROR)
        output = self.logs(self.add_alarm())
        self.assertIn("Spotify puede fallar; hay respaldo: música local (4 pistas).", output)

    def test_logs_problem_details_for_errors_and_warnings(self):
        for status in (ERROR, WARNING):
            with self.subTest(status=status):
                detail = "/usr/bin/ffmpeg tardó más de 3 s"
                retry = "Timeout persistente tras un único reintento (2 intentos)."
                self.checker.result = HealthReport(MONDAY_0700, "test", tuple(
                    CheckResult(c.id, c.name, status, "No se puede ejecutar", (detail, retry))
                    if c.id == "ffmpeg" else c for c in report().results))
                output = self.logs(self.add_alarm(source="local"))
                self.assertIn(f"ffmpeg={status} (No se puede ejecutar): {detail}; {retry}", output)

    def test_ok_details_are_not_logged(self):
        self.checker.result = HealthReport(MONDAY_0700, "test", tuple(
            CheckResult(c.id, c.name, OK, c.summary, ("detalle de éxito",))
            for c in report().results))
        self.assertNotIn("detalle de éxito", self.logs(self.add_alarm()))

    def test_spotify_failure_with_only_emergency_wav(self):
        self.checker.result = report(raspotify=ERROR, local_music=WARNING)
        output = self.logs(self.add_alarm())
        self.assertIn("Spotify puede fallar; hay respaldo: WAV de emergencia.", output)

    def test_no_fallback_at_all(self):
        self.checker.result = report(spotify=ERROR, ffmpeg=ERROR, emergency=ERROR)
        output = self.logs(self.add_alarm())
        self.assertIn("NO hay respaldo", output)

    def test_chosen_track_that_no_longer_exists(self):
        self.checker.result = report()
        output = self.logs(self.add_alarm(source="local", track="borrada.mp3"))
        self.assertIn("sin música local disponible: sonará el WAV de emergencia.", output)

    def test_local_alarm_does_not_log_spotify(self):
        output = self.logs(self.add_alarm(source="local"))
        self.assertIn("(local): audio=ok local_music=ok", output)
        self.assertNotIn("spotify=", output)
        self.assertIn("todo listo.", output)

    def test_audio_error_is_highlighted(self):
        self.checker.result = report(audio=ERROR)
        self.assertIn("la salida de audio USB no está disponible", self.logs(self.add_alarm()))

    def test_disabled_or_deleted_alarm_is_skipped(self):
        alarm_id = self.add_alarm(enabled=0)
        self.assertIn("omitido", self.logs(alarm_id))
        self.assertIn("omitido", self.logs(99))
        self.assertEqual(self.checker.triggers, [])

    def test_total_failure_never_raises(self):
        self.checker.error = RuntimeError("todo roto")
        with self.assertLogs("alarms", "ERROR") as captured:
            self.preflight.run(self.add_alarm(), "2026-09-28T07:30")
        self.assertIn("La alarma sonará igualmente", "\n".join(captured.output))

    def test_broken_database_never_raises(self):
        self.preflight.database = os.path.join(os.path.dirname(self.db_path), "no", "x.db")
        with self.assertLogs("alarms", "ERROR"):
            self.preflight.run(1)

    def test_failed_preflight_and_the_alarm_still_rings_on_time(self):
        """El job de pre-flight revienta y, a su hora, check_alarms dispara la alarma."""
        alarm_id = self.add_alarm()
        sched = paused_scheduler(self)
        self.checker.error = RuntimeError("todo roto")
        self.preflight.start(sched)
        job = sched.get_job(job_id(alarm_id))
        self.assertEqual(job.next_run_time.replace(tzinfo=None), datetime(2026, 9, 28, 7, 25))
        with self.assertLogs("alarms", "ERROR"):
            job.func(*job.args, **job.kwargs)  # lo que ejecutaría APScheduler a las 07:25
        manager = mock.Mock()
        fired = scheduler.check_alarms(self.db_path, datetime(2026, 9, 28, 7, 30), manager=manager)
        self.assertEqual(fired, ["Trabajo"])
        self.assertEqual(manager.start.call_args.args[0]["id"], alarm_id)

    def test_preflight_does_not_touch_playback_or_the_alarm(self):
        alarm_id = self.add_alarm()
        before = alarm_rows(self.db_path)
        self.logs(alarm_id)
        self.assertEqual(alarm_rows(self.db_path), before)


if __name__ == "__main__":
    unittest.main()
