"""Bluetooth (BlueALSA) y alarmas: ALARMA > SPOTIFY > BLUETOOTH.

Nunca se ejecuta systemctl de verdad: `run` es un doble y, por si acaso,
subprocess.run está parcheado para fallar en todos los tests.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from bluetooth_audio import BluetoothAudio, NoBluetooth, create_bluetooth, unit_name  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import SpotifyClient  # noqa: E402
from spotify_player import SpotifyAlarmPlayer  # noqa: E402

NOW = datetime(2026, 9, 28, 7, 30)
LOCAL = {"id": 1, "name": "Despertador", "time": "07:30", "source": "local", "spotify_uri": None}
SPOTIFY = {"id": 2, "name": "Música", "time": "07:30", "source": "spotify",
           "spotify_uri": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"}


def no_real_systemctl(test):
    guard = mock.patch.object(subprocess, "run",
                              side_effect=AssertionError("los tests no deben ejecutar systemctl"))
    guard.start()
    test.addCleanup(guard.stop)


class FakeSystemctl:
    """Doble de subprocess.run para systemctl: guarda las órdenes y simula el estado."""

    def __init__(self, state="active", fail=(), raises=None):
        self.state = state
        self.fail = set(fail)      # verbos que terminan con código 1
        self.raises = raises       # excepción que lanza cualquier llamada
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.raises is not None:
            raise self.raises
        verb = command[2]
        if verb in self.fail:
            return subprocess.CompletedProcess(command, 1, "", "Access denied")
        if verb == "is-active":
            code = 0 if self.state == "active" else 3
            return subprocess.CompletedProcess(command, code, self.state + "\n", "")
        if verb == "stop":
            self.state = "inactive"
        elif verb == "start":
            self.state = "active"
        return subprocess.CompletedProcess(command, 0, "", "")

    @property
    def verbs(self):
        return [command[2] for command in self.calls if command[2] != "is-active"]


class BluetoothAudioTest(unittest.TestCase):
    def setUp(self):
        no_real_systemctl(self)

    def make(self, **kwargs):
        self.systemctl = FakeSystemctl(**kwargs)
        return BluetoothAudio("bluealsa-aplay", run=self.systemctl)

    def test_unit_name(self):
        self.assertEqual(unit_name("bluealsa-aplay"), "bluealsa-aplay.service")
        self.assertEqual(unit_name(" otro.service "), "otro.service")

    def test_pause_stops_active_player_and_resume_starts_it(self):
        bt = self.make()
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertTrue(bt.pause())
            self.assertTrue(bt.resume())
        self.assertEqual(self.systemctl.calls, [
            ["systemctl", "--no-ask-password", "is-active", "bluealsa-aplay.service"],
            ["systemctl", "--no-ask-password", "stop", "bluealsa-aplay.service"],
            ["systemctl", "--no-ask-password", "start", "bluealsa-aplay.service"],
        ])
        text = "\n".join(logs.output)
        self.assertIn("Bluetooth pausado por alarma", text)
        self.assertIn("Bluetooth disponible de nuevo", text)

    def test_only_start_and_stop_never_touches_bluez(self):
        bt = self.make()
        bt.pause()
        bt.resume()
        for command in self.systemctl.calls:
            self.assertEqual(command[:2], ["systemctl", "--no-ask-password"])  # nunca pide contraseña
            self.assertIn(command[2], ("is-active", "stop", "start"))
            self.assertEqual(command[3], "bluealsa-aplay.service")
            self.assertNotIn("sudo", command)

    def test_player_already_stopped_is_left_alone(self):
        bt = self.make(state="inactive")
        self.assertFalse(bt.pause())
        self.assertFalse(bt.resume())  # no lo paró Groove: no se arranca
        self.assertEqual(self.systemctl.verbs, [])

    def test_pause_is_idempotent(self):
        bt = self.make()
        bt.pause()
        bt.pause()  # otra alarma sustituye a la primera
        self.assertEqual(self.systemctl.verbs, ["stop"])
        bt.resume()
        bt.resume()
        self.assertEqual(self.systemctl.verbs, ["stop", "start"])

    def test_errors_never_raise(self):
        for raises in (FileNotFoundError("systemctl"),
                       subprocess.TimeoutExpired("systemctl", 8), OSError("boom")):
            with self.subTest(raises=type(raises).__name__):
                bt = self.make(raises=raises)
                with self.assertLogs("alarms", "WARNING"):
                    self.assertTrue(bt.pause())  # estado desconocido: se intenta parar
                    self.assertFalse(bt.resume())
                self.assertFalse(bt.paused)

    def test_permission_denied_is_logged(self):
        bt = self.make(fail={"stop", "start"})
        with self.assertLogs("alarms", "WARNING") as logs:
            bt.pause()
            bt.resume()
        text = "\n".join(logs.output)
        self.assertIn("Access denied", text)
        self.assertIn("No se pudo detener bluealsa-aplay.service", text)
        self.assertIn("sudo systemctl start bluealsa-aplay.service", text)

    def test_create_bluetooth(self):
        self.assertIsInstance(create_bluetooth({}, platform="win32"), NoBluetooth)
        for value in ("none", "", "off"):
            with self.subTest(value=value):
                self.assertIsInstance(create_bluetooth({"BLUETOOTH_SERVICE": value},
                                                       platform="linux"), NoBluetooth)
        self.assertEqual(create_bluetooth({}, platform="linux").unit, "bluealsa-aplay.service")
        self.assertEqual(create_bluetooth({"BLUETOOTH_SERVICE": "mi-bt"}, platform="linux").unit,
                         "mi-bt.service")


class ManagerBluetoothTest(unittest.TestCase):
    """El ciclo alarma → Bluetooth → alarma, con un BluetoothAudio sobre systemctl falso."""

    def setUp(self):
        no_real_systemctl(self)
        self.systemctl = FakeSystemctl()
        self.bt = BluetoothAudio("bluealsa-aplay", run=self.systemctl)
        self.local = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyAlarmPlayer)
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = True
        self.jobs = []
        self.manager = AlarmPlaybackManager(
            self.local, self.spotify, clock=lambda: NOW, bluetooth=self.bt,
            schedule_once=lambda run_at, callback: self.jobs.append(callback) or mock.Mock())

    def test_bluetooth_stopped_before_any_sound(self):
        for alarm, sound in ((LOCAL, self.local.play), (SPOTIFY, self.spotify.play)):
            with self.subTest(source=alarm["source"]):
                sound.side_effect = lambda *a, **k: self.assertEqual(self.systemctl.state,
                                                                     "inactive") or True
                self.manager.start(alarm)
                sound.assert_called()
                self.manager.stop()
                self.assertEqual(self.systemctl.state, "active")

    def test_stop_makes_bluetooth_available_again(self):
        self.manager.start(LOCAL)
        self.assertEqual(self.systemctl.verbs, ["stop"])
        self.manager.stop()
        self.assertEqual(self.systemctl.verbs, ["stop", "start"])
        self.manager.stop()  # STOP idempotente: no se vuelve a arrancar
        self.assertEqual(self.systemctl.verbs, ["stop", "start"])

    def test_snooze_frees_bluetooth_and_takes_it_back(self):
        self.manager.start(SPOTIFY)
        self.manager.snooze()
        self.assertEqual(self.systemctl.verbs, ["stop", "start"])
        self.assertEqual(self.systemctl.state, "active")  # Bluetooth usable los 10 min
        self.jobs[-1]()  # vuelve a sonar
        self.assertEqual(self.systemctl.verbs, ["stop", "start", "stop"])
        self.assertEqual(self.manager.active.snoozes, 1)
        self.manager.stop()
        self.assertEqual(self.systemctl.verbs, ["stop", "start", "stop", "start"])

    def test_replacing_alarm_keeps_bluetooth_stopped(self):
        self.manager.start(LOCAL)
        self.manager.start(SPOTIFY)
        self.manager.start(LOCAL)
        self.assertEqual(self.systemctl.verbs, ["stop"])  # nada de start entre medias
        self.manager.stop()
        self.assertEqual(self.systemctl.verbs, ["stop", "start"])

    def test_fallback_keeps_bluetooth_stopped(self):
        self.spotify.play.return_value = False
        self.assertEqual(self.manager.start(SPOTIFY), "fallback")
        self.local.play.assert_called_once_with()
        self.assertEqual(self.systemctl.state, "inactive")
        self.manager.stop()
        self.assertEqual(self.systemctl.state, "active")

    def test_cancelled_start_gives_bluetooth_back(self):
        def interrupted(uri, **kwargs):
            self.manager._starting.set()  # STOP mientras buscaba el dispositivo
            return False
        self.spotify.play.side_effect = interrupted
        self.assertEqual(self.manager.start(SPOTIFY), "cancelled")
        self.manager.stop()
        self.assertEqual(self.systemctl.verbs, ["stop", "start"])

    def test_delete_ringing_alarm_gives_bluetooth_back(self):
        self.manager.start(LOCAL)
        self.manager.forget(LOCAL["id"])
        self.assertEqual(self.systemctl.state, "active")

    def test_systemctl_failure_never_blocks_the_alarm(self):
        self.systemctl.raises = FileNotFoundError("systemctl")
        with self.assertLogs("alarms", "WARNING"):
            self.assertEqual(self.manager.start(LOCAL), "local")
            self.local.play.assert_called_once_with()
            self.assertIsNotNone(self.manager.stop())

    def test_bluetooth_exceptions_never_block_the_alarm(self):
        broken = mock.Mock(spec=BluetoothAudio)
        broken.pause.side_effect = RuntimeError("boom")
        broken.resume.side_effect = RuntimeError("boom")
        manager = AlarmPlaybackManager(self.local, self.spotify, clock=lambda: NOW,
                                       bluetooth=broken)
        with self.assertLogs("alarms", "ERROR"):
            self.assertEqual(manager.start(LOCAL), "local")
            result = manager.stop()
        self.local.play.assert_called_once_with()
        self.assertTrue(result.silenced)
        self.assertIsNone(manager.active)


class AppBluetoothTest(unittest.TestCase):
    def setUp(self):
        no_real_systemctl(self)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.addCleanup(scheduler.close_logging)

    def app(self, config=None, **kwargs):
        base = {"TESTING": True, "SECRET_KEY": "t",
                "DATABASE": os.path.join(self.tmpdir.name, "t.db"),
                "ALARM_LOG": os.path.join(self.tmpdir.name, "a.log")}
        base.update(config or {})
        return create_app(base, player=mock.Mock(spec=AudioPlayer),
                          spotify=mock.Mock(spec=SpotifyClient), **kwargs)

    def test_tests_never_manage_bluetooth(self):
        # Aunque la máquina de tests sea Linux: sin doble inyectado, no se gestiona.
        app = self.app()
        self.assertIsInstance(app.extensions["bluetooth"], NoBluetooth)
        self.assertEqual(app.config["BLUETOOTH_SERVICE"], "bluealsa-aplay")

    def test_service_name_from_env(self):
        with mock.patch.dict(os.environ, {"BLUETOOTH_SERVICE": "mi-bt"}):
            self.assertEqual(self.app().config["BLUETOOTH_SERVICE"], "mi-bt")

    def test_test_button_pauses_and_stop_route_resumes(self):
        bt = mock.Mock(spec=BluetoothAudio)
        app = self.app(bluetooth=bt)
        client = app.test_client()
        client.post("/alarms/new", data={"name": "Trabajo", "time": "07:30", "days": ["0"]})
        client.post("/alarms/1/test")
        bt.pause.assert_called_once_with()
        bt.resume.assert_not_called()
        client.post("/playback/stop")
        bt.resume.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
