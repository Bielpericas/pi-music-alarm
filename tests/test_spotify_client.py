"""Tests de SpotifyClient. El transporte HTTP es falso: nunca se sale a Internet."""
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from spotify_client import (  # noqa: E402
    API_BASE,
    TOKEN_URL,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyForbiddenError,
    SpotifyNotConfiguredError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
    SqliteTokenStore,
)

REDIRECT = "http://127.0.0.1:5000/spotify/callback"


def resp(status=200, body=None, headers=None):
    content = json.dumps(body).encode() if body is not None else b""
    return (status, headers or {}, content)


def token_body(access="AT", refresh="RT", expires_in=3600):
    body = {"access_token": access, "token_type": "Bearer", "expires_in": expires_in,
            "scope": "user-read-playback-state user-modify-playback-state"}
    if refresh:
        body["refresh_token"] = refresh
    return body


class FakeTransport:
    """Devuelve respuestas encoladas y registra cada petición."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.responses:
            raise AssertionError(f"Petición inesperada: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class MemoryTokenStore:
    def __init__(self, tokens=None):
        self.data = tokens

    def load(self):
        return dict(self.data) if self.data else None

    def save(self, tokens):
        self.data = dict(tokens)

    def clear(self):
        self.data = None


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class SpotifyClientTest(unittest.TestCase):
    def setUp(self):
        guard = mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("los tests no deben usar la red"))
        guard.start()
        self.addCleanup(guard.stop)

    def make(self, *responses, tokens="valid", configured=True):
        self.clock = Clock()
        self.slept = []
        self.transport = FakeTransport(*responses)
        if tokens == "valid":
            tokens = {"access_token": "AT", "refresh_token": "RT",
                      "expires_at": self.clock.now + 3600, "scope": ""}
        self.store = MemoryTokenStore(tokens)
        return SpotifyClient(
            "CID" if configured else "", "SECRET" if configured else "", REDIRECT,
            self.store, transport=self.transport, clock=self.clock,
            sleep=self.slept.append,
        )

    # --- OAuth ---

    def test_authorize_url(self):
        client = self.make(tokens=None)
        url = urllib.parse.urlparse(client.get_authorize_url("xyz"))
        params = urllib.parse.parse_qs(url.query)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}",
                         "https://accounts.spotify.com/authorize")
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["client_id"], ["CID"])
        self.assertEqual(params["redirect_uri"], [REDIRECT])
        self.assertEqual(params["state"], ["xyz"])
        self.assertEqual(params["scope"],
                         ["user-read-playback-state user-modify-playback-state"])

    def test_not_configured(self):
        client = self.make(tokens=None, configured=False)
        self.assertFalse(client.is_configured)
        with self.assertRaises(SpotifyNotConfiguredError):
            client.get_authorize_url("x")

    def test_exchange_code(self):
        client = self.make(resp(200, token_body()), tokens=None)
        client.exchange_code("CODE")
        call = self.transport.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", TOKEN_URL))
        expected = "Basic " + base64.b64encode(b"CID:SECRET").decode()
        self.assertEqual(call["headers"]["Authorization"], expected)
        self.assertEqual(urllib.parse.parse_qs(call["body"].decode()), {
            "grant_type": ["authorization_code"], "code": ["CODE"],
            "redirect_uri": [REDIRECT]})
        self.assertEqual(self.store.data["access_token"], "AT")
        self.assertEqual(self.store.data["refresh_token"], "RT")
        self.assertEqual(self.store.data["expires_at"], self.clock.now + 3600)
        self.assertTrue(client.is_connected())

    def test_exchange_code_rejected(self):
        client = self.make(resp(400, {"error": "invalid_grant",
                                      "error_description": "Invalid authorization code"}),
                           tokens=None)
        with self.assertRaises(SpotifyAuthError):
            client.exchange_code("BAD")
        self.assertFalse(client.is_connected())

    # --- Llamadas a la API ---

    def test_get_devices(self):
        devices = [{"id": "d1", "name": "PC", "type": "Computer", "is_active": True}]
        client = self.make(resp(200, {"devices": devices}))
        self.assertEqual(client.get_devices(), devices)
        call = self.transport.calls[0]
        self.assertEqual((call["method"], call["url"]), ("GET", API_BASE + "/me/player/devices"))
        self.assertEqual(call["headers"]["Authorization"], "Bearer AT")

    def test_transfer_play_pause(self):
        client = self.make(resp(204), resp(204), resp(204))
        client.transfer_playback("d1")
        client.play("d1")
        client.pause("d1")
        transfer, play, pause = self.transport.calls
        self.assertEqual((transfer["method"], transfer["url"]), ("PUT", API_BASE + "/me/player"))
        self.assertEqual(json.loads(transfer["body"]), {"device_ids": ["d1"], "play": False})
        self.assertEqual(transfer["headers"]["Content-Type"], "application/json")
        self.assertEqual(play["url"], API_BASE + "/me/player/play?device_id=d1")
        self.assertEqual(pause["url"], API_BASE + "/me/player/pause?device_id=d1")
        self.assertEqual(play["body"], b"")  # PUT con Content-Length: 0

    def test_not_connected(self):
        client = self.make(tokens=None)
        with self.assertRaises(SpotifyAuthError):
            client.get_devices()
        self.assertEqual(self.transport.calls, [])

    # --- Refresh automático ---

    def test_refreshes_expired_token_before_calling(self):
        client = self.make(resp(200, token_body(access="NEW", refresh=None)),
                           resp(200, {"devices": []}))
        self.store.data["expires_at"] = self.clock.now + 10  # casi caducado
        client.get_devices()
        refresh, api = self.transport.calls
        self.assertEqual(refresh["url"], TOKEN_URL)
        self.assertEqual(urllib.parse.parse_qs(refresh["body"].decode()),
                         {"grant_type": ["refresh_token"], "refresh_token": ["RT"]})
        self.assertTrue(refresh["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(api["headers"]["Authorization"], "Bearer NEW")
        # Sin refresh_token nuevo en la respuesta: se conserva el anterior.
        self.assertEqual(self.store.data["refresh_token"], "RT")

    def test_refresh_stores_rotated_refresh_token(self):
        client = self.make(resp(200, token_body(access="NEW", refresh="RT2")),
                           resp(200, {"devices": []}))
        self.store.data["expires_at"] = self.clock.now - 1
        client.get_devices()
        self.assertEqual(self.store.data["refresh_token"], "RT2")

    def test_401_refreshes_and_retries_once(self):
        client = self.make(resp(401, {"error": {"status": 401, "message": "expired"}}),
                           resp(200, token_body(access="NEW")),
                           resp(200, {"devices": []}))
        self.assertEqual(client.get_devices(), [])
        self.assertEqual(self.transport.calls[2]["headers"]["Authorization"], "Bearer NEW")

    def test_401_twice_raises_auth_error(self):
        client = self.make(resp(401), resp(200, token_body(access="NEW")), resp(401))
        with self.assertRaises(SpotifyAuthError):
            client.get_devices()
        self.assertEqual(len(self.transport.calls), 3)

    def test_revoked_refresh_token_disconnects(self):
        client = self.make(resp(400, {"error": "invalid_grant",
                                      "error_description": "Refresh token revoked"}))
        self.store.data["expires_at"] = self.clock.now - 1
        with self.assertRaises(SpotifyAuthError):
            client.get_devices()
        self.assertFalse(client.is_connected())

    def test_wrong_client_secret_keeps_tokens(self):
        client = self.make(resp(400, {"error": "invalid_client",
                                      "error_description": "Invalid client"}))
        self.store.data["expires_at"] = self.clock.now - 1
        with self.assertRaises(SpotifyAuthError) as ctx:
            client.get_devices()
        self.assertEqual(ctx.exception.oauth_error, "invalid_client")
        self.assertTrue(client.is_connected())

    # --- Errores ---

    def test_403(self):
        client = self.make(resp(403, {"error": {"status": 403, "message": "Player command failed",
                                                "reason": "PREMIUM_REQUIRED"}}))
        with self.assertRaises(SpotifyForbiddenError) as ctx:
            client.play("d1")
        self.assertIn("PREMIUM_REQUIRED", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 403)

    def test_404_no_active_device(self):
        client = self.make(resp(404, {"error": {"status": 404, "message": "Device not found",
                                                "reason": "NO_ACTIVE_DEVICE"}}))
        with self.assertRaises(SpotifyNotFoundError):
            client.pause()
        self.assertEqual(self.transport.calls[0]["url"], API_BASE + "/me/player/pause")

    def test_429_short_retry_after_waits_and_retries(self):
        client = self.make(resp(429, headers={"retry-after": "2"}), resp(204))
        client.pause("d1")
        self.assertEqual(self.slept, [2])
        self.assertEqual(len(self.transport.calls), 2)

    def test_429_long_retry_after_raises_and_blocks_until_then(self):
        client = self.make(resp(429, headers={"retry-after": "120"}), resp(200, {"devices": []}))
        with self.assertRaises(SpotifyRateLimitError) as ctx:
            client.get_devices()
        self.assertEqual(ctx.exception.retry_after, 120)
        self.assertEqual(self.slept, [])
        # Mientras dure el bloqueo, ni siquiera se llama a Spotify.
        self.clock.now += 60
        with self.assertRaises(SpotifyRateLimitError):
            client.get_devices()
        self.assertEqual(len(self.transport.calls), 1)
        # Pasado el Retry-After, vuelve a funcionar.
        self.clock.now += 61
        self.assertEqual(client.get_devices(), [])

    def test_429_twice_raises(self):
        client = self.make(resp(429, headers={"retry-after": "1"}),
                           resp(429, headers={"retry-after": "1"}))
        with self.assertRaises(SpotifyRateLimitError):
            client.play()
        self.assertEqual(self.slept, [1])

    def test_connection_error(self):
        client = self.make(urllib.error.URLError("getaddrinfo failed"))
        with self.assertRaises(SpotifyConnectionError):
            client.get_devices()

    def test_timeout(self):
        client = self.make(TimeoutError("timed out"))
        with self.assertRaises(SpotifyConnectionError):
            client.pause()


class SqliteTokenStoreTest(unittest.TestCase):
    def test_save_load_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.db")
            db.init_db(path)
            store = SqliteTokenStore(path)
            self.assertIsNone(store.load())
            tokens = {"access_token": "A", "refresh_token": "R", "expires_at": 5.0, "scope": "s"}
            store.save(tokens)
            store.save({**tokens, "access_token": "B"})
            self.assertEqual(store.load()["access_token"], "B")
            store.clear()
            self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
