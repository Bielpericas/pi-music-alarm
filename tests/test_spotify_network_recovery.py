"""Router tardío y reconexión: API, reloj y systemd simulados."""
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import db
from spotify_client import SpotifyAuthError, SpotifyConnectionError, SpotifyError, SpotifyRateLimitError
from spotify_guest import NoSpotifyGuest
from spotify_recovery import SpotifyNetworkRecovery


DEVICE = dict(id="dev", name="Groove", is_active=False)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = str(Path(temp.name) / "db")
        db.init_db(self.database)
        db.write_setting(self.database, "spotify_device_name", "Groove")
        self.client = Mock()
        self.client.is_configured = True
        self.client.is_connected.return_value = True
        self.client.get_devices.return_value = []
        self.playback = SimpleNamespace(active=None, _lock=threading.RLock())
        self.player = SimpleNamespace(service_lock=threading.RLock())
        self.guests = NoSpotifyGuest()
        self.now = 0
        self.state = "active"
        self.enabled_state = "enabled"
        self.commands = []
        self.restart_code = 0
        self.recovery = SpotifyNetworkRecovery(self.database, self.client, self.playback,
                                               self.player, self.guests, platform="linux",
                                               run=self.command, clock=lambda: self.now)

    def command(self, command, **kwargs):
        self.commands.append(command)
        if command[2] == "is-active":
            stdout, code = self.state, 0 if self.state == "active" else 3
        elif command[2] == "is-enabled":
            stdout, code = self.enabled_state, 0
        else:
            stdout, code = "", self.restart_code
        return subprocess.CompletedProcess(command, code, stdout, "")

    def check(self, seconds=30):
        self.now += seconds
        self.recovery.check()

    def restarts(self):
        return [c for c in self.commands if c[2] == "restart"]

    def test_router_late_only_restarts_after_two_successful_api_checks(self):
        self.client.get_devices.side_effect = SpotifyConnectionError("router off")
        self.check()
        self.check()
        self.assertEqual(self.commands, [])
        self.client.get_devices.side_effect = None
        self.check()
        self.assertEqual(self.restarts(), [])
        self.check()
        self.assertEqual(self.restarts(), [["systemctl", "--no-ask-password", "restart", "raspotify.service"]])
        self.client.get_devices.return_value = [DEVICE]
        self.check()
        self.assertIn("disponible", self.recovery.last_status)
        self.check()
        self.assertEqual(len(self.restarts()), 1)

    def test_already_online_and_visible_never_restarts(self):
        self.client.get_devices.return_value = [DEVICE]
        for _ in range(4):
            self.check()
        self.assertEqual(self.restarts(), [])

    def test_startup_missing_device_recovers_even_if_offline_boot_was_not_observed(self):
        self.check()
        self.check()
        self.assertEqual(len(self.restarts()), 1)

    def test_failed_and_inactive_service_after_boot_can_recover(self):
        for state in ("failed", "inactive"):
            with self.subTest(state=state):
                self.recovery._attempts = 0
                self.recovery._next_attempt = 0
                self.state = state
                self.check()
                self.check()
                self.assertTrue(self.restarts())

    def test_retry_limit_cooldown_and_permission_failure(self):
        self.restart_code = 1
        self.check()
        self.check()
        self.assertEqual(len(self.restarts()), 1)
        self.check()
        self.assertEqual(len(self.restarts()), 1)
        for _ in range(10):
            self.check()
        self.assertEqual(len(self.restarts()), 3)
        self.assertIn("Agotados", self.recovery.last_status)

    def test_later_real_outage_can_arm_a_new_episode(self):
        self.client.get_devices.return_value = [DEVICE]
        self.check()
        self.client.get_devices.return_value = []
        self.check()
        self.check()
        self.assertEqual(self.restarts(), [])  # ausencia aislada posterior no amplía autorreparación
        self.client.get_devices.side_effect = SpotifyConnectionError("offline")
        self.check()
        self.client.get_devices.side_effect = None
        self.check()
        self.check()
        self.assertEqual(len(self.restarts()), 1)

    def test_alarm_active_delays_recovery(self):
        self.playback.active = object()
        self.check()
        self.check()
        self.assertEqual(self.restarts(), [])
        self.playback.active = None
        self.check()
        self.assertEqual(len(self.restarts()), 1)

    def test_guests_and_unknown_identity_never_restart(self):
        for enabled in (True, None):
            with self.subTest(enabled=enabled):
                self.recovery.guests = SimpleNamespace(available=True, _lock=threading.Lock(),
                    _observe=lambda: dict(enabled=enabled, active=True))
                self.check()
                self.check()
                self.assertEqual(self.restarts(), [])

    def test_guest_change_in_progress_never_restart(self):
        lock = threading.Lock()
        lock.acquire()
        self.recovery.guests = SimpleNamespace(available=True, _lock=lock)
        self.check()
        self.check()
        self.assertEqual(self.restarts(), [])
        lock.release()

    def test_alarm_start_or_preflight_holds_gate_without_blocking_recovery(self):
        for target, attribute in ((self.playback, "_lock"), (self.player, "service_lock")):
            with self.subTest(lock=attribute):
                lock = threading.Lock()
                setattr(target, attribute, lock)
                lock.acquire()
                try:
                    self.check()
                    self.check()
                    self.assertEqual(self.restarts(), [])
                finally:
                    lock.release()
        self.check()
        self.assertEqual(len(self.restarts()), 1)

    def test_auth_rate_limit_503_and_conflict_never_restart(self):
        for error in (SpotifyAuthError("auth"), SpotifyRateLimitError("wait", 60), SpotifyError("bad", 503)):
            self.client.get_devices.side_effect = error
            self.check()
            self.check()
        self.assertEqual(self.restarts(), [])
        self.client.get_devices.side_effect = None
        self.client.get_devices.return_value = [DEVICE, dict(DEVICE, id="other")]
        self.check()
        self.check()
        self.assertEqual(self.restarts(), [])

    def test_disabled_or_masked_service_is_respected(self):
        for state in ("disabled", "masked", "static"):
            self.enabled_state = state
            self.check()
            self.check()
            self.assertEqual(self.restarts(), [])

    def test_transitioning_service_is_not_restarted(self):
        self.state = "activating"
        self.check()
        self.check()
        self.assertEqual(self.restarts(), [])

    def test_windows_and_disabled_monitor_have_no_jobs_or_commands(self):
        for platform, enabled in (("win32", True), ("linux", False)):
            monitor = SpotifyNetworkRecovery(self.database, self.client, self.playback, self.player,
                                              self.guests, platform=platform, enabled=enabled,
                                              run=self.command)
            sched = Mock()
            monitor.start(sched)
            monitor.check()
            sched.add_job.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_scheduler_uses_one_bounded_job(self):
        scheduler = Mock()
        self.recovery.start(scheduler)
        kwargs = scheduler.add_job.call_args.kwargs
        self.assertEqual(kwargs["id"], "spotify-network-recovery")
        self.assertEqual(kwargs["max_instances"], 1)
        self.assertTrue(kwargs["coalesce"])


if __name__ == "__main__":
    unittest.main()
