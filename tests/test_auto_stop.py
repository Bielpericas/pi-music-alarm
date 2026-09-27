"""Duración máxima / auto-stop de las alarmas. Sin audio, Spotify ni systemctl reales."""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from bluetooth_audio import BluetoothAudio  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import SpotifyClient  # noqa: E402
from spotify_player import SpotifyAlarmPlayer  # noqa: E402

PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
NOW = datetime(2026, 9, 28, 7, 30)  # lunes


def alarm(id=1, name="Despertador", source="local", minutes=30, **extra):
    data = {"id": id, "name": name, "time": "07:30", "source": source,
            "spotify_uri": PLAYLIST_URI if source == "spotify" else None,
            "max_duration_minutes": minutes}
    data.update(extra)
    return data


class FakeScheduler:
    """schedule_once falso: guarda los jobs; se ejecutan a mano."""

    def __init__(self):
        self.jobs = []  # [run_at, callback, cancel]

    def __call__(self, run_at, callback):
        cancel = mock.Mock()
        self.jobs.append([run_at, callback, cancel])
        return cancel


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class AutoStopManagerTest(unittest.TestCase):
    def setUp(self):
        self.local = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyAlarmPlayer)
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = True
        self.bluetooth = mock.Mock(spec=BluetoothAudio)
        self.fades = []
        self.sched = FakeScheduler()
        self.clock = Clock()
        self.manager = AlarmPlaybackManager(
            self.local, self.spotify, schedule_once=self.sched, clock=self.clock,
            fader=self.fader, bluetooth=self.bluetooth)

    def fader(self, set_volume, plan):
        fade = mock.Mock()
        self.fades.append(fade)
        return fade

    def fire(self, index=-1):
        return self.sched.jobs[index][1]()

    # --- Programación ---

    def test_start_schedules_auto_stop_at_the_limit(self):
        with self.assertLogs("alarms", "INFO") as logs:
            self.manager.start(alarm(minutes=45))
        self.assertEqual(len(self.sched.jobs), 1)
        self.assertEqual(self.sched.jobs[0][0], NOW + timedelta(minutes=45))
        self.assertIn("Auto-stop programado en 45 min (a las 08:15)", "\n".join(logs.output))

    def test_no_limit_creates_no_job(self):
        for data in (alarm(minutes=0), alarm(minutes=None),
                     {k: v for k, v in alarm().items() if k != "max_duration_minutes"}):
            with self.subTest(minutes=data.get("max_duration_minutes", "ausente")):
                self.manager.start(data)
                self.manager.stop()
        self.assertEqual(self.sched.jobs, [])

    # --- Al llegar al límite: el mismo camino que STOP ---

    def test_auto_stop_local(self):
        self.manager.start(alarm())
        with self.assertLogs("alarms", "INFO") as logs:
            result = self.fire()
        self.assertIsNone(self.manager.active)
        self.local.stop.assert_called_once_with()
        self.spotify.stop.assert_not_called()
        self.assertEqual(result.active.id, 1)
        self.assertIn("ALARMA DETENIDA POR AUTO-STOP: Despertador (duración máxima: 30 min)",
                      "\n".join(logs.output))

    def test_auto_stop_spotify_cancels_fade_and_pauses(self):
        self.manager.start(alarm(source="spotify", volume_start=20, volume_end=60,
                                 fade_minutes=5))
        self.assertEqual(len(self.fades), 1)
        self.fire()
        self.fades[0].cancel.assert_called_once_with()
        self.spotify.stop.assert_called_once_with()
        self.local.stop.assert_not_called()
        self.assertIsNone(self.manager.active)

    def test_auto_stop_fallback_stops_local(self):
        self.spotify.play.return_value = False
        self.assertEqual(self.manager.start(alarm(source="spotify")), "fallback")
        self.fire()
        self.local.stop.assert_called_once_with()
        self.assertIsNone(self.manager.active)

    def test_auto_stop_gives_bluetooth_back(self):
        self.manager.start(alarm())
        self.bluetooth.pause.assert_called_once_with()
        self.bluetooth.resume.assert_not_called()
        self.fire()
        self.bluetooth.resume.assert_called_once_with()

    def test_auto_stop_is_not_a_snooze(self):
        self.manager.start(alarm())
        self.fire()
        self.assertEqual(self.manager.pending_snoozes, ())
        self.assertEqual(len(self.sched.jobs), 1)  # nada reprogramado

    # --- STOP / +10 MIN / sustitución ---

    def test_stop_before_the_limit_cancels_auto_stop(self):
        self.manager.start(alarm())
        with self.assertLogs("alarms", "INFO") as logs:
            self.manager.stop()
        self.sched.jobs[0][2].assert_called_once_with()
        self.assertIn("Auto-stop cancelado", "\n".join(logs.output))
        self.fire()  # aunque el job llegara a ejecutarse, no hace nada
        self.local.stop.assert_called_once_with()
        self.bluetooth.resume.assert_called_once_with()

    def test_snooze_cancels_and_next_ring_gets_a_full_new_limit(self):
        self.manager.start(alarm(minutes=15))
        self.clock.now = NOW + timedelta(minutes=12)
        pending = self.manager.snooze()
        auto_stop, snooze_job = self.sched.jobs
        auto_stop[2].assert_called_once_with()
        self.assertEqual(snooze_job[0], pending.run_at)
        self.clock.now = pending.run_at  # 07:52: vuelve a sonar
        snooze_job[1]()
        self.assertEqual(self.manager.active.snoozes, 1)
        new_auto_stop = self.sched.jobs[-1]
        self.assertEqual(new_auto_stop[0], pending.run_at + timedelta(minutes=15))
        auto_stop[1]()  # el del primer sonido no para el segundo
        self.assertIsNotNone(self.manager.active)
        new_auto_stop[1]()
        self.assertIsNone(self.manager.active)
        self.assertEqual(self.manager.pending_snoozes, ())

    def test_replacing_alarm_cancels_previous_timer_and_creates_new(self):
        self.manager.start(alarm(id=1, name="Primera", minutes=15))
        self.clock.now = NOW + timedelta(minutes=5)
        self.manager.start(alarm(id=2, name="Segunda", minutes=60))
        first, second = self.sched.jobs
        first[2].assert_called_once_with()
        self.assertEqual(second[0], NOW + timedelta(minutes=65))
        first[1]()  # el viejo llega tarde: ignorado
        self.assertEqual(self.manager.active.alarm["name"], "Segunda")
        second[1]()
        self.assertIsNone(self.manager.active)

    def test_replacing_with_unlimited_alarm_leaves_no_timer(self):
        self.manager.start(alarm(id=1, minutes=15))
        self.manager.start(alarm(id=2, minutes=0))
        self.assertEqual(len(self.sched.jobs), 1)
        self.sched.jobs[0][2].assert_called_once_with()
        self.fire()
        self.assertEqual(self.manager.active.id, 2)

    def test_old_timer_never_stops_a_later_ring_of_the_same_alarm(self):
        # Misma alarma, STOP y vuelve a sonar (p. ej. Probar): el job viejo no
        # se canceló a tiempo (cancel falla) y se ejecuta después.
        self.manager.start(alarm())
        self.sched.jobs[0][2].side_effect = Exception("job ya en ejecución")
        self.manager.stop()
        self.manager.start(alarm())
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertIsNone(self.sched.jobs[0][1]())
        self.assertIn("Auto-stop antiguo ignorado", "\n".join(logs.output))
        self.assertEqual(self.manager.active.id, 1)
        self.assertEqual(self.local.stop.call_count, 1)  # solo el STOP manual
        self.sched.jobs[1][1]()  # el suyo sí la para
        self.assertIsNone(self.manager.active)

    def test_old_timer_does_not_interrupt_a_spotify_alarm_starting(self):
        self.manager.start(alarm(id=1))
        old = self.sched.jobs[0][1]
        self.manager.stop()

        def slow_play(uri, **kwargs):
            old()  # el job viejo llega mientras la nueva busca el dispositivo
            return True
        self.spotify.play.side_effect = slow_play
        self.assertEqual(self.manager.start(alarm(id=2, source="spotify")), "spotify")
        self.spotify.interrupt.assert_not_called()
        self.assertEqual(self.manager.active.id, 2)

    def test_delete_ringing_alarm_cancels_auto_stop(self):
        self.manager.start(alarm())
        self.manager.forget(1)
        self.sched.jobs[0][2].assert_called_once_with()
        self.assertIsNone(self.manager.active)

    def test_cancelled_start_schedules_nothing(self):
        def interrupted(uri, **kwargs):
            self.manager._starting.set()
            return False
        self.spotify.play.side_effect = interrupted
        self.assertEqual(self.manager.start(alarm(source="spotify")), "cancelled")
        self.assertEqual(self.sched.jobs, [])

    def test_schedule_failure_never_blocks_the_alarm(self):
        broken = mock.Mock(side_effect=RuntimeError("scheduler parado"))
        manager = AlarmPlaybackManager(self.local, self.spotify, schedule_once=broken,
                                       clock=self.clock)
        with self.assertLogs("alarms", "ERROR"):
            self.assertEqual(manager.start(alarm()), "local")
        self.local.play.assert_called_once_with()
        self.assertIsNotNone(manager.stop())


class DurationFormTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "t", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "a.log")},
            player=mock.Mock(spec=AudioPlayer), spotify=mock.Mock(spec=SpotifyClient))
        self.client = self.app.test_client()

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def post(self, url="/alarms/new", **extra):
        data = {"name": "Trabajo", "time": "07:30", "days": ["0"]}
        data.update(extra)
        return self.client.post(url, data=data)

    def durations(self):
        conn = sqlite3.connect(self.db_path)
        values = [row[0] for row in conn.execute(
            "SELECT max_duration_minutes FROM alarms ORDER BY id")]
        conn.close()
        return values

    def test_form_offers_the_five_options_with_30_by_default(self):
        html = self.client.get("/alarms/new").get_data(as_text=True)
        self.assertIn('<label for="max_duration_minutes">Duración máxima</label>', html)
        start = html.index('<select id="max_duration_minutes"')
        select = html[start:html.index("</select>", start)]
        options = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)<', select)
        self.assertEqual(options, [("15", "15 minutos"), ("30", "30 minutos"),
                                   ("45", "45 minutos"), ("60", "60 minutos"),
                                   ("0", "Sin límite")])
        self.assertIn('<option value="30" selected', select)

    def test_create_with_each_option(self):
        for value in ("15", "30", "45", "60", "0"):
            self.post(max_duration_minutes=value)
        self.assertEqual(self.durations(), [15, 30, 45, 60, 0])

    def test_missing_field_uses_default(self):
        self.post()
        self.assertEqual(self.durations(), [30])

    def test_arbitrary_values_are_rejected(self):
        for value in ("20", "-5", "90", "abc"):
            with self.subTest(value=value):
                resp = self.post(max_duration_minutes=value)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.durations(), [])

    def test_edit_shows_and_saves_duration(self):
        self.post(max_duration_minutes="45")
        html = self.client.get("/alarms/1/edit").get_data(as_text=True)
        self.assertIn('<option value="45" selected', html)
        self.post("/alarms/1/edit", max_duration_minutes="0")
        self.assertEqual(self.durations(), [0])
        self.assertIn('<option value="0" selected',
                      self.client.get("/alarms/1/edit").get_data(as_text=True))

    def test_probar_uses_the_alarm_duration(self):
        playback = self.app.extensions["playback"]
        sched = FakeScheduler()
        playback.schedule_once = sched
        self.post(max_duration_minutes="15")
        self.client.post("/alarms/1/test")
        self.assertEqual(len(sched.jobs), 1)
        sched.jobs[0][1]()
        self.assertIsNone(playback.active)
        self.post("/alarms/1/edit", max_duration_minutes="0")
        self.client.post("/alarms/1/test")
        self.assertEqual(len(sched.jobs), 1)  # sin límite: ningún job nuevo
        self.client.post("/playback/stop")

    def test_scheduled_alarm_from_db_gets_auto_stop(self):
        self.post(max_duration_minutes="60")
        sched = FakeScheduler()
        manager = AlarmPlaybackManager(mock.Mock(spec=AudioPlayer), None,
                                       schedule_once=sched, clock=lambda: NOW)
        fired = scheduler.check_alarms(self.db_path, now=NOW, manager=manager)
        self.assertEqual(fired, ["Trabajo"])
        self.assertEqual(sched.jobs[0][0], NOW + timedelta(minutes=60))

    def test_migrates_old_alarms_to_default_duration(self):
        old = os.path.join(self.tmpdir.name, "old.db")
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " time TEXT NOT NULL, days TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL"
            " DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " last_triggered TEXT, source TEXT NOT NULL DEFAULT 'local', spotify_uri TEXT,"
            " volume_start INTEGER NOT NULL DEFAULT 20, volume_end INTEGER NOT NULL DEFAULT 60,"
            " fade_minutes INTEGER NOT NULL DEFAULT 5)")
        conn.execute("INSERT INTO alarms (name, time, days, enabled, last_triggered, source,"
                     " spotify_uri, volume_start, volume_end, fade_minutes) VALUES"
                     f" ('Vieja', '06:45', '0,4', 0, '2026-09-25 06:45', 'spotify',"
                     f" '{PLAYLIST_URI}', 10, 80, 15)")
        conn.commit()
        conn.close()
        db.init_db(old)
        db.init_db(old)  # idempotente
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM alarms").fetchone())
        conn.close()
        self.assertEqual(row["max_duration_minutes"], db.DEFAULT_MAX_DURATION)
        self.assertEqual(db.DEFAULT_MAX_DURATION, 30)
        expected = {"name": "Vieja", "time": "06:45", "days": "0,4", "enabled": 0,
                    "last_triggered": "2026-09-25 06:45", "source": "spotify",
                    "spotify_uri": PLAYLIST_URI, "volume_start": 10, "volume_end": 80,
                    "fade_minutes": 15}
        self.assertEqual({key: row[key] for key in expected}, expected)


if __name__ == "__main__":
    unittest.main()
