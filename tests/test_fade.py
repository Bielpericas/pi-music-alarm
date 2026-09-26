"""Volumen por alarma y fade-in (Spotify). Sin audio real ni Spotify real."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from fade import FADE_STEP_SECONDS, VolumeFade, fade_plan  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import API_BASE, SpotifyClient, SpotifyForbiddenError  # noqa: E402
from spotify_player import SpotifyAlarmPlayer  # noqa: E402
from test_spotify_client import FakeTransport, MemoryTokenStore, resp  # noqa: E402

PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
SPOTIFY = {"id": 2, "name": "Música", "time": "07:30", "source": "spotify",
           "spotify_uri": PLAYLIST_URI, "volume_start": 20, "volume_end": 60, "fade_minutes": 5}
LOCAL = {"id": 1, "name": "Despertador", "time": "07:30", "source": "local",
         "spotify_uri": None, "volume_start": 20, "volume_end": 60, "fade_minutes": 5}


def no_network():
    return mock.patch("urllib.request.urlopen",
                      side_effect=AssertionError("los tests no deben usar la red"))


class FadePlanTest(unittest.TestCase):
    def test_default_fade_is_gradual_and_ends_exactly(self):
        plan = fade_plan(20, 60, 300)
        self.assertEqual(len(plan), 300 // FADE_STEP_SECONDS)  # 20 pasos, uno cada 15 s
        times = [t for t, _ in plan]
        volumes = [v for _, v in plan]
        self.assertEqual(times[0], FADE_STEP_SECONDS)
        self.assertEqual(plan[-1], (300, 60))
        self.assertTrue(all(b > a for a, b in zip(volumes, volumes[1:])))  # sube siempre
        self.assertTrue(all(b - a <= 3 for a, b in zip([20] + volumes, volumes)))  # sin saltos
        self.assertTrue(all(b - a == FADE_STEP_SECONDS for a, b in zip(times, times[1:])))

    def test_odd_values_end_exactly_on_target(self):
        for start, end, seconds in ((10, 61, 180), (0, 100, 60), (33, 34, 600), (5, 95, 17)):
            with self.subTest(start=start, end=end, seconds=seconds):
                plan = fade_plan(start, end, seconds)
                self.assertEqual(plan[-1][1], end)
                self.assertAlmostEqual(plan[-1][0], seconds)
                self.assertLessEqual(len(plan), -(-seconds // FADE_STEP_SECONDS))

    def test_small_range_skips_repeated_volumes(self):
        plan = fade_plan(58, 60, 300)
        self.assertEqual([v for _, v in plan], [59, 60])

    def test_no_fade_cases(self):
        self.assertEqual(fade_plan(60, 60, 300), [])
        self.assertEqual(fade_plan(20, 60, 0), [(0, 60)])


class VolumeFadeTest(unittest.TestCase):
    def run_fade(self, plan, set_volume):
        waits = []
        fade = VolumeFade(set_volume, plan, wait=lambda s: waits.append(s) or False,
                          clock=lambda: 0.0)
        fade.run()
        return fade, waits

    def test_applies_every_step_in_order(self):
        calls = []
        plan = fade_plan(20, 60, 300)
        fade, waits = self.run_fade(plan, lambda v: calls.append(v) or True)
        self.assertEqual(calls, [v for _, v in plan])
        self.assertEqual(calls[-1], 60)
        self.assertEqual(fade.last_volume, 60)
        self.assertEqual(waits, [t for t, _ in plan])  # espera hasta cada paso

    def test_error_stops_fade_keeping_last_volume(self):
        calls = []

        def set_volume(v):
            calls.append(v)
            return len(calls) < 3  # el tercer ajuste falla

        with self.assertLogs("alarms", "WARNING"):
            fade, _ = self.run_fade(fade_plan(20, 60, 300), set_volume)
        self.assertEqual(len(calls), 3)  # no sigue intentándolo
        self.assertEqual(fade.last_volume, calls[1])

    def test_exception_stops_fade(self):
        with self.assertLogs("alarms", "ERROR"):
            fade, _ = self.run_fade([(15, 30), (30, 40)],
                                    mock.Mock(side_effect=RuntimeError("boom")))
        self.assertIsNone(fade.last_volume)

    def test_cancel_stops_before_next_step(self):
        calls = []
        fade = VolumeFade(None, fade_plan(20, 60, 300), wait=lambda s: False, clock=lambda: 0.0)

        def set_volume(v):
            calls.append(v)
            if len(calls) == 2:
                fade.cancel()
            return True

        fade._set_volume = set_volume
        fade.run()
        self.assertEqual(len(calls), 2)

    def test_real_thread_ends_promptly_on_cancel(self):
        calls = []
        fade = VolumeFade(lambda v: calls.append(v) or True, [(30, 40), (60, 60)]).start()
        self.assertTrue(fade.is_alive())
        fade.cancel()
        fade._thread.join(timeout=2)
        self.assertFalse(fade.is_alive())  # no quedan hilos huérfanos
        self.assertEqual(calls, [])


class SpotifyVolumeApiTest(unittest.TestCase):
    def setUp(self):
        guard = no_network()
        guard.start()
        self.addCleanup(guard.stop)

    def test_set_volume_request(self):
        transport = FakeTransport(resp(204))
        tokens = {"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12, "scope": ""}
        client = SpotifyClient("CID", "SECRET", "http://127.0.0.1:5000/spotify/callback",
                               MemoryTokenStore(tokens), transport=transport)
        client.set_volume(35, "dev")
        call = transport.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(call["url"], API_BASE + "/me/player/volume?volume_percent=35&device_id=dev")
        with self.assertRaises(ValueError):
            client.set_volume(101, "dev")


class SpotifyAlarmPlayerVolumeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(database)
        conn = sqlite3.connect(database)
        conn.execute("INSERT INTO settings (key, value) VALUES ('spotify_device_id', 'dev')")
        conn.commit()
        conn.close()
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.client.get_devices.return_value = [{"id": "dev", "name": "PC", "type": "Computer", "is_active": True}]
        self.player = SpotifyAlarmPlayer(self.client, database)

    def test_starts_at_initial_volume_before_playing(self):
        self.assertTrue(self.player.play(PLAYLIST_URI, volume=10))
        self.assertEqual(self.client.method_calls, [
            mock.call.get_devices(),
            mock.call.transfer_playback("dev", play=False),
            mock.call.set_volume(10, "dev"),
            mock.call.play("dev", uri=PLAYLIST_URI),
        ])

    def test_initial_volume_error_still_plays(self):
        self.client.set_volume.side_effect = SpotifyForbiddenError("VOLUME_CONTROL_DISALLOW", 403)
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player.play(PLAYLIST_URI, volume=10))
        self.client.play.assert_called_once_with("dev", uri=PLAYLIST_URI)

    def test_set_volume_uses_playing_device_and_never_raises(self):
        self.assertFalse(self.player.set_volume(30))  # aún no suena nada
        self.player.play(PLAYLIST_URI)
        self.assertTrue(self.player.set_volume(30))
        self.client.set_volume.assert_called_with(30, "dev")
        self.client.set_volume.side_effect = SpotifyForbiddenError("403", 403)
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player.set_volume(40))


class ManagerFadeTest(unittest.TestCase):
    def setUp(self):
        self.local = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyAlarmPlayer)
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = True
        self.spotify.set_volume.return_value = True
        self.fades = []  # [(plan, fade_mock)]
        self.jobs = []
        self.manager = AlarmPlaybackManager(
            self.local, self.spotify, fader=self.fake_fader,
            schedule_once=lambda run_at, cb: self.jobs.append(cb) or mock.Mock())

    def fake_fader(self, set_volume, plan):
        fade = mock.Mock()
        fade.set_volume = set_volume
        self.fades.append((plan, fade))
        return fade

    def test_spotify_starts_at_initial_volume_and_fades_to_target(self):
        self.assertEqual(self.manager.start(SPOTIFY), "spotify")
        self.spotify.play.assert_called_once_with(PLAYLIST_URI, volume=20)
        plan, fade = self.fades[0]
        self.assertEqual(plan, fade_plan(20, 60, 300))
        self.assertEqual(plan[-1][1], 60)
        self.assertEqual(fade.set_volume, self.spotify.set_volume)

    def test_without_fade_goes_straight_to_final_volume(self):
        self.manager.start({**SPOTIFY, "fade_minutes": 0})
        self.spotify.play.assert_called_once_with(PLAYLIST_URI, volume=60)
        self.assertEqual(self.fades, [])

    def test_local_alarm_keeps_current_behaviour(self):
        self.manager.start(LOCAL)
        self.local.play.assert_called_once_with()
        self.spotify.set_volume.assert_not_called()
        self.assertEqual(self.fades, [])

    def test_fallback_has_no_fade(self):
        self.spotify.play.return_value = False
        self.assertEqual(self.manager.start(SPOTIFY), "fallback")
        self.assertEqual(self.fades, [])

    def test_stop_cancels_fade_before_pausing(self):
        order = mock.Mock()
        self.manager.start(SPOTIFY)
        fade = self.fades[0][1]
        order.attach_mock(fade.cancel, "cancel")
        order.attach_mock(self.spotify.stop, "pause")
        self.manager.stop()
        self.assertEqual(order.mock_calls, [mock.call.cancel(), mock.call.pause()])
        self.manager.stop()  # idempotente
        fade.cancel.assert_called_once_with()

    def test_snooze_cancels_and_restarts_fade_from_initial_volume(self):
        self.manager.start(SPOTIFY)
        first = self.fades[0][1]
        self.manager.snooze()
        first.cancel.assert_called_once_with()
        self.jobs[-1]()  # pasan los 10 minutos
        self.assertEqual(len(self.fades), 2)
        self.assertEqual(self.fades[1][0], fade_plan(20, 60, 300))  # fade nuevo, desde el inicio
        self.assertEqual(self.spotify.play.call_args_list,
                         [mock.call(PLAYLIST_URI, volume=20)] * 2)

    def test_new_alarm_cancels_previous_fade(self):
        self.manager.start(SPOTIFY)
        first = self.fades[0][1]
        self.manager.start({**SPOTIFY, "id": 3, "volume_start": 10, "volume_end": 40})
        first.cancel.assert_called_once_with()
        self.assertEqual(self.fades[1][0], fade_plan(10, 40, 300))
        self.manager.start(LOCAL)  # una local también cancela el fade en curso
        self.fades[1][1].cancel.assert_called_once_with()

    def test_fader_crash_does_not_stop_alarm(self):
        self.manager.fader = mock.Mock(side_effect=RuntimeError("sin hilos"))
        with self.assertLogs("alarms", "ERROR"):
            self.assertEqual(self.manager.start(SPOTIFY), "spotify")
        self.assertEqual(self.manager.active.via, "spotify")

    def test_real_fade_volume_error_keeps_alarm_playing(self):
        """Fade real (síncrono): falla al tercer ajuste y la alarma sigue sonando."""
        results = iter([True, True, False])
        self.spotify.set_volume.side_effect = lambda v: next(results, False)
        runs = []

        def sync_fader(set_volume, plan):
            fade = VolumeFade(set_volume, plan, wait=lambda s: False, clock=lambda: 0.0)
            runs.append(fade)
            return fade

        self.manager.fader = sync_fader
        self.manager.start(SPOTIFY)
        with self.assertLogs("alarms", "WARNING"):
            runs[0].run()
        self.assertEqual(self.spotify.set_volume.call_count, 3)
        self.assertEqual(self.manager.active.via, "spotify")  # la alarma no se detiene
        self.spotify.stop.assert_not_called()
        self.assertEqual(runs[0].last_volume, 24)  # último volumen conseguido

    def test_real_fade_reaches_target_exactly(self):
        volumes = []
        self.spotify.set_volume.side_effect = lambda v: volumes.append(v) or True
        self.manager.fader = lambda sv, plan: VolumeFade(sv, plan, wait=lambda s: False,
                                                         clock=lambda: 0.0)
        self.manager.start({**SPOTIFY, "volume_start": 10, "volume_end": 60})
        self.manager._fade.run()
        self.assertEqual(volumes[-1], 60)
        self.assertEqual(volumes, sorted(volumes))
        self.assertGreater(len(volumes), 10)  # gradual, no un único salto

    def test_concurrent_stop_during_fade_start(self):
        self.manager.start(SPOTIFY)
        threads = [threading.Thread(target=self.manager.stop) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.fades[0][1].cancel.assert_called_once_with()


class VolumeFormTest(unittest.TestCase):
    def setUp(self):
        guard = no_network()
        guard.start()
        self.addCleanup(guard.stop)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.spotify = mock.Mock(spec=SpotifyClient)
        self.spotify.is_configured = True
        self.spotify.get_devices.return_value = [{"id": "dev", "name": "PC", "type": "Computer", "is_active": True}]
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "alarms.log"),
             "SPOTIFY_RETRY_DELAYS": (0, 0, 0, 0)},  # reintentos sin esperas reales
            player=mock.Mock(spec=AudioPlayer), spotify=self.spotify,
        )
        self.fades = []
        self.app.extensions["playback"].fader = (
            lambda sv, plan: self.fades.append(plan) or mock.Mock())
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
        data = {"name": "Mañana", "time": "07:30", "days": ["0"], "source": "spotify",
                "spotify_uri": PLAYLIST_URI}
        data.update(overrides)
        return self.client.post("/alarms/new", data=data)

    def test_defaults_when_not_sent(self):
        self.create()
        row = self.rows()[0]
        self.assertEqual((row["volume_start"], row["volume_end"], row["fade_minutes"]), (20, 60, 5))

    def test_custom_values_saved_and_shown(self):
        self.create(volume_start="10", volume_end="70", fade_minutes="10")
        row = self.rows()[0]
        self.assertEqual((row["volume_start"], row["volume_end"], row["fade_minutes"]), (10, 70, 10))
        self.assertIn("Volumen: 10 → 70 % · 10 min", self.client.get("/").get_data(as_text=True))

    def test_no_fade_display(self):
        self.create(volume_end="50", fade_minutes="0")
        self.assertIn("Volumen: 50 %", self.client.get("/").get_data(as_text=True))

    def test_local_alarm_does_not_show_volume(self):
        self.create(source="local")
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Volumen:", html)
        self.assertEqual(self.rows()[0]["volume_start"], 20)  # se guarda igualmente

    def test_invalid_values_rejected(self):
        for bad in ({"volume_start": "80", "volume_end": "40"}, {"volume_end": "150"},
                    {"volume_start": "-5"}, {"fade_minutes": "99"}, {"fade_minutes": "abc"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.create(**bad).status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_form_controls_and_edit_prefill(self):
        html = self.client.get("/alarms/new").get_data(as_text=True)
        self.assertIn('name="volume_start" type="range"', html)
        self.assertIn('name="volume_end" type="range"', html)
        self.assertIn('name="fade_minutes"', html)
        self.create(volume_start="15", volume_end="55", fade_minutes="3")
        alarm_id = self.rows()[0]["id"]
        html = self.client.get(f"/alarms/{alarm_id}/edit").get_data(as_text=True)
        self.assertIn('value="15"', html)
        self.assertIn('value="55"', html)
        self.assertIn('<option value="3" selected', html)
        self.client.post(f"/alarms/{alarm_id}/edit", data={
            "name": "Mañana", "time": "07:30", "days": ["0"], "source": "spotify",
            "spotify_uri": PLAYLIST_URI, "volume_start": "25", "volume_end": "65",
            "fade_minutes": "2"})
        row = self.rows()[0]
        self.assertEqual((row["volume_start"], row["volume_end"], row["fade_minutes"]), (25, 65, 2))
        self.assertEqual(row["time"], "07:30")

    def test_probar_uses_alarm_volume(self):
        self.client.post("/spotify/device", data={"device_id": "dev", "device_name": "Groove"})
        self.create(volume_start="10", volume_end="60", fade_minutes="5")
        self.client.post(f"/alarms/{self.rows()[0]['id']}/test")
        self.spotify.set_volume.assert_called_once_with(10, "dev")
        self.assertEqual(self.fades, [fade_plan(10, 60, 300)])

    def test_migrates_old_alarms_with_defaults(self):
        old = os.path.join(self.tmpdir.name, "old.db")
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " time TEXT NOT NULL, days TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL"
            " DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " last_triggered TEXT, source TEXT NOT NULL DEFAULT 'local', spotify_uri TEXT)")
        conn.execute("INSERT INTO alarms (name, time, days, source, spotify_uri)"
                     f" VALUES ('Vieja', '07:30', '0', 'spotify', '{PLAYLIST_URI}')")
        conn.commit()
        conn.close()
        db.init_db(old)
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM alarms").fetchone()
        conn.close()
        self.assertEqual((row["name"], row["source"], row["spotify_uri"]),
                         ("Vieja", "spotify", PLAYLIST_URI))
        self.assertEqual((row["volume_start"], row["volume_end"], row["fade_minutes"]), (20, 60, 5))


if __name__ == "__main__":
    unittest.main()
