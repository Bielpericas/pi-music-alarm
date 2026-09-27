"""Buscador de Spotify del formulario de alarmas y metadata guardada.

Spotify está siempre simulado (transporte falso o mock): ningún test sale a
Internet (urlopen está bloqueado en cada setUp).
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from spotify_client import (  # noqa: E402
    API_BASE,
    MAX_META_NAME,
    TOKEN_URL,
    UI_TIMEOUT,
    SpotifyAuthError,
    SpotifyClient,
    SpotifyConnectionError,
    SpotifyError,
    SpotifyForbiddenError,
    SpotifyNotFoundError,
    SpotifyRateLimitError,
    normalize_item,
    normalize_search,
)
from spotify_player import DEVICE_ID_KEY, SpotifyAlarmPlayer  # noqa: E402
from test_spotify_client import Clock, FakeTransport, MemoryTokenStore, resp, token_body  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TRACK_ID = "0DiWol3AO6WpXZgp0goxAV"
ALBUM_ID = "2noRn2Aes5aoNVsU6iWThc"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
TRACK_URI = f"spotify:track:{TRACK_ID}"
ALBUM_URI = f"spotify:album:{ALBUM_ID}"
PLAYLIST_URI = f"spotify:playlist:{PLAYLIST_ID}"
MONDAY_0730 = datetime(2026, 9, 28, 7, 30)

IMAGES = [{"url": "https://i.scdn.co/image/640", "width": 640, "height": 640},
          {"url": "https://i.scdn.co/image/300", "width": 300, "height": 300},
          {"url": "https://i.scdn.co/image/64", "width": 64, "height": 64}]
DAFT_PUNK = [{"name": "Daft Punk", "id": "4tZwfgrHOc3mvqYlEYSvVi"}]


def track(uri=TRACK_URI, name="One More Time", artists=DAFT_PUNK, images=IMAGES):
    return {"uri": uri, "name": name, "artists": artists, "album": {"images": images},
            "duration_ms": 320357, "preview_url": None}


def album(uri=ALBUM_URI, name="Discovery", artists=DAFT_PUNK, images=IMAGES):
    return {"uri": uri, "name": name, "artists": artists, "images": images,
            "total_tracks": 14}


def playlist(uri=PLAYLIST_URI, name="Discover Weekly", owner=None, images=IMAGES):
    return {"uri": uri, "name": name, "images": images,
            "owner": {"display_name": "Spotify", "id": "spotify"} if owner is None else owner}


def search_body(tracks=(), albums=(), playlists=()):
    return {"tracks": {"items": list(tracks), "total": len(tracks)},
            "albums": {"items": list(albums), "total": len(albums)},
            "playlists": {"items": list(playlists), "total": len(playlists)}}


def webify(uri):
    """spotify:<tipo>:<id> -> https://open.spotify.com/<tipo>/<id>?si=x"""
    _, kind, item_id = uri.split(":")
    return f"https://open.spotify.com/{kind}/{item_id}?si=x"


def no_network(test):
    guard = mock.patch("urllib.request.urlopen",
                       side_effect=AssertionError("los tests no deben usar la red"))
    guard.start()
    test.addCleanup(guard.stop)


class RecordingTransport(FakeTransport):
    """FakeTransport que además apunta el timeout de cada petición."""

    def __call__(self, method, url, headers, body, timeout):
        result = super().__call__(method, url, headers, body, timeout)
        self.calls[-1]["timeout"] = timeout
        return result


# --- Cliente: búsqueda, metadata y normalización ----------------------------

class SpotifySearchClientTest(unittest.TestCase):
    def setUp(self):
        no_network(self)

    def make(self, *responses, tokens="valid"):
        self.clock = Clock()
        self.slept = []
        self.transport = RecordingTransport(*responses)
        if tokens == "valid":
            tokens = {"access_token": "AT", "refresh_token": "RT",
                      "expires_at": self.clock.now + 3600, "scope": ""}
        self.store = MemoryTokenStore(tokens)
        return SpotifyClient("CID", "SECRET", "http://127.0.0.1:5000/spotify/callback",
                             self.store, transport=self.transport, clock=self.clock,
                             sleep=self.slept.append)

    def query_of(self, call):
        return urllib.parse.parse_qs(urllib.parse.urlparse(call["url"]).query)

    # Tipos

    def test_search_track(self):
        client = self.make(resp(200, search_body(tracks=[track()])))
        results = client.search("  one   more time ")
        self.assertEqual(results["tracks"], [{
            "uri": TRACK_URI, "type": "track", "name": "One More Time",
            "subtitle": "Daft Punk",
            "external_url": f"https://open.spotify.com/track/{TRACK_ID}",
            "image_url": "https://i.scdn.co/image/64",
        }])
        self.assertEqual((results["albums"], results["playlists"]), ([], []))
        call = self.transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].startswith(API_BASE + "/search?"))
        params = self.query_of(call)
        self.assertEqual(params["q"], ["one more time"])
        self.assertEqual(params["type"], ["track,album,playlist"])
        self.assertEqual(params["limit"], ["5"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer AT")
        self.assertEqual(call["timeout"], UI_TIMEOUT)

    def test_search_album(self):
        client = self.make(resp(200, search_body(albums=[album()])))
        item = client.search("discovery")["albums"][0]
        self.assertEqual((item["uri"], item["type"], item["name"], item["subtitle"]),
                         (ALBUM_URI, "album", "Discovery", "Daft Punk"))
        self.assertEqual(item["external_url"], f"https://open.spotify.com/album/{ALBUM_ID}")

    def test_search_playlist(self):
        client = self.make(resp(200, search_body(playlists=[
            playlist(),
            playlist(uri="spotify:playlist:1111111111111111111111", name="Sin nombre de autor",
                     owner={"display_name": None, "id": "pepe"}),
            playlist(uri="spotify:playlist:2222222222222222222222", name="Sin autor", owner={}),
        ])))
        items = client.search("weekly")["playlists"]
        self.assertEqual([(i["type"], i["name"], i["subtitle"]) for i in items], [
            ("playlist", "Discover Weekly", "Spotify"),
            ("playlist", "Sin nombre de autor", "pepe"),
            ("playlist", "Sin autor", ""),
        ])

    def test_search_mixed_types(self):
        client = self.make(resp(200, search_body(
            tracks=[track()], albums=[album()],
            playlists=[None, playlist(), None])))  # Spotify manda null en playlists
        results = client.search("daft punk")
        self.assertEqual([len(results[k]) for k in ("tracks", "albums", "playlists")], [1, 1, 1])
        self.assertEqual({i["type"] for k in results for i in results[k]},
                         {"track", "album", "playlist"})

    # Consulta y resultados vacíos

    def test_short_or_empty_query_does_not_call_spotify(self):
        client = self.make()
        for query in ("", " ", "a", "  b  ", None):
            with self.subTest(query=query):
                self.assertEqual(client.search(query),
                                 {"tracks": [], "albums": [], "playlists": []})
        self.assertEqual(self.transport.calls, [])

    def test_long_query_is_truncated(self):
        client = self.make(resp(200, search_body()))
        client.search("x" * 500)
        self.assertEqual(len(self.query_of(self.transport.calls[0])["q"][0]), 100)

    def test_no_results(self):
        client = self.make(resp(200, search_body()))
        self.assertEqual(client.search("zzzzqqq"), {"tracks": [], "albums": [], "playlists": []})

    # Autenticación

    def test_not_authenticated(self):
        client = self.make(tokens=None)
        with self.assertRaises(SpotifyAuthError):
            client.search("daft punk")
        self.assertEqual(self.transport.calls, [])

    def test_expired_token_is_refreshed_before_searching(self):
        client = self.make(resp(200, token_body(access="NEW", refresh=None)),
                           resp(200, search_body(tracks=[track()])),
                           tokens={"access_token": "OLD", "refresh_token": "RT",
                                   "expires_at": 1_000_000.0 + 10, "scope": ""})
        self.assertEqual(len(client.search("daft")["tracks"]), 1)
        self.assertEqual(self.transport.calls[0]["url"], TOKEN_URL)
        self.assertEqual(self.transport.calls[1]["headers"]["Authorization"], "Bearer NEW")
        self.assertEqual(self.store.data["refresh_token"], "RT")  # se conserva el anterior

    def test_401_refreshes_once_and_retries(self):
        client = self.make(resp(401, {"error": {"status": 401, "message": "expired"}}),
                           resp(200, token_body(access="NEW")),
                           resp(200, search_body(albums=[album()])))
        self.assertEqual(len(client.search("discovery")["albums"]), 1)
        self.assertEqual(self.transport.calls[2]["headers"]["Authorization"], "Bearer NEW")

    def test_401_after_refresh_raises_auth_error(self):
        client = self.make(resp(401), resp(200, token_body(access="NEW")), resp(401))
        with self.assertRaises(SpotifyAuthError):
            client.search("discovery")
        self.assertEqual(len(self.transport.calls), 3)  # sin bucles

    def test_403(self):
        client = self.make(resp(403, {"error": {"status": 403, "message": "Forbidden"}}))
        with self.assertRaises(SpotifyForbiddenError):
            client.search("discovery")

    # 429: sin esperas ni reintentos, y sin frenar a las alarmas

    def test_429_is_not_retried_and_pauses_only_the_search(self):
        client = self.make(resp(429, headers={"retry-after": "30"}),
                           resp(204),                                    # play de una alarma
                           resp(200, search_body(tracks=[track()])))
        with self.assertRaises(SpotifyRateLimitError) as ctx:
            client.search("daft punk")
        self.assertEqual(ctx.exception.retry_after, 30)
        self.assertEqual(self.slept, [])            # no bloquea la petición web esperando
        self.assertEqual(len(self.transport.calls), 1)

        # Mientras dura la pausa, el buscador no llama a Spotify...
        with self.assertRaises(SpotifyRateLimitError):
            client.search("daft punk")
        self.assertEqual(len(self.transport.calls), 1)
        # ...pero una alarma sí puede reproducir.
        client.play("dev", uri=PLAYLIST_URI)
        self.assertIn("/me/player/play", self.transport.calls[1]["url"])

        self.clock.now += 31
        self.assertEqual(len(client.search("daft punk")["tracks"]), 1)

    def test_short_429_is_not_retried_either(self):
        client = self.make(resp(429, headers={"retry-after": "1"}))
        with self.assertRaises(SpotifyRateLimitError):
            client.search("daft punk")
        self.assertEqual((self.slept, len(self.transport.calls)), ([], 1))

    def test_real_403_is_not_retried_and_keeps_the_link(self):
        body = {"error": {"status": 403, "message": "Insufficient client scope"}}
        client = self.make(resp(403, body))
        with self.assertRaises(SpotifyForbiddenError) as ctx:
            client.search("daft punk")
        self.assertEqual(ctx.exception.api_message, "Insufficient client scope")
        self.assertEqual(len(self.transport.calls), 1)  # ni refresh ni reintento
        self.assertIsNotNone(self.store.data)           # sigue vinculado

    def test_missing_scopes(self):
        client = self.make()
        self.store.data["scope"] = "user-read-playback-state user-modify-playback-state"
        self.assertEqual(client.missing_scopes(), set())
        self.store.data["scope"] = "user-read-playback-state"
        self.assertEqual(client.missing_scopes(), {"user-modify-playback-state"})
        self.store.data["scope"] = ""  # tokens antiguos sin scope guardado: no se asume nada
        self.assertEqual(client.missing_scopes(), set())
        self.store.data = None
        self.assertEqual(client.missing_scopes(), set())

    # Red

    def test_timeout_and_network_down(self):
        for exc in (TimeoutError("timed out"), urllib.error.URLError("sin DNS"),
                    ConnectionResetError("reset")):
            with self.subTest(exc=exc):
                client = self.make(exc)
                with self.assertRaises(SpotifyConnectionError):
                    client.search("daft punk")

    # Respuestas raras

    def test_malformed_response_is_an_error(self):
        for body in (b"<html>oops</html>", b"[1, 2]", b'"texto"', b"{}", b""):
            with self.subTest(body=body):
                client = self.make((200, {}, body))
                with self.assertRaises(SpotifyError):
                    client.search("daft punk")

    def test_partial_response_keeps_what_is_valid(self):
        body = {
            "tracks": {"items": [
                track(), None, "texto", {"uri": 5}, {"name": "sin uri"},
                track(uri="spotify:episode:" + TRACK_ID),        # tipo no admitido
                track(uri="spotify:album:" + ALBUM_ID),          # tipo que no toca
                track(uri="spotify:track:corto"),
                track(name=""),                                   # sin nombre
                track(),                                          # repetido
            ]},
            "albums": {"items": "no es una lista"},
            "playlists": None,
        }
        results = self.make(resp(200, body)).search("daft punk")
        self.assertEqual([i["uri"] for i in results["tracks"]], [TRACK_URI])
        self.assertEqual((results["albums"], results["playlists"]), ([], []))

    def test_results_without_image(self):
        body = search_body(
            tracks=[track(images=[])], albums=[album(images=None)],
            playlists=[playlist(images=[{"url": "http://inseguro/x.jpg"}, {"width": 64}])])
        results = self.make(resp(200, body)).search("daft punk")
        for key in ("tracks", "albums", "playlists"):
            with self.subTest(key=key):
                self.assertIsNone(results[key][0]["image_url"])
                self.assertTrue(results[key][0]["name"])
        item = normalize_item({"uri": TRACK_URI, "name": "Sin álbum"}, "track")
        self.assertIsNone(item["image_url"])

    def test_thumbnail_is_the_smallest_big_enough(self):
        self.assertEqual(normalize_item(album(), "album")["image_url"],
                         "https://i.scdn.co/image/64")
        big_only = [{"url": "https://i.scdn.co/big", "width": 640},
                    {"url": "https://i.scdn.co/mid", "width": 300}]
        self.assertEqual(normalize_item(album(images=big_only), "album")["image_url"],
                         "https://i.scdn.co/mid")
        no_sizes = [{"url": "https://mosaic.scdn.co/a"}]  # playlists: a veces sin tamaños
        self.assertEqual(normalize_item(playlist(images=no_sizes), "playlist")["image_url"],
                         "https://mosaic.scdn.co/a")

    def test_artists_normalization(self):
        artists = [{"name": "Daft Punk"}, {"name": " Romanthony "}, {"name": ""}, None,
                   "texto", {"id": "sin-nombre"}, {"name": "Todd\nEdwards"}]
        self.assertEqual(normalize_item(track(artists=artists), "track")["subtitle"],
                         "Daft Punk, Romanthony, Todd Edwards")
        self.assertEqual(normalize_item(album(artists=None), "album")["subtitle"], "")
        self.assertEqual(normalize_item(album(artists="Daft Punk"), "album")["subtitle"], "")

    def test_names_are_sanitized_and_limited(self):
        item = normalize_item(track(name="One\x00More\tTime​" + "x" * 500), "track")
        self.assertNotIn("\x00", item["name"])
        self.assertLessEqual(len(item["name"]), MAX_META_NAME)
        self.assertTrue(item["name"].startswith("One More Time"))

    def test_normalized_item_has_only_the_expected_fields(self):
        raw = dict(track(), available_markets=["ES"] * 100, href="https://api.spotify.com/x")
        self.assertEqual(set(normalize_item(raw, "track")),
                         {"uri", "type", "name", "subtitle", "external_url", "image_url"})
        self.assertEqual(normalize_search({"tracks": {"items": [raw]}})["tracks"][0]["uri"],
                         TRACK_URI)

    # Metadata de un URI (alarmas antiguas)

    def test_get_item_for_each_type(self):
        cases = [(TRACK_URI, track(), f"/tracks/{TRACK_ID}", "One More Time", "Daft Punk"),
                 (ALBUM_URI, album(), f"/albums/{ALBUM_ID}", "Discovery", "Daft Punk"),
                 (PLAYLIST_URI, playlist(), f"/playlists/{PLAYLIST_ID}", "Discover Weekly",
                  "Spotify")]
        for uri, body, path, name, subtitle in cases:
            with self.subTest(uri=uri):
                client = self.make(resp(200, body))
                item = client.get_item(uri)
                self.assertEqual((item["uri"], item["name"], item["subtitle"]),
                                 (uri, name, subtitle))
                self.assertTrue(self.transport.calls[0]["url"].startswith(API_BASE + path + "?"))

    def test_get_item_accepts_url_and_keeps_requested_uri(self):
        # Relinking: Spotify puede contestar con otro id; manda el URI de la alarma.
        client = self.make(resp(200, track(uri="spotify:track:1111111111111111111111")))
        item = client.get_item(f"https://open.spotify.com/intl-es/track/{TRACK_ID}?si=x")
        self.assertEqual(item["uri"], TRACK_URI)
        self.assertEqual(item["external_url"], f"https://open.spotify.com/track/{TRACK_ID}")

    def test_get_item_errors(self):
        client = self.make(resp(404, {"error": {"status": 404, "message": "Not found"}}))
        with self.assertRaises(SpotifyNotFoundError):
            client.get_item(PLAYLIST_URI)
        client = self.make(resp(200, {"nada": 1}))
        self.assertIsNone(client.get_item(PLAYLIST_URI))
        client = self.make()
        for bad in ("spotify:artist:" + TRACK_ID, "https://evil.com/track/" + TRACK_ID, ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                client.get_item(bad)
        self.assertEqual(self.transport.calls, [])


# --- Endpoints JSON /spotify/search y /spotify/lookup ------------------------

class AppTestCase(unittest.TestCase):
    def setUp(self):
        no_network(self)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.local = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyClient)
        self.spotify.is_configured = True
        self.spotify.redirect_uri = "http://127.0.0.1:5000/spotify/callback"
        self.spotify.is_connected.return_value = True
        self.spotify.missing_scopes.return_value = set()
        self.spotify.get_devices.return_value = [
            {"id": "dev", "name": "Groove", "type": "Speaker", "is_active": True}]
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "alarms.log"),
             "SPOTIFY_RETRY_DELAYS": (0, 0, 0, 0)},
            player=self.local, spotify=self.spotify)
        self.client = self.app.test_client()

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def get_json(self, url):
        resp = self.client.get(url)
        data = resp.get_json()
        resp.close()
        return resp, data

    def rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM alarms ORDER BY id").fetchall()
        finally:
            conn.close()


class SearchEndpointTest(AppTestCase):
    def test_returns_normalized_json(self):
        results = normalize_search(search_body(tracks=[track()], albums=[album()],
                                               playlists=[playlist()]))
        self.spotify.search.return_value = results
        resp, data = self.get_json("/spotify/search?q=daft%20punk")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data, {"ok": True, "query": "daft punk", "results": results})
        self.assertIn("no-store", resp.headers["Cache-Control"])
        self.spotify.search.assert_called_once_with("daft punk")

    def test_short_query(self):
        for query in ("", "a", "%20%20a%20"):
            with self.subTest(query=query):
                resp, data = self.get_json(f"/spotify/search?q={query}")
                self.assertEqual((resp.status_code, data["error"]), (400, "short_query"))
        resp, _ = self.get_json("/spotify/search")
        self.assertEqual(resp.status_code, 400)
        self.spotify.search.assert_not_called()

    def test_not_connected(self):
        self.spotify.is_connected.return_value = False
        resp, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual((resp.status_code, data["error"]), (409, "not_connected"))
        self.assertIn("Conecta Spotify", data["message"])
        self.spotify.search.assert_not_called()

    def test_not_configured(self):
        self.spotify.is_configured = False
        resp, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual((resp.status_code, data["error"]), (409, "not_configured"))
        self.spotify.search.assert_not_called()

    def test_errors_become_friendly_json(self):
        cases = [
            (SpotifyAuthError("token Bearer AT", 401), 401, "reauth"),
            (SpotifyForbiddenError("403", 403), 403, "forbidden"),
            (SpotifyRateLimitError("429", 30), 429, "rate_limited"),
            (SpotifyConnectionError("timeout"), 503, "unavailable"),
            (SpotifyError("raro", 500), 502, "spotify_error"),
            (RuntimeError("imprevisto"), 502, "spotify_error"),
            (KeyError("items"), 502, "spotify_error"),
        ]
        for exc, status, code in cases:
            with self.subTest(code=code, exc=type(exc).__name__):
                self.spotify.search.side_effect = exc
                with self.assertLogs(self.app.logger, "WARNING") as logs:
                    resp, data = self.get_json("/spotify/search?q=mi%20canci%C3%B3n%20secreta")
                self.assertEqual((resp.status_code, data["ok"], data["error"]),
                                 (status, False, code))
                # Todos recuerdan que el enlace manual sigue funcionando.
                self.assertIn("pegar un enlace", data["message"])
                # Ni tokens, ni cabeceras OAuth, ni la búsqueda en el log o la respuesta.
                text = " ".join(logs.output) + json.dumps(data)
                for secret in ("Bearer", "AT", "secreta"):
                    self.assertNotIn(secret, text)

    def test_403_is_classified(self):
        cases = [
            # Falta de scopes según Spotify: hay que volver a vincular.
            ({"api_message": "Insufficient client scope"}, "reauth"),
            # La app/cuenta no tiene acceso al endpoint: vincular no lo arregla.
            ({"api_message": "Active premium subscription required for the owner of the app"},
             "app_access"),
            ({"api_message": "User not registered in the Developer Dashboard"}, "app_access"),
            # Otro 403 sin más datos: no se culpa a la cuenta.
            ({}, "forbidden"),
            ({"api_message": "Forbidden", "reason": "UNKNOWN"}, "forbidden"),
        ]
        for fields, code in cases:
            with self.subTest(code=code, fields=fields):
                self.spotify.search.side_effect = SpotifyForbiddenError("403", 403, **fields)
                with self.assertLogs(self.app.logger, "WARNING") as logs:
                    resp, data = self.get_json("/spotify/search?q=daft")
                self.assertEqual((resp.status_code, data["error"], data["search_available"]),
                                 (403, code, False))
                self.assertIn("pegar un enlace de Spotify", data["message"])
                self.assertNotIn("cuenta", data["message"])
                if code == "reauth":
                    self.assertTrue(data["message"].startswith(
                        "Vuelve a vincular Spotify para activar la búsqueda."))
                    self.assertEqual(data["action"], "reconnect")
                else:
                    self.assertTrue(data["message"].startswith(
                        "La búsqueda de Spotify no está disponible."))
                    self.assertNotIn("action", data)
                # El motivo de Spotify queda en el log para diagnosticar.
                if fields.get("api_message"):
                    self.assertIn(fields["api_message"], " ".join(logs.output))
        self.spotify.disconnect.assert_not_called()

    def test_403_with_missing_scopes_asks_to_relink(self):
        self.spotify.missing_scopes.return_value = {"user-read-private"}
        self.spotify.search.side_effect = SpotifyForbiddenError("403", 403)
        with self.assertLogs(self.app.logger, "WARNING"):
            _, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual((data["error"], data["action"]), ("reauth", "reconnect"))

    def test_expired_session_asks_to_relink_once(self):
        self.spotify.search.side_effect = SpotifyAuthError("invalid_grant", 400, "invalid_grant")
        with self.assertLogs(self.app.logger, "WARNING"):
            resp, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual((resp.status_code, data["error"], data["search_available"]),
                         (401, "reauth", False))
        self.assertEqual(self.spotify.search.call_count, 1)  # sin bucles

    def test_not_connected_message_mentions_manual_link(self):
        self.spotify.is_connected.return_value = False
        _, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual(data["message"], "Conecta Spotify para usar el buscador. "
                                          "También puedes pegar un enlace directamente.")
        self.assertFalse(data["search_available"])

    def test_429_exposes_retry_after(self):
        self.spotify.search.side_effect = SpotifyRateLimitError("429", 30)
        with self.assertLogs(self.app.logger, "WARNING"):
            resp, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual((resp.status_code, data["retry_after"]), (429, 30))
        self.assertEqual(resp.headers["Retry-After"], "30")
        self.assertIn("30 s", data["message"])


class LookupEndpointTest(AppTestCase):
    def test_lookup_ok(self):
        item = normalize_item(album(), "album")
        self.spotify.get_item.return_value = item
        resp, data = self.get_json(f"/spotify/lookup?uri={ALBUM_URI}")
        self.assertEqual((resp.status_code, data), (200, {"ok": True, "item": item}))

    def test_lookup_errors(self):
        self.spotify.get_item.side_effect = ValueError("mal")
        resp, data = self.get_json("/spotify/lookup?uri=hola")
        self.assertEqual((resp.status_code, data["error"]), (400, "invalid_uri"))

        self.spotify.get_item.side_effect = None
        self.spotify.get_item.return_value = None
        resp, data = self.get_json(f"/spotify/lookup?uri={PLAYLIST_URI}")
        self.assertEqual((resp.status_code, data["error"]), (404, "not_found"))

        for exc, status in ((SpotifyNotFoundError("404", 404), 404),
                            (SpotifyConnectionError("sin red"), 503),
                            (SpotifyRateLimitError("429", 5), 429)):
            with self.subTest(exc=type(exc).__name__):
                self.spotify.get_item.side_effect = exc
                with self.assertLogs(self.app.logger, "WARNING"):
                    resp, _ = self.get_json(f"/spotify/lookup?uri={PLAYLIST_URI}")
                self.assertEqual(resp.status_code, status)

    def test_lookup_not_connected(self):
        self.spotify.is_connected.return_value = False
        resp, data = self.get_json(f"/spotify/lookup?uri={PLAYLIST_URI}")
        self.assertEqual((resp.status_code, data["error"]), (409, "not_connected"))
        self.spotify.get_item.assert_not_called()


# --- Formulario: selección, metadata guardada y validación -------------------

class PickerFormTest(AppTestCase):
    def post(self, url="/alarms/new", **overrides):
        data = {"name": "Música", "time": "07:30", "days": ["0"], "source": "spotify"}
        data.update(overrides)
        return self.client.post(url, data=data)

    def selected(self, uri=ALBUM_URI, name="Discovery", subtitle="Daft Punk", **overrides):
        """Lo que manda el JS al elegir un resultado del buscador."""
        return dict({"spotify_uri": uri, "spotify_name": name, "spotify_subtitle": subtitle,
                     "spotify_meta_uri": uri}, **overrides)

    def edit_html(self, alarm_id):
        return self.client.get(f"/alarms/{alarm_id}/edit").get_data(as_text=True)

    def select_device(self):
        self.client.post("/spotify/device", data={"device_id": "dev", "device_name": "Groove"})

    def check(self):
        return scheduler.check_alarms(self.db_path, MONDAY_0730, player=self.local,
                                      spotify=self.app.extensions["spotify_alarm"])

    # Formulario

    def manual_field(self, html):
        """El <input name="spotify_uri"> con su etiqueta y su texto de ayuda."""
        match = re.search(r'<div class="field spotify-link-field">.*?</div>', html, re.S)
        self.assertIsNotNone(match)
        return match.group(0)

    def test_form_shows_link_field_and_search_when_connected(self):
        html = self.client.get("/alarms/new").get_data(as_text=True)
        self.assertIn("Contenido de Spotify", html)
        field = self.manual_field(html)
        self.assertIn("Pega un enlace de Spotify", field)
        self.assertIn('placeholder="Pega un enlace de Spotify (playlist, álbum o canción)"', field)
        self.assertIn("Admite playlists, álbumes y canciones de Spotify", field)
        self.assertIn('name="spotify_uri"', field)
        # Enlace manual primero y a la vista; después "o" y el buscador.
        self.assertNotIn("<details", html)
        self.assertNotIn("Introducir enlace manualmente", html)
        self.assertLess(html.index('name="spotify_uri"'), html.index('class="or-divider"'))
        self.assertLess(html.index('class="or-divider"'), html.index('id="spotify_search"'))
        self.assertIn("Buscar en Spotify", html)
        self.assertIn('data-search-url="/spotify/search"', html)
        self.assertIn('data-lookup-url="/spotify/lookup"', html)
        self.assertIn('data-connected="true"', html)
        self.assertNotIn("Conecta Spotify para usar el buscador", html)
        # El cuadro de búsqueda no manda nada al servidor (no tiene name).
        self.assertIsNone(re.search(r'<input id="spotify_search"[^>]*\bname=', html))
        self.spotify.search.assert_not_called()

    def test_link_field_always_visible(self):
        self.post(spotify_uri=PLAYLIST_URI)
        self.post(**self.selected())
        pages = [self.client.get("/alarms/new"), self.client.get("/alarms/1/edit"),
                 self.client.get("/alarms/2/edit"), self.post(spotify_uri="mal")]
        self.spotify.is_connected.return_value = False
        pages.append(self.client.get("/alarms/new"))
        for number, resp in enumerate(pages):
            with self.subTest(page=number):
                html = resp.get_data(as_text=True)
                field = self.manual_field(html)
                self.assertNotRegex(field, r"\bhidden\b")
                self.assertNotIn("<details", html)
                self.assertIn("Pega un enlace de Spotify (playlist, álbum o canción)", field)

    def test_form_without_spotify_linked(self):
        self.spotify.is_connected.return_value = False
        html = self.client.get("/alarms/new").get_data(as_text=True)
        text = " ".join(html.split())
        self.assertIn("Conecta Spotify para usar el buscador. También puedes pegar un enlace "
                      "directamente.", text)
        self.assertIn('href="/spotify/"', html)
        self.assertNotIn('id="spotify_search"', html)
        self.assertIn('data-connected="false"', html)
        # El aviso se ve sin JS (la sección no está oculta) y el enlace manual sigue.
        self.assertRegex(html, r'data-search-section\s*>')
        self.assertIn('name="spotify_uri"', self.manual_field(html))
        resp = self.post(spotify_uri=f"https://open.spotify.com/playlist/{PLAYLIST_ID}")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.rows()[0]["spotify_uri"], PLAYLIST_URI)

    def test_form_survives_spotify_state_errors(self):
        self.spotify.is_connected.side_effect = RuntimeError("bd bloqueada")
        with self.assertLogs(self.app.logger, "ERROR"):
            resp = self.client.get("/alarms/new")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Conecta Spotify", resp.get_data(as_text=True))

    # Guardar y editar

    def test_save_selected_result(self):
        self.assertEqual(self.post(**self.selected()).status_code, 302)
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"], row["spotify_subtitle"]),
                         (ALBUM_URI, "Discovery", "Daft Punk"))
        self.assertIn("Spotify · Discovery", self.client.get("/").get_data(as_text=True))

    def test_edit_shows_saved_metadata(self):
        self.post(**self.selected())
        html = self.edit_html(self.rows()[0]["id"])
        self.assertIn("Discovery", html)
        self.assertIn("Álbum · Daft Punk", html)
        self.assertIn(f'href="https://open.spotify.com/album/{ALBUM_ID}"', html)
        self.assertIn(f'name="spotify_meta_uri" value="{ALBUM_URI}"', html)
        self.assertIn('name="spotify_name" value="Discovery"', html)
        self.assertNotIn('data-selection hidden', html)
        self.spotify.get_item.assert_not_called()  # ya hay metadata: no hace falta Spotify

    def test_edit_selecting_another_result(self):
        self.post(**self.selected())
        alarm_id = self.rows()[0]["id"]
        resp = self.post(f"/alarms/{alarm_id}/edit",
                         **self.selected(TRACK_URI, "One More Time", "Daft Punk"))
        self.assertEqual(resp.status_code, 302)
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"]), (TRACK_URI, "One More Time"))
        self.assertIn("Canción · Daft Punk", self.edit_html(alarm_id))

    def test_edit_keeps_metadata_when_nothing_changes(self):
        self.post(**self.selected())
        alarm_id = self.rows()[0]["id"]
        # El formulario de edición reenvía los campos ocultos tal y como los pintó.
        self.post(f"/alarms/{alarm_id}/edit", name="Otra", **self.selected())
        row = self.rows()[0]
        self.assertEqual((row["name"], row["spotify_name"]), ("Otra", "Discovery"))

    def test_old_alarm_with_only_uri(self):
        self.post(spotify_uri=PLAYLIST_URI)  # como antes: sin metadata
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"], row["spotify_subtitle"]),
                         (PLAYLIST_URI, None, None))
        html = self.edit_html(row["id"])
        # Se muestra el URI y el tipo, sin llamar a Spotify al pintar la página:
        # la metadata la pide el navegador a /spotify/lookup.
        self.assertIn(PLAYLIST_URI, html)
        self.assertIn("Playlist de Spotify", html)
        self.assertIn('class="spotify-selection-name is-uri"', html)
        self.assertIn('name="spotify_meta_uri" value=""', html)
        self.assertEqual(self.spotify.get_item.call_count + self.spotify.search.call_count, 0)
        self.assertIn("Spotify · playlist", self.client.get("/").get_data(as_text=True))

    def test_old_alarm_saved_without_resolving_metadata(self):
        self.post(spotify_uri=PLAYLIST_URI)
        alarm_id = self.rows()[0]["id"]
        # Spotify caído: la lookup falla y el formulario se guarda tal cual.
        self.spotify.get_item.side_effect = SpotifyConnectionError("sin red")
        with self.assertLogs(self.app.logger, "WARNING"):
            resp, _ = self.get_json(f"/spotify/lookup?uri={PLAYLIST_URI}")
        self.assertEqual(resp.status_code, 503)
        self.post(f"/alarms/{alarm_id}/edit", name="Sigue", spotify_uri=PLAYLIST_URI,
                  spotify_name="", spotify_subtitle="", spotify_meta_uri="")
        row = self.rows()[0]
        self.assertEqual((row["name"], row["spotify_uri"], row["spotify_name"], row["enabled"]),
                         ("Sigue", PLAYLIST_URI, None, 1))

    def test_old_alarm_upgraded_after_lookup(self):
        self.post(spotify_uri=PLAYLIST_URI)
        alarm_id = self.rows()[0]["id"]
        # El JS rellena los ocultos con /spotify/lookup; al guardar queda la snapshot.
        self.post(f"/alarms/{alarm_id}/edit",
                  **self.selected(PLAYLIST_URI, "Discover Weekly", "Spotify"))
        self.assertEqual(self.rows()[0]["spotify_name"], "Discover Weekly")
        self.assertIn("Playlist · Spotify", self.edit_html(alarm_id))

    # Enlace manual

    def test_manual_links_for_each_type(self):
        cases = [
            (f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=abc", PLAYLIST_URI),
            (f"https://open.spotify.com/album/{ALBUM_ID}", ALBUM_URI),
            (f"https://open.spotify.com/track/{TRACK_ID}?si=1&context=x", TRACK_URI),
            (PLAYLIST_URI, PLAYLIST_URI),
            (ALBUM_URI, ALBUM_URI),
            (TRACK_URI, TRACK_URI),
            (f"  http://open.spotify.com/intl-es/track/{TRACK_ID}/  ", TRACK_URI),
        ]
        for text, uri in cases:
            with self.subTest(text=text):
                resp = self.post(spotify_uri=text)
                self.assertEqual(resp.status_code, 302)
                row = self.rows()[-1]
                self.assertEqual((row["source"], row["spotify_uri"], row["spotify_name"]),
                                 ("spotify", uri, None))

    def test_manual_links_play_exactly_that_content(self):
        self.select_device()
        playback = self.app.extensions["playback"]
        for number, uri in enumerate((TRACK_URI, ALBUM_URI, PLAYLIST_URI)):
            with self.subTest(uri=uri):
                self.spotify.reset_mock()
                self.post(name=f"A{number}", spotify_uri=webify(uri))
                playback.start(self.rows()[-1], manual=True)
                playback.stop()
                self.spotify.play.assert_called_once_with("dev", uri=uri)
                # Álbum/playlist: se pide el nº de pistas (inicio aleatorio); canción: no.
                asked = mock.call.get_track_count(uri) in self.spotify.method_calls
                self.assertEqual(asked, uri != TRACK_URI)
        self.local.play.assert_not_called()

    def test_invalid_and_unsupported_links_rejected(self):
        for bad in ("https://open.spotify.com/playlist/", "open.spotify.com/album/x",
                    f"https://open.spotify.com/artist/{TRACK_ID}",
                    f"https://open.spotify.com/episode/{TRACK_ID}",
                    f"https://open.spotify.com/show/{TRACK_ID}",
                    f"spotify:artist:{TRACK_ID}", f"spotify:user:spotify:playlist:{PLAYLIST_ID}",
                    f"https://spotify.link/{TRACK_ID}", "Daft Punk"):
            with self.subTest(bad=bad):
                resp = self.post(spotify_uri=bad)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("Pega una URL de open.spotify.com", resp.get_data(as_text=True))
        self.assertEqual(self.rows(), [])

    def test_search_selection_with_web_url_in_link_field(self):
        # El JS escribe en el campo el enlace open.spotify.com del resultado.
        url = f"https://open.spotify.com/album/{ALBUM_ID}"
        self.post(**self.selected(uri=url, spotify_meta_uri=ALBUM_URI))
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"]), (ALBUM_URI, "Discovery"))

    def test_select_then_paste_other_link_drops_old_metadata(self):
        self.post(**self.selected())  # «Discovery» elegido con el buscador
        alarm_id = self.rows()[0]["id"]
        self.assertEqual(self.rows()[0]["spotify_name"], "Discovery")
        # Luego se pega otra URL y llega la metadata vieja en los ocultos.
        self.post(f"/alarms/{alarm_id}/edit",
                  **self.selected(uri=f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
                                  spotify_meta_uri=ALBUM_URI))
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"], row["spotify_subtitle"]),
                         (PLAYLIST_URI, None, None))
        html = self.edit_html(alarm_id)
        self.assertNotIn("Discovery", html)
        self.assertIn("Playlist de Spotify", html)

    def test_search_403_keeps_manual_link_working(self):
        self.spotify.search.side_effect = SpotifyForbiddenError(
            "403", 403, api_message="Active premium subscription required for the owner of the app")
        with self.assertLogs(self.app.logger, "WARNING"):
            resp, data = self.get_json("/spotify/search?q=daft")
        self.assertEqual((resp.status_code, data["error"], data["search_available"]),
                         (403, "app_access", False))
        self.assertEqual(data["message"], "La búsqueda de Spotify no está disponible. "
                                          "Puedes pegar un enlace de Spotify arriba.")
        # El formulario sigue igual y se puede guardar con un enlace.
        html = self.client.get("/alarms/new").get_data(as_text=True)
        self.assertIn('name="spotify_uri"', self.manual_field(html))
        resp = self.post(spotify_uri=f"https://open.spotify.com/playlist/{PLAYLIST_ID}")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.rows()[0]["spotify_uri"], PLAYLIST_URI)

    def test_manual_url_still_works(self):
        url = f"https://open.spotify.com/intl-es/album/{ALBUM_ID}?si=abc"
        self.assertEqual(self.post(spotify_uri=url).status_code, 302)
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"]), (ALBUM_URI, None))

    def test_manual_url_with_matching_lookup_metadata(self):
        url = f"https://open.spotify.com/album/{ALBUM_ID}"
        self.post(**self.selected(uri=url, spotify_meta_uri=ALBUM_URI))
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"]), (ALBUM_URI, "Discovery"))

    def test_manual_link_after_selection_discards_stale_metadata(self):
        # Se eligió «Discovery» y luego se pegó otro enlace a mano.
        self.post(**self.selected(uri=PLAYLIST_URI, spotify_meta_uri=ALBUM_URI))
        row = self.rows()[0]
        self.assertEqual((row["spotify_uri"], row["spotify_name"], row["spotify_subtitle"]),
                         (PLAYLIST_URI, None, None))

    # Validación del servidor

    def test_invalid_or_tampered_uri_rejected(self):
        for bad in ("", "hola", f"spotify:artist:{TRACK_ID}", f"spotify:episode:{TRACK_ID}",
                    "javascript:alert(1)", f"spotify:track:{TRACK_ID}; rm -rf /",
                    f"https://evil.com/track/{TRACK_ID}"):
            with self.subTest(bad=bad):
                resp = self.post(**self.selected(uri=bad, spotify_meta_uri=ALBUM_URI))
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_error_rerender_keeps_selection(self):
        resp = self.post(name="", **self.selected())
        self.assertEqual(resp.status_code, 400)
        html = resp.get_data(as_text=True)
        self.assertIn("Álbum · Daft Punk", html)
        self.assertIn(f'name="spotify_meta_uri" value="{ALBUM_URI}"', html)

    def test_tampered_metadata_is_sanitized(self):
        self.post(**self.selected(name="  <script>alert(1)</script>\x00\n" + "x" * 1000,
                                  subtitle="Daft\tPunk\r\n" + "y" * 1000,
                                  spotify_type="track"))  # un campo que no existe
        row = self.rows()[0]
        self.assertTrue(row["spotify_name"].startswith("<script>alert(1)</script> x"))
        self.assertLessEqual(len(row["spotify_name"]), MAX_META_NAME)
        self.assertNotIn("\x00", row["spotify_name"])
        self.assertTrue(row["spotify_subtitle"].startswith("Daft Punk y"))
        self.assertLessEqual(len(row["spotify_subtitle"]), 160)
        self.assertEqual(row["spotify_uri"], ALBUM_URI)
        # Se escapa al pintar, y el tipo sale del URI (Álbum), no del POST.
        html = self.edit_html(row["id"])
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Álbum · Daft Punk", html)
        self.assertNotIn("Canción ·", html)

    def test_metadata_ignored_without_valid_meta_uri_or_name(self):
        for overrides in ({"spotify_meta_uri": ""}, {"spotify_meta_uri": "basura"},
                          {"spotify_meta_uri": TRACK_URI}, {"spotify_name": "   "}):
            with self.subTest(overrides=overrides):
                self.post(**self.selected(**overrides))
                row = self.rows()[-1]
                self.assertEqual((row["spotify_uri"], row["spotify_name"],
                                  row["spotify_subtitle"]), (ALBUM_URI, None, None))

    def test_local_alarm_ignores_spotify_metadata(self):
        self.post(source="local", **self.selected())
        row = self.rows()[0]
        self.assertEqual((row["source"], row["spotify_uri"], row["spotify_name"]),
                         ("local", None, None))

    def test_tampered_metadata_does_not_change_playback(self):
        self.select_device()
        # Nombre y subtítulo intentan "colar" otro contenido: da igual.
        self.post(**self.selected(uri=PLAYLIST_URI, name=f"spotify:track:{TRACK_ID}",
                                  subtitle=f"https://open.spotify.com/track/{TRACK_ID}"))
        self.assertEqual(self.check(), ["Música"])
        self.spotify.play.assert_called_once_with("dev", uri=PLAYLIST_URI)
        self.local.play.assert_not_called()

    # Aislamiento: el buscador no afecta a las alarmas

    def test_search_failures_never_affect_scheduler_or_playback(self):
        self.select_device()
        self.post(spotify_uri=PLAYLIST_URI)
        for exc in (SpotifyRateLimitError("429", 60), SpotifyConnectionError("caído"),
                    RuntimeError("bug")):
            self.spotify.search.side_effect = exc
            with self.assertLogs(self.app.logger, "WARNING"):
                self.client.get("/spotify/search?q=daft")
        self.assertEqual(self.check(), ["Música"])
        self.spotify.play.assert_called_once_with("dev", uri=PLAYLIST_URI)
        self.local.play.assert_not_called()
        self.assertEqual(self.rows()[0]["spotify_uri"], PLAYLIST_URI)

    def test_playback_never_uses_search_or_metadata_calls(self):
        self.select_device()
        self.post(**self.selected(uri=PLAYLIST_URI, name="Discover Weekly"))
        self.check()
        called = {name for name, _args, _kwargs in self.spotify.method_calls}
        self.assertFalse(called & {"search", "get_item"})

    # Migración

    def test_migrates_old_spotify_alarms(self):
        scheduler.close_logging()
        old = os.path.join(self.tmpdir.name, "old.db")
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " time TEXT NOT NULL, days TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL"
            " DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " last_triggered TEXT, source TEXT NOT NULL DEFAULT 'local', spotify_uri TEXT,"
            " volume_start INTEGER NOT NULL DEFAULT 20, volume_end INTEGER NOT NULL DEFAULT 60,"
            " fade_minutes INTEGER NOT NULL DEFAULT 5,"
            " max_duration_minutes INTEGER NOT NULL DEFAULT 30, local_track TEXT)")
        conn.execute("INSERT INTO alarms (name, time, days, source, spotify_uri, volume_start)"
                     " VALUES ('Vieja', '07:30', '0', 'spotify', ?, 35)", (PLAYLIST_URI,))
        conn.commit()
        conn.close()
        db.init_db(old)
        db.init_db(old)  # idempotente
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM alarms").fetchone()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, 'dev')", (DEVICE_ID_KEY,))
        conn.commit()
        conn.close()
        self.assertEqual((row["name"], row["source"], row["spotify_uri"], row["volume_start"],
                          row["spotify_name"], row["spotify_subtitle"]),
                         ("Vieja", "spotify", PLAYLIST_URI, 35, None, None))
        # Y sigue sonando igual.
        player = SpotifyAlarmPlayer(self.spotify, old, retry_delays=(0, 0, 0, 0))
        fired = scheduler.check_alarms(old, MONDAY_0730, player=self.local, spotify=player)
        self.assertEqual(fired, ["Vieja"])
        self.spotify.play.assert_called_once_with("dev", uri=PLAYLIST_URI)


class PickerScriptTest(unittest.TestCase):
    """Comprobaciones estáticas del JS del buscador (no hay navegador en los tests)."""

    def setUp(self):
        self.source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    def test_debounce_cancel_and_min_chars(self):
        self.assertRegex(self.source, r"DEBOUNCE_MS = [345]\d\d;")
        self.assertIn("MIN_CHARS = 2", self.source)
        self.assertIn("AbortController", self.source)
        self.assertIn("mine !== seq", self.source)  # respuestas antiguas ignoradas

    def test_rate_limit_and_messages(self):
        self.assertIn("blockedUntil", self.source)
        self.assertIn("retry_after", self.source)
        for text in ("Buscando…", "Sin resultados."):
            self.assertIn(text, self.source)

    def test_results_are_rendered_as_text(self):
        self.assertNotRegex(self.source, r"\.innerHTML|\.outerHTML|insertAdjacentHTML")

    def test_link_formats_match_the_server_parser(self):
        # El JS reconoce los mismos tipos que parse_spotify_uri para comparar URIs.
        self.assertIn("spotify:(track|album|playlist):([A-Za-z0-9]{22})", self.source)
        self.assertIn(r"(track|album|playlist)\/([A-Za-z0-9]{22})", self.source)

    def test_permanent_search_errors_stop_searching(self):
        self.assertIn("search_available === false", self.source)
        self.assertIn("input.disabled = true", self.source)

    def test_manual_link_wins_over_old_metadata(self):
        self.assertIn('uriInput.addEventListener("input", onManualEdit)', self.source)
        self.assertIn("setMeta(null)", self.source)

    def test_enter_does_not_submit_the_alarm_form(self):
        self.assertIn('event.key !== "Enter"', self.source)
        self.assertIn("event.preventDefault()", self.source)


if __name__ == "__main__":
    unittest.main()
