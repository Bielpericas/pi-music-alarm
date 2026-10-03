"""Transiciones de audio, control conservado y restauración acotada."""
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import scheduler
from app import create_app
from audio_player import AudioPlayer, FfmpegPlayer
from bluetooth_audio import BluetoothAudio
from playback import AlarmPlaybackManager
from sleep_timer import SleepTimerManager
from spotify_client import SpotifyClient, SpotifyConnectionError, SpotifyError
from tests.test_bluetooth import FakeSystemctl

LOCAL = dict(id=1, name="Primera", time="07:00", source="local", spotify_uri=None)
SPOTIFY = dict(LOCAL, id=2, name="Segunda", source="spotify", spotify_uri="spotify:track:x")


class TransitionsTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.wav = Mock(spec=AudioPlayer)
        self.wav.play.side_effect = lambda: self.events.append("local-play") or True
        self.wav.stop.side_effect = lambda: self.events.append("local-stop") or True
        self.spotify = Mock()
        self.spotify.play.side_effect = lambda *a, **k: self.events.append("spotify-play") or True
        self.spotify.stop.side_effect = lambda: self.events.append("spotify-stop") or True
        self.client = Mock(spec=SpotifyClient)
        self.client.play.side_effect = lambda *a: self.events.append("manual-play")
        self.client.pause.side_effect = lambda *a: self.events.append("manual-pause")
        self.ctl = FakeSystemctl()
        self.bt = BluetoothAudio(run=self.ctl)
        self.jobs = []
        self.manager = AlarmPlaybackManager(self.wav, self.spotify, bluetooth=self.bt,
            manual_client=self.client, schedule_once=self.schedule)

    def schedule(self, when, callback):
        cancel = Mock()
        self.jobs.append((callback, cancel))
        return cancel

    def test_spotify_to_local_pauses_before_opening_local(self):
        self.manager.start(SPOTIFY)
        self.events.clear()
        self.manager.start(LOCAL)
        self.assertEqual(self.events, ["spotify-stop", "local-play"])
        self.assertEqual(self.ctl.verbs, ["stop"])

    def test_local_to_spotify_stops_before_play(self):
        self.manager.start(LOCAL)
        self.events.clear()
        self.manager.start(SPOTIFY)
        self.assertEqual(self.events, ["local-stop", "spotify-play"])

    def test_failed_local_replacement_retains_old_alarm(self):
        self.manager.start(LOCAL)
        self.wav.stop.side_effect = None
        self.wav.stop.return_value = False
        self.assertEqual(self.manager.start(SPOTIFY), "stop_pending")
        self.assertEqual(self.manager.active.id, 1)
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.spotify.play.assert_not_called()
        self.assertEqual(self.ctl.state, "inactive")
        self.wav.stop.return_value = True
        self.jobs[-1][0]()
        self.assertIsNone(self.manager.active)
        self.assertEqual(self.manager.start(SPOTIFY), "spotify")

    def test_failed_spotify_replacement_prevents_local_overlap(self):
        self.manager.start(SPOTIFY)
        self.spotify.stop.side_effect = SpotifyConnectionError("offline")
        self.assertEqual(self.manager.start(LOCAL), "stop_pending")
        self.wav.play.assert_not_called()
        self.assertEqual(self.manager.active.id, 2)

    def test_failed_bluetooth_stop_defers_and_can_recover(self):
        self.ctl.fail.add("stop")
        self.assertEqual(self.manager.start(LOCAL), "stop_pending")
        self.assertEqual(self.manager.active.via, "bluetooth")
        self.wav.play.assert_not_called()
        self.assertFalse(self.bt.paused)
        self.ctl.fail.clear()
        self.jobs[-1][0]()
        self.assertIsNone(self.manager.active)
        self.assertEqual(self.manager.start(LOCAL), "local")

    def test_successful_command_with_service_still_running_is_not_pause(self):
        def lying(command, **kwargs):
            if command[2] == "stop":
                return subprocess.CompletedProcess(command, 0, "", "")
            return self.ctl(command, **kwargs)
        self.bt._run = lying
        self.assertEqual(self.manager.start(LOCAL), "stop_pending")
        self.assertFalse(self.bt.paused)

    def test_failed_restore_preserves_intent_and_recovers_automatically(self):
        self.manager.start(LOCAL)
        self.ctl.fail.add("start")
        self.assertTrue(self.manager.stop().silenced)
        self.assertTrue(self.bt.paused)
        self.assertTrue(self.bt.restore_pending)
        self.assertTrue(self.manager.bluetooth_restore_pending)
        self.ctl.fail.clear()
        self.jobs[-1][0]()
        self.assertEqual(self.ctl.state, "active")
        self.assertFalse(self.manager.bluetooth_restore_pending)
        self.assertFalse(self.bt.restore_pending)

    def test_restore_limit_duplicate_callbacks_and_manual_recovery(self):
        self.manager.start(LOCAL)
        self.ctl.fail.add("start")
        self.manager.stop()
        for _ in range(3):
            callback = self.jobs[-1][0]
            callback()
            callback()
        self.assertEqual(self.ctl.verbs.count("start"), 4)
        self.ctl.fail.clear()
        self.assertTrue(self.manager.restore_bluetooth())
        self.assertFalse(self.manager.bluetooth_restore_pending)

    def test_old_restore_cannot_start_bluetooth_during_new_alarm(self):
        self.manager.start(LOCAL)
        self.ctl.fail.add("start")
        self.manager.stop()
        callback = self.jobs[-1][0]
        self.ctl.fail.clear()
        self.manager.start(SPOTIFY)
        callback()
        self.assertEqual(self.ctl.state, "inactive")
        self.assertFalse(self.manager.restore_bluetooth())

    def test_originally_stopped_bluetooth_is_never_started(self):
        self.ctl.state = "inactive"
        self.manager.start(LOCAL)
        self.manager.stop()
        self.assertEqual(self.ctl.verbs, [])

    def test_failed_music_cleanup_never_opens_emergency_wav(self):
        music = self.manager.music = Mock()
        music.play.return_value = False
        music.stop.return_value = False
        self.assertEqual(self.manager.start(LOCAL), "stop_pending")
        self.wav.play.assert_not_called()
        music.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)

    def test_failed_wav_cleanup_retains_pending_local_control(self):
        self.wav.play.side_effect = None
        self.wav.play.return_value = False
        self.wav.stop.side_effect = None
        self.wav.stop.return_value = False
        self.assertEqual(self.manager.start(LOCAL), "stop_pending")
        self.assertEqual(self.manager.active.via, "local")
        self.wav.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)

    def test_acknowledged_start_with_failed_service_retains_restore_intent(self):
        self.manager.start(LOCAL)
        def lying(command, **kwargs):
            if command[2] == "start":
                return subprocess.CompletedProcess(command, 0, "", "")
            return self.ctl(command, **kwargs)
        self.bt._run = lying
        self.manager.stop()
        self.assertTrue(self.bt.restore_pending)
        self.assertTrue(self.bt.paused)
        self.assertTrue(self.manager.bluetooth_restore_pending)

    def test_manual_play_releases_bluetooth_and_pause_restores_it(self):
        self.manager.control_spotify("play", "dev")
        self.assertEqual(self.ctl.state, "inactive")
        self.manager.control_spotify("pause", "dev")
        self.assertEqual(self.ctl.state, "active")

    def test_manual_spotify_is_paused_before_alarm_opens_audio(self):
        self.manager.control_spotify("play", "dev")
        self.events.clear()
        self.manager.start(LOCAL)
        self.assertEqual(self.events, ["manual-pause", "local-play"])
        self.assertIsNone(self.manager.manual_spotify_device)

    def test_manual_pause_failure_defers_without_forgetting_target(self):
        self.manager.control_spotify("play", "dev")
        self.client.pause.side_effect = SpotifyConnectionError("offline")
        self.assertEqual(self.manager.start(LOCAL), "stop_pending")
        self.wav.play.assert_not_called()
        self.assertEqual(self.manager.active.via, "manual_spotify")
        self.assertEqual(self.manager.manual_spotify_device, "dev")
        self.client.pause.side_effect = None
        self.assertTrue(self.manager.stop().silenced)

    def test_all_manual_commands_refuse_during_alarm(self):
        self.manager.start(LOCAL)
        for action in ("play", "pause", "transfer"):
            with self.assertRaises(SpotifyError):
                self.manager.control_spotify(action, "dev")
        self.client.play.assert_not_called()
        self.client.pause.assert_not_called()
        self.client.transfer_playback.assert_not_called()

    def test_uncertain_manual_play_retains_target_until_pause(self):
        self.client.play.side_effect = SpotifyConnectionError("reply lost")
        with self.assertRaises(SpotifyError):
            self.manager.control_spotify("play", "dev")
        self.assertEqual(self.manager.manual_spotify_device, "dev")
        self.assertEqual(self.ctl.state, "inactive")
        self.manager.control_spotify("pause", "dev")
        self.assertEqual(self.ctl.state, "active")

    def test_known_manual_rejection_returns_bluetooth(self):
        self.client.play.side_effect = SpotifyError("rejected", 403)
        with self.assertRaises(SpotifyError):
            self.manager.control_spotify("play", "dev")
        self.assertIsNone(self.manager.manual_spotify_device)
        self.assertEqual(self.ctl.state, "active")

    def test_pause_other_device_never_releases_owned_bluetooth(self):
        self.manager.control_spotify("play", "dev")
        self.manager.control_spotify("pause", "other")
        self.assertEqual(self.ctl.state, "inactive")
        self.assertEqual(self.manager.manual_spotify_device, "dev")

    def test_sleep_timer_pause_returns_owned_bluetooth(self):
        self.manager.control_spotify("play", "dev")
        self.client.get_devices.return_value = [dict(id="dev", name="Groove", is_active=True)]
        sleep = SleepTimerManager(self.client, playback=self.manager, schedule_once=self.schedule)
        self.manager.on_alarm_start = sleep.invalidate_for_alarm
        sleep.start("spotify", 15)
        self.jobs[-1][0]()
        self.assertEqual(self.ctl.state, "active")
        self.assertIsNone(self.manager.manual_spotify_device)

    def test_manual_command_finishes_before_concurrent_alarm_preemption(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def play(device):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release")
            self.events.append("manual-play")
        self.client.play.side_effect = play
        def run(call):
            try:
                call()
            except Exception as exc:
                errors.append(exc)
        manual = threading.Thread(target=lambda: run(lambda: self.manager.control_spotify("play", "dev")))
        alarm = threading.Thread(target=lambda: run(lambda: self.manager.start(LOCAL)))
        manual.start()
        self.assertTrue(entered.wait(1))
        alarm.start()
        self.wav.play.assert_not_called()
        release.set()
        manual.join(2)
        alarm.join(2)
        self.assertFalse(manual.is_alive() or alarm.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.events, ["manual-play", "manual-pause", "local-play"])

    def test_manual_play_waits_for_preflight_service_gate(self):
        entered = threading.Event()
        class Gate:
            def __init__(self):
                self.lock = threading.RLock()
            def acquire(self, **kwargs):
                entered.set()
                return self.lock.acquire(**kwargs)
            def release(self):
                self.lock.release()
        gate = self.manager._service_lock = Gate()
        gate.lock.acquire()
        errors = []
        def play():
            try:
                self.manager.control_spotify("play", "dev")
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=play)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.client.play.assert_not_called()
            self.assertEqual(self.ctl.state, "active")
        finally:
            gate.lock.release()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.client.play.assert_called_once_with("dev")

    def test_stop_during_bluetooth_release_never_starts_local_audio(self):
        entered, release = threading.Event(), threading.Event()
        original = self.ctl
        def command(command, **kwargs):
            if command[2] == "stop":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("release not signalled")
            return original(command, **kwargs)
        self.bt._run = command
        outcomes = []
        starter = threading.Thread(target=lambda: outcomes.append(self.manager.start(LOCAL)))
        starter.start()
        self.assertTrue(entered.wait(1))
        starting = self.manager._starting
        stopper = threading.Thread(target=self.manager.stop)
        stopper.start()
        try:
            self.assertTrue(starting.wait(1))
        finally:
            release.set()
            starter.join(2)
            stopper.join(2)
        self.assertFalse(starter.is_alive() or stopper.is_alive())
        self.assertEqual(outcomes, ["cancelled"])
        self.wav.play.assert_not_called()
        self.assertIsNone(self.manager.active)

    def test_stop_during_previous_source_release_never_starts_replacement(self):
        self.manager.start(LOCAL)
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set()
            if not release.wait(2):
                raise AssertionError("release not signalled")
            return True
        self.wav.stop.side_effect = stop
        outcomes = []
        starter = threading.Thread(target=lambda: outcomes.append(self.manager.start(SPOTIFY)))
        starter.start()
        self.assertTrue(entered.wait(1))
        starting = self.manager._starting
        stopper = threading.Thread(target=self.manager.stop)
        stopper.start()
        try:
            self.assertTrue(starting.wait(1))
        finally:
            release.set()
            starter.join(2)
            stopper.join(2)
        self.assertFalse(starter.is_alive() or stopper.is_alive())
        self.assertEqual(outcomes, ["cancelled"])
        self.spotify.play.assert_not_called()
        self.assertIsNone(self.manager.active)

    def test_failed_ffmpeg_start_cleanup_keeps_handle(self):
        process = Mock()
        process.wait.side_effect = OSError("wait failed")
        process.poll.return_value = None
        process.terminate.side_effect = OSError("termination failed")
        player = FfmpegPlayer(popen=Mock(return_value=process))
        self.assertFalse(player.play("track.mp3"))
        self.assertIs(player._process, process)
        process.wait.side_effect = process.terminate.side_effect = None
        self.assertTrue(player.stop())
        self.assertIsNone(player._process)


class TransitionRoutesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(scheduler.close_logging)
        root = Path(temp.name)
        self.ctl = FakeSystemctl()
        self.spotify = Mock(spec=SpotifyClient)
        self.spotify.is_configured = True
        self.spotify.is_connected.return_value = True
        self.spotify.redirect_uri = "http://localhost/spotify/callback"
        self.spotify.missing_scopes.return_value = []
        self.spotify.get_devices.return_value = []
        self.wav = Mock(spec=AudioPlayer)
        self.wav.play.return_value = self.wav.stop.return_value = True
        self.app = create_app(dict(TESTING=True, SECRET_KEY="t", DATABASE=str(root / "db"),
                                   ALARM_LOG=str(root / "log")), player=self.wav,
                              spotify=self.spotify, bluetooth=BluetoothAudio(run=self.ctl))
        self.client = self.app.test_client()
        self.client.post("/alarms/new", data=dict(name="Alarma", time="07:00"))
        self.client.post("/spotify/device", data=dict(device_id="dev", device_name="Groove"))
        self.manager = self.app.extensions["playback"]
        self.manager.schedule_once = Mock(return_value=Mock())

    def test_manual_routes_explain_alarm_priority(self):
        self.manager.start(LOCAL)
        for action in ("play", "pause", "transfer"):
            response = self.client.post("/spotify/" + action, follow_redirects=True)
            self.assertIn("Hay una alarma activa", response.get_data(as_text=True))
        self.spotify.play.assert_not_called()
        self.spotify.pause.assert_not_called()
        self.spotify.transfer_playback.assert_not_called()

    def test_restore_notice_polling_key_match_and_clear(self):
        self.manager.start(LOCAL)
        self.ctl.fail.add("start")
        self.manager.stop()
        data = self.client.get("/playback/state").json
        self.assertTrue(data["bluetooth_restore_pending"])
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-playback-key="' + data["key"] + '"', html)
        self.assertIn("Reintentar Bluetooth", html)
        self.ctl.fail.clear()
        self.client.post("/playback/restore-bluetooth")
        restored = self.client.get("/playback/state").json
        self.assertFalse(restored["bluetooth_restore_pending"])
        self.assertNotEqual(restored["key"], data["key"])

    def test_blocked_bluetooth_is_explained_without_reported_playback(self):
        self.ctl.fail.add("stop")
        response = self.client.post("/alarms/1/test", follow_redirects=True)
        self.assertIn("la alarma no ha arrancado", response.get_data(as_text=True))
        self.wav.play.assert_not_called()
        self.assertEqual(self.client.get("/playback/state").json["active"]["status"], "stop_pending")


if __name__ == "__main__":
    unittest.main()
