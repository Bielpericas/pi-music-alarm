"""Interfaz (rediseño Groove) y PWA: plantillas, manifest y service worker."""
import json
import os
import re
import sqlite3
import struct
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scheduler  # noqa: E402
import ui  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from spotify_client import SpotifyClient  # noqa: E402

MONDAY_0700 = datetime(2026, 9, 28, 7, 0)  # lunes


def alarm(time="07:30", days="0,1,2,3,4", enabled=1, name="Trabajo"):
    return {"id": 1, "name": name, "time": time, "days": days, "enabled": enabled,
            "source": "local", "spotify_uri": None}


class NextAlarmTest(unittest.TestCase):
    def test_same_day_later(self):
        self.assertEqual(ui.next_occurrence(alarm(), MONDAY_0700), datetime(2026, 9, 28, 7, 30))

    def test_skips_to_next_selected_day(self):
        weekend = alarm(days="5,6")
        self.assertEqual(ui.next_occurrence(weekend, MONDAY_0700), datetime(2026, 10, 3, 7, 30))

    def test_time_already_passed_today(self):
        early = alarm(time="06:00", days="0")
        self.assertEqual(ui.next_occurrence(early, MONDAY_0700), datetime(2026, 10, 5, 6, 0))

    def test_one_time_alarm(self):
        once = alarm(time="06:00", days="")
        self.assertEqual(ui.next_occurrence(once, MONDAY_0700), datetime(2026, 9, 29, 6, 0))

    def test_disabled_is_ignored(self):
        self.assertIsNone(ui.next_occurrence(alarm(enabled=0), MONDAY_0700))
        chosen, when = ui.next_alarm([alarm(enabled=0), alarm(time="08:00", name="Otra")],
                                     MONDAY_0700)
        self.assertEqual((chosen["name"], when), ("Otra", datetime(2026, 9, 28, 8, 0)))
        self.assertEqual(ui.next_alarm([], MONDAY_0700), (None, None))

    def test_texts(self):
        self.assertEqual(ui.describe_day(datetime(2026, 9, 28, 7, 30), MONDAY_0700), "Hoy")
        self.assertEqual(ui.describe_day(datetime(2026, 9, 29, 7, 30), MONDAY_0700), "Mañana")
        self.assertEqual(ui.describe_day(datetime(2026, 10, 1, 7, 30), MONDAY_0700), "El jueves")
        self.assertEqual(ui.describe_countdown(datetime(2026, 9, 28, 7, 30), MONDAY_0700),
                         "Suena en 30 min")
        self.assertEqual(ui.describe_countdown(datetime(2026, 9, 28, 16, 12), MONDAY_0700),
                         "Suena en 9 h 12 min")
        self.assertEqual(ui.fade_label(0), "Sin subida: directo al volumen final")
        self.assertEqual(ui.fade_label(5), "Sube durante 5 minutos")

    def test_sun_height_bounds(self):
        self.assertEqual(ui.sun_height(MONDAY_0700, MONDAY_0700), 1.0)
        self.assertEqual(ui.sun_height(datetime(2026, 10, 5, 7, 0), MONDAY_0700), 0.0)


class UiRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        spotify = mock.Mock(spec=SpotifyClient)
        spotify.is_configured = False
        spotify.redirect_uri = "http://127.0.0.1:5000/spotify/callback"
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "t", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "a.log")},
            player=mock.Mock(spec=AudioPlayer), spotify=spotify)
        self.client = self.app.test_client()

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def html(self, url="/"):
        return self.client.get(url).get_data(as_text=True)

    def create(self, **overrides):
        data = {"name": "Trabajo", "time": "07:30", "days": ["0", "1", "2", "3", "4"]}
        data.update(overrides)
        self.client.post("/alarms/new", data=data)

    # --- Plantilla base / PWA ---

    def test_base_has_pwa_and_mobile_meta(self):
        html = self.html()
        for snippet in ('<link rel="manifest" href="/manifest.webmanifest">',
                        '<meta name="theme-color" content="#0f1830">',
                        'rel="apple-touch-icon"', 'apple-mobile-web-app-capable',
                        'viewport-fit=cover', "<title>Alarmas · Groove</title>"):
            self.assertIn(snippet, html)

    def test_navigation_marks_current_section(self):
        home = self.html("/")
        self.assertIn('class="nav nav-bottom"', home)
        self.assertRegex(home, r'href="/"\s+aria-current="page"')
        spotify = self.html("/spotify/")
        self.assertRegex(spotify, r'href="/spotify/"\s+aria-current="page"')
        self.assertNotIn("Ajustes", home)  # no se inventan secciones que no existen

    def test_offline_banner_and_polling_marker_on_every_page(self):
        for url in ("/", "/spotify/", "/alarms/new"):
            with self.subTest(url=url):
                html = self.html(url)
                self.assertIn("data-offline-banner hidden", html)
                self.assertIn("data-playback-key=", html)

    # --- Pantalla principal ---

    def test_next_alarm_card(self):
        self.assertNotIn("Próxima alarma", self.html())
        self.create()
        html = self.html()
        self.assertIn("Próxima alarma", html)
        self.assertIn('class="next-time">07:30<', html)
        self.assertIn("Suena en", html)

    def test_disabled_alarm_is_not_next(self):
        self.create()
        self.client.post("/alarms/1/toggle")
        self.assertNotIn("Próxima alarma", self.html())

    def test_alarm_card_actions_and_switch(self):
        self.create()
        html = self.html()
        self.assertIn('role="switch"', html)
        self.assertIn('aria-checked="true"', html)
        self.assertIn('<details class="menu">', html)
        for action in ("/alarms/1/edit", "/alarms/1/test", "/alarms/1/delete"):
            self.assertIn(action, html)
        self.assertIn('class="fab" href="/alarms/new"', html)

    def test_empty_state_invites_to_create(self):
        html = self.html()
        self.assertIn("No hay alarmas todavía", html)
        self.assertIn("/alarms/new", html)

    # --- Formulario ---

    def test_form_day_chips_and_segmented_source(self):
        html = self.html("/alarms/new")
        letters = re.findall(r'<span aria-hidden="true">([A-Z])</span>', html)
        self.assertEqual(letters, ["L", "M", "X", "J", "V", "S", "D"])
        self.assertIn('<span class="visually-hidden">miércoles</span>', html)
        self.assertIn('class="segmented"', html)
        self.assertIn("data-spotify-field", html)
        self.assertIn("Sube durante 5 minutos", html)

    def test_form_still_works_without_js(self):
        # Las operaciones esenciales son formularios normales (POST + redirección).
        self.create(source="spotify", spotify_uri="spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
                    volume_start="10", volume_end="50", fade_minutes="3")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT source, volume_start, fade_minutes FROM alarms").fetchone()
        conn.close()
        self.assertEqual(row, ("spotify", 10, 3))

    # --- Manifest y service worker ---

    def test_manifest(self):
        resp = self.client.get("/manifest.webmanifest")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/manifest+json", resp.content_type)
        data = json.loads(resp.get_data(as_text=True))
        resp.close()
        self.assertEqual((data["name"], data["short_name"]), ("Groove", "Groove"))
        self.assertEqual((data["display"], data["start_url"], data["scope"]),
                         ("standalone", "/", "/"))
        self.assertEqual(data["theme_color"], "#0f1830")
        self.assertEqual(data["background_color"], "#0f1830")
        sizes = {icon["sizes"] for icon in data["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes)
        self.assertTrue(any(icon.get("purpose") == "maskable" for icon in data["icons"]))
        for icon in data["icons"]:
            with self.subTest(icon=icon["src"]):
                path = ROOT / icon["src"].lstrip("/")
                self.assertTrue(path.is_file())
                if icon["type"] == "image/png":
                    width, height = struct.unpack(">II", path.read_bytes()[16:24])
                    self.assertEqual(f"{width}x{height}", icon["sizes"])

    def test_service_worker_served_from_root_without_cache(self):
        resp = self.client.get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("javascript", resp.content_type)
        self.assertIn("no-cache", resp.headers["Cache-Control"])
        self.assertEqual(resp.headers["Service-Worker-Allowed"], "/")
        resp.close()

    def test_service_worker_only_caches_static_shell(self):
        source = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        shell = re.search(r"var SHELL = \[(.*?)\];", source, re.S).group(1)
        cached = re.findall(r'"([^"]+)"', shell)
        self.assertTrue(cached)
        for path in cached:
            with self.subTest(path=path):
                self.assertTrue(path.startswith("/static/"))
                self.assertTrue((ROOT / path.lstrip("/")).is_file())
        # Nada dinámico en la caché, y los POST nunca se interceptan.
        for dynamic in ("/playback", "/spotify", "/alarms", "callback"):
            self.assertNotIn(dynamic, shell)
        self.assertIn('request.method !== "GET"', source)

    def test_offline_page_is_honest(self):
        html = (ROOT / "static" / "offline.html").read_text(encoding="utf-8")
        self.assertIn("No hay conexión con Groove", html)
        resp = self.client.get("/static/offline.html")
        self.assertEqual(resp.status_code, 200)
        resp.close()

    # --- Sin dependencias externas ni estilos inline ---

    def test_no_external_resources_or_inline_styles(self):
        files = list((ROOT / "templates").glob("*.html")) + [
            ROOT / "static" / "style.css", ROOT / "static" / "app.js",
            ROOT / "static" / "sw.js", ROOT / "static" / "offline.html"]
        for path in files:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(src|href)=\"https?://")
                self.assertNotRegex(text, r"@import|fonts\.googleapis|cdn\.")
                if path.suffix == ".html":
                    self.assertNotIn(' style="', text)


if __name__ == "__main__":
    unittest.main()
