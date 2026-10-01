"""Guest-mode persistence, HTTP validation and alarm integration, without systemd."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import db
import scheduler
from app import create_app
from audio_player import AudioPlayer
from spotify_client import SpotifyClient
from spotify_guest import GuestModeError, SETTING, SpotifyGuest, create_spotify_guest
from tests.test_bluetooth_manager import FakeBluetoothctl, PHONE, TABLET, no_real_processes
from tests.test_sleep_timer import bluez, Clock, FakeScheduler


class FakeHelper:
    def __init__(self, path):
        self.path = path
        self.enabled = False
        self.active = True
        self.calls = []
        self.failure = None
        self.unconfirmed = False

    def __call__(self, command, **kwargs):
        mode = command[-1].removeprefix("groove-spotify-mode@").removesuffix(".service")
        self.calls.append(mode)
        if mode == self.failure:
            return subprocess.CompletedProcess(command, 1, "COMMAND-SECRET", "COMMAND-SECRET")
        if mode != "status":
            self.enabled = mode == "guest"
        self.path.write_text(json.dumps({"enabled": None if self.unconfirmed else self.enabled,
                                         "active": self.active, "error": None,
                                         "username": "DO_NOT_EXPOSE", "token": "DO_NOT_EXPOSE"}))
        return subprocess.CompletedProcess(command, 0, "", "")


class GuestFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = str(Path(self.tmp.name) / "db")
        db.init_db(self.database)
        self.path = Path(self.tmp.name) / "status.json"
        self.helper = FakeHelper(self.path)
        self.manager = SpotifyGuest(self.database, run=self.helper, status_path=self.path)


class GuestManagerTest(GuestFixture):
    def test_initial_and_missing_state_are_private(self):
        self.manager.reconcile()
        self.assertEqual(self.helper.calls, ["private", "status"])
        self.assertFalse(self.manager.status()["enabled"])

    def test_enable_disable_and_restart_restore_boolean(self):
        self.manager.set_enabled(True)
        self.assertEqual(db.read_setting(self.database, SETTING), "true")
        self.helper.enabled = False  # safe boot guard runs before Groove
        self.manager.reconcile()
        self.assertTrue(self.manager.status()["enabled"])
        self.manager.set_enabled(False)
        self.assertEqual(db.read_setting(self.database, SETTING), "false")
        self.manager.reconcile()
        self.assertFalse(self.helper.enabled)

    def test_invalid_saved_values_never_enable_guests(self):
        for invalid in ("True", "1", "yes", "{", "null", "", "false"):
            with self.subTest(invalid=invalid):
                db.write_setting(self.database, SETTING, invalid)
                self.manager.reconcile()
                self.assertFalse(self.helper.enabled)

    def test_state_read_error_falls_back_private_without_logging_exception(self):
        with mock.patch.object(db, "read_setting", side_effect=RuntimeError("SECRET")):
            self.manager.reconcile()
        self.assertFalse(self.helper.enabled)

    def test_failed_guest_start_falls_back_private_at_startup(self):
        db.write_setting(self.database, SETTING, "true")
        self.helper.failure = "guest"
        with self.assertLogs("spotify_guest", "WARNING") as logs:
            self.manager.reconcile()
        self.assertFalse(self.helper.enabled)
        self.assertEqual(db.read_setting(self.database, SETTING), "false")
        self.assertNotIn("SECRET", "".join(logs.output))

    def test_failed_change_never_persists_requested_mode_or_exposes_output(self):
        db.write_setting(self.database, SETTING, "false")
        self.helper.failure = "guest"
        with self.assertRaises(GuestModeError) as caught:
            self.manager.set_enabled(True)
        self.assertEqual(db.read_setting(self.database, SETTING), "false")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_persistence_failure_rolls_back_live_mode(self):
        with mock.patch.object(db, "write_setting", side_effect=RuntimeError("SECRET")):
            with self.assertRaises(GuestModeError):
                self.manager.set_enabled(True)
        self.assertFalse(self.helper.enabled)

    def test_unconfirmed_status_does_not_persist_success(self):
        self.helper.unconfirmed = True
        with self.assertRaises(GuestModeError):
            self.manager.set_enabled(True)
        self.assertIsNone(db.read_setting(self.database, SETTING))

    def test_status_whitelists_fields(self):
        self.assertEqual(set(self.manager.status()), {"available", "enabled", "active", "busy"})

    def test_malformed_status_is_unknown_not_private(self):
        for content in ('{"enabled": "false", "active": true}', '[]', '{}', '{'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with mock.patch.object(self.manager, "_action"):
                    self.assertIsNone(self.manager.status()["enabled"])

    def test_concurrent_changes_are_rejected(self):
        self.manager._lock.acquire()
        self.addCleanup(self.manager._lock.release)
        self.assertTrue(self.manager.status()["busy"])
        with self.assertRaises(GuestModeError):
            self.manager.set_enabled(True)

    def test_strict_boolean_and_fixed_unit_names(self):
        for invalid in (1, 0, "true", None, [], {}):
            with self.assertRaises(ValueError):
                self.manager.set_enabled(invalid)
        commands = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.manager.run = commands
        self.manager._action("guest")
        self.assertEqual(commands.call_args.args[0], ["systemctl", "--no-ask-password", "start",
                                                      "groove-spotify-mode@guest.service"])

    def test_command_timeout_yields_unknown_without_leaking(self):
        self.manager.run = mock.Mock(side_effect=subprocess.TimeoutExpired("SECRET", 200))
        self.assertIsNone(self.manager.status()["enabled"])

    def test_private_recovery_can_proceed_after_failed_observation(self):
        self.helper.enabled = True
        with mock.patch.object(self.manager, "_observe", side_effect=[GuestModeError("unknown"),
                               {"enabled": False, "active": True}]):
            self.manager.set_enabled(False)
        self.assertFalse(self.helper.enabled)

    def test_unreadable_database_at_app_init_still_attempts_private(self):
        self.helper.enabled = True
        with mock.patch.object(db, "init_app", side_effect=RuntimeError("damaged DB")), \
                mock.patch.object(db, "read_setting", side_effect=RuntimeError("damaged DB")):
            with self.assertRaises(RuntimeError):
                create_app({"TESTING": True}, spotify_guest=self.manager)
        self.assertFalse(self.helper.enabled)

    def test_alarm_reclaims_primary_and_persists_private(self):
        self.manager.set_enabled(True)
        self.manager.before_alarm()
        self.assertFalse(self.helper.enabled)
        self.assertEqual(db.read_setting(self.database, SETTING), "false")
        self.helper.calls.clear()
        self.manager.before_alarm()
        self.assertEqual(self.helper.calls, ["status"])  # no restart in normal private mode

    def test_windows_and_custom_services_never_launch_helper(self):
        for platform, service in (("win32", "raspotify"), ("linux", "none"), ("linux", "other")):
            with mock.patch("spotify_guest.sys.platform", platform), \
                    mock.patch("spotify_guest.HELPER", mock.Mock(is_file=lambda: True)):
                self.assertFalse(create_spotify_guest({"RASPOTIFY_SERVICE": service}).available)


class GuestViewsTest(GuestFixture):
    def setUp(self):
        super().setUp()
        self.spotify = mock.Mock(spec=SpotifyClient)
        self.spotify.is_configured = False
        self.spotify.redirect_uri = "http://localhost/spotify/callback"
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": self.database,
                               "ALARM_LOG": str(Path(self.tmp.name) / "alarms.log")},
                              player=mock.Mock(spec=AudioPlayer), spotify=self.spotify,
                              spotify_guest=self.manager)
        self.addCleanup(scheduler.close_logging)
        self.client = self.app.test_client()
        self.client.get("/spotify/")
        with self.client.session_transaction() as session:
            self.csrf = session["spotify_guest_csrf"]

    def test_switch_uses_actual_backend_even_if_saved_state_differs(self):
        db.write_setting(self.database, SETTING, "false")
        self.helper.enabled = True
        html = self.client.get("/spotify/").get_data(as_text=True)
        self.assertIn('aria-checked="true"', html)
        self.assertIn("Las cuentas Spotify de esta Wi-Fi", html)
        self.assertNotIn("DO_NOT_EXPOSE", html)

    def test_post_changes_mode_and_redirects(self):
        response = self.client.post("/spotify/guest", data={"csrf": self.csrf, "enabled": "true"},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-checked="true"', response.get_data(as_text=True))
        self.assertEqual(db.read_setting(self.database, SETTING), "true")

    def test_guest_changes_leave_bluetooth_connections_and_sleep_timer_untouched(self):
        no_real_processes(self)
        ctl = FakeBluetoothctl()
        ctl.add(PHONE, "Móvil", connected=True)
        ctl.add(TABLET, "Tablet")
        bt = bluez(ctl)
        self.app.extensions["bluetooth_manager"] = bt
        sleep = self.app.extensions["sleep_timer"]
        sleep.bluetooth = bt
        sleep._clock = clock = Clock()
        sleep.schedule_once = jobs = FakeScheduler()
        timer = sleep.start("bluetooth", 15, mac=PHONE)
        for enabled in ("true", "false"):
            with self.subTest(enabled=enabled):
                ctl.calls.clear()
                self.client.post("/spotify/guest", data={"csrf": self.csrf, "enabled": enabled})
                self.assertEqual(ctl.calls, [])
                self.assertIs(sleep.timer, timer)
                self.client.post(f"/bluetooth/devices/{TABLET}/connect")
                self.client.get("/bluetooth/")
                self.assertTrue(ctl.devices[PHONE]["connected"])
                self.assertTrue(ctl.devices[TABLET]["connected"])
                self.assertFalse([c for c in ctl.calls if c[1] == "disconnect"])
        clock.advance(minutes=15)
        self.assertEqual(jobs.fire(), "expired")
        self.assertFalse(ctl.devices[PHONE]["connected"])
        self.assertTrue(ctl.devices[TABLET]["connected"])

    def test_invalid_input_or_csrf_never_touches_helper(self):
        for data in ({"enabled": "true"}, {"csrf": "evil", "enabled": "true"},
                     {"csrf": self.csrf, "enabled": "yes"},
                     {"csrf": self.csrf, "enabled": "true", "path": "/tmp"},
                     {"csrf": self.csrf, "enabled": ["true", "false"]}):
            with self.subTest(data=data):
                self.helper.calls.clear()
                self.assertEqual(self.client.post("/spotify/guest", data=data).status_code, 400)
                self.assertEqual(self.helper.calls, [])

    def test_api_no_store_no_secret_and_unknown_hides_switch(self):
        response = self.client.get("/spotify/guest/status")
        self.assertTrue(response.cache_control.no_store)
        self.assertNotIn("DO_NOT_EXPOSE", response.get_data(as_text=True))
        self.helper.unconfirmed = True
        html = self.client.get("/spotify/").get_data(as_text=True)
        self.assertNotIn('role="switch"', html)
        self.assertIn("Volver a privado", html)

    def test_change_failure_shows_observed_rollback_and_safe_message(self):
        self.helper.failure = "guest"
        html = self.client.post("/spotify/guest", data={"csrf": self.csrf, "enabled": "true"},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn('aria-checked="false"', html)
        self.assertIn("No se pudo cambiar", html)
        self.assertNotIn("COMMAND-SECRET", html)

    def test_active_spotify_alarm_blocks_toggle(self):
        with mock.patch.object(type(self.app.extensions["playback"]), "active",
                               new_callable=mock.PropertyMock,
                               return_value=mock.Mock(alarm={"source": "spotify"})):
            self.helper.calls.clear()
            self.client.post("/spotify/guest", data={"csrf": self.csrf, "enabled": "true"})
            self.assertEqual(self.helper.calls, [])

    def test_alarm_hook_runs_before_spotify_and_failure_preserves_fallback(self):
        self.helper.enabled = True
        self.spotify.is_configured = True
        alarm = self.app.extensions["spotify_alarm"]
        alarm._start = mock.Mock(return_value="dev")
        self.assertTrue(alarm.play("spotify:track:example"))
        self.assertFalse(self.helper.enabled)
        self.helper.enabled = True
        self.helper.failure = "private"
        alarm._start.reset_mock()
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(alarm.play("spotify:track:example"))
        alarm._start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
