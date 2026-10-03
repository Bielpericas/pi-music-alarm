"""Regresiones del audio y STOP; sin red, servicios ni sonido real."""
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scheduler
from app import create_app, playback_key
from audio_player import AudioPlayer, FfmpegPlayer, LocalAudioPlayer, NullAudioPlayer
from playback import AlarmPlaybackManager


ALARM = dict(id=1, name="Prueba", time="07:00", source="local", spotify_uri=None,
             max_duration_minutes=15)


class MonitoredPlayer:
    def __init__(self, starts=True):
        self.starts = starts
        self.callbacks = []
        self.stop = Mock(return_value=True)

    def play_monitored(self, *args, on_finished):
        self.callbacks.append(on_finished)
        return self.starts


class PlaybackFailuresTest(unittest.TestCase):
    def setUp(self):
        self.wav = MonitoredPlayer()
        self.music = MonitoredPlayer()
        self.spotify = Mock()
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = True
        self.bluetooth = Mock()
        self.cancel = Mock()
        self.schedule = Mock(return_value=self.cancel)
        self.manager = AlarmPlaybackManager(self.wav, self.spotify, music=self.music,
                                            bluetooth=self.bluetooth, schedule_once=self.schedule)

    def test_no_audio_is_failure_and_does_not_schedule_auto_stop(self):
        self.wav.starts = self.music.starts = False
        self.assertEqual(self.manager.start(ALARM), "failed")
        self.assertEqual(self.manager.active.status, "failed")
        self.schedule.assert_not_called()
        self.bluetooth.resume.assert_called_once()

    def test_disabled_audio_does_not_report_success(self):
        manager = AlarmPlaybackManager(NullAudioPlayer())
        self.assertEqual(manager.start(ALARM), "failed")

    def test_emergency_exception_is_failure(self):
        self.music.starts = False
        self.manager.player = Mock(spec=AudioPlayer)
        self.manager.player.play.side_effect = OSError("no audio")
        self.assertEqual(self.manager.start(ALARM), "failed")

    def test_late_music_exit_starts_emergency_and_keeps_deadline(self):
        self.manager.start(ALARM)
        self.music.callbacks[0](False)
        self.assertEqual(len(self.wav.callbacks), 1)
        self.assertEqual(self.manager.active.status, "playing")
        self.schedule.assert_called_once()
        self.cancel.assert_not_called()
        self.bluetooth.resume.assert_not_called()

    def test_late_music_exit_and_failed_emergency(self):
        self.manager.start(ALARM)
        self.wav.starts = False
        self.music.callbacks[0](False)
        self.assertEqual(self.manager.active.status, "failed")
        self.cancel.assert_called_once()
        self.bluetooth.resume.assert_called_once()

    def test_emergency_exit_changes_polling_key_and_can_be_acknowledged(self):
        self.music.starts = False
        self.manager.start(ALARM)
        key = playback_key(self.manager.active, ())
        self.wav.callbacks[0](False)
        self.assertEqual(self.manager.active.status, "failed")
        self.assertNotEqual(key, playback_key(self.manager.active, ()))
        self.assertTrue(self.manager.stop().silenced)
        self.assertIsNone(self.manager.active)

    def test_normal_emergency_end_is_finished_not_failed(self):
        self.music.starts = False
        self.manager.start(ALARM)
        self.wav.callbacks[0](True)
        self.assertEqual(self.manager.active.status, "finished")

    def test_callback_after_stop_never_starts_fallback(self):
        self.manager.start(ALARM)
        self.manager.stop()
        self.music.callbacks[0](False)
        self.assertEqual(self.wav.callbacks, [])
        self.assertIsNone(self.manager.active)

    def test_callback_from_previous_alarm_is_ignored(self):
        self.manager.start(ALARM)
        old = self.music.callbacks[0]
        self.manager.start(dict(ALARM, id=2))
        old(False)
        self.assertEqual(self.wav.callbacks, [])
        self.assertEqual(self.manager.active.id, 2)
        self.assertEqual(self.manager.active.status, "playing")

    def test_spotify_stop_failure_replaces_auto_stop_with_retry_and_keeps_bluetooth(self):
        self.manager.start(dict(ALARM, source="spotify", spotify_uri="spotify:track:x"))
        self.spotify.stop.return_value = False
        self.assertFalse(self.manager.stop().silenced)
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.cancel.assert_called_once()  # se sustituye el auto-stop por un reintento
        self.assertEqual(self.schedule.call_count, 2)
        self.bluetooth.resume.assert_not_called()
        self.spotify.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)
        self.assertEqual(self.cancel.call_count, 2)  # se cancela también el reintento
        self.bluetooth.resume.assert_called_once()

    def test_failed_snooze_does_not_create_pending_alarm(self):
        self.manager.start(ALARM)
        self.music.stop.return_value = False
        self.assertIsNone(self.manager.snooze())
        self.assertEqual(self.manager.pending_snoozes, ())
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.assertEqual(self.schedule.call_count, 2)  # auto-stop y reintento; ningún snooze

    def test_failed_wav_stop_can_be_retried(self):
        self.music.starts = False
        self.manager.start(ALARM)
        self.wav.stop.return_value = False
        self.assertFalse(self.manager.stop().silenced)
        self.wav.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)

    def test_auto_stop_failure_keeps_manual_stop(self):
        self.manager.start(ALARM)
        callback = self.schedule.call_args.args[1]
        self.music.stop.return_value = False
        callback()
        self.assertEqual(self.manager.active.status, "stop_pending")
        self.music.stop.return_value = True
        self.assertTrue(self.manager.stop().silenced)

    def test_new_start_cannot_discard_pending_stop(self):
        self.manager.start(ALARM)
        self.music.stop.return_value = False
        self.manager.stop()
        self.assertEqual(self.manager.start(dict(ALARM, id=2)), "stop_pending")
        self.assertEqual(self.manager.active.id, 1)


class PlayerCompletionTest(unittest.TestCase):
    def test_aplay_reports_failure_success_and_unexpected_signal(self):
        for code in (0, 1, -9):
            with self.subTest(code=code):
                player = LocalAudioPlayer("unused", platform="linux")
                process = Mock(returncode=code)
                process.communicate.return_value = (None, b"")
                player._process = process
                callback = Mock()
                player._watch(process, callback)
                callback.assert_called_once_with(code == 0)

    def test_obsolete_aplay_does_not_notify_or_clear_new_process(self):
        player = LocalAudioPlayer("unused", platform="linux")
        process = Mock(returncode=1)
        process.communicate.return_value = (None, b"")
        current = player._process = Mock()
        callback = Mock()
        player._watch(process, callback)
        callback.assert_not_called()
        self.assertIs(player._process, current)

    def test_intentional_windows_stop_is_not_logged_as_failure(self):
        # En Windows terminate() deja código 1: tras stop() no es un fallo.
        player = LocalAudioPlayer("unused", platform="win32")
        process = Mock(returncode=1)
        process.poll.return_value = None
        process.communicate.return_value = (None, b"")
        player._process = process
        with patch.dict("sys.modules", winsound=Mock()):  # también en Linux
            self.assertTrue(player.stop())
        callback = Mock()
        with self.assertNoLogs("alarms", "ERROR"):
            player._watch(process, callback)
        callback.assert_not_called()

    def test_unexpected_wav_exit_is_still_logged(self):
        player = LocalAudioPlayer("unused", platform="win32")
        process = Mock(returncode=1)
        process.communicate.return_value = (None, b"boom")
        player._process = process
        with self.assertLogs("alarms", "ERROR") as logs:
            player._watch(process, Mock())
        self.assertIn("código 1: boom", "\n".join(logs.output))

    def test_ffmpeg_any_unexpected_exit_notifies_outside_lock(self):
        for code in (0, 1, -9):
            with self.subTest(code=code):
                player = FfmpegPlayer()
                process = Mock(returncode=code, stderr=io.BytesIO())
                player._process = process
                callback = Mock(side_effect=lambda ok: player.stop())
                thread = threading.Thread(target=player._watch, args=(process, callback), daemon=True)
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                callback.assert_called_once_with(False)

    def test_failed_process_stop_retains_handle(self):
        for player in (FfmpegPlayer(), LocalAudioPlayer("unused", platform="linux")):
            with self.subTest(player=type(player).__name__):
                process = Mock()
                process.poll.return_value = None
                process.terminate.side_effect = OSError("denied")
                player._process = process
                self.assertFalse(player.stop())
                self.assertIs(player._process, process)
                process.terminate.side_effect = None
                self.assertTrue(player.stop())
                self.assertIsNone(player._process)

    def test_windows_monitor_uses_hidden_synchronous_child(self):
        player = LocalAudioPlayer("unused", platform="win32")
        with patch("audio_player.subprocess.Popen") as popen, patch("audio_player.threading.Thread"):
            player._play_windows_process(Mock())
        args = popen.call_args.args[0]
        self.assertNotIn("SND_ASYNC", args[2])
        self.assertIn("creationflags", popen.call_args.kwargs)
        self.assertEqual(args[-1], "unused")


class FailureRoutesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.addCleanup(scheduler.close_logging)
        root = Path(temp.name)
        self.player = Mock(spec=AudioPlayer)
        self.app = create_app(dict(TESTING=True, SECRET_KEY="test", DATABASE=str(root / "db"),
                                   ALARM_LOG=str(root / "log")), player=self.player)
        self.client = self.app.test_client()
        self.client.post("/alarms/new", data=dict(name="Prueba", time="07:00"))
        self.manager = self.app.extensions["playback"]

    def test_cancelled_manual_test_does_not_raise(self):
        with patch.object(self.manager, "start", return_value="cancelled"):
            response = self.client.post("/alarms/1/test", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("prueba cancelada", response.get_data(as_text=True))

    def test_stop_while_manual_spotify_test_is_starting(self):
        import db
        with self.app.app_context():
            db.get_db().execute("UPDATE alarms SET source='spotify', spotify_uri='spotify:track:x'")
            db.get_db().commit()
        entered, interrupted = threading.Event(), threading.Event()
        spotify = self.manager.spotify = Mock()
        spotify.interrupt.side_effect = interrupted.set
        def start(*args, **kwargs):
            entered.set()
            if not interrupted.wait(2):
                raise AssertionError("STOP no interrumpió el arranque")
            return False
        spotify.play.side_effect = start
        responses = []
        def request_test():
            with self.app.test_client() as client:
                responses.append(client.post("/alarms/1/test", follow_redirects=True))
        thread = threading.Thread(target=request_test, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.manager.stop().silenced)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(responses[0].status_code, 200)
        self.assertIn("prueba cancelada", responses[0].get_data(as_text=True))
        self.player.play.assert_not_called()

    def test_failed_audio_is_visible_in_html_and_json(self):
        self.player.play.return_value = False
        response = self.client.post("/alarms/1/test", follow_redirects=True)
        self.assertIn("No se pudo reproducir el sonido", response.get_data(as_text=True))
        self.assertEqual(self.client.get("/playback/state").json["active"]["status"], "failed")

    def test_failed_snooze_explains_failure_and_stop_can_retry(self):
        self.manager.start(ALARM)
        self.player.stop.return_value = False
        response = self.client.post("/playback/snooze", follow_redirects=True)
        self.assertIn("no se ha pospuesto", response.get_data(as_text=True))
        self.assertIn("Vuelve a pulsar STOP", response.get_data(as_text=True))
        self.player.stop.return_value = True
        self.client.post("/playback/stop")
        self.assertIsNone(self.manager.active)
