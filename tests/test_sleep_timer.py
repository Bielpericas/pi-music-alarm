"""Temporizador de sueño (sleep_timer.py) y sus rutas.

Nunca se toca Spotify ni Bluetooth de verdad: Spotify es un doble de
SpotifyClient, BlueZ es FakeBluetoothctl (el mismo doble que usan los tests
de bluetooth_manager) y subprocess.run / Popen están parcheados para fallar.
Los jobs del scheduler se guardan en FakeScheduler y se disparan a mano.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402
import sleep_timer as st  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from bluetooth_audio import BluetoothAudio  # noqa: E402
from bluetooth_manager import BluetoothManager, InvalidMac  # noqa: E402
from playback import AlarmPlaybackManager  # noqa: E402
from sleep_timer import (  # noqa: E402
    InvalidSleepTimer, SleepTimerError, SleepTimerManager,
)
from spotify_client import SpotifyClient, SpotifyConnectionError, SpotifyError  # noqa: E402
from spotify_player import SpotifyAlarmPlayer  # noqa: E402
from tests.test_bluetooth_manager import FakeBluetoothctl, no_real_processes  # noqa: E402

PHONE = "AA:BB:CC:DD:EE:01"
TABLET = "AA:BB:CC:DD:EE:02"
PHONE_NAME = "Redmi Note 11 Pro 5G"
GROOVE = {"id": "groove-id", "name": "Groove", "type": "Speaker", "is_active": True,
          "is_restricted": False}
LAPTOP = {"id": "laptop-id", "name": "Portátil", "type": "Computer", "is_active": False,
          "is_restricted": False}
NIGHT = datetime(2026, 9, 28, 23, 30)
PLAYLIST_URI = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"


def alarm(id=1, name="Despertador", source="local", minutes=0):
    return {"id": id, "name": name, "time": "00:15", "source": source,
            "spotify_uri": PLAYLIST_URI if source == "spotify" else None,
            "max_duration_minutes": minutes}


class FakeScheduler:
    """schedule_once falso: guarda los jobs; se ejecutan a mano."""

    def __init__(self):
        self.jobs = []  # [run_at, callback, cancel]

    def __call__(self, run_at, callback):
        cancel = mock.Mock()
        self.jobs.append([run_at, callback, cancel])
        return cancel

    def fire(self, index=-1):
        return self.jobs[index][1]()


class Clock:
    def __init__(self, now=NIGHT):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def fake_spotify(devices=None):
    spotify = mock.Mock(spec=SpotifyClient)
    spotify.is_configured = True
    spotify.is_connected.return_value = True
    spotify.redirect_uri = "http://127.0.0.1:5000/spotify/callback"
    spotify.get_devices.return_value = [dict(GROOVE), dict(LAPTOP)] if devices is None else devices
    return spotify


def fake_ctl():
    return FakeBluetoothctl({
        PHONE: {"name": PHONE_NAME, "paired": True, "trusted": True, "connected": True},
        TABLET: {"name": "Tablet", "paired": True, "trusted": True, "connected": False},
    })


def bluez(ctl):
    return BluetoothManager(run=ctl, agent_factory=mock.Mock(side_effect=AssertionError),
                            sleep=lambda s: None, start_watcher=False)


class SleepTimerTestCase(unittest.TestCase):
    def setUp(self):
        no_real_processes(self)
        self.clock = Clock()
        self.jobs = FakeScheduler()
        self.alarm_jobs = FakeScheduler()
        self.spotify = fake_spotify()
        self.ctl = fake_ctl()
        self.bt = bluez(self.ctl)
        # Alarmas reales (AlarmPlaybackManager) con reproductores de mentira.
        self.local = mock.Mock(spec=AudioPlayer)
        self.alarm_spotify = mock.Mock(spec=SpotifyAlarmPlayer)
        self.alarm_spotify.play.return_value = True
        self.alarm_spotify.stop.return_value = True
        self.playback = AlarmPlaybackManager(
            self.local, self.alarm_spotify, schedule_once=self.alarm_jobs, clock=self.clock,
            fader=mock.Mock(), bluetooth=mock.Mock(spec=BluetoothAudio))
        self.sleep = SleepTimerManager(self.spotify, self.bt, playback=self.playback,
                                       schedule_once=self.jobs, clock=self.clock)
        self.playback.on_alarm_start = self.sleep.invalidate_for_alarm

    def bt_verbs(self):
        return self.ctl.verbs()

    def assert_nothing_touched(self):
        self.spotify.pause.assert_not_called()
        self.assertFalse([v for v in self.bt_verbs() if v.startswith("disconnect")])


# --- Crear, validar, cancelar y consultar -------------------------------------

class CreateTest(SleepTimerTestCase):
    def test_allowed_durations_create_and_schedule(self):
        for minutes in (15, 30, 45, 60):
            with self.subTest(minutes=minutes):
                timer = self.sleep.start("spotify", minutes)
                self.assertEqual(timer.minutes, minutes)
                self.assertEqual(timer.expires_at, NIGHT + timedelta(minutes=minutes))
                self.assertEqual(self.jobs.jobs[-1][0], timer.expires_at)
                self.assertEqual(self.sleep.status()["remaining"], minutes * 60)

    def test_minutes_as_form_text(self):
        self.assertEqual(self.sleep.start("spotify", " 45 ").minutes, 45)

    def test_invalid_durations_are_rejected(self):
        for minutes in (0, 10, 16, 90, -15, "abc", "", None, "15.5", "1e1"):
            with self.subTest(minutes=minutes):
                with self.assertRaises(InvalidSleepTimer):
                    self.sleep.start("spotify", minutes)
        self.assertEqual(self.jobs.jobs, [])
        self.assertIsNone(self.sleep.timer)
        self.spotify.get_devices.assert_not_called()

    def test_invalid_source_is_rejected(self):
        for source in ("", "local", "SPOTIFY", "bluetooth; rm -rf /", None):
            with self.subTest(source=source):
                with self.assertRaises(InvalidSleepTimer):
                    self.sleep.start(source, 30)
        self.assertEqual(self.jobs.jobs, [])

    def test_invalid_mac_never_reaches_bluetoothctl(self):
        for mac in (None, "", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:0G", "$(reboot)",
                    f"{PHONE}; remove {PHONE}", PHONE_NAME):
            with self.subTest(mac=mac):
                with self.assertRaises(InvalidMac):
                    self.sleep.start("bluetooth", 30, mac=mac)
        self.assertEqual(self.ctl.calls, [])

    def test_spotify_target_is_the_active_device(self):
        timer = self.sleep.start("spotify", 30)
        self.assertEqual((timer.source, timer.target_id, timer.target_name),
                         ("spotify", "groove-id", "Groove"))
        self.assertEqual(timer.label, "Spotify · Groove")
        self.spotify.transfer_playback.assert_not_called()
        self.spotify.pause.assert_not_called()

    def test_spotify_needs_something_playing(self):
        self.spotify.get_devices.return_value = [dict(GROOVE, is_active=False)]
        with self.assertRaisesRegex(SleepTimerError, "No hay nada sonando"):
            self.sleep.start("spotify", 30)
        self.spotify.is_connected.return_value = False
        with self.assertRaisesRegex(SleepTimerError, "no está conectado"):
            self.sleep.start("spotify", 30)
        self.assertEqual(self.jobs.jobs, [])

    def test_spotify_error_while_creating(self):
        self.spotify.get_devices.side_effect = SpotifyConnectionError("sin red")
        with self.assertRaisesRegex(SleepTimerError, "No se pudo consultar Spotify"):
            self.sleep.start("spotify", 30)
        self.assertIsNone(self.sleep.timer)

    def test_bluetooth_target_is_the_connected_device(self):
        timer = self.sleep.start("bluetooth", 45, mac=PHONE.lower())
        self.assertEqual((timer.source, timer.target_id), ("bluetooth", PHONE))
        self.assertEqual(timer.label, f"Bluetooth · {PHONE_NAME}")
        self.assertFalse([v for v in self.bt_verbs() if not v.startswith("devices")])

    def test_bluetooth_device_must_be_connected_and_known(self):
        with self.assertRaisesRegex(SleepTimerError, "no está conectado"):
            self.sleep.start("bluetooth", 30, mac=TABLET)
        with self.assertRaisesRegex(SleepTimerError, "no está entre tus dispositivos"):
            self.sleep.start("bluetooth", 30, mac="11:22:33:44:55:66")
        self.assertEqual(self.jobs.jobs, [])

    def test_cannot_start_while_an_alarm_rings(self):
        self.playback.start(alarm())
        with self.assertRaisesRegex(SleepTimerError, "alarma sonando"):
            self.sleep.start("spotify", 30)
        self.assertEqual(self.jobs.jobs, [])

    def test_logs_start(self):
        with self.assertLogs("alarms", "INFO") as logs:
            self.sleep.start("spotify", 30)
            self.sleep.start("bluetooth", 45, mac=PHONE)
        text = "\n".join(logs.output)
        self.assertIn("Sleep timer iniciado: Spotify, 30 min", text)
        self.assertIn(f"Sleep timer iniciado: Bluetooth ({PHONE_NAME}, {PHONE}), 45 min", text)


class CancelAndStatusTest(SleepTimerTestCase):
    def test_cancelled_bluetooth_timer_cannot_disconnect_after_reconnection(self):
        self.sleep.start("bluetooth", 15, mac=PHONE)
        self.jobs.jobs[0][2].side_effect = RuntimeError("job already queued")
        self.sleep.cancel()
        self.ctl.devices[PHONE]["connected"] = False
        self.bt.connect(PHONE)
        self.clock.advance(minutes=15)
        self.ctl.calls.clear()
        self.assertIsNone(self.jobs.fire(0))
        self.assert_nothing_touched()
        self.assertTrue(self.ctl.devices[PHONE]["connected"])

    def test_replaced_bluetooth_generation_cannot_disconnect_either_target(self):
        self.ctl.devices[TABLET]["connected"] = True
        self.sleep.start("bluetooth", 15, mac=PHONE)
        self.jobs.jobs[0][2].side_effect = RuntimeError("job already queued")
        current = self.sleep.start("bluetooth", 30, mac=TABLET)
        cancel_current = self.sleep._cancel_job
        self.clock.advance(minutes=15)
        self.assertIsNone(self.jobs.fire(0))
        self.assertIs(self.sleep.timer, current)
        self.assertIs(self.sleep._cancel_job, cancel_current)
        self.assert_nothing_touched()
        self.clock.advance(minutes=15)
        self.assertEqual(self.jobs.fire(1), "expired")
        self.assertTrue(self.ctl.devices[PHONE]["connected"])
        self.assertFalse(self.ctl.devices[TABLET]["connected"])
        self.assertEqual([v for v in self.bt_verbs() if v.startswith("disconnect")],
                         [f"disconnect {TABLET}"])

    def test_cancel(self):
        self.sleep.start("spotify", 30)
        with self.assertLogs("alarms", "INFO") as logs:
            cancelled = self.sleep.cancel()
        self.assertEqual(cancelled.minutes, 30)
        self.assertIn("Sleep timer cancelado", "\n".join(logs.output))
        self.jobs.jobs[0][2].assert_called_once()  # job de APScheduler cancelado
        self.assertFalse(self.sleep.status()["active"])
        self.assertEqual(self.sleep.status()["last"]["outcome"], "cancelled")
        # Aunque el job llegara a ejecutarse, no hace nada.
        self.jobs.fire(0)
        self.assert_nothing_touched()

    def test_cancel_without_timer(self):
        self.assertIsNone(self.sleep.cancel())

    def test_status_countdown(self):
        self.assertEqual(self.sleep.status()["active"], False)
        self.sleep.start("bluetooth", 30, mac=PHONE)
        self.clock.advance(seconds=18)
        state = self.sleep.status()
        self.assertTrue(state["active"])
        self.assertEqual(state["remaining"], 30 * 60 - 18)  # 29:42
        self.assertEqual(state["source"], "bluetooth")
        self.assertEqual(state["label"], f"Bluetooth · {PHONE_NAME}")
        self.assertEqual(state["expires_at"], "2026-09-29T00:00:00")
        self.assertEqual(state["allowed_minutes"], [15, 30, 45, 60])
        self.assertNotIn("mac", state)  # la MAC no hace falta en la interfaz
        self.clock.advance(hours=2)
        self.assertEqual(self.sleep.status()["remaining"], 0)  # nunca negativo

    def test_status_forgets_old_results(self):
        self.sleep.start("spotify", 15)
        self.sleep.cancel()
        self.assertIsNotNone(self.sleep.status()["last"])
        self.clock.advance(minutes=11)
        self.assertIsNone(self.sleep.status()["last"])

    def test_single_instance_new_timer_replaces_old(self):
        first = self.sleep.start("spotify", 60)
        second = self.sleep.start("bluetooth", 15, mac=PHONE)
        self.assertIs(self.sleep.timer, second)
        self.assertNotEqual(first.generation, second.generation)
        self.jobs.jobs[0][2].assert_called_once()
        # El job del primero, si llegara a ejecutarse, no para Spotify.
        self.assertIsNone(self.jobs.fire(0))
        self.spotify.pause.assert_not_called()
        self.assertIs(self.sleep.timer, second)


# --- Vencimiento ---------------------------------------------------------------

class ExpireSpotifyTest(SleepTimerTestCase):
    def test_pauses_the_target_device_only(self):
        self.sleep.start("spotify", 30)
        self.clock.advance(minutes=30)
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertEqual(self.jobs.fire(), "expired")
        self.spotify.pause.assert_called_once_with("groove-id")
        for method in ("transfer_playback", "play", "set_volume", "disconnect"):
            getattr(self.spotify, method).assert_not_called()
        self.alarm_spotify.stop.assert_not_called()  # las alarmas no se tocan
        self.assertIn("Sleep timer finalizado: Spotify pausado", "\n".join(logs.output))
        self.assertIsNone(self.sleep.timer)
        self.assertEqual(self.sleep.status()["last"]["outcome"], "expired")
        self.assertEqual(self.bt_verbs(), [])  # Bluetooth ni se consulta

    def test_source_changed_to_another_spotify_device(self):
        self.sleep.start("spotify", 30)
        self.spotify.get_devices.return_value = [dict(GROOVE, is_active=False),
                                                 dict(LAPTOP, is_active=True)]
        self.assertEqual(self.jobs.fire(), "skipped")
        self.spotify.pause.assert_not_called()
        self.assertIsNone(self.sleep.timer)

    def test_target_device_disappeared(self):
        self.sleep.start("spotify", 30)
        self.spotify.get_devices.return_value = [dict(LAPTOP, is_active=True)]
        self.assertEqual(self.jobs.fire(), "skipped")
        self.spotify.pause.assert_not_called()

    def test_spotify_pause_fails_without_raising_or_retrying(self):
        self.sleep.start("spotify", 30)
        self.spotify.pause.side_effect = SpotifyError("Player command failed", status=502)
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertEqual(self.jobs.fire(), "failed")
        self.spotify.pause.assert_called_once()
        self.assertIn("sin pausar Spotify", "\n".join(logs.output))
        self.assertIsNone(self.sleep.timer)
        self.assertEqual(len(self.jobs.jobs), 1)  # no se reprograma nada

    def test_spotify_unreachable_at_expiry(self):
        self.sleep.start("spotify", 30)
        self.spotify.get_devices.side_effect = SpotifyConnectionError("sin red")
        self.assertEqual(self.jobs.fire(), "failed")
        self.spotify.pause.assert_not_called()
        self.assertIsNone(self.sleep.timer)


class ExpireBluetoothTest(SleepTimerTestCase):
    def test_completed_timer_cannot_disconnect_a_later_connection(self):
        self.sleep.start("bluetooth", 15, mac=PHONE)
        self.clock.advance(minutes=15)
        self.assertEqual(self.jobs.fire(), "expired")
        self.bt.connect(PHONE)
        self.ctl.calls.clear()
        self.assertIsNone(self.jobs.fire())
        self.assert_nothing_touched()
        self.assertTrue(self.ctl.devices[PHONE]["connected"])

    def test_disconnects_but_keeps_pairing_and_trust(self):
        self.sleep.start("bluetooth", 45, mac=PHONE)
        self.ctl.calls.clear()
        with self.assertLogs("alarms", "INFO") as logs:
            self.assertEqual(self.jobs.fire(), "expired")
        verbs = self.bt_verbs()
        self.assertIn(f"disconnect {PHONE}", verbs)
        # Solo consultas y la desconexión: nada de remove/untrust/power/visibilidad.
        self.assertEqual([v for v in verbs if not v.startswith("devices")],
                         [f"disconnect {PHONE}"])
        phone = self.ctl.devices[PHONE]
        self.assertEqual((phone["paired"], phone["trusted"], phone["connected"]),
                         (True, True, False))
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)
        self.assertTrue(self.ctl.powered)
        self.assertIn("Sleep timer finalizado: dispositivo Bluetooth desconectado",
                      "\n".join(logs.output))
        self.spotify.pause.assert_not_called()
        # Se puede volver a conectar como siempre.
        self.assertTrue(self.bt.connect(PHONE).mac == PHONE)
        self.assertTrue(self.ctl.devices[PHONE]["connected"])

    def test_already_disconnected(self):
        self.sleep.start("bluetooth", 30, mac=PHONE)
        self.ctl.devices[PHONE]["connected"] = False
        self.ctl.calls.clear()
        self.assertEqual(self.jobs.fire(), "skipped")
        self.assertFalse([v for v in self.bt_verbs() if not v.startswith("devices")])
        self.assertTrue(self.ctl.devices[PHONE]["paired"])

    def test_device_disappeared(self):
        self.sleep.start("bluetooth", 30, mac=PHONE)
        del self.ctl.devices[PHONE]  # olvidado desde otro sitio
        self.assertEqual(self.jobs.fire(), "skipped")
        self.assertFalse([v for v in self.bt_verbs() if v.startswith("disconnect")])
        self.assertIsNone(self.sleep.timer)

    def test_disconnect_fails_without_raising(self):
        self.sleep.start("bluetooth", 30, mac=PHONE)
        self.ctl.fail["disconnect"] = (1, "Failed to disconnect: org.bluez.Error.Failed\n")
        self.assertEqual(self.jobs.fire(), "failed")
        self.assertEqual(len([v for v in self.bt_verbs() if v.startswith("disconnect")]), 1)
        self.assertIsNone(self.sleep.timer)

    def test_unexpected_error_never_reaches_the_scheduler(self):
        self.sleep.start("bluetooth", 30, mac=PHONE)
        self.bt.device = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertEqual(self.jobs.fire(), "failed")
        self.assertIsNone(self.sleep.timer)

    def test_timer_never_touches_a_different_source(self):
        # Bluetooth programado; al vencer, solo se toca ese dispositivo.
        self.ctl.devices[TABLET]["connected"] = True
        self.sleep.start("bluetooth", 30, mac=PHONE)
        self.jobs.fire()
        self.assertTrue(self.ctl.devices[TABLET]["connected"])
        self.spotify.get_devices.assert_not_called()
        self.spotify.pause.assert_not_called()


# --- Alarmas: prioridad absoluta ---------------------------------------------

class AlarmPriorityTest(SleepTimerTestCase):
    def test_alarm_start_invalidates_timer(self):
        self.sleep.start("spotify", 60)
        with self.assertLogs("alarms", "INFO") as logs:
            self.playback.start(alarm())
        self.assertIsNone(self.sleep.timer)
        self.jobs.jobs[0][2].assert_called_once()
        self.assertIn("Sleep timer invalidado por alarma «Despertador»", "\n".join(logs.output))
        self.assertEqual(self.sleep.status()["last"]["outcome"], "alarm")

    def test_example_23_30_timer_00_15_alarm_00_30_nothing_happens(self):
        self.sleep.start("spotify", 60)                        # 23:30, vence a las 00:30
        self.clock.now = datetime(2026, 9, 29, 0, 15)
        self.playback.start(alarm(source="spotify"))           # 00:15 empieza la alarma
        self.clock.now = datetime(2026, 9, 29, 0, 30)
        self.assertIsNone(self.jobs.fire(0))                   # 00:30: el job viejo
        self.spotify.pause.assert_not_called()
        self.alarm_spotify.stop.assert_not_called()
        self.assertIsNotNone(self.playback.active)             # la alarma sigue sonando
        self.assertEqual(self.playback.active.via, "spotify")

    def test_old_timer_after_alarm_was_stopped_does_nothing(self):
        self.sleep.start("bluetooth", 30, mac=PHONE)
        self.playback.start(alarm())
        self.playback.stop()
        self.jobs.fire(0)
        self.assertFalse([v for v in self.bt_verbs() if v.startswith("disconnect")])

    def test_generation_guard_even_if_the_hook_did_not_run(self):
        # Defensa en profundidad: sin el aviso, la generación de alarmas decide.
        self.playback.on_alarm_start = None
        self.sleep.start("spotify", 30)
        self.playback.start(alarm())
        self.playback.stop()
        self.assertEqual(self.jobs.fire(0), "alarm")
        self.assert_nothing_touched()

    def test_alarm_ringing_at_expiry_is_never_stopped(self):
        self.playback.on_alarm_start = None
        self.sleep.start("spotify", 30)
        self.playback.start(alarm(source="spotify"))
        self.jobs.fire(0)
        self.spotify.pause.assert_not_called()
        self.assertIsNotNone(self.playback.active)

    def test_snooze_restart_also_invalidates(self):
        self.playback.start(alarm())
        self.playback.snooze()                                 # vuelve dentro de 10 min
        self.sleep.start("spotify", 30)
        self.alarm_jobs.fire(0)                                # suena el snooze
        self.assertIsNone(self.sleep.timer)

    def test_hook_failure_never_blocks_the_alarm(self):
        self.playback.on_alarm_start = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertEqual(self.playback.start(alarm()), "local")
        self.local.play.assert_called_once()

    def test_generation_increases_with_each_alarm(self):
        before = self.playback.generation
        self.playback.start(alarm())
        self.playback.start(alarm(id=2, name="Otra"))
        self.assertEqual(self.playback.generation, before + 2)

    def test_alarm_starting_while_timer_is_being_created(self):
        def alarm_meanwhile(timeout=None):
            self.playback.start(alarm())
            self.playback.stop()
            return [dict(GROOVE)]
        self.spotify.get_devices.side_effect = alarm_meanwhile
        with self.assertRaisesRegex(SleepTimerError, "alarma"):
            self.sleep.start("spotify", 30)
        self.assertIsNone(self.sleep.timer)
        self.assertEqual(self.jobs.jobs, [])

    def test_race_expiry_in_progress_finishes_before_the_alarm_plays(self):
        """El vencimiento y una alarma a la vez: la pausa nunca llega después."""
        order = []
        pausing, release = threading.Event(), threading.Event()

        def slow_pause(device_id):
            order.append("sleep-pause-start")
            pausing.set()
            release.wait(5)
            order.append("sleep-pause-end")

        self.spotify.pause.side_effect = slow_pause
        self.alarm_spotify.play.side_effect = lambda *a, **k: order.append("alarm-play") or True
        self.sleep.start("spotify", 30)
        expiry = threading.Thread(target=self.jobs.fire)
        expiry.start()
        self.assertTrue(pausing.wait(5))
        ringing = threading.Thread(target=self.playback.start, args=(alarm(source="spotify"),))
        ringing.start()
        time.sleep(0.2)
        self.assertNotIn("alarm-play", order)  # la alarma espera a que termine la pausa
        release.set()
        expiry.join(5)
        ringing.join(5)
        self.assertEqual(order, ["sleep-pause-start", "sleep-pause-end", "alarm-play"])
        self.assertEqual(self.playback.active.via, "spotify")

    def test_race_alarm_first_then_expiry_does_nothing(self):
        self.sleep.start("spotify", 30)
        callback = self.jobs.jobs[0][1]
        ringing = threading.Thread(target=self.playback.start, args=(alarm(source="spotify"),))
        ringing.start()
        ringing.join(5)
        callback()
        self.spotify.pause.assert_not_called()
        self.assertEqual(self.playback.active.via, "spotify")


class AutoStopRegressionTest(SleepTimerTestCase):
    def test_alarm_auto_stop_still_works_with_sleep_timer(self):
        self.sleep.start("spotify", 60)
        self.playback.start(alarm(minutes=15))
        self.assertIsNone(self.sleep.timer)
        auto_stop = self.alarm_jobs.jobs[-1]
        self.assertEqual(auto_stop[0], NIGHT + timedelta(minutes=15))
        result = auto_stop[1]()
        self.assertIsNotNone(result)
        self.assertIsNone(self.playback.active)
        self.local.stop.assert_called()
        # Y el temporizador anulado sigue sin hacer nada después.
        self.jobs.fire(0)
        self.spotify.pause.assert_not_called()


# --- Rutas, páginas y persistencia -------------------------------------------

class SleepTimerRoutesTest(unittest.TestCase):
    def setUp(self):
        no_real_processes(self)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.spotify = fake_spotify()
        self.ctl = fake_ctl()
        self.app = self.make_app()
        self.client = self.app.test_client()

    def make_app(self):
        app = create_app(
            {"TESTING": True, "SECRET_KEY": "t", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "a.log")},
            player=mock.Mock(spec=AudioPlayer), spotify=self.spotify,
            bluetooth=mock.Mock(paused=False), bluetooth_manager=bluez(self.ctl))
        self.jobs = FakeScheduler()
        app.extensions["sleep_timer"].schedule_once = self.jobs  # sin hilos Timer reales
        app.extensions["playback"].schedule_once = FakeScheduler()
        return app

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    @property
    def sleep(self):
        return self.app.extensions["sleep_timer"]

    def start(self, follow=True, **form):
        return self.client.post("/sleep-timer/start", data=form, follow_redirects=follow)

    def start_json(self, **form):
        return self.client.post("/sleep-timer/start", data=form,
                                headers={"Accept": "application/json"})

    def test_wired_to_the_central_alarm_start(self):
        playback = self.app.extensions["playback"]
        self.assertEqual(playback.on_alarm_start, self.sleep.invalidate_for_alarm)

    def test_status_json(self):
        resp = self.client.get("/sleep-timer/status")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-store", resp.headers["Cache-Control"])
        self.assertEqual(resp.get_json()["active"], False)

    def test_start_spotify_from_form(self):
        resp = self.start(source="spotify", minutes="30", return_to="spotify")
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.request.path, "/spotify/")
        self.assertIn("se apagará en 30 min", html)
        self.assertIn("Apagar en", html)
        state = self.client.get("/sleep-timer/status").get_json()
        self.assertEqual((state["active"], state["source"], state["minutes"]),
                         (True, "spotify", 30))

    def test_start_bluetooth_json(self):
        resp = self.start_json(source="bluetooth", minutes="45", mac=PHONE)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["state"]["label"], f"Bluetooth · {PHONE_NAME}")

    def test_invalid_input(self):
        for form, status in (({"source": "spotify", "minutes": "20"}, 400),
                             ({"source": "spotify", "minutes": ""}, 400),
                             ({"source": "otra", "minutes": "30"}, 400),
                             ({"source": "bluetooth", "minutes": "30", "mac": "x;reboot"}, 400),
                             ({"source": "bluetooth", "minutes": "30"}, 400),
                             ({"source": "bluetooth", "minutes": "30", "mac": TABLET}, 409)):
            with self.subTest(form=form):
                resp = self.start_json(**form)
                self.assertEqual(resp.status_code, status)
                self.assertFalse(resp.get_json()["ok"])
        self.assertIsNone(self.sleep.timer)
        self.assertFalse([c for c in self.ctl.calls if c[1] == "disconnect"])

    def test_invalid_form_flashes_error(self):
        html = self.start(source="spotify", minutes="90", return_to="spotify").get_data(
            as_text=True)
        self.assertIn("Duración no válida", html)
        self.assertIsNone(self.sleep.timer)

    def test_side_effects_only_via_post(self):
        self.assertEqual(self.client.get("/sleep-timer/start?source=spotify&minutes=30")
                         .status_code, 405)
        self.assertEqual(self.client.get("/sleep-timer/cancel").status_code, 405)
        self.assertIsNone(self.sleep.timer)

    def test_return_to_is_a_closed_list(self):
        resp = self.start(follow=False, source="spotify", minutes="15",
                          return_to="https://evil.example/")
        self.assertEqual(resp.headers["Location"], "/")

    def test_cancel(self):
        self.start(source="spotify", minutes="15")
        html = self.client.post("/sleep-timer/cancel", data={"return_to": "bluetooth"},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Temporizador de sueño cancelado", html)
        self.assertIsNone(self.sleep.timer)
        html = self.client.post("/sleep-timer/cancel", follow_redirects=True).get_data(
            as_text=True)
        self.assertIn("No había ningún temporizador", html)

    def test_pages_show_the_card(self):
        spotify = self.client.get("/spotify/").get_data(as_text=True)
        self.assertIn("Temporizador de sueño", spotify)
        self.assertIn("Spotify · Groove", spotify)
        for minutes in (15, 30, 45, 60):
            self.assertIn(f'name="minutes" value="{minutes}"', spotify)
        bt = self.client.get("/bluetooth/").get_data(as_text=True)
        self.assertIn(f"Bluetooth · {PHONE_NAME}", bt)
        self.assertIn(f'name="mac" value="{PHONE}"', bt)
        self.assertNotIn(f'name="mac" value="{TABLET}"', bt)  # desconectado: no se ofrece
        home = self.client.get("/").get_data(as_text=True)
        self.assertRegex(home, r"data-sleep-timer[^>]*data-only-active hidden")

    def test_home_shows_countdown_when_active(self):
        self.start(source="bluetooth", minutes="30", mac=PHONE)
        home = self.client.get("/").get_data(as_text=True)
        self.assertNotRegex(home, r"data-only-active hidden")
        self.assertIn("30:00", home)
        self.assertIn(f"Bluetooth · {PHONE_NAME}", home)
        self.assertIn("/sleep-timer/cancel", home)

    def test_testing_an_alarm_invalidates_the_timer(self):
        import db
        with self.app.app_context():
            db.create_alarm("Prueba", "07:00", [])
            alarm_id = db.list_alarms()[0]["id"]
        self.start(source="spotify", minutes="60")
        self.assertEqual(self.client.post(f"/alarms/{alarm_id}/test").status_code, 302)
        self.assertIsNone(self.sleep.timer)
        self.assertEqual(self.jobs.fire(0), None)
        self.spotify.pause.assert_not_called()

    def test_restart_does_not_restore_the_timer(self):
        self.start(source="spotify", minutes="60")
        self.assertIsNotNone(self.sleep.timer)
        restarted = self.make_app()
        state = restarted.extensions["sleep_timer"].status()
        self.assertFalse(state["active"])
        self.assertIsNone(state["last"])
        self.spotify.pause.assert_not_called()


class ModuleTest(unittest.TestCase):
    def test_allowed_minutes(self):
        self.assertEqual(st.ALLOWED_MINUTES, (15, 30, 45, 60))

    def test_no_shell_or_subprocess_in_sleep_timer(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "sleep_timer.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("subprocess", source)
        self.assertNotIn("shell=True", source)
        for call in (".forget(", ".trust(", ".remove(", "ensure_private", ".disconnect()"):
            self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
