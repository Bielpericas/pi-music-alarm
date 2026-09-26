"""Tests de la preparación para Raspberry Pi: serve.py, aplay -D y plantilla systemd."""
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audio_player  # noqa: E402
from audio_player import LocalAudioPlayer, create_player  # noqa: E402
from serve import server_settings  # noqa: E402


class ServerSettingsTest(unittest.TestCase):
    def test_defaults_listen_on_local_network(self):
        self.assertEqual(server_settings({}), ("0.0.0.0", 5000, 4))

    def test_custom_values(self):
        env = {"HOST": "192.168.1.50", "PORT": "8080", "THREADS": "2"}
        self.assertEqual(server_settings(env), ("192.168.1.50", 8080, 2))

    def test_invalid_values(self):
        for env in ({"PORT": "abc"}, {"PORT": "70000"}, {"THREADS": "0"}):
            with self.subTest(env=env):
                with self.assertRaises(SystemExit):
                    server_settings(env)


class AlsaDeviceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.sound = os.path.join(self.tmpdir.name, "alarm.wav")
        with wave.open(self.sound, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 80)

    def play_linux(self, **kwargs):
        player = LocalAudioPlayer(self.sound, platform="linux", **kwargs)
        with mock.patch.object(audio_player.subprocess, "Popen") as popen, \
                mock.patch.object(audio_player.threading, "Thread"):
            self.assertTrue(player.play())
        return popen.call_args.args[0]

    def test_aplay_with_device(self):
        self.assertEqual(self.play_linux(alsa_device="plughw:CARD=Device,DEV=0"),
                         ["aplay", "-q", "-D", "plughw:CARD=Device,DEV=0", self.sound])

    def test_aplay_without_device_uses_default(self):
        self.assertEqual(self.play_linux(alsa_device=""), ["aplay", "-q", self.sound])

    def test_create_player_passes_alsa_device(self):
        player = create_player({"AUDIO_BACKEND": "local", "SOUND_PATH": self.sound,
                                "ALSA_DEVICE": "default"})
        self.assertEqual(player.alsa_device, "default")


class SystemdTemplateTest(unittest.TestCase):
    def test_template_is_parametrized(self):
        text = (ROOT / "deploy" / "pi-music-alarm.service.template").read_text(encoding="utf-8")
        for placeholder in ("@USER@", "@GROUP@", "@APP_DIR@"):
            self.assertIn(placeholder, text)
        self.assertNotIn("/home/", text)
        self.assertIn("ExecStart=@APP_DIR@/.venv/bin/python @APP_DIR@/serve.py", text)

    def test_linux_files_have_lf_endings(self):
        for name in ("deploy/install-service.sh", "deploy/pi-music-alarm.service.template"):
            with self.subTest(name=name):
                self.assertNotIn(b"\r\n", (ROOT / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
