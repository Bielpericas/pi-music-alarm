"""Estado "frío" de Groove tras reiniciar Raspotify (403 Restriction violated).

Groove aparece en /me/player/devices pero la primera orden puede fallar con
403 "Player command failed: Restriction violated" (reason UNKNOWN). Sin red ni
esperas reales: Spotify está simulado y los tiempos son inyectables.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import (  # noqa: E402
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    _api_error,
)
from spotify_player import (  # noqa: E402
    DEVICE_ID_KEY,
    DEVICE_NAME_KEY,
    SpotifyAlarmPlayer,
    is_cold_start_restriction,
)

PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
ALARM = {"id": 7, "name": "Despertador", "time": "07:30", "source": "spotify",
         "spotify_uri": PLAYLIST_URI, "volume_start": 20, "volume_end": 60, "fade_minutes": 5}


def restriction_violated():
    """El 403 exacto que devuelve Spotify con Groove "frío"."""
    body = {"error": {"status": 403, "message": "Player command failed: Restriction violated",
                      "reason": "UNKNOWN"}}
    return _api_error(403, json.dumps(body).encode())


def premium_required():
    body = {"error": {"status": 403, "message": "Player command failed: Premium required",
                      "reason": "PREMIUM_REQUIRED"}}
    return _api_error(403, json.dumps(body).encode())


class FakeSpotify:
    """Spotify Connect simulado con estado: transferir activa el dispositivo."""

    def __init__(self, *devices):
        self.devices = [dict(d) for d in devices]
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.client.get_devices.side_effect = self.get_devices
        self.client.transfer_playback.side_effect = self.transfer
        self.activate_on_transfer = True

    def get_devices(self):
        return [dict(d) for d in self.devices]

    def transfer(self, device_id, play=False):
        if self.activate_on_transfer:
            for d in self.devices:
                d["is_active"] = d["id"] == device_id

    def calls(self, name):
        return [c for c in self.client.method_calls if c[0] == name]


def groove(device_id="groove-1", active=False, restricted=False):
    return {"id": device_id, "name": "Groove", "type": "Speaker",
            "is_active": active, "is_restricted": restricted}


TABLET = {"id": "tablet-1", "name": "Tablet", "type": "Tablet", "is_active": True,
          "is_restricted": False}


class ClassificationTest(unittest.TestCase):
    def test_restriction_violated_unknown_is_transient(self):
        exc = restriction_violated()
        self.assertIsInstance(exc, SpotifyForbiddenError)
        self.assertEqual((exc.reason, exc.api_message),
                         ("UNKNOWN", "Player command failed: Restriction violated"))
        self.assertTrue(is_cold_start_restriction(exc))

    def test_other_403_are_not_transient(self):
        self.assertFalse(is_cold_start_restriction(premium_required()))
        registered = _api_error(403, json.dumps(
            {"error": {"status": 403, "message": "User not registered in the Developer "
                       "Dashboard"}}).encode())
        self.assertFalse(is_cold_start_restriction(registered))
        other_reason = _api_error(403, json.dumps(
            {"error": {"status": 403, "message": "Player command failed: Restriction violated",
                       "reason": "VOLUME_CONTROL_DISALLOW"}}).encode())
        self.assertFalse(is_cold_start_restriction(other_reason))
        self.assertFalse(is_cold_start_restriction(SpotifyNotFoundError("x", 404)))


class ColdStartTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        db.write_setting(self.database, DEVICE_ID_KEY, "groove-1")
        db.write_setting(self.database, DEVICE_NAME_KEY, "Groove")
        self.waits = []

    def player(self, fake, **kwargs):
        kwargs.setdefault("wait", lambda s: self.waits.append(s) or False)
        return SpotifyAlarmPlayer(fake.client, self.database, **kwargs)

    def test_403_on_play_then_works(self):
        fake = FakeSpotify(TABLET, groove())
        fake.client.play.side_effect = [restriction_violated(), None]
        player = self.player(fake)
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertTrue(player.play(PLAYLIST_URI, volume=20))
        self.assertEqual(len(fake.calls("play")), 2)
        self.assertEqual(self.waits, [2])  # backoff corto antes del segundo intento
        self.assertTrue(any("Restriction violated" in line for line in logs.output))
        # El reintento vuelve a resolver el dispositivo y repite el orden completo.
        names = [c[0] for c in fake.client.method_calls]
        self.assertEqual(names, [
            "get_devices", "transfer_playback", "get_devices", "set_volume", "play",
            "get_devices", "transfer_playback", "set_volume", "play",
        ])
        fake.client.play.assert_called_with("groove-1", uri=PLAYLIST_URI)

    def test_403_on_transfer_then_works(self):
        fake = FakeSpotify(groove())
        fake.client.transfer_playback.side_effect = [restriction_violated(), None]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual(len(fake.calls("transfer_playback")), 2)
        self.assertEqual(self.waits[0], 2)

    def test_persistent_403_ends_in_failure(self):
        fake = FakeSpotify(groove())
        fake.client.play.side_effect = restriction_violated()
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertFalse(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual(len(fake.calls("play")), 4)
        self.assertEqual([w for w in self.waits if w >= 2], [2, 4, 6])
        self.assertTrue(any("tras 4 intentos" in line for line in logs.output))

    def test_other_403_is_not_retried(self):
        fake = FakeSpotify(groove())
        fake.client.play.side_effect = premium_required()
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual(len(fake.calls("play")), 1)
        self.assertEqual([w for w in self.waits if w >= 2], [])

    def test_waits_until_active_before_playing(self):
        fake = FakeSpotify(TABLET, groove())
        fake.activate_on_transfer = False
        polls = iter([groove(), groove(active=True)])  # se activa en la 2.ª comprobación
        first_list = [TABLET, groove()]
        fake.client.get_devices.side_effect = [first_list, [TABLET, next(polls)],
                                               [TABLET, next(polls)]]
        player = self.player(fake)
        self.assertTrue(player.play(PLAYLIST_URI, volume=20))
        self.assertEqual(self.waits, [1])  # una espera corta hasta verlo activo
        names = [c[0] for c in fake.client.method_calls]
        self.assertEqual(names, ["get_devices", "transfer_playback", "get_devices",
                                 "get_devices", "set_volume", "play"])

    def test_restricted_device_is_not_ready(self):
        fake = FakeSpotify(groove())
        fake.activate_on_transfer = False
        fake.client.get_devices.side_effect = [
            [groove()], [groove(active=True, restricted=True)], [groove(active=True)]]
        self.assertTrue(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual(self.waits, [1])

    def test_plays_anyway_if_never_reported_active(self):
        fake = FakeSpotify(groove())
        fake.activate_on_transfer = False  # nunca figura como activo
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertTrue(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual(self.waits, [1, 1])  # comprobaciones acotadas
        self.assertEqual(len(fake.calls("play")), 1)
        self.assertTrue(any("se intenta reproducir igualmente" in line for line in logs.output))

    def test_already_active_skips_readiness_check(self):
        fake = FakeSpotify(groove(active=True))
        self.assertTrue(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual([c[0] for c in fake.client.method_calls],
                         ["get_devices", "transfer_playback", "play"])

    def test_device_vanishing_after_transfer_is_resolved_again(self):
        fake = FakeSpotify(groove())
        new_id = groove("groove-2")
        fake.activate_on_transfer = False
        fake.client.get_devices.side_effect = [
            [groove()],            # intento 1: resolver
            [],                    # comprobación tras transferir: ha desaparecido
            [new_id],              # intento 2: vuelve a resolver (Raspotify con ID nuevo)
            [{**new_id, "is_active": True}],
        ]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player(fake).play(PLAYLIST_URI))
        fake.client.play.assert_called_once_with("groove-2", uri=PLAYLIST_URI)
        self.assertEqual(db.read_setting(self.database, DEVICE_ID_KEY), "groove-2")

    def test_network_and_404_still_retried(self):
        fake = FakeSpotify(groove())
        fake.client.play.side_effect = [SpotifyConnectionError("sin red"),
                                        SpotifyNotFoundError("NO_ACTIVE_DEVICE", 404), None]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player(fake).play(PLAYLIST_URI))
        self.assertEqual(len(fake.calls("play")), 3)


class ColdStartManagerTest(unittest.TestCase):
    """Con AlarmPlaybackManager: fallback, STOP y Snooze durante las esperas."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        db.write_setting(self.database, DEVICE_ID_KEY, "groove-1")
        db.write_setting(self.database, DEVICE_NAME_KEY, "Groove")
        self.local = mock.Mock(spec=AudioPlayer)
        self.jobs = []

    def manager(self, fake, **kwargs):
        spotify = SpotifyAlarmPlayer(fake.client, self.database, **kwargs)
        return AlarmPlaybackManager(
            self.local, spotify,
            schedule_once=lambda run_at, cb: self.jobs.append(cb) or mock.Mock(),
            fader=lambda sv, plan: mock.Mock())

    def test_403_then_works_plays_on_groove_without_fallback(self):
        fake = FakeSpotify(groove())
        fake.client.play.side_effect = [restriction_violated(), None]
        manager = self.manager(fake, wait=lambda s: False)
        with self.assertLogs("alarms", "WARNING"):
            self.assertEqual(manager.start(ALARM), "spotify")
        self.local.play.assert_not_called()

    def test_persistent_403_falls_back_to_wav(self):
        fake = FakeSpotify(groove())
        fake.client.play.side_effect = restriction_violated()
        manager = self.manager(fake, wait=lambda s: False)
        with self.assertLogs("alarms", "WARNING"):
            self.assertEqual(manager.start(ALARM), "fallback")
        self.local.play.assert_called_once_with()

    def start_blocked_in_backoff(self, manager, fake):
        """Arranca la alarma en un hilo y espera a que quede en el backoff tras el 403."""
        failed = threading.Event()

        def play(*args, **kwargs):
            failed.set()
            raise restriction_violated()

        fake.client.play.side_effect = play
        result = {}
        thread = threading.Thread(target=lambda: result.update(via=manager.start(ALARM)))
        thread.start()
        self.assertTrue(failed.wait(5))
        return thread, result

    def test_stop_during_backoff_cancels_without_fallback(self):
        fake = FakeSpotify(groove())
        # Esperas reales largas (Event): STOP tiene que cortarlas al momento.
        manager = self.manager(fake, retry_delays=(0, 30, 30, 30))
        thread, result = self.start_blocked_in_backoff(manager, fake)
        stopped = manager.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["via"], "cancelled")
        self.assertIsNotNone(stopped)
        self.assertIsNone(manager.active)
        self.local.play.assert_not_called()  # sin fallback local
        self.assertEqual(len(fake.calls("play")), 1)  # no hubo más intentos

    def test_snooze_during_backoff_cancels_without_fallback(self):
        fake = FakeSpotify(groove())
        manager = self.manager(fake, retry_delays=(0, 30, 30, 30))
        thread, result = self.start_blocked_in_backoff(manager, fake)
        pending = manager.snooze()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["via"], "cancelled")
        self.assertEqual(pending.alarm["id"], 7)
        self.local.play.assert_not_called()
        self.assertEqual(len(fake.calls("play")), 1)

    def test_stop_during_readiness_wait(self):
        fake = FakeSpotify(groove())
        fake.activate_on_transfer = False  # se queda esperando a que figure activo
        polled = threading.Event()
        devices = fake.client.get_devices.side_effect

        def get_devices():
            if fake.calls("transfer_playback"):
                polled.set()
            return devices()

        fake.client.get_devices.side_effect = get_devices
        manager = self.manager(fake, ready_delays=(0, 30, 30))
        result = {}
        thread = threading.Thread(target=lambda: result.update(via=manager.start(ALARM)))
        thread.start()
        self.assertTrue(polled.wait(5))
        manager.stop()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["via"], "cancelled")
        fake.client.play.assert_not_called()
        self.local.play.assert_not_called()


if __name__ == "__main__":
    unittest.main()
