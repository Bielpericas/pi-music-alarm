"""STOP y Snooze de la alarma que suena. Sin audio real ni Spotify real."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import SpotifyClient, SpotifyConnectionError  # noqa: E402
from spotify_player import SpotifyAlarmPlayer  # noqa: E402

PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
NOW = datetime(2026, 9, 28, 7, 30, 0)  # lunes
LOCAL = {"id": 1, "name": "Despertador", "time": "07:30", "source": "local", "spotify_uri": None}
SPOTIFY = {"id": 2, "name": "Música", "time": "07:30", "source": "spotify",
           "spotify_uri": PLAYLIST_URI}


class FakeScheduler:
    """schedule_once falso: guarda los jobs y permite ejecutarlos a mano."""

    def __init__(self):
        self.jobs = []  # [run_at, callback, cancel_mock]

    def __call__(self, run_at, callback):
        cancel = mock.Mock()
        self.jobs.append([run_at, callback, cancel])
        return cancel

    def run_last(self):
        _, callback, cancel = self.jobs[-1]
        if not cancel.called:
            callback()


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.local = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyAlarmPlayer)
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = True
        self.sched = FakeScheduler()
        self.clock = Clock()
        self.manager = AlarmPlaybackManager(self.local, self.spotify,
                                            schedule_once=self.sched, clock=self.clock)

    # --- start ---

    def test_start_records_active_alarm(self):
        self.assertEqual(self.manager.start(LOCAL), "local")
        active = self.manager.active
        self.assertEqual((active.id, active.via, active.started_at), (1, "local", NOW))
        self.local.play.assert_called_once_with()

    def test_start_spotify_and_fallback(self):
        self.assertEqual(self.manager.start(SPOTIFY), "spotify")
        self.spotify.play.assert_called_once_with(PLAYLIST_URI)
        self.local.play.assert_not_called()
        self.spotify.play.return_value = False
        self.assertEqual(self.manager.start(SPOTIFY), "fallback")
        self.assertEqual(self.manager.active.via, "fallback")
        self.local.play.assert_called_once_with()

    # --- STOP ---

    def test_stop_local(self):
        self.manager.start(LOCAL)
        result = self.manager.stop()
        self.assertEqual(result.active.id, 1)
        self.assertTrue(result.silenced)
        self.local.stop.assert_called_once_with()
        self.spotify.stop.assert_not_called()
        self.assertIsNone(self.manager.active)

    def test_stop_spotify_pauses(self):
        self.manager.start(SPOTIFY)
        self.manager.stop()
        self.spotify.stop.assert_called_once_with()
        self.local.stop.assert_not_called()
        self.assertIsNone(self.manager.active)

    def test_stop_after_fallback_stops_local(self):
        self.spotify.play.return_value = False
        self.manager.start(SPOTIFY)
        self.manager.stop()
        self.local.stop.assert_called_once_with()
        self.spotify.stop.assert_not_called()

    def test_stop_is_idempotent(self):
        self.manager.start(LOCAL)
        self.assertIsNotNone(self.manager.stop())
        self.assertIsNone(self.manager.stop())
        self.assertIsNone(self.manager.stop())
        self.local.stop.assert_called_once_with()

    def test_stop_without_alarm(self):
        self.assertIsNone(self.manager.stop())
        self.local.stop.assert_not_called()

    def test_stop_reports_when_spotify_pause_fails(self):
        self.spotify.stop.return_value = False
        self.manager.start(SPOTIFY)
        result = self.manager.stop()
        self.assertFalse(result.silenced)
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.spotify.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)
        self.assertIsNone(self.manager.active)
        self.assertEqual(self.spotify.stop.call_count, 2)

    def test_concurrent_stops_stop_once(self):
        self.manager.start(LOCAL)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.manager.stop()))
                   for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(r is not None for r in results), 1)
        self.local.stop.assert_called_once_with()

    # --- Snooze ---

    def test_snooze_local(self):
        self.manager.start(LOCAL)
        pending = self.manager.snooze()
        self.local.stop.assert_called_once_with()
        self.assertIsNone(self.manager.active)
        self.assertEqual(pending.run_at, NOW + timedelta(minutes=10))
        self.assertEqual(self.sched.jobs[0][0], NOW + timedelta(minutes=10))
        self.assertEqual(self.manager.pending_snoozes, (pending,))
        # Pasan los 10 minutos: vuelve a sonar la misma alarma.
        self.clock.now = NOW + timedelta(minutes=10)
        self.sched.run_last()
        active = self.manager.active
        self.assertEqual((active.id, active.via, active.snoozes), (1, "local", 1))
        self.assertEqual(active.alarm["time"], "07:30")  # la hora original no cambia
        self.assertEqual(self.local.play.call_count, 2)
        self.assertEqual(self.manager.pending_snoozes, ())

    def test_snooze_spotify_keeps_source_and_content(self):
        self.manager.start(SPOTIFY)
        self.manager.snooze()
        self.spotify.stop.assert_called_once_with()
        self.sched.run_last()
        self.assertEqual(self.spotify.play.call_args_list,
                         [mock.call(PLAYLIST_URI), mock.call(PLAYLIST_URI)])
        self.assertEqual(self.manager.active.via, "spotify")

    def test_snooze_again(self):
        self.manager.start(LOCAL)
        self.manager.snooze()
        self.clock.now = NOW + timedelta(minutes=10)
        self.sched.run_last()
        pending = self.manager.snooze()
        self.assertEqual(pending.run_at, NOW + timedelta(minutes=20))
        self.assertEqual(pending.snoozes, 2)
        self.sched.run_last()
        self.assertEqual(self.manager.active.snoozes, 2)
        self.assertEqual(self.local.play.call_count, 3)

    def test_snooze_without_alarm_or_twice(self):
        self.assertIsNone(self.manager.snooze())
        self.manager.start(LOCAL)
        self.assertIsNotNone(self.manager.snooze())
        self.assertIsNone(self.manager.snooze())  # doble pulsación
        self.assertEqual(len(self.sched.jobs), 1)

    def test_stop_after_snooze_does_not_cancel_snooze(self):
        self.manager.start(LOCAL)
        self.manager.snooze()
        self.assertIsNone(self.manager.stop())  # no suena nada: no hace nada
        self.assertEqual(len(self.manager.pending_snoozes), 1)

    def test_cancel_snooze(self):
        self.manager.start(LOCAL)
        self.manager.snooze()
        self.assertTrue(self.manager.cancel_snooze(1))
        self.assertFalse(self.manager.cancel_snooze(1))  # idempotente
        self.sched.jobs[0][2].assert_called_once_with()
        self.sched.jobs[0][1]()
        self.assertIsNone(self.manager.active)

    def test_same_alarm_ringing_again_cancels_its_snooze(self):
        self.manager.start(LOCAL)
        self.manager.snooze()
        self.manager.start(LOCAL)  # p. ej. Probar mientras estaba pospuesta
        self.assertEqual(self.manager.pending_snoozes, ())
        self.manager.stop()
        self.sched.jobs[0][1]()  # el job antiguo ya no hace sonar nada
        self.assertIsNone(self.manager.active)

    def test_other_alarm_keeps_pending_snooze(self):
        self.manager.start(LOCAL)
        self.manager.snooze()
        self.manager.start(SPOTIFY)
        self.assertEqual(len(self.manager.pending_snoozes), 1)

    def test_forget_cancels_snooze_and_stops(self):
        self.manager.start(LOCAL)
        self.manager.snooze()
        self.manager.forget(1)
        self.sched.jobs[0][2].assert_called_once_with()  # job cancelado
        self.assertEqual(self.manager.pending_snoozes, ())
        self.sched.jobs[0][1]()  # aunque el job llegara a ejecutarse...
        self.assertIsNone(self.manager.active)  # ...no suena
        self.manager.start(LOCAL)
        self.manager.forget(1)
        self.assertIsNone(self.manager.active)

    # --- Una alarma nueva sustituye a la activa ---

    def test_new_alarm_replaces_local(self):
        self.manager.start(LOCAL)
        other = {**LOCAL, "id": 3, "name": "Otra"}
        self.manager.start(other)
        self.assertEqual(self.manager.active.id, 3)
        self.local.stop.assert_called_once_with()
        self.assertEqual(self.local.play.call_count, 2)

    def test_local_alarm_replaces_spotify_pauses_it(self):
        self.manager.start(SPOTIFY)
        self.manager.start(LOCAL)
        self.spotify.stop.assert_called_once_with()
        self.assertEqual(self.manager.active.via, "local")

    def test_spotify_alarm_replaces_spotify_without_pausing(self):
        self.manager.start(SPOTIFY)
        self.manager.start({**SPOTIFY, "id": 4})
        self.spotify.stop.assert_not_called()  # la nueva reproducción ya sustituye
        self.assertEqual(self.manager.active.id, 4)

    def test_spotify_alarm_replacing_local_stops_wav(self):
        self.manager.start(LOCAL)
        self.manager.start(SPOTIFY)
        self.local.stop.assert_called_once_with()


class SpotifyAlarmPlayerStopTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        import db
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        conn = sqlite3.connect(self.database)
        conn.execute("INSERT INTO settings (key, value) VALUES ('spotify_device_id', 'dev')")
        conn.commit()
        conn.close()
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.client.get_devices.return_value = [{"id": "dev", "name": "PC", "type": "Computer", "is_active": True}]
        self.player = SpotifyAlarmPlayer(self.client, self.database)

    def test_stop_pauses_device_where_it_played(self):
        self.assertFalse(self.player.stop())  # nunca sonó: nada que pausar
        self.player.play(PLAYLIST_URI)
        self.assertTrue(self.player.stop())
        self.client.pause.assert_called_once_with("dev")

    def test_stop_error_returns_false(self):
        self.player.play(PLAYLIST_URI)
        self.client.pause.side_effect = SpotifyConnectionError("sin red")
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player.stop())


class PlaybackRoutesTest(unittest.TestCase):
    """Interfaz y rutas /playback/..., con Spotify y audio simulados."""

    def setUp(self):
        guard = mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("los tests no deben usar la red"))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.local = mock.Mock(spec=AudioPlayer)
        self.client_spotify = mock.Mock(spec=SpotifyClient)
        self.client_spotify.is_configured = True
        self.client_spotify.get_devices.return_value = [{"id": "dev", "name": "PC", "type": "Computer", "is_active": True}]
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "alarms.log"),
             "SPOTIFY_RETRY_DELAYS": (0, 0, 0, 0)},  # reintentos sin esperas reales
            player=self.local, spotify=self.client_spotify,
        )
        self.manager = self.app.extensions["playback"]
        self.sched = FakeScheduler()
        self.manager.schedule_once = self.sched
        self.client = self.app.test_client()

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM alarms ORDER BY id").fetchall()
        finally:
            conn.close()

    def create(self, **overrides):
        data = {"name": "Despertador", "time": "07:30", "days": ["0"]}
        data.update(overrides)
        self.client.post("/alarms/new", data=data)
        return self.rows()[-1]["id"]

    def html(self, url="/"):
        return self.client.get(url).get_data(as_text=True)

    def post(self, url):
        return self.client.post(url, follow_redirects=True).get_data(as_text=True)

    # --- Visibilidad de los botones ---

    def test_buttons_only_when_alarm_active(self):
        alarm_id = self.create()
        self.assertNotIn("+10 MIN", self.html())
        self.assertNotIn(">STOP<", self.html())
        self.post(f"/alarms/{alarm_id}/test")
        html = self.html()
        self.assertIn(">STOP<", html)
        self.assertIn("+10 MIN", html)
        self.assertIn("ALARMA", html)
        self.assertIn("07:30 · Despertador", html)
        self.assertIn("Sonido local", html)
        self.post("/playback/stop")
        self.assertNotIn(">STOP<", self.html())

    def test_banner_shows_spotify_content(self):
        self.client.post("/spotify/device", data={"device_id": "dev", "device_name": "PC"})
        alarm_id = self.create(name="Mañana", source="spotify", spotify_uri=PLAYLIST_URI)
        self.post(f"/alarms/{alarm_id}/test")
        html = self.html()
        self.assertIn("07:30 · Mañana", html)
        self.assertIn("Spotify · playlist", html)

    # --- STOP ---

    def test_stop_route_is_idempotent(self):
        alarm_id = self.create()
        self.post(f"/alarms/{alarm_id}/test")
        self.assertIn("detenida", self.post("/playback/stop"))
        html = self.post("/playback/stop")
        self.assertIn("No hay ninguna alarma sonando", html)
        self.local.stop.assert_called_once_with()

    def test_stop_spotify_route_pauses(self):
        self.client.post("/spotify/device", data={"device_id": "dev", "device_name": "PC"})
        alarm_id = self.create(source="spotify", spotify_uri=PLAYLIST_URI)
        self.post(f"/alarms/{alarm_id}/test")
        self.post("/playback/stop")
        self.client_spotify.pause.assert_called_once_with("dev")

    def test_stop_keeps_schedule(self):
        recurring = self.create()
        once = self.create(name="Una vez", days=[])
        # Se disparan de verdad por el scheduler (la puntual se desactiva sola).
        scheduler.check_alarms(self.db_path, NOW, manager=self.manager)
        self.assertEqual(self.manager.active.id, once)  # la última gana
        self.post("/playback/stop")
        rows = {r["id"]: r for r in self.rows()}
        self.assertEqual((rows[recurring]["enabled"], rows[recurring]["time"]), (1, "07:30"))
        self.assertEqual(rows[once]["enabled"], 0)  # sigue desactivada

    # --- Snooze ---

    def test_snooze_route(self):
        alarm_id = self.create()
        scheduler.check_alarms(self.db_path, NOW, manager=self.manager)
        before = [dict(r) for r in self.rows()]
        html = self.post("/playback/snooze")
        self.assertIn("pospuesta hasta las", html)
        self.assertIn("vuelve a sonar a las", html)
        self.assertNotIn(">STOP<", html)
        self.local.stop.assert_called_once_with()
        # No cambia la alarma original ni aparece una alarma nueva en la lista.
        self.assertEqual([dict(r) for r in self.rows()], before)
        self.assertEqual(html.count('class="alarm '), 1)
        # A los 10 minutos vuelve a sonar la misma alarma.
        self.sched.run_last()
        self.assertEqual(self.manager.active.id, alarm_id)
        self.assertIn(">STOP<", self.html())
        self.assertIn("pospuesta 1 vez", self.html())

    def test_snooze_without_active_alarm(self):
        self.assertIn("No hay ninguna alarma sonando", self.post("/playback/snooze"))
        self.assertEqual(self.sched.jobs, [])

    def test_cancel_snooze_button(self):
        alarm_id = self.create()
        self.post(f"/alarms/{alarm_id}/test")
        html = self.post("/playback/snooze")
        self.assertIn(f"/playback/snooze/{alarm_id}/cancel", html)
        html = self.post(f"/playback/snooze/{alarm_id}/cancel")
        self.assertIn("Snooze cancelado", html)
        self.assertNotIn("vuelve a sonar a las", html)
        self.assertEqual(self.manager.pending_snoozes, ())

    def test_delete_alarm_cancels_snooze(self):
        alarm_id = self.create()
        self.post(f"/alarms/{alarm_id}/test")
        self.post("/playback/snooze")
        self.client.post(f"/alarms/{alarm_id}/delete")
        self.assertEqual(self.manager.pending_snoozes, ())
        self.sched.run_last()
        self.assertIsNone(self.manager.active)

    def test_state_endpoint_key_changes(self):
        alarm_id = self.create()
        idle = self.client.get("/playback/state").get_json()
        self.assertIsNone(idle["active"])
        self.post(f"/alarms/{alarm_id}/test")
        ringing = self.client.get("/playback/state").get_json()
        self.assertEqual(ringing["active"]["name"], "Despertador")
        self.assertNotEqual(idle["key"], ringing["key"])
        self.assertIn(f'data-playback-key="{ringing["key"]}"', self.html())


if __name__ == "__main__":
    unittest.main()
