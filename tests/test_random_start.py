"""Inicio aleatorio en álbumes y playlists (offset.position). Sin red ni esperas."""
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import (  # noqa: E402
    API_BASE,
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    _api_error,
)
from spotify_player import DEVICE_ID_KEY, DEVICE_NAME_KEY, SpotifyAlarmPlayer  # noqa: E402
from test_spotify_client import FakeTransport, MemoryTokenStore, resp  # noqa: E402

PLAYLIST = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
ALBUM = "spotify:album:4uLU6hMCjMI75M1A2tKUQC"
TRACK = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
GROOVE = {"id": "groove-1", "name": "Groove", "type": "Speaker", "is_active": True,
          "is_restricted": False}


def spotify_alarm(uri):
    return {"id": 3, "name": "Despertador", "time": "07:30", "source": "spotify",
            "spotify_uri": uri, "volume_start": 20, "volume_end": 60, "fade_minutes": 5}


class FixedRandom:
    """randrange(n) devuelve los valores dados en orden y registra cada n."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def randrange(self, n):
        self.calls.append(n)
        value = self.values.pop(0)
        assert 0 <= value < n
        return value


class ClientTest(unittest.TestCase):
    def setUp(self):
        guard = mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("los tests no deben usar la red"))
        guard.start()
        self.addCleanup(guard.stop)

    def make(self, *responses):
        self.transport = FakeTransport(*responses)
        tokens = {"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12, "scope": ""}
        return SpotifyClient("CID", "SECRET", "http://127.0.0.1:5000/spotify/callback",
                             MemoryTokenStore(tokens), transport=self.transport)

    def test_playlist_track_count(self):
        client = self.make(resp(200, {"items": {"total": 100}}))
        self.assertEqual(client.get_track_count(PLAYLIST), 100)
        url = urllib.parse.urlparse(self.transport.calls[0]["url"])
        self.assertEqual(url.path, "/v1/playlists/37i9dQZF1DXcBWIGoYBM5M")
        self.assertEqual(urllib.parse.parse_qs(url.query), {"fields": ["items.total"]})

    def test_playlist_track_count_legacy_field(self):
        client = self.make(resp(200, {"tracks": {"total": 12}}))
        self.assertEqual(client.get_track_count(PLAYLIST), 12)

    def test_playlist_without_total(self):
        # Playlists ajenas: Spotify no da su contenido.
        client = self.make(resp(200, {}))
        self.assertIsNone(client.get_track_count(PLAYLIST))

    def test_album_track_count(self):
        client = self.make(resp(200, {"total_tracks": 14, "tracks": {"total": 14}}))
        self.assertEqual(client.get_track_count(ALBUM), 14)
        self.assertEqual(self.transport.calls[0]["url"], API_BASE + "/albums/4uLU6hMCjMI75M1A2tKUQC")

    def test_track_needs_no_request(self):
        client = self.make()
        self.assertIsNone(client.get_track_count(TRACK))
        self.assertEqual(self.transport.calls, [])

    def test_play_with_offset_position(self):
        client = self.make(resp(204), resp(204))
        client.play("dev", uri=PLAYLIST, offset=41)
        client.play("dev", uri=TRACK, offset=41)  # una canción ignora el offset
        playlist_body, track_body = (json.loads(c["body"]) for c in self.transport.calls)
        self.assertEqual(playlist_body, {"context_uri": PLAYLIST, "offset": {"position": 41}})
        self.assertEqual(track_body, {"uris": [TRACK]})


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        db.write_setting(self.database, DEVICE_ID_KEY, "groove-1")
        db.write_setting(self.database, DEVICE_NAME_KEY, "Groove")
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.client.get_devices.return_value = [GROOVE]

    def player(self, rng):
        return SpotifyAlarmPlayer(self.client, self.database, rng=rng,
                                  wait=lambda s: False, ready_delays=())

    def test_playlist_starts_at_random_position(self):
        self.client.get_track_count.return_value = 100
        rng = FixedRandom(42)
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertTrue(self.player(rng).play(PLAYLIST, volume=20))
        self.assertEqual(rng.calls, [100])
        # Directamente en la pista elegida: una sola orden de reproducción, sin saltos.
        self.client.play.assert_called_once_with("groove-1", uri=PLAYLIST, offset=42)
        self.assertEqual([c[0] for c in self.client.method_calls],
                         ["get_track_count", "get_devices", "transfer_playback",
                          "set_volume", "play"])
        self.assertTrue(any("pista 43 de 100" in line for line in logs.output))

    def test_album_starts_at_random_position(self):
        self.client.get_track_count.return_value = 14
        rng = FixedRandom(13)
        self.assertTrue(self.player(rng).play(ALBUM))
        self.assertEqual(rng.calls, [14])
        self.client.play.assert_called_once_with("groove-1", uri=ALBUM, offset=13)

    def test_track_is_unchanged(self):
        rng = FixedRandom()
        self.assertTrue(self.player(rng).play(TRACK))
        self.client.get_track_count.assert_not_called()
        self.assertEqual(rng.calls, [])
        self.client.play.assert_called_once_with("groove-1", uri=TRACK)

    def test_metadata_failure_falls_back_to_first_track(self):
        failures = [SpotifyForbiddenError("sin acceso", 403), SpotifyNotFoundError("ajena", 404),
                    SpotifyConnectionError("sin red"), None, 0]
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                self.client.reset_mock()
                self.client.get_devices.return_value = [GROOVE]
                if isinstance(failure, Exception):
                    self.client.get_track_count.side_effect = failure
                else:
                    self.client.get_track_count.side_effect = None
                    self.client.get_track_count.return_value = failure
                rng = FixedRandom()
                with self.assertLogs("alarms", "INFO"):
                    self.assertTrue(self.player(rng).play(PLAYLIST))  # la alarma no falla
                self.assertEqual(rng.calls, [])
                self.client.play.assert_called_once_with("groove-1", uri=PLAYLIST)

    def test_rejected_position_plays_from_the_start(self):
        self.client.get_track_count.return_value = 50
        self.client.play.side_effect = [SpotifyError("Invalid offset", 400), None]
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player(FixedRandom(7)).play(PLAYLIST))
        self.assertEqual(self.client.play.call_args_list, [
            mock.call("groove-1", uri=PLAYLIST, offset=7),
            mock.call("groove-1", uri=PLAYLIST),
        ])

    def test_cold_start_retry_keeps_the_same_position(self):
        self.client.get_track_count.return_value = 30
        restriction = _api_error(403, json.dumps({"error": {
            "status": 403, "message": "Player command failed: Restriction violated",
            "reason": "UNKNOWN"}}).encode())
        self.client.play.side_effect = [restriction, None]
        rng = FixedRandom(9)
        with self.assertLogs("alarms", "WARNING"):
            self.assertTrue(self.player(rng).play(PLAYLIST))
        self.assertEqual(rng.calls, [30])  # se elige una sola vez
        self.assertEqual(self.client.get_track_count.call_count, 1)
        self.assertEqual(self.client.play.call_args_list,
                         [mock.call("groove-1", uri=PLAYLIST, offset=9)] * 2)

    def test_default_random_stays_in_range(self):
        self.client.get_track_count.return_value = 5
        player = SpotifyAlarmPlayer(self.client, self.database, wait=lambda s: False,
                                    ready_delays=())
        seen = set()
        for _ in range(60):
            player.play(PLAYLIST)
            seen.add(self.client.play.call_args.kwargs["offset"])
        self.assertTrue(seen <= set(range(5)))
        self.assertGreater(len(seen), 1)  # no siempre la misma pista


class SnoozeTest(unittest.TestCase):
    def test_snooze_picks_a_new_random_track(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        database = os.path.join(tmpdir.name, "t.db")
        db.init_db(database)
        db.write_setting(database, DEVICE_ID_KEY, "groove-1")
        db.write_setting(database, DEVICE_NAME_KEY, "Groove")
        client = mock.Mock(spec=SpotifyClient)
        client.is_configured = True
        client.get_devices.return_value = [GROOVE]
        client.get_track_count.return_value = 100
        rng = FixedRandom(3, 57)
        jobs = []
        manager = AlarmPlaybackManager(
            mock.Mock(spec=AudioPlayer),
            SpotifyAlarmPlayer(client, database, rng=rng, wait=lambda s: False, ready_delays=()),
            schedule_once=lambda run_at, cb: jobs.append(cb) or mock.Mock(),
            fader=lambda sv, plan: mock.Mock())

        self.assertEqual(manager.start(spotify_alarm(PLAYLIST)), "spotify")
        manager.snooze()
        jobs[-1]()  # pasan los 10 minutos
        self.assertEqual(manager.active.via, "spotify")
        self.assertEqual(rng.calls, [100, 100])  # se vuelve a elegir
        self.assertEqual(client.get_track_count.call_count, 2)
        self.assertEqual([c.kwargs["offset"] for c in client.play.call_args_list], [3, 57])


if __name__ == "__main__":
    unittest.main()
