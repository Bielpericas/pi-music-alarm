"""Página Bluetooth y BluetoothManager (BlueZ).

Nunca se toca Bluetooth de verdad: `bluetoothctl` es un doble (FakeBluetoothctl)
que simula el adaptador y los dispositivos, el agente persistente es FakeAgent,
y subprocess.run / subprocess.Popen están parcheados para fallar en cada test.
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bluetooth_manager as bm  # noqa: E402
import scheduler  # noqa: E402
from app import create_app  # noqa: E402
from audio_player import AudioPlayer  # noqa: E402
from bluetooth_manager import (  # noqa: E402
    BluetoothError,
    BluetoothManager,
    BluetoothUnavailable,
    InvalidMac,
    NoBluetoothManager,
    normalize_mac,
    parse_devices,
    parse_show,
    run_failsafe,
)
from spotify_client import SpotifyClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PHONE = "AA:BB:CC:DD:EE:01"
TABLET = "AA:BB:CC:DD:EE:02"
LAPTOP = "AA:BB:CC:DD:EE:03"
NEW = "11:22:33:44:55:66"
START = datetime(2026, 9, 28, 22, 0, 0)


def no_real_processes(test):
    for name in ("run", "Popen"):
        guard = mock.patch.object(subprocess, name, side_effect=AssertionError(
            "los tests no deben ejecutar procesos reales"))
        guard.start()
        test.addCleanup(guard.stop)


class FakeBluetoothctl:
    """Doble de subprocess.run para `bluetoothctl <orden>`: simula BlueZ."""

    def __init__(self, devices=None, powered=True, available=True, discoverable=False,
                 pairable=False, alias="Groove"):
        self.available = available
        self.powered = powered
        self.discoverable = discoverable
        self.pairable = pairable
        self.alias = alias
        # MAC -> {"name", "paired", "trusted", "connected"}
        self.devices = {mac: dict(info) for mac, info in (devices or {}).items()}
        self.calls = []
        self.fail = {}        # verbo -> (código, salida)
        self.raises = {}      # verbo -> excepción
        self.in_range = set(self.devices)  # los que se pueden conectar

    def add(self, mac, name, paired=True, trusted=True, connected=False):
        self.devices[mac] = {"name": name, "paired": paired, "trusted": trusted,
                             "connected": connected}
        self.in_range.add(mac)

    def __call__(self, command, **kwargs):
        assert isinstance(command, list), "siempre una lista de argumentos"
        assert command[0] == "bluetoothctl"
        assert not kwargs.get("shell"), "nunca shell=True"
        assert kwargs.get("timeout"), "siempre con timeout"
        self.calls.append(command)
        verb, args = command[1], command[2:]
        if verb in self.raises:
            raise self.raises[verb]
        if verb in self.fail:
            code, out = self.fail[verb]
            return subprocess.CompletedProcess(command, code, out, "")
        if not self.available:
            return subprocess.CompletedProcess(command, 1, "No default controller available\n",
                                               "")
        out = getattr(self, "_" + verb.replace("-", "_"))(*args)
        return subprocess.CompletedProcess(command, 0, out, "")

    def _show(self):
        yn = lambda value: "yes" if value else "no"  # noqa: E731
        return (f"Controller B8:27:EB:00:00:01 (public)\n\tName: raspberrypi\n"
                f"\tAlias: {self.alias}\n\tClass: 0x006c0414\n\tPowered: {yn(self.powered)}\n"
                f"\tDiscoverable: {yn(self.discoverable)}\n\tDiscoverableTimeout: 0x000000b4\n"
                f"\tPairable: {yn(self.pairable)}\n\tDiscovering: no\n")

    def _devices(self, flt=None):
        keys = {"Paired": "paired", "Trusted": "trusted", "Connected": "connected"}
        lines = [f"Device {mac} {info['name']}" for mac, info in self.devices.items()
                 if flt is None or info[keys[flt]]]
        return "\n".join(lines) + ("\n" if lines else "")

    def _discoverable(self, value):
        self.discoverable = value == "on"
        return f"Changing discoverable {value} succeeded\n"

    def _pairable(self, value):
        self.pairable = value == "on"
        return f"Changing pairable {value} succeeded\n"

    def _connect(self, mac):
        if mac not in self.in_range:
            return (f"Attempting to connect to {mac}\n"
                    "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout\n")
        self.devices[mac]["connected"] = True
        return f"Attempting to connect to {mac}\n[CHG] Device {mac} Connected: yes\nConnection successful\n"

    def _disconnect(self, mac):
        self.devices[mac]["connected"] = False
        return f"Attempting to disconnect from {mac}\nSuccessful disconnected\n"

    def _trust(self, mac):
        self.devices[mac]["trusted"] = True
        return f"[CHG] Device {mac} Trusted: yes\nChanging {mac} trust succeeded\n"

    def _remove(self, mac):
        del self.devices[mac]
        return "[DEL] Device ...\nDevice has been removed\n"

    def verbs(self):
        return [" ".join(c[1:]) for c in self.calls]


class FakeAgent:
    """Doble del `bluetoothctl --agent` persistente."""

    instances = []

    def __init__(self, ctl, on_output, register=True, default=True):
        self.ctl = ctl
        self.on_output = on_output
        self.register = register
        self.default = default
        self.sent = []
        self.buffer = ""
        self.started = self.stopped = False
        self._alive = False
        FakeAgent.instances.append(self)

    def start(self):
        self.started = self._alive = True
        if self.register:
            self.emit("Agent registered\n[bluetooth]# ")

    def emit(self, text):
        self.buffer += text
        self.on_output(text)

    def wait_for(self, marker, timeout):
        return marker in self.buffer

    def send(self, command):
        bm.check_agent_command(command)  # la misma lista cerrada que el agente real
        self.sent.append(command)
        if command == "default-agent" and self.default:
            self.emit("Default agent request successful\n")
        elif command in ("pairable on", "pairable off", "discoverable on", "discoverable off"):
            setting, value = command.split()
            setattr(self.ctl, setting, value == "on")
        elif command == "quit":
            self._alive = False

    def alive(self):
        return self._alive

    def stop(self):
        self.stopped = True
        self._alive = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        no_real_processes(self)
        FakeAgent.instances = []
        self.ctl = FakeBluetoothctl({
            PHONE: {"name": "Redmi Note 11 Pro 5G", "paired": True, "trusted": True,
                    "connected": True},
            TABLET: {"name": "Tablet", "paired": True, "trusted": True, "connected": False},
            LAPTOP: {"name": "Portátil", "paired": True, "trusted": False, "connected": False},
        })
        self.clock = Clock()
        self.slept = []
        self.agent_options = {}
        self.manager = self.make_manager()

    def make_manager(self):
        return BluetoothManager(
            run=self.ctl,
            agent_factory=lambda on_output: FakeAgent(self.ctl, on_output, **self.agent_options),
            clock=self.clock,
            now=lambda: START + timedelta(seconds=self.clock.t - 1000.0),
            sleep=self.slept.append, start_watcher=False)

    @property
    def agent(self):
        return FakeAgent.instances[-1]

    def advance(self, seconds):
        self.clock.t += seconds


# --- Parsing y consultas ------------------------------------------------------

class ParsingTest(ManagerTestCase):
    def test_adapter_active_and_private(self):
        adapter = self.manager.adapter()
        self.assertTrue(adapter.available and adapter.powered)
        self.assertFalse(adapter.discoverable or adapter.pairable)
        self.assertTrue(adapter.private)
        self.assertEqual(adapter.alias, "Groove")

    def test_adapter_powered_off(self):
        self.ctl.powered = False
        adapter = self.manager.adapter()
        self.assertTrue(adapter.available)
        self.assertFalse(adapter.powered)

    def test_no_adapter(self):
        self.ctl.available = False
        self.assertFalse(self.manager.adapter().available)
        self.assertFalse(parse_show("No default controller available").available)

    def test_empty_list(self):
        self.ctl.devices = {}
        self.assertEqual(self.manager.devices(), [])

    def test_several_devices_states(self):
        devices = {d.mac: d for d in self.manager.devices()}
        self.assertEqual(list(devices), [PHONE, LAPTOP, TABLET])  # conectados primero
        phone, laptop, tablet = devices[PHONE], devices[LAPTOP], devices[TABLET]
        self.assertTrue(phone.connected and phone.paired and phone.trusted)
        self.assertFalse(tablet.connected)
        self.assertTrue(tablet.paired and tablet.trusted)
        self.assertTrue(laptop.paired)
        self.assertFalse(laptop.trusted)

    def test_devices_only_seen_in_a_scan_are_not_listed(self):
        self.ctl.add("DE:AD:BE:EF:00:01", "Vecino", paired=False, trusted=False)
        self.assertNotIn("DE:AD:BE:EF:00:01", [d.mac for d in self.manager.devices()])

    def test_names_with_spaces_and_odd_characters(self):
        text = ("Device AA:BB:CC:DD:EE:01 Redmi Note 11 Pro 5G\n"
                "Device aa:bb:cc:dd:ee:02 Tablet de Lucía 🎧 (salón)\n"
                "Device AA:BB:CC:DD:EE:03 \x1b[1;39mCasco\tBT\x07\n"
                "Device AA:BB:CC:DD:EE:04\n"
                "Device AA:BB:CC:DD:EE:05 <script>alert(1)</script>\n"
                "Device AA:BB:CC:DD:EE:06 " + "x" * 300 + "\n"
                "[NEW] Controller B8:27:EB:00:00:01 raspberrypi [default]\n"
                "basura\n")
        names = parse_devices(text)
        self.assertEqual(names["AA:BB:CC:DD:EE:01"], "Redmi Note 11 Pro 5G")
        self.assertEqual(names["AA:BB:CC:DD:EE:02"], "Tablet de Lucía 🎧 (salón)")
        self.assertEqual(names["AA:BB:CC:DD:EE:03"], "Casco BT")
        self.assertEqual(names["AA:BB:CC:DD:EE:04"], "AA:BB:CC:DD:EE:04")  # sin nombre: la MAC
        self.assertEqual(names["AA:BB:CC:DD:EE:05"], "<script>alert(1)</script>")  # texto, sin más
        self.assertEqual(len(names["AA:BB:CC:DD:EE:06"]), bm.MAX_NAME)
        self.assertEqual(len(names), 6)

    def test_invalid_macs(self):
        for bad in ("", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "GG:BB:CC:DD:EE:FF",
                    "AA-BB-CC-DD-EE-FF", "AA:BB:CC:DD:EE:FF; reboot", "../../etc/passwd",
                    "$(reboot)", "AA:BB:CC:DD:EE:FF\nremove XX", " AA:BB:CC:DD:EE:FF", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidMac):
                    normalize_mac(bad)
                with self.assertRaises(InvalidMac):
                    self.manager.connect(bad)
        self.assertEqual(self.ctl.calls, [])
        self.assertEqual(normalize_mac("aa:bb:cc:dd:ee:0f"), "AA:BB:CC:DD:EE:0F")

    def test_bluetoothctl_missing_or_slow(self):
        self.ctl.raises["show"] = FileNotFoundError()
        with self.assertRaises(BluetoothUnavailable):
            self.manager.adapter()
        self.ctl.raises["show"] = subprocess.TimeoutExpired("bluetoothctl", 5)
        with self.assertRaises(BluetoothError) as ctx:
            self.manager.adapter()
        self.assertEqual(str(ctx.exception), "Bluetooth no respondió a tiempo.")


# --- Acciones sobre dispositivos --------------------------------------------------

class DeviceActionsTest(ManagerTestCase):
    def test_connect(self):
        device = self.manager.connect(TABLET.lower())
        self.assertEqual(device.name, "Tablet")
        self.assertIn(["bluetoothctl", "connect", TABLET], self.ctl.calls)
        self.assertTrue(self.ctl.devices[TABLET]["connected"])

    def test_connect_out_of_range(self):
        self.ctl.in_range.discard(TABLET)
        with self.assertLogs("alarms", "WARNING") as logs:
            with self.assertRaises(BluetoothError) as ctx:
                self.manager.connect(TABLET)
        self.assertEqual(str(ctx.exception), "No se pudo conectar. Comprueba que el dispositivo "
                                             "está encendido y cerca.")
        self.assertIn("page-timeout", ctx.exception.detail)  # el detalle técnico, al log
        self.assertIn("page-timeout", " ".join(logs.output))

    def test_connect_nonzero_exit(self):
        self.ctl.fail["connect"] = (1, "Device AA:BB:CC:DD:EE:02 not available\n")
        with self.assertLogs("alarms", "WARNING"), self.assertRaises(BluetoothError):
            self.manager.connect(TABLET)

    def test_disconnect_keeps_pairing_and_trust(self):
        self.manager.disconnect(PHONE)
        info = self.ctl.devices[PHONE]
        self.assertEqual((info["connected"], info["paired"], info["trusted"]),
                         (False, True, True))
        self.assertIn(["bluetoothctl", "disconnect", PHONE], self.ctl.calls)
        self.assertFalse(any(c[1] in ("remove", "untrust") for c in self.ctl.calls))

    def test_trust(self):
        self.manager.trust(LAPTOP)
        self.assertTrue(self.ctl.devices[LAPTOP]["trusted"])
        self.assertIn(["bluetoothctl", "trust", LAPTOP], self.ctl.calls)

    def test_forget(self):
        self.manager.forget(TABLET)
        self.assertNotIn(TABLET, self.ctl.devices)
        self.assertIn(["bluetoothctl", "remove", TABLET], self.ctl.calls)

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(BluetoothError):
            self.manager.forget("DE:AD:BE:EF:00:99")
        self.assertFalse(any(c[1] == "remove" for c in self.ctl.calls))

    def test_commands_never_include_device_names(self):
        self.ctl.devices[TABLET]["name"] = "; rm -rf / $(reboot)"
        self.manager.connect(TABLET)
        self.manager.trust(TABLET)
        for command in self.ctl.calls:
            self.assertFalse(any("rm -rf" in part for part in command))
            self.assertTrue(all(isinstance(part, str) for part in command))


# --- Ventana de emparejamiento -------------------------------------------------------

class PairingTest(ManagerTestCase):
    def start(self):
        session, created = self.manager.start_pairing()
        self.assertTrue(created)
        return session

    def test_start_pairing(self):
        session = self.start()
        self.assertTrue(session.active)
        self.assertEqual(session.started_at, START)
        self.assertEqual(session.expires_at, START + timedelta(minutes=2))
        self.assertEqual(self.manager.session_view()["remaining"], 120)
        self.assertEqual(self.agent.sent, ["default-agent", "discoverable-timeout 120",
                                           "pairable on", "discoverable on"])
        self.assertTrue(self.ctl.discoverable and self.ctl.pairable)
        self.assertTrue(self.agent.started)

    def test_pairing_already_active_reuses_session(self):
        first = self.start()
        again, created = self.manager.start_pairing()
        self.assertIs(again, first)
        self.assertFalse(created)
        self.assertEqual(len(FakeAgent.instances), 1)  # un solo agente

    def test_countdown(self):
        self.start()
        self.advance(18)
        self.manager.tick()
        view = self.manager.session_view()
        self.assertEqual((view["state"], view["remaining"]), ("active", 102))
        self.assertTrue(self.ctl.discoverable)

    def test_timeout_after_two_minutes_goes_private(self):
        self.start()
        agent = self.agent
        self.advance(119)
        self.manager.tick()
        self.assertTrue(self.manager.session.active)
        self.advance(1)
        with self.assertLogs("alarms", "INFO") as logs:
            self.manager.tick()
        session = self.manager.session
        self.assertEqual(session.state, bm.TIMEOUT)
        self.assertEqual(session.remaining(self.clock()), 0)
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)
        self.assertTrue(session.private_again)
        self.assertTrue(agent.stopped)
        self.assertIn("discoverable off", self.ctl.verbs())
        self.assertIn("pairable off", self.ctl.verbs())
        self.assertIn("cerrado por tiempo", " ".join(logs.output))
        self.assertEqual(session.devices, [])

    def test_manual_cancel(self):
        self.start()
        agent = self.agent
        with self.assertLogs("alarms", "INFO") as logs:
            session = self.manager.cancel_pairing()
        self.assertEqual(session.state, bm.CANCELLED)
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)
        self.assertTrue(agent.stopped)
        self.assertIn("cancelado", " ".join(logs.output))
        self.assertIsNone(self.manager.cancel_pairing())  # ya no hay nada que cancelar

    def test_new_device_is_trusted_and_window_closes(self):
        self.start()
        agent = self.agent
        # Un móvil nuevo se empareja; el portátil (emparejado, sin confianza) no cambia.
        self.ctl.add(NEW, "Móvil nuevo", paired=True, trusted=False)
        agent.emit(f"[CHG] Device {NEW} Paired: yes\n")
        with self.assertLogs("alarms", "INFO") as logs:
            self.manager.tick()
        session = self.manager.session
        self.assertEqual(session.state, bm.PAIRED)
        self.assertEqual([(d.mac, d.name, d.trusted) for d in session.devices],
                         [(NEW, "Móvil nuevo", True)])
        self.assertTrue(self.ctl.devices[NEW]["trusted"])
        self.assertFalse(self.ctl.devices[LAPTOP]["trusted"])   # nunca se confía en otros
        trusted = [c[2] for c in self.ctl.calls if c[1] == "trust"]
        self.assertEqual(trusted, [NEW])
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)
        self.assertTrue(agent.stopped)
        text = " ".join(logs.output)
        self.assertIn("nuevo dispositivo emparejado", text)
        self.assertIn("tras emparejar", text)

    def test_new_device_detected_by_polling_without_event(self):
        self.start()
        self.ctl.add(NEW, "Móvil nuevo", paired=True, trusted=False)
        self.manager.tick()                       # aún no toca mirar
        self.assertTrue(self.manager.session.active)
        self.advance(bm.POLL_SECONDS)
        self.manager.tick()
        self.assertEqual(self.manager.session.state, bm.PAIRED)

    def test_known_device_re_paired_during_window_is_trusted(self):
        self.start()
        self.ctl.devices[LAPTOP]["trusted"] = False
        self.agent.emit(f"[CHG] Device {LAPTOP} Bonded: yes\n")
        self.manager.tick()
        self.assertEqual([d.mac for d in self.manager.session.devices], [LAPTOP])
        self.assertTrue(self.ctl.devices[LAPTOP]["trusted"])

    def test_nothing_is_trusted_outside_a_groove_window(self):
        self.ctl.add(NEW, "Por bluetoothctl", paired=True, trusted=False)
        self.manager.tick()  # sin ventana: no hace nada
        self.assertFalse(self.ctl.devices[NEW]["trusted"])
        self.assertEqual([c for c in self.ctl.calls if c[1] == "trust"], [])

    def test_agent_prompts_are_answered_only_while_open(self):
        self.start()
        agent = self.agent
        with self.assertLogs("alarms", "INFO") as logs:
            agent.emit("[agent] Confirm passkey 123456 (yes/no): ")
            agent.emit("[agent] Authorize service 0000110d-0000-1000-8000-00805f9b34fb (yes/")
            agent.emit("no): ")  # la pregunta llega partida
        self.assertEqual(agent.sent[-2:], ["yes", "yes"])
        self.assertNotIn("123456", " ".join(logs.output))  # la clave no va al log
        self.manager.cancel_pairing()
        self.manager._agent = agent  # por si llegara algo tarde, se rechaza
        with self.assertLogs("alarms", "INFO"):
            agent.emit("[agent] Accept pairing (yes/no): ")
        self.assertEqual(agent.sent[-1], "no")

    def test_pin_request_is_logged_not_answered(self):
        self.start()
        sent = len(self.agent.sent)
        with self.assertLogs("alarms", "WARNING") as logs:
            self.agent.emit("[agent] Enter PIN code: ")
        self.assertEqual(len(self.agent.sent), sent)
        self.assertIn("PIN", " ".join(logs.output))

    def test_agent_died_closes_the_window(self):
        self.start()
        self.agent._alive = False
        with self.assertLogs("alarms", "WARNING"):
            self.manager.tick()
        self.assertEqual(self.manager.session.state, bm.FAILED)
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)

    def test_start_fails_if_agent_does_not_register(self):
        self.agent_options = {"register": False}
        with self.assertLogs("alarms", "WARNING"), self.assertRaises(BluetoothError) as ctx:
            self.manager.start_pairing()
        self.assertEqual(str(ctx.exception), "No se pudo preparar el emparejamiento.")
        self.assertIsNone(self.manager.session)
        self.assertTrue(self.agent.stopped)
        self.assertIn("discoverable off", self.ctl.verbs())

    def test_start_fails_if_groove_does_not_become_visible(self):
        self.ctl.fail["show"] = (0, self.ctl._show())  # show sigue diciendo "no"
        with self.assertLogs("alarms", "WARNING"), self.assertRaises(BluetoothError):
            self.manager.start_pairing()
        self.assertEqual(self.manager.session.state, bm.FAILED)
        self.assertTrue(self.agent.stopped)
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)

    def test_start_fails_without_adapter_or_powered_off(self):
        self.ctl.powered = False
        with self.assertRaises(BluetoothError):
            self.manager.start_pairing()
        self.ctl.available = False
        with self.assertRaises(BluetoothUnavailable):
            self.manager.start_pairing()
        self.assertEqual(FakeAgent.instances, [])

    def test_new_window_after_one_closes(self):
        self.start()
        self.manager.cancel_pairing()
        session, created = self.manager.start_pairing()
        self.assertTrue(created and session.active)
        self.assertEqual(len(FakeAgent.instances), 2)

    def test_recent_view(self):
        self.assertIsNone(self.manager.recent_view())
        self.start()
        self.assertIsNone(self.manager.recent_view())  # aún abierta
        self.advance(120)
        self.manager.tick()
        self.assertEqual(self.manager.recent_view()["state"], "timeout")
        self.advance(bm.RECENT_SECONDS + 1)
        self.assertIsNone(self.manager.recent_view())


class WatcherThreadTest(ManagerTestCase):
    def test_server_side_timeout_without_any_request(self):
        # Con el hilo vigilante real y un plazo corto: se cierra sola, sin JS.
        import time as real_time
        manager = BluetoothManager(
            run=self.ctl, agent_factory=lambda cb: FakeAgent(self.ctl, cb),
            pairing_seconds=1, poll_seconds=0.2, sleep=lambda s: None)
        manager.start_pairing()
        for _ in range(60):
            if not manager.session.active:
                break
            real_time.sleep(0.05)
        self.assertEqual(manager.session.state, bm.TIMEOUT)
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)


# --- Fail-safe al arrancar ------------------------------------------------------------

class FailsafeTest(ManagerTestCase):
    def test_ensure_private_closes_a_leftover_visible_state(self):
        self.ctl.discoverable = self.ctl.pairable = True
        self.assertTrue(self.manager.ensure_private())
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)

    def test_failsafe_retries_and_never_raises(self):
        self.ctl.raises["discoverable"] = FileNotFoundError()
        slept = []
        with self.assertLogs("alarms", "WARNING") as logs:
            self.assertFalse(run_failsafe(self.manager, delays=(0, 5, 20), sleep=slept.append))
        self.assertEqual(slept, [5, 20])
        self.assertIn("intento 3/3", " ".join(logs.output))

    def test_failsafe_survives_unexpected_exceptions(self):
        broken = mock.Mock()
        broken.ensure_private.side_effect = RuntimeError("dbus caído")
        with self.assertLogs("alarms", "WARNING"):
            self.assertFalse(run_failsafe(broken, delays=(0,)))


# --- App: página, rutas y aislamiento ----------------------------------------------------

class BluetoothPageTest(ManagerTestCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "t.db")
        self.spotify = mock.Mock(spec=SpotifyClient)
        self.spotify.is_configured = True
        self.spotify.is_connected.return_value = False
        self.spotify.redirect_uri = "http://127.0.0.1:5000/spotify/callback"
        self.audio = mock.Mock()
        self.audio.paused = False
        self.app = self.make_app()
        self.client = self.app.test_client()
        self.ctl.calls.clear()  # el fail-safe de arranque ya habló con bluetoothctl

    def make_app(self, manager=None):
        return create_app(
            {"TESTING": True, "SECRET_KEY": "t", "DATABASE": self.db_path,
             "ALARM_LOG": os.path.join(self.tmpdir.name, "a.log")},
            player=mock.Mock(spec=AudioPlayer), spotify=self.spotify, bluetooth=self.audio,
            bluetooth_manager=manager or self.manager)

    def tearDown(self):
        scheduler.close_logging()
        self.tmpdir.cleanup()

    def html(self, url="/bluetooth/"):
        return self.client.get(url).get_data(as_text=True)

    def post(self, url):
        return self.client.post(url, follow_redirects=True).get_data(as_text=True)

    # Página

    def test_page_shows_status_and_devices(self):
        html = self.html()
        text = " ".join(html.split())
        self.assertIn("<h1>Bluetooth</h1>", html)
        self.assertIn("Activo", text)
        self.assertIn("Privado", text)
        self.assertIn("Groove está en modo privado. Tus dispositivos emparejados pueden conectarse.",
                      text)
        self.assertIn("Tus dispositivos", html)
        for name in ("Redmi Note 11 Pro 5G", "Tablet", "Portátil"):
            self.assertIn(name, html)
        self.assertIn("Emparejar nuevo dispositivo", html)
        # Conectado -> Desconectar; desconectado -> Conectar; sin confianza -> Confiar.
        self.assertIn(f'action="/bluetooth/devices/{PHONE}/disconnect"', html)
        self.assertIn(f'action="/bluetooth/devices/{TABLET}/connect"', html)
        self.assertIn(f'action="/bluetooth/devices/{LAPTOP}/trust"', html)
        self.assertNotIn(f'action="/bluetooth/devices/{PHONE}/trust"', html)
        self.assertIn("sin confianza", html)
        # La MAC solo como detalle técnico.
        self.assertIn(f"MAC <code>{PHONE}</code>", html)
        name_line = re.search(r'<span class="device-name">([^<]*)</span>', html).group(1)
        self.assertNotIn(":", name_line)

    def test_empty_list(self):
        self.ctl.devices = {}
        self.assertIn("Aún no hay dispositivos emparejados.", self.html())

    def test_powered_off_and_no_adapter(self):
        self.ctl.powered = False
        html = self.html()
        self.assertIn("Apagado", html)
        self.assertRegex(html, r"bt-pair-btn\" type=\"submit\"\s+disabled")
        self.ctl.available = False
        self.assertIn("Sin adaptador", self.html())

    def test_not_available_off_the_pi(self):
        app = self.make_app(NoBluetoothManager())
        html = app.test_client().get("/bluetooth/").get_data(as_text=True)
        self.assertIn("Bluetooth solo se gestiona en la Raspberry", html)

    def test_visible_outside_a_session_offers_going_private(self):
        self.ctl.discoverable = True
        html = self.html()
        self.assertIn("Visible", html)
        self.assertIn('action="/bluetooth/private"', html)
        self.assertIn("Groove está en modo privado.", self.post("/bluetooth/private"))
        self.assertFalse(self.ctl.discoverable)

    def test_malicious_name_is_rendered_as_text(self):
        evil = '<img src=x onerror="alert(1)">"><script>alert(2)</script>'
        self.ctl.devices[TABLET]["name"] = evil
        html = self.html()
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<script>alert(2)", html)
        self.assertIn("&lt;img src=x onerror=&#34;alert(1)&#34;&gt;", html)
        # También en el mensaje de confirmación y en los avisos.
        self.assertIn('data-confirm="¿Olvidar &lt;img', html)
        flash = self.post(f"/bluetooth/devices/{TABLET}/connect")
        self.assertNotIn("<script>alert(2)", flash)

    def test_forget_asks_for_confirmation(self):
        html = self.html()
        form = re.search(rf'<form method="post" action="/bluetooth/devices/{PHONE}/forget"\s+'
                         r'data-confirm="([^"]*)"', html)
        self.assertIsNotNone(form)
        self.assertEqual(form.group(1), "¿Olvidar Redmi Note 11 Pro 5G? Tendrás que volver a "
                                        "emparejarlo para utilizarlo con Groove.")
        source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('getAttribute("data-confirm")', source)

    # Acciones

    def test_actions_via_post(self):
        text = self.post(f"/bluetooth/devices/{TABLET}/connect")
        self.assertIn("«Tablet» conectado.", text)
        text = self.post(f"/bluetooth/devices/{TABLET}/disconnect")
        self.assertIn("«Tablet» desconectado. Sigue emparejado", text)
        text = self.post(f"/bluetooth/devices/{LAPTOP}/trust")
        self.assertIn("«Portátil» es ahora de confianza", text)
        text = self.post(f"/bluetooth/devices/{TABLET}/forget")
        self.assertIn("Dispositivo olvidado.", text)
        self.assertNotIn(TABLET, self.ctl.devices)

    def test_connect_failure_is_friendly(self):
        self.ctl.in_range.discard(TABLET)
        with self.assertLogs("alarms", "WARNING"):
            text = self.post(f"/bluetooth/devices/{TABLET}/connect")
        self.assertIn("No se pudo conectar. Comprueba que el dispositivo está encendido y cerca.",
                      text)
        self.assertNotIn("org.bluez", text)  # nada técnico en la web

    def test_actions_only_via_post(self):
        urls = [f"/bluetooth/devices/{PHONE}/{verb}"
                for verb in ("connect", "disconnect", "trust", "forget")]
        urls += ["/bluetooth/pairing/start", "/bluetooth/pairing/cancel", "/bluetooth/private"]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.ctl.calls, [])
        self.assertEqual(FakeAgent.instances, [])

    def test_mac_injection_in_url(self):
        for mac in ("AA:BB:CC:DD:EE:FF;reboot", "$(reboot)", "AA:BB:CC:DD:EE",
                    "AA%3ABB%3ACC%3ADD%3AEE%3AFF%20remove", "..%2F..%2Fetc", "-h", "--help",
                    "AA:BB:CC:DD:EE:FF%0Aremove%20AA:BB:CC:DD:EE:01"):
            with self.subTest(mac=mac):
                resp = self.client.post(f"/bluetooth/devices/{mac}/forget")
                self.assertIn(resp.status_code, (404, 405))
        self.assertEqual(self.ctl.calls, [])
        self.assertIn(PHONE, self.ctl.devices)

    def test_no_generic_action_endpoint(self):
        for url in ("/bluetooth/devices/%s/remove" % PHONE, "/bluetooth/devices/%s/untrust" % PHONE,
                    "/bluetooth/run", "/bluetooth/command"):
            with self.subTest(url=url):
                self.assertEqual(self.client.post(url).status_code, 404)
        self.assertEqual(self.ctl.calls, [])

    # Emparejamiento desde la web

    def test_pairing_start_page_and_status(self):
        text = self.post("/bluetooth/pairing/start")
        self.assertIn("Groove está visible durante 2 minutos", text)
        html = self.html()
        self.assertIn("Groove está visible", html)
        self.assertIn('<span data-bt-countdown>02:00</span> restantes', html)
        self.assertIn("Busca «Groove» desde tu móvil, tablet u ordenador.", html)
        self.assertIn("Cancelar emparejamiento", html)
        self.assertIn('data-state="active"', html)
        self.advance(18)
        resp = self.client.get("/bluetooth/status")
        data = resp.get_json()
        self.assertEqual((data["session"]["state"], data["session"]["remaining"]), ("active", 102))
        self.assertIn("no-store", resp.headers["Cache-Control"])
        self.assertIn("01:42", self.html())

    def test_pairing_twice_keeps_one_session(self):
        self.post("/bluetooth/pairing/start")
        text = self.post("/bluetooth/pairing/start")
        self.assertIn("Ya hay un emparejamiento en curso.", text)
        self.assertEqual(len(FakeAgent.instances), 1)

    def test_pairing_cancel_from_web(self):
        self.post("/bluetooth/pairing/start")
        text = self.post("/bluetooth/pairing/cancel")
        self.assertIn("Emparejamiento cancelado. Groove vuelve a estar en modo privado.", text)
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)
        self.assertIn("Emparejar nuevo dispositivo", text)

    def test_pairing_result_shown_after_success(self):
        self.post("/bluetooth/pairing/start")
        self.ctl.add(NEW, "Móvil nuevo", paired=True, trusted=False)
        self.agent.emit(f"[CHG] Device {NEW} Paired: yes\n")
        self.manager.tick()
        html = self.html()
        self.assertIn("«Móvil nuevo» emparejado y de confianza.", html)
        self.assertIn('data-state="paired"', html)
        self.assertIn("Móvil nuevo", html)

    def test_pairing_does_not_touch_spotify_or_alarms(self):
        self.client.post("/alarms/new", data={"name": "Trabajo", "time": "07:30",
                                              "days": ["0"]})
        before = self.alarm_rows()
        self.spotify.reset_mock()
        self.post("/bluetooth/pairing/start")
        self.ctl.add(NEW, "Móvil nuevo", paired=True, trusted=False)
        self.advance(bm.POLL_SECONDS)
        self.manager.tick()
        self.post(f"/bluetooth/devices/{NEW}/disconnect")
        self.assertEqual(self.spotify.method_calls, [])
        self.assertEqual(self.alarm_rows(), before)
        self.audio.pause.assert_not_called()
        self.audio.resume.assert_not_called()

    def alarm_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT * FROM alarms").fetchall()
        finally:
            conn.close()

    # Alarma sonando

    def test_alarm_paused_audio_is_not_shown_as_broken(self):
        self.audio.paused = True  # la alarma paró bluealsa-aplay
        health = self.app.extensions["health"]
        health.platform = "linux"
        states = {"bluealsa.service": "active", "bluealsa-aplay.service": "inactive"}

        def fake_systemctl(command, **kwargs):
            if command[0] == "systemctl":
                return subprocess.CompletedProcess(command, 0, states[command[-1]] + "\n", "")
            return self.ctl(command, **kwargs)

        health._run = fake_systemctl
        html = self.html()
        text = " ".join(html.split())
        self.assertIn("Pausado por la alarma", text)
        self.assertIn("Audio Bluetooth pausado por la alarma: el adaptador y los dispositivos "
                      "siguen conectados", text)
        self.assertIn("Activo", text)
        self.assertIn("Redmi Note 11 Pro 5G", html)
        self.assertNotIn("Reproductor detenido", html)
        self.assertNotIn("dot-off", html.split("Tus dispositivos")[0].split("Audio")[1])

    # Arranque

    def test_startup_failsafe_makes_groove_private(self):
        self.ctl.discoverable = self.ctl.pairable = True
        self.make_app()
        self.assertFalse(self.ctl.discoverable or self.ctl.pairable)

    def test_startup_failsafe_failure_does_not_block_groove(self):
        broken = mock.Mock(spec=BluetoothManager)
        broken.available = True
        broken.ensure_private.side_effect = RuntimeError("bluetoothd no responde")
        app = self.make_app(broken)
        self.assertEqual(app.test_client().get("/").status_code, 200)
        broken.ensure_private.assert_called()
        scheduler.close_logging()
        log = Path(self.tmpdir.name, "a.log").read_text(encoding="utf-8")
        self.assertIn("no se pudo asegurar el modo privado", log)

    # Navegación y diseño

    def test_navigation_has_bluetooth(self):
        for url in ("/", "/bluetooth/", "/spotify/", "/alarms/new"):
            with self.subTest(url=url):
                html = self.html(url)
                self.assertGreaterEqual(html.count('href="/bluetooth/"'), 2)  # arriba y abajo
        self.assertRegex(self.html(), r'href="/bluetooth/"\s+aria-current="page"')
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr))", css)
        self.assertIn("text-overflow: ellipsis", css)


class SafetyTest(unittest.TestCase):
    """Comprobaciones estáticas del código."""

    def test_no_shell_true_nor_os_system(self):
        for name in ("bluetooth_manager.py", "bluetooth_views.py", "bluetooth_audio.py",
                     "health.py"):
            with self.subTest(file=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotRegex(source, r"shell\s*=\s*True")
                self.assertNotIn("os.system", source)
                self.assertNotIn("os.popen", source)

    def test_views_do_not_run_commands(self):
        source = (ROOT / "bluetooth_views.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("bluetoothctl", source.split('"""', 2)[2])

    def test_agent_only_accepts_a_closed_list_of_commands(self):
        for ok in ("default-agent", "pairable on", "discoverable off", "yes", "no", "quit",
                   "discoverable-timeout 120"):
            bm.check_agent_command(ok)
        for bad in ("remove AA:BB:CC:DD:EE:FF", "trust AA:BB:CC:DD:EE:FF", "power off",
                    "yes\nremove AA:BB:CC:DD:EE:FF", "discoverable-timeout 1; ls", "shell"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bm.check_agent_command(bad)

    @unittest.skipUnless(hasattr(os, "openpty"), "pty solo existe en Linux/macOS")
    def test_real_agent_class_uses_bluetoothctl_without_shell(self):
        popen = mock.Mock()
        popen.return_value.poll.return_value = 0
        agent = bm.BluetoothctlAgent(lambda text: None, popen=popen)
        agent.start()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["bluetoothctl", "--agent", "NoInputNoOutput"])
        self.assertNotIn("shell", kwargs)
        agent.stop()


if __name__ == "__main__":
    unittest.main()
