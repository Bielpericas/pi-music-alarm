"""Tests de audio_player con mocks: nunca suena nada de verdad."""
import os
import sys
import tempfile
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audio_player  # noqa: E402
from audio_player import (  # noqa: E402
    LocalAudioPlayer,
    NullAudioPlayer,
    create_player,
)


def fake_winsound():
    ws = mock.Mock()
    ws.SND_FILENAME = 0x20000
    ws.SND_ASYNC = 0x1
    ws.SND_NODEFAULT = 0x2
    return ws


def write_wav(path):
    """WAV mínimo válido (10 ms de silencio). Nunca se llega a reproducir."""
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 80)


class LocalAudioPlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.sound = os.path.join(self.tmpdir.name, "alarm.wav")
        write_wav(self.sound)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_file_logs_error_and_does_not_raise(self):
        player = LocalAudioPlayer(os.path.join(self.tmpdir.name, "no.wav"), platform="linux")
        with mock.patch.object(audio_player.subprocess, "Popen") as popen, \
                self.assertLogs("alarms", "ERROR") as logs:
            self.assertFalse(player.play())
        popen.assert_not_called()
        self.assertIn("No se encuentra", logs.output[0])

    def test_windows_uses_winsound_async(self):
        ws = fake_winsound()
        player = LocalAudioPlayer(self.sound, platform="win32")
        with mock.patch.dict(sys.modules, {"winsound": ws}):
            self.assertTrue(player.play())
        ws.PlaySound.assert_called_with(
            self.sound, ws.SND_FILENAME | ws.SND_ASYNC | ws.SND_NODEFAULT
        )

    def test_invalid_wav_is_logged_and_not_played(self):
        bogus = os.path.join(self.tmpdir.name, "bogus.wav")
        with open(bogus, "w") as f:
            f.write("esto no es un wav")
        ws = fake_winsound()
        player = LocalAudioPlayer(bogus, platform="win32")
        with mock.patch.dict(sys.modules, {"winsound": ws}), \
                self.assertLogs("alarms", "ERROR") as logs:
            self.assertFalse(player.play())
        ws.PlaySound.assert_not_called()
        self.assertIn("no es un WAV", logs.output[0])

    def test_windows_error_is_logged(self):
        ws = fake_winsound()
        ws.PlaySound.side_effect = [None, RuntimeError("Failed to play sound")]
        player = LocalAudioPlayer(self.sound, platform="win32")
        with mock.patch.dict(sys.modules, {"winsound": ws}), \
                self.assertLogs("alarms", "ERROR"):
            self.assertFalse(player.play())

    def test_linux_uses_aplay_without_waiting(self):
        player = LocalAudioPlayer(self.sound, platform="linux")
        with mock.patch.object(audio_player.subprocess, "Popen") as popen, \
                mock.patch.object(audio_player.threading, "Thread") as thread:
            self.assertTrue(player.play())
        self.assertEqual(popen.call_args.args[0], ["aplay", "-q", self.sound])
        # No espera al proceso en el hilo que llama: delega en un hilo daemon.
        popen.return_value.wait.assert_not_called()
        popen.return_value.communicate.assert_not_called()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()

    def test_linux_missing_aplay_is_logged(self):
        player = LocalAudioPlayer(self.sound, platform="linux")
        with mock.patch.object(audio_player.subprocess, "Popen",
                               side_effect=FileNotFoundError("aplay")), \
                self.assertLogs("alarms", "ERROR"):
            self.assertFalse(player.play())

    def test_aplay_failure_is_logged_by_watcher(self):
        player = LocalAudioPlayer(self.sound, platform="linux")
        process = mock.Mock(returncode=1)
        process.communicate.return_value = (None, b"formato no soportado")
        with self.assertLogs("alarms", "ERROR") as logs:
            player._watch(process)
        self.assertIn("formato no soportado", logs.output[0])

    def test_new_play_stops_previous_aplay(self):
        player = LocalAudioPlayer(self.sound, platform="linux")
        first, second = mock.Mock(), mock.Mock()
        first.poll.return_value = None  # sigue sonando
        with mock.patch.object(audio_player.subprocess, "Popen",
                               side_effect=[first, second]), \
                mock.patch.object(audio_player.threading, "Thread"):
            player.play()
            player.play()
        first.terminate.assert_called_once()

    def test_unsupported_platform_is_logged(self):
        player = LocalAudioPlayer(self.sound, platform="darwin")
        with self.assertLogs("alarms", "ERROR"):
            self.assertFalse(player.play())


class CreatePlayerTest(unittest.TestCase):
    def test_local_backend(self):
        player = create_player({"AUDIO_BACKEND": "local", "SOUND_PATH": "x.wav"})
        self.assertIsInstance(player, LocalAudioPlayer)
        self.assertEqual(player.sound_path.name, "x.wav")

    def test_none_backend(self):
        self.assertIsInstance(create_player({"AUDIO_BACKEND": "none"}), NullAudioPlayer)

    def test_unknown_backend(self):
        with self.assertRaises(ValueError):
            create_player({"AUDIO_BACKEND": "spotify"})


if __name__ == "__main__":
    unittest.main()
