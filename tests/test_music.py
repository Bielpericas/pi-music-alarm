"""Biblioteca de música local, ffmpeg y página Música.

Nunca se ejecuta ffmpeg ni ALSA: subprocess.Popen está bloqueado y los
procesos son dobles. Todos los ficheros viven en directorios temporales.
"""
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import (  # noqa: E402
    DEFAULT_MUSIC_DEVICE, AudioPlayer, FfmpegPlayer, create_music_player,
)
from bluetooth_audio import BluetoothAudio  # noqa: E402
from music_library import LocalMusic, MusicLibrary, UploadError, is_safe_name  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from spotify_client import SpotifyClient  # noqa: E402
from spotify_player import SpotifyAlarmPlayer  # noqa: E402

NOW = datetime(2026, 9, 28, 7, 30)
PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
MP3_NO_TAG = b"\xff\xfb\x90\x64" + b"\x00" * 64
OGG = b"OggS\x00\x02" + b"\x00" * 64
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64


def block_real_processes(test):
    for target in ("Popen", "run"):
        guard = mock.patch.object(subprocess, target,
                                  side_effect=AssertionError("los tests no ejecutan procesos"))
        guard.start()
        test.addCleanup(guard.stop)


def alarm(source="local", track=None, id=1, name="Despertador", **extra):
    data = {"id": id, "name": name, "time": "07:30", "source": source,
            "spotify_uri": PLAYLIST_URI if source == "spotify" else None,
            "local_track": track}
    data.update(extra)
    return data


class FakeProcess:
    """Proceso falso de ffmpeg: sigue "sonando" hasta terminate()/kill()."""

    def __init__(self, exit_code=None, ignores_terminate=False, stderr=b""):
        self.returncode = exit_code
        self.ignores_terminate = ignores_terminate
        self.stderr = io.BytesIO(stderr)
        self.terminated = self.killed = False
        self._done = threading.Event()
        if exit_code is not None:
            self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.ignores_terminate:
            self._finish(-15)

    def kill(self):
        self.killed = True
        self._finish(-9)

    def _finish(self, code):
        if self.returncode is None:
            self.returncode = code
        self._done.set()

    @property
    def running(self):
        return self.returncode is None


class FakePopen:
    """Sustituye a subprocess.Popen: guarda órdenes y devuelve FakeProcess."""

    def __init__(self, **process_kwargs):
        self.process_kwargs = process_kwargs
        self.calls = []
        self.processes = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        process = FakeProcess(**self.process_kwargs)
        self.processes.append(process)
        return process

    @property
    def running(self):
        return [p for p in self.processes if p.running]


def make_player(popen, **kwargs):
    kwargs.setdefault("startup_grace", 0.01)
    kwargs.setdefault("stop_timeout", 0.05)
    return FfmpegPlayer("ffmpeg", "plughw:CARD=Device,DEV=0", popen=popen, **kwargs)


class TempDirTest(unittest.TestCase):
    def setUp(self):
        block_real_processes(self)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.base = Path(self.tmpdir.name)
        self.music = self.base / "music"
        self.library = MusicLibrary(self.music)

    def add(self, name, data=MP3):
        self.music.mkdir(exist_ok=True)
        (self.music / name).write_bytes(data)


# --- Biblioteca ---

class LibraryTest(TempDirTest):
    def test_discovers_mp3_ogg_wav_in_stable_order(self):
        for name in ("b.ogg", "Alba.MP3", "c.wav", "aurora.mp3"):
            self.add(name)
        self.assertEqual(self.library.names(), ["Alba.MP3", "aurora.mp3", "b.ogg", "c.wav"])
        track = self.library.tracks()[0]
        self.assertEqual((track.name, track.kind, track.size), ("Alba.MP3", "MP3", len(MP3)))

    def test_ignores_unsupported_hidden_and_folders(self):
        for name in ("song.flac", "notes.txt", "cover.jpg", ".hidden.mp3",
                     ".upload-123.part", "noext"):
            self.add(name)
        (self.music / "sub.mp3").mkdir()
        self.add("ok.wav")
        self.assertEqual(self.library.names(), ["ok.wav"])

    def test_missing_or_empty_library(self):
        self.assertEqual(self.library.tracks(), [])  # la carpeta no existe
        self.assertTrue(self.library.ensure_dir())
        self.assertTrue(self.music.is_dir())
        self.assertEqual(self.library.tracks(), [])
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertIsNone(self.library.pick(None))
        self.assertIn("está vacía", "\n".join(logs.output))

    def test_rejects_path_traversal_and_foreign_paths(self):
        self.add("ok.mp3")
        (self.base / "outside.mp3").write_bytes(MP3)
        for name in ("../outside.mp3", "..\\outside.mp3", str(self.base / "outside.mp3"),
                     "/etc/passwd.mp3", "sub/ok.mp3", "C:ok.mp3", "..", ".mp3", "",
                     " ok.mp3", "ok.mp3\0", None, 42, "ok.txt"):
            with self.subTest(name=name):
                self.assertFalse(is_safe_name(name) and self.library.path(name))
                self.assertIsNone(self.library.path(name))
        self.assertEqual(self.library.path("ok.mp3"), self.music / "ok.mp3")

    @unittest.skipIf(sys.platform == "win32", "enlaces simbólicos: se prueba en Linux")
    def test_symlink_pointing_outside_is_ignored(self):
        (self.base / "secret.mp3").write_bytes(MP3)
        self.music.mkdir()
        os.symlink(self.base / "secret.mp3", self.music / "link.mp3")
        self.assertEqual(self.library.names(), [])
        self.assertIsNone(self.library.path("link.mp3"))

    def test_pick_specific_and_random(self):
        for name in ("a.mp3", "b.ogg", "c.wav"):
            self.add(name)
        self.assertEqual(self.library.pick("b.ogg"), self.music / "b.ogg")
        choice = mock.Mock(return_value="c.wav")
        self.assertEqual(self.library.pick(None, choice), self.music / "c.wav")
        choice.assert_called_once_with(["a.mp3", "b.ogg", "c.wav"])
        self.assertEqual(self.library.pick("", choice), self.music / "c.wav")

    def test_pick_deleted_track_does_not_substitute(self):
        self.add("other.mp3")
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertIsNone(self.library.pick("gone.mp3"))
        self.assertIn("«gone.mp3» ya no está en la biblioteca", "\n".join(logs.output))


class UploadTest(TempDirTest):
    def save(self, filename, data, max_bytes=1024 * 1024):
        return self.library.save_upload(filename, io.BytesIO(data), max_bytes)

    def test_saves_valid_files_creating_the_folder(self):
        for filename, data in (("Sunrise.MP3", MP3), ("raw.mp3", MP3_NO_TAG),
                               ("chill.ogg", OGG), ("alarm.wav", WAV)):
            with self.subTest(filename=filename):
                name = self.save(filename, data)
                self.assertEqual((self.music / name).read_bytes(), data)
        self.assertEqual(self.library.names(), ["alarm.wav", "chill.ogg", "raw.mp3", "Sunrise.mp3"])
        self.assertEqual([p.name for p in self.music.iterdir() if p.name.startswith(".")], [])

    def test_sanitizes_names_and_never_leaves_the_folder(self):
        for filename in ("../../evil.mp3", "..\\..\\evil2.mp3", "/tmp/evil3.mp3",
                         "C:\\Windows\\evil4.mp3"):
            with self.subTest(filename=filename):
                name = self.save(filename, MP3)
                self.assertTrue((self.music / name).is_file())
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["music"])
        self.assertEqual(self.save("canción de día.mp3", MP3), "cancion_de_dia.mp3")

    def test_rejects_unsupported_or_fake_files(self):
        cases = [("song.flac", MP3), ("notes.txt", b"hola"), ("noext", MP3), ("", MP3),
                 ("../..", MP3), ("fake.mp3", b"<html>no es audio</html>"),
                 ("fake.ogg", WAV), ("empty.wav", b"")]
        for filename, data in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(UploadError):
                    self.save(filename, data)
        self.assertEqual(self.library.names(), [])
        self.assertEqual(list(self.music.iterdir()), [])  # sin temporales olvidados

    def test_size_limit(self):
        with self.assertRaises(UploadError) as ctx:
            self.save("big.mp3", MP3 + b"\x00" * 2048, max_bytes=1024)
        self.assertIn("demasiado grande", str(ctx.exception))
        self.assertEqual(list(self.music.iterdir()), [])

    def test_never_overwrites(self):
        self.save("song.mp3", MP3)
        with self.assertRaises(UploadError) as ctx:
            self.save("song.mp3", MP3_NO_TAG)
        self.assertIn("Ya existe «song.mp3»", str(ctx.exception))
        self.assertEqual((self.music / "song.mp3").read_bytes(), MP3)

    def test_delete_only_inside_library(self):
        self.add("song.mp3")
        (self.base / "outside.mp3").write_bytes(MP3)
        for name in ("../outside.mp3", str(self.base / "outside.mp3"), "missing.mp3", ""):
            with self.subTest(name=name):
                self.assertFalse(self.library.delete(name))
        self.assertTrue((self.base / "outside.mp3").exists())
        self.assertTrue(self.library.delete("song.mp3"))
        self.assertEqual(self.library.names(), [])


# --- ffmpeg ---

class FfmpegPlayerTest(TempDirTest):
    def test_command_without_shell(self):
        popen = FakePopen()
        player = make_player(popen)
        self.assertTrue(player.play(self.music / "a b; rm -rf.mp3"))
        command, kwargs = popen.calls[0]
        self.assertEqual(command, [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-stream_loop", "-1",
            "-i", str(self.music / "a b; rm -rf.mp3"), "-f", "alsa", "plughw:CARD=Device,DEV=0"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        player.stop()

    def test_stop_terminates(self):
        popen = FakePopen()
        player = make_player(popen)
        player.play("a.mp3")
        self.assertTrue(player.stop())
        process = popen.processes[0]
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertEqual(popen.running, [])
        self.assertTrue(player.stop())  # idempotente

    def test_process_ignoring_terminate_is_killed(self):
        popen = FakePopen(ignores_terminate=True)
        player = make_player(popen)
        player.play("a.mp3")
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertTrue(player.stop())
        self.assertTrue(popen.processes[0].killed)
        self.assertEqual(popen.running, [])
        self.assertIn("kill", "\n".join(logs.output))

    def test_play_replaces_previous_process(self):
        popen = FakePopen()
        player = make_player(popen)
        player.play("a.mp3")
        player.play("b.mp3")
        self.assertEqual(len(popen.running), 1)
        self.assertIs(popen.running[0], popen.processes[1])
        player.stop()

    def test_ffmpeg_not_installed(self):
        player = make_player(mock.Mock(side_effect=FileNotFoundError("ffmpeg")))
        with self.assertLogs("alarms", "ERROR") as logs:
            self.assertFalse(player.play("a.mp3"))
        self.assertIn("sudo apt install -y ffmpeg", "\n".join(logs.output))
        self.assertTrue(player.stop())

    def test_ffmpeg_failing_immediately(self):
        popen = FakePopen(exit_code=1, stderr=b"a.mp3: Invalid data found when processing input")
        player = make_player(popen)
        with self.assertLogs("alarms", "ERROR") as logs:
            self.assertFalse(player.play("a.mp3"))
        self.assertIn("Invalid data found", "\n".join(logs.output))

    def test_create_music_player(self):
        self.assertIsNone(create_music_player({}, platform="win32"))
        self.assertIsNone(create_music_player({"AUDIO_BACKEND": "none"}, platform="linux"))
        player = create_music_player({}, platform="linux")
        self.assertEqual((player.binary, player.alsa_device), ("ffmpeg", DEFAULT_MUSIC_DEVICE))
        player = create_music_player({"FFMPEG_BINARY": "/opt/ffmpeg", "ALSA_DEVICE": "hw:1"},
                                     platform="linux")
        self.assertEqual((player.binary, player.alsa_device), ("/opt/ffmpeg", "hw:1"))


# --- Alarmas: Spotify → música local → WAV de emergencia ---

class ManagerMusicTest(TempDirTest):
    def setUp(self):
        super().setUp()
        for name in ("a.mp3", "b.ogg", "c.wav"):
            self.add(name)
        self.popen = FakePopen()
        self.choices = iter(["a.mp3", "b.ogg", "c.wav"])
        self.music_player = LocalMusic(self.library, make_player(self.popen),
                                       choice=lambda names: next(self.choices))
        self.emergency = mock.Mock(spec=AudioPlayer)
        self.spotify = mock.Mock(spec=SpotifyAlarmPlayer)
        self.spotify.play.return_value = True
        self.spotify.stop.return_value = True
        self.bluetooth = mock.Mock(spec=BluetoothAudio)
        self.jobs = []
        self.manager = AlarmPlaybackManager(
            self.emergency, self.spotify, clock=lambda: NOW, bluetooth=self.bluetooth,
            music=self.music_player,
            schedule_once=lambda run_at, callback: self.jobs.append(callback) or mock.Mock())
        self.addCleanup(self.manager.stop)

    def played(self):
        return [Path(command[command.index("-i") + 1]).name for command, _ in self.popen.calls]

    def test_running_ffmpeg_crash_reaches_manager_and_uses_wav(self):
        fallback = threading.Event()
        self.emergency.play.side_effect = lambda: fallback.set() or True
        self.manager.start(alarm(track="a.mp3"))
        self.popen.processes[0]._finish(1)
        self.assertTrue(fallback.wait(2), "El vigilante no activó el respaldo")
        with self.manager._lock:
            self.assertEqual(self.manager.active.status, "playing")
        self.emergency.play.assert_called_once_with()

    def test_running_ffmpeg_crash_and_unavailable_wav_reports_failure(self):
        fallback = threading.Event()
        self.emergency.play.side_effect = lambda: fallback.set() and False
        self.manager.start(alarm(track="a.mp3"))
        self.popen.processes[0]._finish(1)
        self.assertTrue(fallback.wait(2))
        with self.manager._lock:
            self.assertEqual(self.manager.active.status, "failed")

    def test_specific_track(self):
        self.assertEqual(self.manager.start(alarm(track="b.ogg")), "local")
        self.assertEqual(self.played(), ["b.ogg"])
        self.emergency.play.assert_not_called()

    def test_random_track_chosen_again_after_snooze(self):
        self.manager.start(alarm())
        self.manager.snooze()
        self.assertEqual(self.popen.running, [])
        self.jobs[-1]()  # vuelve a sonar
        self.assertEqual(self.played(), ["a.mp3", "b.ogg"])

    def test_deleted_track_uses_emergency_wav(self):
        self.library.delete("b.ogg")
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertEqual(self.manager.start(alarm(track="b.ogg")), "local")
        self.assertEqual(self.popen.calls, [])
        self.emergency.play.assert_called_once_with()
        text = "\n".join(logs.output)
        self.assertIn("«b.ogg» ya no está en la biblioteca", text)
        self.assertIn("suena el WAV de emergencia", text)

    def test_empty_library_uses_emergency_wav(self):
        for name in ("a.mp3", "b.ogg", "c.wav"):
            self.library.delete(name)
        with self.assertLogs("alarms", "WARNING"):
            self.manager.start(alarm())
        self.emergency.play.assert_called_once_with()

    def test_ffmpeg_missing_or_failing_uses_emergency_wav(self):
        for popen in (mock.Mock(side_effect=FileNotFoundError("ffmpeg")), FakePopen(exit_code=1)):
            with self.subTest(popen=popen):
                self.emergency.reset_mock()
                self.music_player.player = make_player(popen)
                with self.assertLogs("alarms", "WARNING"):
                    self.assertEqual(self.manager.start(alarm(track="a.mp3")), "local")
                self.emergency.play.assert_called_once_with()
                self.manager.stop()

    def test_spotify_fallback_plays_the_alarm_selection(self):
        self.spotify.play.return_value = False
        self.assertEqual(self.manager.start(alarm("spotify", track="c.wav")), "fallback")
        self.assertEqual(self.played(), ["c.wav"])
        self.emergency.play.assert_not_called()
        self.bluetooth.pause.assert_called_once_with()
        self.bluetooth.resume.assert_not_called()  # sigue parado mientras suena el respaldo
        self.manager.stop()
        self.assertEqual(self.popen.running, [])
        self.bluetooth.resume.assert_called_once_with()

    def test_spotify_ok_does_not_touch_local_music(self):
        self.assertEqual(self.manager.start(alarm("spotify", track="c.wav")), "spotify")
        self.assertEqual(self.popen.calls, [])

    def test_stop_snooze_autostop_delete_and_replace_leave_no_ffmpeg(self):
        def auto_stop():
            self.jobs[-1]()
        actions = {
            "stop": self.manager.stop,
            "snooze": self.manager.snooze,
            "auto-stop": auto_stop,
            "borrar": lambda: self.manager.forget(1),
            "otra alarma local": lambda: self.manager.start(alarm(id=2, track="a.mp3")),
            "otra alarma Spotify": lambda: self.manager.start(alarm("spotify", id=3)),
        }
        for label, action in actions.items():
            with self.subTest(action=label):
                self.choices = iter(["a.mp3"] * 3)
                self.manager.start(alarm(track="b.ogg", max_duration_minutes=30))
                first = self.popen.processes[-1]
                self.assertTrue(first.running)
                action()
                self.assertFalse(first.running)
                self.manager.stop()
                self.assertEqual(self.popen.running, [])

    def test_stuck_ffmpeg_is_killed_on_stop(self):
        self.music_player.player = make_player(FakePopen(ignores_terminate=True))
        self.manager.start(alarm(track="a.mp3"))
        process = self.music_player.player._process
        with self.assertLogs("alarms", "WARNING"):
            result = self.manager.stop()
        self.assertTrue(result.silenced)
        self.assertTrue(process.killed)


# --- Página Música y formularios ---

class MusicWebTest(unittest.TestCase):
    def setUp(self):
        block_real_processes(self)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.addCleanup(scheduler.close_logging)
        self.base = Path(self.tmpdir.name)
        self.db_path = str(self.base / "t.db")
        self.music = self.base / "music"
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self, **config):
        base = {"TESTING": True, "SECRET_KEY": "t", "DATABASE": self.db_path,
                "ALARM_LOG": str(self.base / "a.log")}
        base.update(config)
        return create_app(base, player=mock.Mock(spec=AudioPlayer),
                          spotify=mock.Mock(spec=SpotifyClient))

    def upload(self, filename, data, follow=True):
        return self.client.post("/music/upload", data={"file": (io.BytesIO(data), filename)},
                                content_type="multipart/form-data", follow_redirects=follow)

    def page(self, url="/music/"):
        return self.client.get(url).get_data(as_text=True)

    def create_alarm(self, **extra):
        data = {"name": "Trabajo", "time": "07:30", "days": ["0"]}
        data.update(extra)
        return self.client.post("/alarms/new", data=data)

    def local_tracks(self):
        conn = sqlite3.connect(self.db_path)
        rows = [row[0] for row in conn.execute("SELECT local_track FROM alarms ORDER BY id")]
        conn.close()
        return rows

    def test_folder_created_inside_instance_and_page_in_navigation(self):
        self.assertTrue(self.music.is_dir())
        self.assertEqual(self.app.config["MUSIC_DIR"], str(self.music))
        html = self.page()
        self.assertRegex(html, r'href="/music/"\s+aria-current="page"')
        self.assertIn("Todavía no hay música local", html)
        self.assertIn('enctype="multipart/form-data"', html)
        self.assertIn("Máximo 50 MB", html)

    def test_upload_each_format_and_list(self):
        for filename, data in (("sunrise.mp3", MP3), ("chill.ogg", OGG), ("emergency.wav", WAV)):
            with self.subTest(filename=filename):
                html = self.upload(filename, data).get_data(as_text=True)
                self.assertIn(f"«{filename}» añadida a la música local.", html)
        html = self.page()
        for text in ("sunrise.mp3", "chill.ogg", "emergency.wav", ">MP3<", ">OGG<", ">WAV<",
                     "1 KB", "Eliminar"):
            self.assertIn(text, html)
        self.assertIn("data-confirm=\"¿Eliminar «chill.ogg»?", html)

    def test_upload_errors_are_friendly(self):
        cases = [("song.flac", MP3, "Tipo de archivo no admitido"),
                 ("fake.mp3", b"no soy un mp3", "no parece un MP3 válido")]
        for filename, data, message in cases:
            with self.subTest(filename=filename):
                html = self.upload(filename, data).get_data(as_text=True)
                self.assertIn(message, html)
        html = self.client.post("/music/upload", data={}, follow_redirects=True).get_data(
            as_text=True)
        self.assertIn("Elige un archivo", html)
        self.assertEqual(list(self.music.iterdir()), [])

    def test_upload_path_traversal_stays_in_library(self):
        self.upload("../../../evil.mp3", MP3)
        self.upload("..\\..\\evil2.mp3", MP3)
        self.assertEqual(sorted(p.name for p in self.music.iterdir()), ["evil.mp3", "evil2.mp3"])
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),
                         ["a.log", "music", "t.db"])

    def test_upload_size_limit(self):
        self.app.config["LOCAL_MUSIC_MAX_UPLOAD_MB"] = 1
        self.app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
        # Mayor que la petición permitida: lo corta Flask (413) con un mensaje amable.
        resp = self.upload("huge.mp3", MP3 + b"\x00" * (3 * 1024 * 1024), follow=False)
        self.assertEqual(resp.status_code, 302)
        html = self.page()
        self.assertIn("demasiado grande (máximo 1 MB)", html)
        # Cabe en la petición pero el fichero supera el límite exacto.
        html = self.upload("big.mp3", MP3 + b"\x00" * (1024 * 1024)).get_data(as_text=True)
        self.assertIn("demasiado grande (máximo 1 MB)", html)
        self.assertEqual(list(self.music.iterdir()), [])

    def test_upload_limit_from_config(self):
        app = self.make_app(LOCAL_MUSIC_MAX_UPLOAD_MB=5)
        self.assertEqual(app.config["MAX_CONTENT_LENGTH"], 6 * 1024 * 1024)

    def test_duplicate_is_rejected(self):
        self.upload("song.mp3", MP3)
        html = self.upload("song.mp3", MP3_NO_TAG).get_data(as_text=True)
        self.assertIn("Ya existe «song.mp3»", html)
        self.assertEqual((self.music / "song.mp3").read_bytes(), MP3)

    def test_write_error(self):
        with mock.patch.object(MusicLibrary, "_write_limited", side_effect=OSError("disco lleno")):
            with self.assertLogs("alarms", "ERROR"):
                html = self.upload("song.mp3", MP3).get_data(as_text=True)
        self.assertIn("No se pudo guardar el archivo", html)
        self.assertEqual(list(self.music.iterdir()), [])

    def test_folder_recreated_if_removed(self):
        self.music.rmdir()
        self.upload("song.mp3", MP3)
        self.assertTrue((self.music / "song.mp3").is_file())

    def test_delete(self):
        self.upload("song.mp3", MP3)
        html = self.client.post("/music/delete", data={"name": "song.mp3"},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("«song.mp3» eliminada de la música local.", html)
        self.assertFalse((self.music / "song.mp3").exists())

    def test_delete_outside_library_is_refused(self):
        for name in ("../t.db", self.db_path, "../a.log", "missing.mp3", ""):
            with self.subTest(name=name):
                html = self.client.post("/music/delete", data={"name": name},
                                        follow_redirects=True).get_data(as_text=True)
                self.assertIn("no está en la biblioteca", html)
        self.assertTrue(Path(self.db_path).exists())

    def test_selectors_follow_the_library_immediately(self):
        html = self.page("/alarms/new")
        self.assertIn("Aún no hay música local", html)
        self.assertNotIn('<select id="local_track"', html)
        self.upload("sunrise.mp3", MP3)
        html = self.page("/alarms/new")
        self.assertIn('<option value="" selected>Aleatorio</option>', html)
        self.assertIn('<option value="sunrise.mp3" >sunrise.mp3</option>', html)
        self.assertIn(">Música local</label>", html)
        self.client.post("/music/delete", data={"name": "sunrise.mp3"})
        self.assertNotIn('value="sunrise.mp3"', self.page("/alarms/new"))

    def test_create_and_edit_with_track_or_random(self):
        self.upload("sunrise.mp3", MP3)
        self.create_alarm(local_track="sunrise.mp3")
        self.create_alarm(local_track="")
        self.create_alarm()
        self.assertEqual(self.local_tracks(), ["sunrise.mp3", None, None])
        html = self.page("/alarms/1/edit")
        self.assertIn('<option value="sunrise.mp3" selected>', html)
        self.client.post("/alarms/1/edit", data={"name": "Trabajo", "time": "07:30",
                                                 "local_track": ""})
        self.assertEqual(self.local_tracks()[0], None)

    def test_spotify_alarm_labels_it_backup_music(self):
        self.upload("sunrise.mp3", MP3)
        self.create_alarm(source="spotify", spotify_uri=PLAYLIST_URI, local_track="sunrise.mp3")
        html = self.page("/alarms/1/edit")
        self.assertIn(">Música de respaldo</label>", html)
        self.assertIn('data-label-local="Música local"', html)

    def test_invalid_track_is_rejected(self):
        self.upload("sunrise.mp3", MP3)
        for value in ("other.mp3", "../t.db", "/etc/passwd"):
            with self.subTest(value=value):
                resp = self.create_alarm(local_track=value)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("no está en la biblioteca", resp.get_data(as_text=True))
        self.assertEqual(self.local_tracks(), [])

    def test_alarm_with_deleted_track(self):
        self.upload("sunrise.mp3", MP3)
        self.upload("chill.ogg", OGG)
        self.create_alarm(local_track="sunrise.mp3")
        html = self.page("/music/")
        self.assertIn("elegida en 1 alarma", html)
        self.assertIn("La usan: Trabajo", html)
        self.client.post("/music/delete", data={"name": "sunrise.mp3"})
        self.assertEqual(self.local_tracks(), ["sunrise.mp3"])  # la alarma no se toca
        html = self.page("/alarms/1/edit")
        self.assertIn('<option value="sunrise.mp3" selected>sunrise.mp3 (no disponible)</option>',
                      html)
        self.assertIn("«sunrise.mp3» ya no está", html)
        # Guardar sin cambiarla la conserva; se puede elegir otra.
        form = {"name": "Trabajo", "time": "07:30", "local_track": "sunrise.mp3"}
        self.assertEqual(self.client.post("/alarms/1/edit", data=form).status_code, 302)
        self.assertEqual(self.local_tracks(), ["sunrise.mp3"])
        form["local_track"] = "chill.ogg"
        self.client.post("/alarms/1/edit", data=form)
        self.assertEqual(self.local_tracks(), ["chill.ogg"])
        self.assertNotIn("no disponible", self.page("/alarms/1/edit"))

    def test_deleted_track_at_ring_time_uses_emergency(self):
        popen = FakePopen()
        library = self.app.extensions["music_library"]
        emergency = mock.Mock(spec=AudioPlayer)
        manager = AlarmPlaybackManager(emergency, None, clock=lambda: NOW,
                                       music=LocalMusic(library, make_player(popen)))
        self.upload("sunrise.mp3", MP3)
        self.create_alarm(local_track="sunrise.mp3")
        self.client.post("/music/delete", data={"name": "sunrise.mp3"})
        with self.assertLogs("alarms", "WARNING"):
            fired = scheduler.check_alarms(self.db_path, now=NOW, manager=manager)
        self.assertEqual(fired, ["Trabajo"])
        emergency.play.assert_called_once_with()
        self.assertEqual(popen.calls, [])

    def test_tests_never_launch_ffmpeg(self):
        self.assertIsNone(self.app.extensions["music"])

    def test_migrates_existing_alarms_to_random(self):
        old = str(self.base / "old.db")
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE alarms (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            " time TEXT NOT NULL, days TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL"
            " DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " last_triggered TEXT, source TEXT NOT NULL DEFAULT 'local', spotify_uri TEXT,"
            " volume_start INTEGER NOT NULL DEFAULT 20, volume_end INTEGER NOT NULL DEFAULT 60,"
            " fade_minutes INTEGER NOT NULL DEFAULT 5,"
            " max_duration_minutes INTEGER NOT NULL DEFAULT 30)")
        conn.execute("INSERT INTO alarms (name, time, days, source, spotify_uri,"
                     " max_duration_minutes) VALUES"
                     f" ('Vieja', '06:45', '0,4', 'spotify', '{PLAYLIST_URI}', 45)")
        conn.commit()
        conn.close()
        db.init_db(old)
        db.init_db(old)
        conn = sqlite3.connect(old)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM alarms").fetchone())
        conn.close()
        self.assertIsNone(row["local_track"])  # Aleatorio
        self.assertEqual((row["name"], row["time"], row["days"], row["source"],
                          row["spotify_uri"], row["max_duration_minutes"]),
                         ("Vieja", "06:45", "0,4", "spotify", PLAYLIST_URI, 45))


if __name__ == "__main__":
    unittest.main()
