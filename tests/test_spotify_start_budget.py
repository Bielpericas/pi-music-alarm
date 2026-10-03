"""Plazo global, DNS lento, cancelación y órdenes de reproducción inciertas."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import db
from audio_player import AudioPlayer
from playback import AlarmPlaybackManager, play_alarm_sound
from spotify_client import SpotifyClient, SpotifyConnectionError, SpotifyError
from spotify_guest import SpotifyGuest
from spotify_player import SpotifyAlarmPlayer
from spotify_transport import deadline_transport, resolve
from startup_budget import StartupBudget, StartupCancelled, StartupExpired, budget_lock, current_budget, run_budgeted


URI = "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
ALARM = dict(id=1, name="Prueba", time="07:00", source="spotify", spotify_uri=URI)
DEVICE = dict(id="dev", name="Groove", is_active=True)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BudgetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = str(self.root / "db")
        db.init_db(self.database)
        db.write_setting(self.database, "spotify_device_name", "Groove")
        self.clock = Clock()
        self.tokens = Mock()
        self.tokens.load.return_value = dict(access_token="AT", refresh_token="RT", expires_at=9e12)
        self.calls = []
        self.client = SpotifyClient("id", "secret", "redirect", self.tokens,
                                    transport=self.transport, sleep=self.clock.sleep)
        self.spotify = SpotifyAlarmPlayer(self.client, self.database, clock=self.clock,
                                          wait=lambda s: self.clock.sleep(s) or False)
        self.local = Mock(spec=AudioPlayer)
        self.local.play.return_value = True
        self.local.stop.return_value = True
        self.manager = AlarmPlaybackManager(self.local, self.spotify, schedule_once=Mock())

    def transport(self, method, url, headers, body, timeout):
        self.calls.append((method, url, timeout))
        if method == "GET":
            return 200, {}, json.dumps({"devices": [DEVICE]}).encode()
        return 204, {}, b""

    def test_normal_start_uses_spotify_without_fallback(self):
        self.assertEqual(self.manager.start(ALARM), "spotify")
        self.local.play.assert_not_called()

    def test_requests_share_one_deadline_including_refresh(self):
        self.tokens.load.return_value["expires_at"] = 0
        timeouts = []
        def transport(method, url, headers, body, timeout):
            timeouts.append(timeout)
            self.clock.now += timeout
            if "api/token" in url:
                return 200, {}, b'{"access_token":"new","expires_in":3600}'
            raise TimeoutError("network")
        self.client._transport = transport
        self.assertEqual(self.manager.start(ALARM), "fallback")
        self.assertLessEqual(self.clock.now, 20)
        self.assertEqual(timeouts[0], 10)
        self.local.play.assert_called_once()

    def test_metadata_and_backoffs_are_inside_deadline(self):
        alarm = dict(ALARM, spotify_uri="spotify:playlist:37i9dQZF1DXcBWIGoYBM5M")
        def transport(method, url, headers, body, timeout):
            self.calls.append((method, url, timeout))
            self.clock.now += timeout
            raise TimeoutError("slow response")
        self.client._transport = transport
        self.assertEqual(self.manager.start(alarm), "fallback")
        self.assertEqual(self.clock.now, 20)
        self.assertEqual([c[2] for c in self.calls], [10, 10])
        self.assertFalse(any(c[0] == "PUT" for c in self.calls))

    def test_short_429_wait_consumes_same_budget(self):
        def transport(method, url, headers, body, timeout):
            self.clock.now += 8
            return 429, {"retry-after": "5"}, b""
        self.client._transport = transport
        self.assertEqual(self.manager.start(ALARM), "fallback")
        self.assertGreaterEqual(self.clock.now, 20)
        self.assertEqual(self.clock.now, 21)  # doble inyectado excede su timeout; no más órdenes

    def test_late_read_response_cannot_issue_play(self):
        self.client._transport = Mock(side_effect=lambda *args: (
            self.clock.sleep(25) or (200, {}, json.dumps({"devices": [DEVICE]}).encode())))
        self.assertEqual(self.manager.start(ALARM), "fallback")
        self.assertEqual(self.client._transport.call_count, 1)

    def test_cancel_after_play_sent_confirms_pause_before_returning(self):
        def transport(method, url, headers, body, timeout):
            self.calls.append((method, url, timeout))
            if "/me/player/play?" in url:
                self.spotify.interrupt()
            return (200, {}, json.dumps({"devices": [DEVICE]}).encode()) if method == "GET" else (204, {}, b"")
        self.client._transport = transport
        self.assertFalse(self.spotify.play(URI))
        self.assertTrue(any("/pause?" in c[1] for c in self.calls))
        self.assertFalse(self.spotify.start_stop_pending)

    def test_uncertain_play_and_failed_pause_keeps_stop_pending_without_local_overlap(self):
        def transport(method, url, headers, body, timeout):
            self.calls.append((method, url, timeout))
            if "/me/player/play?" in url:
                self.clock.now = 20
                raise TimeoutError("play acknowledgement lost")
            if "/pause?" in url:
                self.clock.now += timeout
                raise TimeoutError("pause unavailable")
            return (200, {}, json.dumps({"devices": [DEVICE]}).encode()) if method == "GET" else (204, {}, b"")
        self.client._transport = transport
        self.assertEqual(self.manager.start(ALARM), "stop_pending")
        self.assertEqual(self.manager.active.via, "spotify")
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.local.play.assert_not_called()
        self.assertEqual(self.clock.now, 22)  # 20 s arranque + hasta 2 s de pausa de limpieza
        self.manager.schedule_once.assert_called_once()
        self.client._transport = self.transport
        self.assertTrue(self.manager.stop().silenced)

    def test_stop_already_requested_before_play_never_starts_spotify_or_local(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(play_alarm_sound(ALARM, self.local, self.spotify, interrupted=stop), "cancelled")
        self.assertEqual(self.calls, [])
        self.local.play.assert_not_called()

    def test_rejected_retry_does_not_forget_earlier_uncertain_play(self):
        self.spotify.client = Mock()
        self.spotify.client.play.side_effect = [SpotifyConnectionError("lost reply"),
                                               SpotifyError("missing device", 404)]
        with StartupBudget(20, clock=self.clock).activate():
            for _ in range(2):
                with self.assertRaises(SpotifyError):
                    self.spotify._send_play("dev", URI)
        self.spotify._silence_uncertain_start()
        self.spotify.client.pause.assert_called_once_with("dev")

    def test_uncertain_start_never_schedules_fade(self):
        self.spotify.start_stop_pending = True
        self.manager.fader = Mock()
        with patch.object(self.spotify, "play_interruptible", return_value=False):
            alarm = dict(ALARM, volume_start=20, volume_end=80, fade_minutes=5)
            self.assertEqual(self.manager.start(alarm), "stop_pending")
        self.manager.fader.assert_not_called()

    def test_budget_does_not_affect_next_request_or_other_thread(self):
        observed = []
        with StartupBudget(20, clock=self.clock).activate():
            worker = threading.Thread(target=lambda: observed.append(current_budget()))
            worker.start()
            worker.join(1)
        self.assertEqual(observed, [None])
        self.assertIsNone(current_budget())
        self.client.get_devices()
        self.assertEqual(self.calls[-1][2], 10)

    def test_token_lock_wait_is_inside_deadline(self):
        self.client._lock.acquire()
        try:
            started = time.monotonic()
            with StartupBudget(0.05).activate(), self.assertRaises(StartupExpired):
                self.client.get_devices()
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            self.client._lock.release()

    def test_guest_lock_wait_is_inside_deadline(self):
        guests = SpotifyGuest(self.database, run=Mock())
        guests._lock.acquire()
        try:
            with StartupBudget(0.05).activate(), self.assertRaises(StartupExpired):
                guests.before_alarm()
            guests.run.assert_not_called()
        finally:
            guests._lock.release()

    def test_guest_commands_use_remaining_budget_not_200_seconds(self):
        path = self.root / "status.json"
        timeouts = []
        def helper(command, **kwargs):
            timeouts.append(kwargs["timeout"])
            self.clock.now += 4
            path.write_text(json.dumps(dict(enabled=False, active=True, error=None)))
            return subprocess.CompletedProcess(command, 0, "", "")
        guests = SpotifyGuest(self.database, run=helper, status_path=path)
        with StartupBudget(10, clock=self.clock).activate():
            guests.before_alarm()
        self.assertEqual(timeouts, [10])
        self.assertEqual(self.clock.now, 4)

    def test_guest_change_then_observation_share_budget(self):
        path = self.root / "status.json"
        timeouts = []
        def helper(command, **kwargs):
            timeouts.append(kwargs["timeout"])
            self.clock.now += 3
            path.write_text(json.dumps(dict(enabled=len(timeouts) == 1, active=True, error=None)))
            return subprocess.CompletedProcess(command, 0, "", "")
        guests = SpotifyGuest(self.database, run=helper, status_path=path)
        with StartupBudget(10, clock=self.clock).activate():
            guests.before_alarm()
        self.assertEqual(timeouts, [10, 7, 4])

    def test_blocked_dns_is_bounded_and_never_sends_a_request_after_deadline(self):
        release = threading.Event()
        entered = threading.Event()
        def dns(*args, **kwargs):
            entered.set()
            release.wait(2)
            return []
        try:
            with patch("spotify_transport.socket.getaddrinfo", side_effect=dns), \
                    patch("spotify_transport.socket.socket") as socket:
                started = time.monotonic()
                with self.assertRaises(StartupExpired):
                    resolve("blocked.example.test", 443, StartupBudget(0.05))
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - started, 0.5)
                socket.assert_not_called()
        finally:
            release.set()

    def test_cancelled_budget_never_acquires_lock(self):
        cancelled = threading.Event()
        cancelled.set()
        lock = threading.Lock()
        with StartupBudget(10, cancelled).activate(), self.assertRaises(StartupCancelled):
            with budget_lock(lock):
                self.fail("must not enter")
        self.assertFalse(lock.locked())

    def test_stop_cancels_and_reaps_waiting_subprocess_client(self):
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        self.addCleanup(timer.cancel)
        timer.start()
        started = time.monotonic()
        with StartupBudget(20, cancelled).activate(), self.assertRaises(StartupCancelled):
            run_budgeted([sys.executable, "-c", "import time; time.sleep(5)"],
                         subprocess.run, 200, capture_output=True, text=True)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_blocked_headers_are_interrupted_by_absolute_deadline(self):
        stopped = threading.Event()
        connection = Mock()
        connection.sock.shutdown.side_effect = lambda *args: stopped.set()
        def response():
            if stopped.wait(1):
                raise OSError("socket interrupted")
            self.fail("deadline failed to interrupt headers")
        connection.getresponse.side_effect = response
        started = time.monotonic()
        with patch("spotify_transport.DeadlineHTTPSConnection", return_value=connection):
            with self.assertRaises(OSError):
                deadline_transport("GET", "https://api.spotify.com/v1/me/player/devices",
                                   {}, None, 10, StartupBudget(0.05))
        self.assertLess(time.monotonic() - started, 0.5)
        connection.close.assert_called_once()

    def test_stop_interrupts_in_progress_http_before_returning(self):
        cancelled = threading.Event()
        stopped = threading.Event()
        connection = Mock()
        connection.sock.shutdown.side_effect = lambda *args: stopped.set()
        def response():
            cancelled.set()
            if stopped.wait(1):
                raise OSError("socket interrupted")
            self.fail("STOP failed to interrupt request")
        connection.getresponse.side_effect = response
        with patch("spotify_transport.DeadlineHTTPSConnection", return_value=connection):
            with self.assertRaises(OSError):
                deadline_transport("PUT", "https://api.spotify.com/v1/me/player/play",
                                   {}, b"{}", 10, StartupBudget(20, cancelled))
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_trickled_body_cannot_extend_absolute_deadline(self):
        stopped = threading.Event()
        connection = Mock()
        connection.sock.shutdown.side_effect = lambda *args: stopped.set()
        def read(size):
            if stopped.wait(0.01):
                raise OSError("socket interrupted")
            return b"x"
        connection.getresponse.return_value.read1.side_effect = read
        started = time.monotonic()
        with patch("spotify_transport.DeadlineHTTPSConnection", return_value=connection):
            with self.assertRaises(OSError):
                deadline_transport("GET", "https://api.spotify.com/v1/me/player/devices",
                                   {}, None, 10, StartupBudget(0.05))
        self.assertLess(time.monotonic() - started, 0.5)
        connection.close.assert_called_once()

    def test_connection_close_body_retains_socket_for_deadline_guard(self):
        stopped = threading.Event()
        connection = Mock()
        sock = connection.sock
        sock.shutdown.side_effect = lambda *args: stopped.set()
        response = Mock()
        def headers():
            connection.sock = None  # HTTPConnection separa el socket del cuerpo
            return response
        def read(size):
            if stopped.wait(1):
                raise OSError("body interrupted")
            self.fail("detached socket escaped the deadline")
        connection.getresponse.side_effect = headers
        response.read1.side_effect = read
        with patch("spotify_transport.DeadlineHTTPSConnection", return_value=connection):
            with self.assertRaises(OSError):
                deadline_transport("GET", "https://api.spotify.com/v1/me/player/devices",
                                   {}, None, 10, StartupBudget(0.05))
        response.close.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
