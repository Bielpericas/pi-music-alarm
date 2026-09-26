"""Alarmas con fuente local o Spotify. Spotify está completamente simulado."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from spotify_client import (  # noqa: E402
    API_BASE,
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyForbiddenError,
    parse_spotify_uri,
)
from spotify_player import DEVICE_ID_KEY, SpotifyAlarmPlayer  # noqa: E402
from test_spotify_client import FakeTransport, MemoryTokenStore, resp  # noqa: E402

TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
PLAYLIST_URI = f"spotify:playlist:{PLAYLIST_ID}"
MONDAY_0730 = datetime(2026, 9, 28, 7, 30)


def no_network():
    return mock.patch("urllib.request.urlopen",
                      side_effect=AssertionError("los tests no deben usar la red"))


class ParseSpotifyUriTest(unittest.TestCase):
    def test_valid_inputs(self):
        cases = {
            f"spotify:track:{TRACK_ID}": f"spotify:track:{TRACK_ID}",
            f"https://open.spotify.com/track/{TRACK_ID}": f"spotify:track:{TRACK_ID}",
            f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=abc123&pt=x": PLAYLIST_URI,
            f"https://open.spotify.com/intl-es/album/{TRACK_ID}": f"spotify:album:{TRACK_ID}",
            f"  http://open.spotify.com/playlist/{PLAYLIST_ID}/  ": PLAYLIST_URI,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_spotify_uri(text), expected)

    def test_invalid_inputs(self):
        for text in ["", "hola", f"spotify:artist:{TRACK_ID}",
                     f"https://open.spotify.com/episode/{TRACK_ID}",
                     "spotify:track:corto", f"https://evil.com/track/{TRACK_ID}",
                     f"https://open.spotify.com/track/{TRACK_ID}extra"]:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_spotify_uri(text)


class ClientPlayUriTest(unittest.TestCase):
    def setUp(self):
        guard = no_network()
        guard.start()
        self.addCleanup(guard.stop)

    def make(self, *responses):
        self.transport = FakeTransport(*responses)
        tokens = {"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12, "scope": ""}
        return SpotifyClient("CID", "SECRET", "http://127.0.0.1:5000/spotify/callback",
                             MemoryTokenStore(tokens), transport=self.transport)

    def test_track_uses_uris(self):
        self.make(resp(204)).play("dev", uri=f"spotify:track:{TRACK_ID}")
        call = self.transport.calls[0]
        self.assertEqual(call["url"], API_BASE + "/me/player/play?device_id=dev")
        self.assertEqual(json.loads(call["body"]), {"uris": [f"spotify:track:{TRACK_ID}"]})

    def test_playlist_and_album_use_context_uri(self):
        for uri in (PLAYLIST_URI, f"spotify:album:{TRACK_ID}"):
            with self.subTest(uri=uri):
                self.make(resp(204)).play("dev", uri=uri)
                self.assertEqual(json.loads(self.transport.calls[0]["body"]),
                                 {"context_uri": uri})


class SpotifyAlarmPlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.database = os.path.join(self.tmpdir.name, "t.db")
        db.init_db(self.database)
        self.client = mock.Mock(spec=SpotifyClient)
        self.client.is_configured = True
        self.player = SpotifyAlarmPlayer(self.client, self.database)

    def select_device(self, device_id="dev"):
        conn = sqlite3.connect(self.database)
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (DEVICE_ID_KEY, device_id))
        conn.commit()
        conn.close()

    def test_transfers_then_plays_on_selected_device(self):
        self.select_device()
        self.assertTrue(self.player.play(PLAYLIST_URI))
        self.assertEqual(self.client.method_calls, [
            mock.call.transfer_playback("dev", play=False),
            mock.call.play("dev", uri=PLAYLIST_URI),
        ])

    def test_no_device_selected(self):
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player.play(PLAYLIST_URI))
        self.client.transfer_playback.assert_not_called()

    def test_not_configured(self):
        self.client.is_configured = False
        self.select_device()
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(self.player.play(PLAYLIST_URI))

    def test_spotify_errors_return_false(self):
        self.select_device()
        for exc in (SpotifyForbiddenError("403", 403), SpotifyConnectionError("sin red"),
                    RuntimeError("imprevisto")):
            with self.subTest(exc=exc):
                self.client.play.side_effect = exc
                with self.assertLogs("alarms", "WARNING"):
                    self.assertFalse(self.player.play(PLAYLIST_URI))


class AlarmSourceAppTest(unittest.TestCase):
    """Formulario, lista, scheduler y botón Probar con ambas fuentes."""

    def setUp(self):
        guard = no_network()
        guard.start()
        self.addCleanup(guard.stop)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.local = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyClient)
        self.spotify.is_configured = True
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "alarms.log")},
            player=self.local, spotify=self.spotify,
        )
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
        data = {"name": "Trabajo", "time": "07:30", "days": ["0"]}
        data.update(overrides)
        return self.client.post("/alarms/new", data=data)

    def create_spotify(self, uri=f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=x"):
        return self.create(name="Música", source="spotify", spotify_uri=uri)

    def select_device(self, device_id="dev"):
        self.client.post("/spotify/device", data={"device_id": device_id, "device_name": "PC"})

    def check(self):
        return scheduler.check_alarms(self.db_path, MONDAY_0730, player=self.local,
                                      spotify=self.app.extensions["spotify_alarm"])

    # --- Formulario y persistencia ---

    def test_default_source_is_local(self):
        self.create(spotify_uri="ignorado")
        row = self.rows()[0]
        self.assertEqual(row["source"], "local")
        self.assertIsNone(row["spotify_uri"])

    def test_create_spotify_alarm_stores_uri(self):
        self.assertEqual(self.create_spotify().status_code, 302)
        row = self.rows()[0]
        self.assertEqual((row["source"], row["spotify_uri"]), ("spotify", PLAYLIST_URI))

    def test_invalid_spotify_input_rejected(self):
        for bad in ({"source": "spotify", "spotify_uri": ""},
                    {"source": "spotify", "spotify_uri": "https://example.com"},
                    {"source": "youtube"}):
            with self.subTest(bad=bad):
                resp = self.create(**bad)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.rows(), [])
        # Si hay error, se conserva lo que escribió el usuario.
        html = self.create(source="spotify", spotify_uri="mal").get_data(as_text=True)
        self.assertIn('value="mal"', html)

    def test_list_shows_source(self):
        self.create()
        self.create_spotify()
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(">Local<", html)
        self.assertIn("Spotify · playlist", html)

    def test_form_has_source_selector(self):
        html = self.client.get("/alarms/new").get_data(as_text=True)
        self.assertIn('name="source" value="local"', html)
        self.assertIn('name="source" value="spotify"', html)
        self.assertIn('name="spotify_uri"', html)

    def test_edit_alarm_switches_source(self):
        self.create()
        alarm_id = self.rows()[0]["id"]
        html = self.client.get(f"/alarms/{alarm_id}/edit").get_data(as_text=True)
        self.assertIn("Editar alarma", html)
        self.assertIn('value="Trabajo"', html)
        resp = self.client.post(f"/alarms/{alarm_id}/edit", data={
            "name": "Trabajo", "time": "07:45", "days": ["0"],
            "source": "spotify", "spotify_uri": PLAYLIST_URI})
        self.assertEqual(resp.status_code, 302)
        row = self.rows()[0]
        self.assertEqual((row["time"], row["source"], row["spotify_uri"]),
                         ("07:45", "spotify", PLAYLIST_URI))
        # Y de vuelta a local: se borra la URI.
        self.client.post(f"/alarms/{alarm_id}/edit", data={
            "name": "Trabajo", "time": "07:45", "days": ["0"], "source": "local"})
        self.assertEqual((self.rows()[0]["source"], self.rows()[0]["spotify_uri"]),
                         ("local", None))

    def test_edit_missing_alarm_404(self):
        self.assertEqual(self.client.get("/alarms/999/edit").status_code, 404)

    def test_migrates_db_without_source_columns(self):
        scheduler.close_logging()
        old = os.path.join(self.tmpdir.name, "old.db")
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " time TEXT NOT NULL, days TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL"
            " DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " last_triggered TEXT)")
        conn.execute("INSERT INTO alarms (name, time, days) VALUES ('Vieja', '07:30', '0')")
        conn.commit()
        conn.close()
        db.init_db(old)
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM alarms").fetchone()
        conn.close()
        self.assertEqual((row["name"], row["source"], row["spotify_uri"]),
                         ("Vieja", "local", None))

    # --- Scheduler ---

    def test_scheduled_local_alarm_plays_wav_only(self):
        self.create()
        self.assertEqual(self.check(), ["Trabajo"])
        self.local.play.assert_called_once_with()
        self.spotify.transfer_playback.assert_not_called()
        self.spotify.play.assert_not_called()

    def test_scheduled_spotify_alarm_transfers_and_plays(self):
        self.select_device("dev")
        self.create_spotify()
        self.assertEqual(self.check(), ["Música"])
        self.assertEqual(self.spotify.method_calls, [
            mock.call.transfer_playback("dev", play=False),
            mock.call.play("dev", uri=PLAYLIST_URI),
        ])
        self.local.play.assert_not_called()

    def test_scheduled_spotify_failure_falls_back_to_wav(self):
        self.select_device("dev")
        self.create_spotify()
        self.spotify.play.side_effect = SpotifyForbiddenError("PREMIUM_REQUIRED", 403)
        with self.assertLogs("alarms", "WARNING") as logs:
            self.check()
        self.local.play.assert_called_once_with()
        self.assertTrue(any("respaldo" in line for line in logs.output))

    def test_spotify_without_selected_device_falls_back(self):
        self.create_spotify()
        self.check()
        self.spotify.play.assert_not_called()
        self.local.play.assert_called_once_with()

    def test_fire_alarm_without_spotify_player_falls_back(self):
        alarm = {"name": "X", "source": "spotify", "spotify_uri": PLAYLIST_URI}
        self.assertEqual(scheduler.fire_alarm(alarm, player=self.local), "fallback")
        self.local.play.assert_called_once_with()

    # --- Botón Probar ---

    def probar(self):
        alarm_id = self.rows()[-1]["id"]
        return self.client.post(f"/alarms/{alarm_id}/test",
                                follow_redirects=True).get_data(as_text=True)

    def test_probar_local(self):
        self.create()
        self.assertIn("sonando el WAV local", self.probar())
        self.local.play.assert_called_once_with()
        self.spotify.play.assert_not_called()

    def test_probar_spotify_runs_same_flow(self):
        self.select_device("dev")
        self.create_spotify()
        self.assertIn("reproduciendo en Spotify", self.probar())
        self.assertEqual(self.spotify.method_calls, [
            mock.call.transfer_playback("dev", play=False),
            mock.call.play("dev", uri=PLAYLIST_URI),
        ])
        self.local.play.assert_not_called()
        # Probar no cuenta como disparo programado.
        self.assertIsNone(self.rows()[0]["last_triggered"])

    def test_probar_spotify_failure_falls_back(self):
        self.select_device("dev")
        self.create_spotify()
        self.spotify.transfer_playback.side_effect = SpotifyConnectionError("sin red")
        html = self.probar()
        self.assertIn("Spotify falló", html)
        self.local.play.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
