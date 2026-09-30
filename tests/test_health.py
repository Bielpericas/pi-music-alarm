"""Health checks (health.py) y página Diagnóstico.

Nunca se ejecuta systemctl, ffmpeg, bluetoothctl ni ALSA de verdad:
subprocess está bloqueado, los comandos pasan por un doble (`FakeRun`) y
/proc/asound es una carpeta temporal. Spotify es un doble.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from health import (  # noqa: E402
    ERROR, OK, SPOTIFY_DEVICE_MISSING, SPOTIFY_TIMEOUT, WARNING, HealthChecker, HealthReport,
    parse_alsa_device,
)
from music_library import MusicLibrary  # noqa: E402
from spotify_client import (  # noqa: E402
    SpotifyAuthError, SpotifyConnectionError, SpotifyError, SpotifyForbiddenError,
    SpotifyRateLimitError,
)
from spotify_player import DEVICE_ID_KEY, DEVICE_NAME_KEY  # noqa: E402

NOW = datetime(2026, 9, 28, 7, 0)  # lunes
GROOVE = {"id": "groove-1", "name": "Groove", "is_active": False, "is_restricted": False}
PHONE = {"id": "phone-1", "name": "Móvil", "is_active": True, "is_restricted": False}
SECRET = "super-secreto-no-mostrar"
ACCESS_TOKEN = "token-de-acceso-no-mostrar"


def block_real_processes(test):
    for target in ("Popen", "run", "check_output", "call"):
        guard = mock.patch.object(subprocess, target,
                                  side_effect=AssertionError("los tests no ejecutan procesos"))
        guard.start()
        test.addCleanup(guard.stop)


class FakeRun:
    """Doble de subprocess.run. `responses`: {tupla de argumentos: salida o excepción}.

    La clave es el comando sin el binario completo (p. ej. ("systemctl",
    "--no-ask-password", "is-active", "raspotify.service")).
    """

    def __init__(self, responses=None, default=None):
        self.responses = dict(responses or {})
        self.default = default
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        key = (Path(command[0]).name,) + tuple(command[1:])
        response = self.responses.get(key, self.default)
        if response is None:
            raise AssertionError(f"comando inesperado: {command}")
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, tuple):
            stdout, code = response
        else:
            stdout, code = response, 0
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

    def commands(self):
        return [call[0] for call in self.calls]


def systemctl(unit):
    return ("systemctl", "--no-ask-password", "is-active", unit)


def service_states(raspotify="active", bluealsa="active", player="active"):
    return {systemctl("raspotify.service"): (raspotify + "\n", 0 if raspotify == "active" else 3),
            systemctl("bluealsa.service"): (bluealsa + "\n", 0 if bluealsa == "active" else 3),
            systemctl("bluealsa-aplay.service"): (player + "\n", 0 if player == "active" else 3)}


def write_wav(path, seconds=1, rate=8000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * rate * seconds)


class FakeScheduler:
    """Lo mínimo de APScheduler que mira el check del scheduler."""

    def __init__(self, state=1, jobs=("check_alarms",)):
        self.state = state
        self.jobs = [SimpleNamespace(id=job) for job in jobs]

    def get_job(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)

    def get_jobs(self):
        return list(self.jobs)


class HealthTestCase(unittest.TestCase):
    def setUp(self):
        block_real_processes(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.db_path = str(self.tmp / "test.db")
        db.init_db(self.db_path)
        self.music_dir = self.tmp / "music"
        self.music_dir.mkdir()
        self.sound = self.tmp / "alarm.wav"
        write_wav(self.sound)
        self.asound = self.tmp / "asound"
        self.make_card("Device", status="closed\n")
        self.run = FakeRun({**service_states(),
                            ("ffmpeg", "-hide_banner", "-version"):
                                "ffmpeg version 5.1.6-0+deb12u1+rpt1 Copyright (c) 2000-2024\n",
                            ("bluetoothctl", "devices", "Connected"):
                                "Device AA:BB:CC:DD:EE:FF Móvil\n"})
        self.spotify = mock.Mock(is_configured=True)
        self.spotify.is_connected.return_value = True
        self.spotify.get_devices.return_value = [PHONE, GROOVE]
        self.bluetooth = SimpleNamespace(paused=False)
        self.config = {"AUDIO_BACKEND": "local", "ALSA_DEVICE": "plughw:CARD=Device,DEV=0",
                       "FFMPEG_BINARY": "ffmpeg", "SOUND_PATH": str(self.sound),
                       "SPOTIFY_DEVICE_NAME": "Groove", "SPOTIFY_CLIENT_SECRET": SECRET,
                       "SCHEDULER_ENABLED": True}

    def make_card(self, name, dev=0, status="closed\n", number=1):
        card = self.asound / f"card{number}"
        (card / f"pcm{dev}p" / "sub0").mkdir(parents=True)
        (card / f"pcm{dev}p" / "sub0" / "status").write_text(status, encoding="utf-8")
        # /proc/asound/<id> es un enlace a cardN; aquí basta una copia de la estructura.
        alias = self.asound / name
        (alias / f"pcm{dev}p" / "sub0").mkdir(parents=True)
        (alias / f"pcm{dev}p" / "sub0" / "status").write_text(status, encoding="utf-8")
        cards = self.asound / "cards"
        previous = cards.read_text(encoding="utf-8") if cards.exists() else ""
        cards.write_text(previous + f" {number} [{name:<15}]: USB-Audio - USB Audio\n",
                         encoding="utf-8")

    def checker(self, **overrides):
        options = dict(database=self.db_path, library=MusicLibrary(self.music_dir),
                       spotify=self.spotify, bluetooth=self.bluetooth,
                       scheduler=FakeScheduler(), run=self.run,
                       which=lambda binary: f"/usr/bin/{binary}", platform="linux",
                       asound_dir=str(self.asound), clock=lambda: NOW)
        options.update(overrides)
        return HealthChecker(self.config, **options)

    def check(self, check_id, **overrides):
        return self.checker(**overrides).checks()[check_id]()

    def add_track(self, name="despertar.mp3"):
        (self.music_dir / name).write_bytes(b"ID3" + b"\x00" * 32)


class AudioCheckTest(HealthTestCase):
    def test_usb_card_available(self):
        result = self.check("audio")
        self.assertEqual((result.status, result.summary), (OK, "Disponible"))
        self.assertIn("Salida: plughw:CARD=Device,DEV=0", result.details)
        self.assertIn("Libre.", result.details)
        self.assertEqual(self.run.calls, [])  # solo lee /proc/asound: no ejecuta nada

    def test_card_in_use_is_still_ok(self):
        (self.asound / "Device" / "pcm0p" / "sub0" / "status").write_text(
            "state: RUNNING\nowner_pid: 123\n", encoding="utf-8")
        result = self.check("audio")
        self.assertEqual(result.status, OK)
        self.assertIn("En uso ahora mismo (reproduciendo).", result.details)

    def test_missing_card_is_error_and_lists_cards(self):
        self.config["ALSA_DEVICE"] = "plughw:CARD=Otra,DEV=0"
        result = self.check("audio")
        self.assertEqual((result.status, result.summary), (ERROR, "Tarjeta no encontrada"))
        self.assertIn("Tarjetas detectadas: Device.", result.details)

    def test_missing_playback_device(self):
        self.config["ALSA_DEVICE"] = "plughw:CARD=Device,DEV=3"
        self.assertEqual(self.check("audio").status, ERROR)

    def test_no_alsa_at_all(self):
        result = self.check("audio", asound_dir=str(self.tmp / "no-existe"))
        self.assertEqual((result.status, result.summary), (ERROR, "ALSA no disponible"))

    def test_numeric_hw_device(self):
        self.config["ALSA_DEVICE"] = "hw:1,0"
        self.assertEqual(self.check("audio").status, OK)

    def test_empty_setting_uses_ffmpeg_default(self):
        self.config["ALSA_DEVICE"] = ""
        self.assertIn("Salida: plughw:CARD=Device,DEV=0", self.check("audio").details)

    def test_unverifiable_device_and_other_platforms_are_warnings(self):
        self.config["ALSA_DEVICE"] = "default"
        self.assertEqual(self.check("audio").status, WARNING)
        self.config["ALSA_DEVICE"] = "plughw:CARD=Device,DEV=0"
        self.assertEqual(self.check("audio", platform="win32").status, WARNING)

    def test_parse_alsa_device(self):
        self.assertEqual(parse_alsa_device("plughw:CARD=Device,DEV=0"), ("Device", 0))
        self.assertEqual(parse_alsa_device("hw:CARD=USB,DEV=1"), ("USB", 1))
        self.assertEqual(parse_alsa_device("plughw:2,0"), ("2", 0))
        self.assertIsNone(parse_alsa_device("default"))


class FfmpegCheckTest(HealthTestCase):
    def test_version(self):
        result = self.check("ffmpeg")
        self.assertEqual((result.status, result.summary), (OK, "Versión 5.1.6-0+deb12u1+rpt1"))
        command, kwargs = self.run.calls[0]
        self.assertEqual(command, ["/usr/bin/ffmpeg", "-hide_banner", "-version"])
        self.assertEqual(kwargs["timeout"], 3)
        self.assertEqual(len(self.run.calls), 1)

    def test_not_installed(self):
        result = self.check("ffmpeg", which=lambda binary: None)
        self.assertEqual((result.status, result.summary), (ERROR, "No instalado"))
        self.assertEqual(self.run.calls, [])

    def test_not_installed_on_windows_is_only_a_warning(self):
        self.assertEqual(self.check("ffmpeg", which=lambda b: None, platform="win32").status,
                         WARNING)

    def test_timeout(self):
        self.run.responses[("ffmpeg", "-hide_banner", "-version")] = \
            subprocess.TimeoutExpired("ffmpeg", 3)
        result = self.check("ffmpeg")
        self.assertEqual(result.status, ERROR)
        self.assertIn("tardó más de 3 s", result.details[0])
        self.assertIn("2 intentos", result.details[1])
        self.assertEqual(len(self.run.calls), 2)
        self.assertEqual([kwargs["timeout"] for _, kwargs in self.run.calls], [3, 3])

    def test_initial_timeout_then_success(self):
        run = mock.Mock(side_effect=[subprocess.TimeoutExpired("ffmpeg", 3),
                                     subprocess.CompletedProcess([], 0, "ffmpeg version 5.1\n")])
        with self.assertLogs("alarms", "WARNING") as logs:
            result = self.check("ffmpeg", run=run)
        self.assertEqual((result.status, result.summary), (OK, "Versión 5.1"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0], run.call_args_list[1])
        self.assertIn("tardó más de 3 s", "\n".join(logs.output))

    def test_oserror_and_missing_binary_are_not_retried(self):
        for error in (OSError(8, "Exec format error"), PermissionError(13, "denied"),
                      FileNotFoundError()):
            with self.subTest(error=error):
                run = mock.Mock(side_effect=error)
                result = self.check("ffmpeg", run=run)
                self.assertEqual((result.status, result.summary), (ERROR, "No se puede ejecutar"))
                self.assertTrue(result.details)
                run.assert_called_once()
                if error.strerror:
                    self.assertIn(error.strerror, result.details[0])

    def test_retry_does_not_hide_real_errors(self):
        for response in (OSError(8, "Exec format error"),
                         subprocess.CompletedProcess([], 1, "")):
            with self.subTest(response=response):
                run = mock.Mock(side_effect=[subprocess.TimeoutExpired("ffmpeg", 3), response])
                result = self.check("ffmpeg", run=run)
                self.assertEqual(result.status, ERROR)
                self.assertEqual(run.call_count, 2)
                self.assertIn("Exec format error" if isinstance(response, OSError) else "código 1",
                              result.details[0])

    def test_broken_binary(self):
        self.run.responses[("ffmpeg", "-hide_banner", "-version")] = ("", 1)
        self.assertEqual(self.check("ffmpeg").status, ERROR)
        self.assertEqual(len(self.run.calls), 1)
        self.run.responses[("ffmpeg", "-hide_banner", "-version")] = PermissionError(13, "denied")
        self.assertEqual(self.check("ffmpeg").summary, "No se puede ejecutar")


class LocalMusicCheckTest(HealthTestCase):
    def test_counts_valid_tracks(self):
        self.add_track("a.mp3")
        self.add_track("b.ogg")
        (self.music_dir / "notas.txt").write_text("no es música", encoding="utf-8")
        result = self.check("local_music")
        self.assertEqual((result.status, result.summary), (OK, "2 pistas"))

    def test_empty_library_is_a_warning(self):
        result = self.check("local_music")
        self.assertEqual((result.status, result.summary), (WARNING, "Biblioteca vacía"))
        self.assertIn("WAV de emergencia", result.details[0])

    def test_missing_folder_that_can_be_created(self):
        result = self.check("local_music", library=MusicLibrary(self.tmp / "nueva"))
        self.assertEqual(result.status, WARNING)
        self.assertFalse((self.tmp / "nueva").exists())  # el check no crea nada


class EmergencyCheckTest(HealthTestCase):
    def test_valid_wav(self):
        result = self.check("emergency")
        self.assertEqual((result.status, result.summary), (OK, "Listo"))
        self.assertIn("alarm.wav · 1.0 s · 8000 Hz", result.details)

    def test_missing_wav_is_an_error(self):
        self.sound.unlink()
        result = self.check("emergency")
        self.assertEqual((result.status, result.summary), (ERROR, "Falta el archivo"))
        self.assertNotIn(str(self.tmp), " ".join(result.details))  # solo el nombre, sin ruta

    def test_invalid_wav(self):
        self.sound.write_bytes(b"esto no es un wav")
        self.assertEqual(self.check("emergency").summary, "WAV no válido")


class RaspotifyCheckTest(HealthTestCase):
    def test_active(self):
        result = self.check("raspotify")
        self.assertEqual((result.status, result.summary), (OK, "Activo"))
        self.assertEqual(self.run.commands(),
                         [["systemctl", "--no-ask-password", "is-active", "raspotify.service"]])

    def test_stopped(self):
        self.run.responses.update(service_states(raspotify="inactive"))
        result = self.check("raspotify")
        self.assertEqual((result.status, result.summary), (ERROR, "Detenido"))

    def test_failed(self):
        self.run.responses.update(service_states(raspotify="failed"))
        self.assertEqual(self.check("raspotify").summary, "Ha fallado")

    def test_systemctl_timeout_or_missing(self):
        for exc, text in ((subprocess.TimeoutExpired("systemctl", 3), "tardó más de 3 s"),
                          (FileNotFoundError(), "no está instalado")):
            with self.subTest(exc=exc):
                self.run.responses[systemctl("raspotify.service")] = exc
                result = self.check("raspotify")
                self.assertEqual((result.status, result.summary), (ERROR, "No se pudo consultar"))
                self.assertIn(text, result.details[0])

    def test_not_checked_outside_linux(self):
        self.assertEqual(self.check("raspotify", platform="win32").status, WARNING)
        self.assertEqual(self.run.calls, [])


class SpotifyCheckTest(HealthTestCase):
    def test_groove_available(self):
        result = self.check("spotify")
        self.assertEqual((result.status, result.summary), (OK, "Conectado"))
        self.assertIn("«Groove» disponible", result.details)
        self.spotify.get_devices.assert_called_once_with(timeout=SPOTIFY_TIMEOUT)

    def test_uses_saved_device(self):
        db.write_setting(self.db_path, DEVICE_ID_KEY, "phone-1")
        db.write_setting(self.db_path, DEVICE_NAME_KEY, "Móvil")
        self.assertIn("«Móvil» disponible", self.check("spotify").details)

    def test_never_touches_playback(self):
        self.check("spotify")
        for method in ("play", "pause", "transfer_playback", "set_volume", "disconnect"):
            getattr(self.spotify, method).assert_not_called()

    def test_groove_missing_is_a_warning(self):
        self.spotify.get_devices.return_value = [PHONE]
        result = self.check("spotify")
        self.assertEqual(result.status, WARNING)
        self.assertIn("«Groove» no aparece ahora en Spotify.", result.details)

    def test_not_configured(self):
        self.spotify.is_configured = False
        result = self.check("spotify")
        self.assertEqual((result.status, result.summary), (WARNING, "No configurado"))
        self.spotify.get_devices.assert_not_called()

    def test_not_authenticated(self):
        self.spotify.is_connected.return_value = False
        result = self.check("spotify")
        self.assertEqual((result.status, result.summary), (WARNING, "Sin cuenta vinculada"))
        self.spotify.get_devices.assert_not_called()

    def test_api_down(self):
        self.spotify.get_devices.side_effect = SpotifyConnectionError(
            "No se pudo conectar con Spotify: timed out")
        result = self.check("spotify")
        self.assertEqual((result.status, result.summary), (ERROR, "Sin conexión con Spotify"))

    def test_api_error(self):
        self.spotify.get_devices.side_effect = SpotifyError("Error de Spotify: boom", 503)
        result = self.check("spotify")
        self.assertEqual((result.status, result.summary), (ERROR, "Error de la API"))
        self.assertIn("HTTP 503", result.details[0])

    def test_rejected_tokens(self):
        self.spotify.get_devices.side_effect = SpotifyAuthError("token rechazado", 401)
        self.assertEqual(self.check("spotify").summary, "Sesión no válida")

    def test_rate_limited_is_a_warning(self):
        self.spotify.get_devices.side_effect = SpotifyRateLimitError("espera", 30)
        self.assertEqual(self.check("spotify").status, WARNING)

    def test_groove_missing_has_machine_readable_code(self):
        self.spotify.get_devices.return_value = [PHONE]
        self.assertEqual(self.check("spotify").code, SPOTIFY_DEVICE_MISSING)
        self.spotify.get_devices.return_value = []  # cero dispositivos (el caso real)
        self.assertEqual(self.check("spotify").code, SPOTIFY_DEVICE_MISSING)

    def test_saved_id_missing_without_name_is_device_missing(self):
        self.config["SPOTIFY_DEVICE_NAME"] = ""
        db.write_setting(self.db_path, DEVICE_ID_KEY, "viejo-id")
        self.spotify.get_devices.return_value = [PHONE]
        self.assertEqual(self.check("spotify").code, SPOTIFY_DEVICE_MISSING)

    def test_other_outcomes_have_no_device_missing_code(self):
        errors = {
            "401": SpotifyAuthError("token rechazado", 401),
            "403": SpotifyForbiddenError("Premium requerido", 403),
            "429": SpotifyRateLimitError("espera", 30),
            "red": SpotifyConnectionError("timed out"),
            "503": SpotifyError("Error de Spotify: boom", 503),
        }
        for label, exc in errors.items():
            with self.subTest(label):
                self.spotify.get_devices.side_effect = exc
                self.assertIsNone(self.check("spotify").code)
        self.spotify.get_devices.side_effect = None
        with self.subTest("disponible"):
            self.assertIsNone(self.check("spotify").code)
        with self.subTest("conflicto"):
            self.spotify.get_devices.return_value = [GROOVE, dict(GROOVE, id="groove-2")]
            result = self.check("spotify")
            self.assertEqual(result.status, WARNING)
            self.assertIsNone(result.code)
        with self.subTest("sin dispositivo elegido"):
            self.config["SPOTIFY_DEVICE_NAME"] = ""
            self.assertIsNone(self.check("spotify").code)
        with self.subTest("sin cuenta vinculada"):
            self.spotify.is_connected.return_value = False
            self.assertIsNone(self.check("spotify").code)
        with self.subTest("no configurado"):
            self.spotify.is_configured = False
            self.assertIsNone(self.check("spotify").code)

    def test_code_is_not_exposed_in_the_report(self):
        self.spotify.get_devices.return_value = [PHONE]
        self.assertNotIn("code", self.checker().run().as_dict()["checks"][1])


class BluetoothCheckTest(HealthTestCase):
    def test_active_with_connected_devices(self):
        result = self.check("bluetooth")
        self.assertEqual((result.status, result.summary), (OK, "BlueALSA activo"))
        self.assertIn("1 dispositivo conectado", result.details)

    def test_bluealsa_stopped(self):
        self.run.responses.update(service_states(bluealsa="inactive"))
        result = self.check("bluetooth")
        self.assertEqual((result.status, result.summary), (ERROR, "BlueALSA detenido"))

    def test_player_stopped(self):
        self.run.responses.update(service_states(player="inactive"))
        self.assertEqual(self.check("bluetooth").summary, "Reproductor detenido")

    def test_player_stopped_by_an_alarm_is_ok(self):
        self.run.responses.update(service_states(player="inactive"))
        self.bluetooth.paused = True
        result = self.check("bluetooth")
        self.assertEqual((result.status, result.summary), (OK, "Pausado por la alarma"))

    def test_device_count_is_optional(self):
        self.run.responses[("bluetoothctl", "devices", "Connected")] = \
            subprocess.TimeoutExpired("bluetoothctl", 3)
        result = self.check("bluetooth")
        self.assertEqual(result.status, OK)
        self.assertFalse(any("conectado" in line for line in result.details))

    def test_systemctl_unavailable(self):
        self.run.responses[systemctl("bluealsa.service")] = FileNotFoundError()
        self.assertEqual(self.check("bluetooth").status, ERROR)

    def test_only_reads(self):
        self.check("bluetooth")
        for command in self.run.commands():
            if command[0] == "systemctl":
                self.assertEqual(command[2], "is-active")
            else:
                self.assertEqual(command, ["bluetoothctl", "devices", "Connected"])


class SchedulerCheckTest(HealthTestCase):
    def add_alarm(self, name="Trabajo", time="07:30", days="0,1,2,3,4", enabled=1):
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO alarms (name, time, days, enabled) VALUES (?, ?, ?, ?)",
                     (name, time, days, enabled))
        conn.commit()
        conn.close()

    def test_running_with_next_alarm(self):
        self.add_alarm()
        self.add_alarm("Apagada", "06:00", enabled=0)
        sched = FakeScheduler(jobs=("check_alarms", "preflight-sync", "preflight:1"))
        result = self.check("scheduler", scheduler=sched)
        self.assertEqual((result.status, result.summary), (OK, "Funcionando"))
        self.assertIn("Próxima alarma: 07:30 (hoy) · «Trabajo»", result.details)
        self.assertIn("1 alarma activa · 1 pre-flight programado", result.details)

    def test_without_alarms(self):
        self.assertIn("Sin próximas alarmas", self.check("scheduler").details)

    def test_not_started(self):
        result = self.check("scheduler", scheduler=None)
        self.assertEqual((result.status, result.summary), (ERROR, "No iniciado"))
        self.config["SCHEDULER_ENABLED"] = False
        self.assertEqual(self.check("scheduler", scheduler=None).status, WARNING)

    def test_stopped_or_without_alarm_job(self):
        self.assertEqual(self.check("scheduler", scheduler=FakeScheduler(state=0)).summary, "Parado")
        self.assertEqual(self.check("scheduler", scheduler=FakeScheduler(jobs=())).status, ERROR)


class RunnerTest(HealthTestCase):
    def test_runs_every_check_in_order_and_remembers_it(self):
        checker = self.checker()
        self.assertIsNone(checker.last)
        report = checker.run(trigger="Manual")
        self.assertEqual([r.id for r in report.results],
                         ["audio", "spotify", "raspotify", "local_music", "ffmpeg", "emergency",
                          "bluetooth", "scheduler"])
        self.assertIs(checker.last, report)
        self.assertEqual((report.trigger, report.checked_at), ("Manual", NOW))
        self.assertEqual(report.status, WARNING)  # biblioteca vacía
        self.assertEqual(report.count(WARNING), 1)

    def test_a_failing_check_does_not_break_the_others(self):
        checker = self.checker()
        with mock.patch.object(checker, "check_audio", side_effect=RuntimeError("boom")), \
                self.assertLogs("alarms", "ERROR"):
            report = checker.run()
        audio = report.get("audio")
        self.assertEqual((audio.status, audio.summary), (ERROR, "No se pudo comprobar"))
        self.assertNotIn("boom", " ".join(audio.details))  # sin trazas en la web
        self.assertEqual(report.status_of("emergency"), OK)
        self.assertEqual(len(report.results), 8)

    def test_a_hanging_check_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        checker = self.checker(total_timeout=0.2)
        with mock.patch.object(checker, "check_spotify", side_effect=lambda: release.wait(5)), \
                self.assertLogs("alarms", "WARNING"):
            report = checker.run()
        self.assertEqual((report.get("spotify").status, report.get("spotify").summary),
                         (ERROR, "Sin respuesta"))
        self.assertEqual(report.status_of("raspotify"), OK)

    def test_every_dependency_down(self):
        self.run = FakeRun(default=subprocess.TimeoutExpired("x", 3))
        self.spotify.get_devices.side_effect = SpotifyConnectionError("sin red")
        self.sound.unlink()
        report = self.checker(asound_dir=str(self.tmp / "nada"), scheduler=None).run()
        self.assertEqual(len(report.results), 8)
        for check_id in ("audio", "spotify", "raspotify", "ffmpeg", "emergency", "bluetooth",
                         "scheduler"):
            self.assertEqual(report.status_of(check_id), ERROR, check_id)

    def test_report_has_no_secrets(self):
        text = str(self.checker().run().as_dict())
        self.assertNotIn(SECRET, text)
        self.assertNotIn(str(self.tmp), text)


class DiagnosticsPageTest(unittest.TestCase):
    def setUp(self):
        block_real_processes(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(scheduler.close_logging)
        self.tmp = Path(tmp.name)
        self.db_path = str(self.tmp / "test.db")
        spotify = mock.Mock(is_configured=True, redirect_uri="http://127.0.0.1:5000/cb")
        spotify.is_connected.return_value = True
        spotify.get_devices.return_value = [GROOVE]
        self.app = create_app(
            {"TESTING": True, "DATABASE": self.db_path, "ALARM_LOG": str(self.tmp / "a.log"),
             "SPOTIFY_CLIENT_SECRET": SECRET, "SPOTIFY_DEVICE_NAME": "Groove",
             "SOUND_PATH": str(self.tmp / "alarm.wav")},
            player=mock.Mock(spec=AudioPlayer), spotify=spotify)
        write_wav(self.tmp / "alarm.wav")
        self.health = self.app.extensions["health"]
        # Como en la Raspberry, pero con dobles: nada se ejecuta de verdad.
        self.health.platform = "linux"
        self.health._run = FakeRun(service_states(), default=("", 1))
        self.health._which = lambda binary: None
        self.health.asound = self.tmp / "sin-alsa"
        self.times = iter([datetime(2026, 9, 28, 7, 0, 5), datetime(2026, 9, 28, 7, 1, 10)])
        self.health._clock = lambda: next(self.times)
        self.client = self.app.test_client()

    def html(self, url="/diagnostics/"):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_page_shows_every_component(self):
        html = self.html()
        for text in ("Diagnóstico", "Audio USB", "Spotify", "Raspotify", "Música local",
                     "ffmpeg", "WAV de emergencia", "Bluetooth", "Scheduler",
                     "«Groove» disponible", "Biblioteca vacía", "BlueALSA activo",
                     "Comprobar ahora", "Última comprobación", "28/09 07:00:05",
                     "Al abrir Diagnóstico"):
            self.assertIn(text, html)
        self.assertRegex(html, r'href="/diagnostics/"\s+aria-current="page"')

    def test_first_visit_runs_later_visits_reuse_the_last_report(self):
        self.html()
        self.assertIn("28/09 07:00:05", self.html())  # no hay segunda comprobación

    def test_check_now_button(self):
        self.html()
        resp = self.client.post("/diagnostics/check")
        self.assertEqual(resp.status_code, 302)
        html = self.html()
        self.assertIn("28/09 07:01:10", html)
        self.assertIn("Manual", html)
        self.assertIn("Comprobación terminada", html)

    def test_a_crashing_check_does_not_break_the_page(self):
        with mock.patch.object(self.health, "check_scheduler", side_effect=ValueError("x")), \
                self.assertLogs("alarms", "ERROR"):
            html = self.html()
        self.assertIn("No se pudo comprobar", html)
        self.assertNotIn("Traceback", html)

    def test_no_secrets_or_tokens(self):
        db.init_db(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO spotify_auth (id, access_token, refresh_token, expires_at)"
                     " VALUES (1, ?, 'refresh-no-mostrar', 0)", (ACCESS_TOKEN,))
        conn.commit()
        conn.close()
        html = self.html()
        json_text = self.client.get("/diagnostics/report.json").get_data(as_text=True)
        for text in (html, json_text):
            for secret in (SECRET, ACCESS_TOKEN, "refresh-no-mostrar", str(self.tmp)):
                self.assertNotIn(secret, text)

    def test_report_json(self):
        self.assertEqual(self.client.get("/diagnostics/report.json").get_json()["checks"], [])
        self.client.post("/diagnostics/check")
        data = self.client.get("/diagnostics/report.json").get_json()
        self.assertEqual(data["trigger"], "Manual")
        self.assertEqual(len(data["checks"]), 8)

    def test_nav_link_on_every_page(self):
        for url in ("/", "/spotify/", "/music/"):
            with self.subTest(url=url):
                self.assertIn('href="/diagnostics/"', self.html(url))

    def test_shows_preflight_report(self):
        self.health._last = HealthReport(datetime(2026, 9, 28, 7, 25), "Pre-flight de «Trabajo»",
                                         ())
        self.assertIn("Pre-flight de «Trabajo»", self.html())


if __name__ == "__main__":
    unittest.main()
