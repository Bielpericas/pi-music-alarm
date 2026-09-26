"""Tests de la sección /spotify con un SpotifyClient simulado (mock)."""
import os
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from spotify_client import (  # noqa: E402
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
)

DEVICES = [
    {"id": "pc", "name": "Mi PC", "type": "Computer", "is_active": True, "is_restricted": False},
    {"id": "movil", "name": "Mi móvil", "type": "Smartphone", "is_active": False,
     "is_restricted": False},
]


class SpotifyViewsTest(unittest.TestCase):
    def setUp(self):
        # Red de seguridad: cualquier acceso real a la red hace fallar el test.
        guard = mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("los tests no deben usar la red"))
        guard.start()
        self.addCleanup(guard.stop)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.spotify = mock.Mock(spec=SpotifyClient)
        self.spotify.is_configured = True
        self.spotify.redirect_uri = "http://127.0.0.1:5000/spotify/callback"
        self.spotify.is_connected.return_value = True
        self.spotify.get_devices.return_value = DEVICES
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "test",
             "DATABASE": os.path.join(self.tmpdir.name, "t.db"),
             "ALARM_LOG": os.path.join(self.tmpdir.name, "alarms.log")},
            player=mock.Mock(spec=AudioPlayer),
            spotify=self.spotify,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def page(self, follow=False):
        return self.client.get("/spotify/", follow_redirects=follow).get_data(as_text=True)

    def post(self, url, **data):
        return self.client.post(url, data=data, follow_redirects=True).get_data(as_text=True)

    # --- Estado ---

    def test_not_configured(self):
        self.spotify.is_configured = False
        html = self.page()
        self.assertIn("No configurado", html)
        self.assertNotIn("Conectar Spotify", html)
        self.spotify.get_devices.assert_not_called()

    def test_not_connected_shows_connect_button(self):
        self.spotify.is_connected.return_value = False
        html = self.page()
        self.assertIn("No conectado", html)
        self.assertIn("Conectar Spotify", html)
        self.spotify.get_devices.assert_not_called()

    def test_connected_lists_devices_and_active(self):
        html = self.page()
        self.assertIn("Conectado", html)
        self.assertIn("Mi PC", html)
        self.assertIn("Mi móvil", html)
        self.assertIn("Activo: <strong>Mi PC</strong>", html)

    def test_devices_error_is_shown_not_500(self):
        self.spotify.get_devices.side_effect = SpotifyForbiddenError("403", 403)
        resp = self.client.get("/spotify/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Premium", resp.get_data(as_text=True))

    def test_nav_link(self):
        self.assertIn('href="/spotify/"', self.client.get("/").get_data(as_text=True))

    # --- OAuth ---

    def test_connect_redirects_with_state(self):
        self.spotify.get_authorize_url.side_effect = lambda s: f"https://accounts.example/?state={s}"
        resp = self.client.get("/spotify/connect")
        self.assertEqual(resp.status_code, 302)
        state = parse_qs(urlparse(resp.location).query)["state"][0]
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["spotify_state"], state)

    def test_callback_valid_state_exchanges_code(self):
        with self.client.session_transaction() as sess:
            sess["spotify_state"] = "abc"
        html = self.client.get("/spotify/callback?code=CODE&state=abc",
                               follow_redirects=True).get_data(as_text=True)
        self.spotify.exchange_code.assert_called_once_with("CODE")
        self.assertIn("vinculada", html)

    def test_callback_wrong_state_is_rejected(self):
        with self.client.session_transaction() as sess:
            sess["spotify_state"] = "abc"
        html = self.client.get("/spotify/callback?code=CODE&state=evil",
                               follow_redirects=True).get_data(as_text=True)
        self.spotify.exchange_code.assert_not_called()
        self.assertIn("no coincide", html)

    def test_callback_user_denied(self):
        with self.client.session_transaction() as sess:
            sess["spotify_state"] = "abc"
        html = self.client.get("/spotify/callback?error=access_denied&state=abc",
                               follow_redirects=True).get_data(as_text=True)
        self.spotify.exchange_code.assert_not_called()
        self.assertIn("access_denied", html)

    def test_disconnect(self):
        self.post("/spotify/disconnect")
        self.spotify.disconnect.assert_called_once()

    # --- Dispositivo y controles ---

    def test_controls_require_selected_device(self):
        html = self.post("/spotify/play")
        self.assertIn("Primero selecciona", html)
        self.spotify.play.assert_not_called()

    def test_select_device_then_transfer_play_pause(self):
        html = self.post("/spotify/device", device_id="movil", device_name="Mi móvil")
        self.assertIn("seleccionado", html)
        self.assertIn("Dispositivo seleccionado: <strong>Mi móvil</strong>", html)
        self.post("/spotify/transfer")
        self.post("/spotify/play")
        self.post("/spotify/pause")
        self.spotify.transfer_playback.assert_called_once_with("movil", play=False)
        self.spotify.play.assert_called_once_with("movil")
        self.spotify.pause.assert_called_once_with("movil")

    def test_control_errors_are_flashed(self):
        self.post("/spotify/device", device_id="pc", device_name="Mi PC")
        cases = [
            (SpotifyNotFoundError("no device", 404), "Transferir"),
            (SpotifyRateLimitError("Demasiadas peticiones", 30), "Demasiadas peticiones"),
            (SpotifyConnectionError("No se pudo conectar"), "No se pudo conectar"),
        ]
        for exc, text in cases:
            with self.subTest(exc=type(exc).__name__):
                self.spotify.play.side_effect = exc
                resp = self.client.post("/spotify/play", follow_redirects=True)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(text, resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
