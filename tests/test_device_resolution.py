"""Resolución resiliente del dispositivo Spotify (Groove). Sin esperas reales ni red."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from playback import ActiveAlarm, AlarmPlaybackManager  # noqa: E402
from spotify_client import (  # noqa: E402
    SpotifyAuthError,
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
)
from spotify_player import (  # noqa: E402
    DEVICE_ID_KEY,
    DEVICE_NAME_KEY,
    DeviceConflict,
    DeviceUnavailable,
    SpotifyAlarmPlayer,
    choose_device,
)

PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
ALARM = {"id": 7, "name": "Despertador", "time": "07:30", "source": "spotify",
         "spotify_uri": PLAYLIST_URI, "volume_start": 20, "volume_end": 60, "fade_minutes": 5}


def device(device_id, name, active=False):
    return {"id": device_id, "name": name, "type": "Speaker", "is_active": active,
            "is_restricted": False}


TABLET = device("tablet-1", "Tablet salón", active=True)
GROOVE = device("groove-1", "Groove")
GROOVE_NEW = device("groove-2", "Groove")  # mismo Groove tras reiniciar Raspotify


class ChooseDeviceTest(unittest.TestCase):
    def test_saved_id_wins(self):
        self.assertEqual(choose_device([TABLET, GROOVE], "groove-1", "Groove"), GROOVE)

    def test_by_name_case_insensitive(self):
        renamed = device("groove-9", "  gROOVE ")
        self.assertEqual(choose_device([TABLET, renamed], "gone", "Groove"), renamed)

    def test_never_picks_the_active_device_by_default(self):
        with self.assertRaises(DeviceUnavailable):
            choose_device([TABLET], "gone", "Groove")

    def test_duplicates(self):
        # Si uno de los dos es el guardado, se usa ese.
        self.assertEqual(choose_device([GROOVE_NEW, GROOVE], "groove-1", "Groove"), GROOVE)
        # Si ninguno lo es, no se elige al azar.
        with self.assertRaises(DeviceConflict):
            choose_device([GROOVE_NEW, GROOVE], "gone", "Groove")

    def test_unknown_name_and_devices_without_id(self):
        with self.assertRaises(DeviceUnavailable):
            choose_device([GROOVE], "gone", "")
        with self.assertRaises(DeviceUnavailable):
            choose_device([{"id": None, "name": "Groove"}], "gone", "Groove")


class ResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.waits = []
        self.player = SpotifyAlarmPlayer(self.client, self.database,
                                         wait=lambda s: self.waits.append(s) or False,
                                         ready_delays=())  # la activación: test_cold_start

    def save(self, device_id=None, name=None):
        if device_id is not None:
            db.write_setting(self.database, DEVICE_ID_KEY, device_id)
        if name is not None:
            db.write_setting(self.database, DEVICE_NAME_KEY, name)

    def setting(self, key):
        return db.read_setting(self.database, key)

    def test_saved_id_still_valid(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.return_value = [TABLET, GROOVE]
        self.assertTrue(self.player.play(PLAYLIST_URI, volume=20))
        self.assertEqual(self.client.method_calls, [
            mock.call.get_track_count(PLAYLIST_URI),  # nº de pistas (inicio aleatorio); el mock no da un número: sin offset
            mock.call.get_devices(),
            mock.call.transfer_playback("groove-1", play=False),
            mock.call.set_volume(20, "groove-1"),
            mock.call.play("groove-1", uri=PLAYLIST_URI),
        ])
        self.assertEqual(self.waits, [])

    def test_saved_id_gone_found_by_name_and_saved(self):
        self.save("groove-old", "Groove")
        self.client.get_devices.return_value = [TABLET, GROOVE_NEW]
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertTrue(self.player.play(PLAYLIST_URI))
        self.client.transfer_playback.assert_called_once_with("groove-2", play=False)
        self.assertEqual(self.setting(DEVICE_ID_KEY), "groove-2")  # ID actualizado
        self.assertTrue(any("ID actualizado" in line for line in logs.output))

    def test_other_device_was_playing_before_the_alarm(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.return_value = [TABLET, GROOVE]  # la tablet está activa
        self.player.play(PLAYLIST_URI, volume=20)
        self.client.transfer_playback.assert_called_once_with("groove-1", play=False)
        self.client.play.assert_called_once_with("groove-1", uri=PLAYLIST_URI)
        self.player.stop()
        self.client.pause.assert_called_once_with("groove-1")  # nunca se toca la tablet

    def test_raspotify_changes_device_id_between_alarms(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.return_value = [GROOVE]
        self.player.play(PLAYLIST_URI)
        self.client.get_devices.return_value = [GROOVE_NEW]  # Raspotify reiniciado
        self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.client.transfer_playback.call_args_list[-1],
                         mock.call("groove-2", play=False))
        self.assertEqual(self.setting(DEVICE_ID_KEY), "groove-2")
        self.player.set_volume(30)
        self.client.set_volume.assert_called_with(30, "groove-2")

    def test_groove_appears_on_third_attempt(self):
        self.save("groove-old", "Groove")
        self.client.get_devices.side_effect = [[TABLET], [TABLET], [TABLET, GROOVE_NEW]]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.waits, [2, 4])  # inmediato, +2 s, +4 s
        self.client.transfer_playback.assert_called_once_with("groove-2", play=False)

    def test_groove_appears_on_second_attempt(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.side_effect = [[], [GROOVE]]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.waits, [2])

    def test_groove_never_appears(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.return_value = [TABLET]
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertFalse(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.client.get_devices.call_count, 4)
        self.assertEqual(self.waits, [2, 4, 6])  # 12 s como máximo, no más
        self.client.transfer_playback.assert_not_called()
        self.assertTrue(any("tras 4 intentos" in line for line in logs.output))

    def test_transient_errors_are_retried(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.side_effect = [SpotifyConnectionError("sin red"), [GROOVE],
                                               [GROOVE]]
        self.client.transfer_playback.side_effect = [SpotifyNotFoundError("aún no listo", 404),
                                                     None]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.waits, [2, 4])
        self.assertEqual(self.client.transfer_playback.call_count, 2)

    def test_non_recoverable_errors_are_not_retried(self):
        self.save("groove-1", "Groove")
        for exc in (SpotifyAuthError("token revocado", 401),
                    SpotifyForbiddenError("PREMIUM_REQUIRED", 403),
                    SpotifyRateLimitError("espera", 120)):
            with self.subTest(exc=type(exc).__name__):
                self.client.reset_mock()
                self.waits.clear()
                self.client.get_devices.side_effect = exc
                with self.assertLogs("alarms", "WARNING"):
                    self.assertFalse(self.player.play(PLAYLIST_URI))
                self.assertEqual(self.client.get_devices.call_count, 1)
                self.assertEqual(self.waits, [])

    def test_forbidden_transfer_is_not_retried(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.return_value = [GROOVE]
        self.client.transfer_playback.side_effect = SpotifyForbiddenError("restringido", 403)
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.client.transfer_playback.call_count, 1)

    def test_two_devices_named_groove(self):
        self.save("groove-1", "Groove")
        self.client.get_devices.return_value = [GROOVE_NEW, GROOVE]
        self.assertTrue(self.player.play(PLAYLIST_URI))  # uno es el guardado: se usa
        self.client.transfer_playback.assert_called_once_with("groove-1", play=False)

        self.client.reset_mock()
        self.save("groove-gone")
        self.client.get_devices.return_value = [GROOVE_NEW, device("groove-3", "groove")]
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertFalse(self.player.play(PLAYLIST_URI))  # no se puede decidir
        self.client.transfer_playback.assert_not_called()
        self.assertEqual(self.client.get_devices.call_count, 1)  # sin reintentos
        self.assertTrue(any("dispositivos llamados" in line for line in logs.output))
        self.assertEqual(self.setting(DEVICE_ID_KEY), "groove-gone")  # no se cambia

    def test_old_install_without_name_self_heals(self):
        self.save("groove-1")  # instalación antigua: solo había ID
        self.client.get_devices.return_value = [GROOVE]
        self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.setting(DEVICE_NAME_KEY), "Groove")

    def test_preferred_name_from_config_when_nothing_saved(self):
        self.player.preferred_name = "Groove"
        self.client.get_devices.return_value = [TABLET, GROOVE]
        self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.setting(DEVICE_ID_KEY), "groove-1")
        self.assertEqual(self.setting(DEVICE_NAME_KEY), "Groove")

    def test_nothing_selected(self):
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player.play(PLAYLIST_URI))
        self.client.get_devices.assert_not_called()


class ManagerResolutionTest(unittest.TestCase):
    """Integración con AlarmPlaybackManager: fallback, snooze y STOP."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        db.write_setting(self.database, DEVICE_ID_KEY, "groove-1")
        db.write_setting(self.database, DEVICE_NAME_KEY, "Groove")
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.local = mock.Mock(spec=AudioPlayer)
        self.jobs = []
        self.fades = []

    def make(self, retry_delays=(0, 2, 4, 6), wait=None):
        self.spotify = SpotifyAlarmPlayer(
            self.client, self.database, retry_delays=retry_delays,
            wait=wait or (lambda s: False), ready_delays=())
        return AlarmPlaybackManager(
            self.local, self.spotify,
            schedule_once=lambda run_at, cb: self.jobs.append(cb) or mock.Mock(),
            fader=lambda sv, plan: self.fades.append(plan) or mock.Mock())

    def test_full_order_resolve_transfer_volume_play_fade(self):
        manager = self.make()
        self.client.get_devices.return_value = [TABLET, GROOVE_NEW]
        self.assertEqual(manager.start(ALARM), "spotify")
        self.assertEqual(self.client.method_calls, [
            mock.call.get_track_count(PLAYLIST_URI),  # nº de pistas (inicio aleatorio); el mock no da un número: sin offset
            mock.call.get_devices(),
            mock.call.transfer_playback("groove-2", play=False),
            mock.call.set_volume(20, "groove-2"),
            mock.call.play("groove-2", uri=PLAYLIST_URI),
        ])
        self.assertEqual(len(self.fades), 1)  # el fade empieza después
        self.local.play.assert_not_called()

    def test_never_found_falls_back_to_wav(self):
        manager = self.make()
        self.client.get_devices.return_value = [TABLET]
        with self.assertLogs("alarms", "WARNING"):
            self.assertEqual(manager.start(ALARM), "fallback")
        self.local.play.assert_called_once_with()
        self.assertEqual(self.fades, [])

    def test_conflict_falls_back_to_wav(self):
        manager = self.make()
        db.write_setting(self.database, DEVICE_ID_KEY, "gone")
        self.client.get_devices.return_value = [GROOVE, GROOVE_NEW]
        with self.assertLogs("alarms", "WARNING"):
            self.assertEqual(manager.start(ALARM), "fallback")
        self.local.play.assert_called_once_with()

    def test_snooze_resolves_the_device_again(self):
        manager = self.make()
        self.client.get_devices.return_value = [GROOVE]
        manager.start(ALARM)
        manager.snooze()
        self.client.get_devices.return_value = [GROOVE_NEW]  # Groove reinició durante el snooze
        self.jobs[-1]()  # pasan los 10 minutos
        self.assertEqual(self.client.get_devices.call_count, 2)
        self.assertEqual(self.client.transfer_playback.call_args_list[-1],
                         mock.call("groove-2", play=False))
        self.assertEqual(manager.active.via, "spotify")

    def run_start_in_thread(self, manager):
        first_attempt = threading.Event()

        def get_devices():
            first_attempt.set()
            return [TABLET]  # Groove no aparece: quedará esperando para reintentar

        self.client.get_devices.side_effect = get_devices
        result = {}
        thread = threading.Thread(target=lambda: result.update(via=manager.start(ALARM)))
        thread.start()
        self.assertTrue(first_attempt.wait(5))
        return thread, result

    def test_stop_during_retries(self):
        # Esperas largas y reales (Event): STOP debe cortarlas al momento.
        manager = self.make(retry_delays=(0, 30, 30, 30), wait=None)
        manager.spotify._wait = manager.spotify._interrupted.wait
        thread, result = self.run_start_in_thread(manager)
        self.assertEqual(manager.active.via, "connecting")  # STOP visible mientras busca
        stopped = manager.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["via"], "cancelled")
        self.assertIsNotNone(stopped)
        self.assertIsNone(manager.active)
        self.local.play.assert_not_called()  # no suena el WAV de respaldo
        self.client.transfer_playback.assert_not_called()
        self.assertEqual(self.client.get_devices.call_count, 1)

    def test_snooze_during_retries(self):
        manager = self.make(retry_delays=(0, 30, 30, 30), wait=None)
        manager.spotify._wait = manager.spotify._interrupted.wait
        thread, result = self.run_start_in_thread(manager)
        pending = manager.snooze()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["via"], "cancelled")
        self.assertEqual(pending.alarm["id"], 7)
        self.local.play.assert_not_called()


class ConnectingBannerTest(unittest.TestCase):
    def test_connecting_state_shows_stop(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        app = create_app({"TESTING": True, "SECRET_KEY": "t",
                          "DATABASE": os.path.join(tmpdir.name, "t.db"),
                          "ALARM_LOG": os.path.join(tmpdir.name, "a.log")},
                         player=mock.Mock(spec=AudioPlayer),
                         spotify=mock.Mock(spec=SpotifyClient))
        self.addCleanup(scheduler.close_logging)
        app.extensions["playback"]._active = ActiveAlarm(ALARM, datetime(2026, 9, 28, 7, 30),
                                                        "connecting")
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("Conectando con Spotify", html)
        self.assertIn(">STOP<", html)

    def test_select_saves_id_and_name(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        database = os.path.join(tmpdir.name, "t.db")
        app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE": database,
                          "ALARM_LOG": os.path.join(tmpdir.name, "a.log")},
                         player=mock.Mock(spec=AudioPlayer),
                         spotify=mock.Mock(spec=SpotifyClient))
        self.addCleanup(scheduler.close_logging)
        app.test_client().post("/spotify/device",
                               data={"device_id": "groove-1", "device_name": "Groove"})
        self.assertEqual(db.read_setting(database, DEVICE_ID_KEY), "groove-1")
        self.assertEqual(db.read_setting(database, DEVICE_NAME_KEY), "Groove")


if __name__ == "__main__":
    unittest.main()
