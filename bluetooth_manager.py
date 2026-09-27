"""Gestión de BlueZ desde Groove: estado, dispositivos y ventana de emparejamiento.

Es el único módulo que habla con BlueZ (vía `bluetoothctl`). No toca la
reproducción: `bluetooth_audio.py` sigue siendo el único que para y arranca
`bluealsa-aplay` para las alarmas.

    BluetoothManager ──> BlueZ (bluetoothctl)
    BluetoothAudio   ──> bluealsa-aplay (systemctl)

Modo normal: **privado** (Discoverable: no, Pairable: no). El adaptador sigue
encendido y los dispositivos emparejados y de confianza pueden reconectarse.

Ventana de emparejamiento (`start_pairing`): durante PAIRING_SECONDS Groove
arranca un `bluetoothctl --agent NoInputNoOutput` persistente (en un pty, como
si fuera interactivo), lo pone como agente por defecto, activa pairable y
discoverable y contesta "yes" a las peticiones del agente. En cuanto se
empareja un dispositivo nuevo lo marca como de confianza y cierra la ventana.
Al terminar (emparejado, tiempo agotado, cancelado o error) siempre deja
discoverable off y pairable off y cierra el agente.

Seguridad:
- Sin shell. Los argumentos son constantes o una MAC validada (`normalize_mac`).
- Los nombres de los dispositivos son texto no confiable: nunca forman parte
  de un comando; solo se muestran (escapados) y se recortan.
- Al agente solo se le envían órdenes de una lista cerrada (`AGENT_COMMANDS`).
- Solo se confía automáticamente en dispositivos emparejados durante una
  ventana abierta explícitamente desde Groove.

Fail-safe: `ensure_private()` se llama al arrancar Groove (en segundo plano,
con reintentos) y al cerrar cada ventana. Además, durante la ventana se fija
`discoverable-timeout` para que BlueZ oculte a Groove por sí mismo aunque el
proceso muera.
"""
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("alarms")

BLUETOOTHCTL = "bluetoothctl"
PAIRING_SECONDS = 120
COMMAND_TIMEOUT = 5        # s para órdenes rápidas (show, devices, trust...)
CONNECT_TIMEOUT = 20       # s: conectar puede tardar si el dispositivo está lejos
AGENT_TIMEOUT = 5          # s para que el agente se registre
POLL_SECONDS = 2           # cada cuánto se miran los emparejados durante la ventana
MAX_NAME = 80
AGENT_CAPABILITY = "NoInputNoOutput"   # Raspberry sin pantalla ni teclado
FAILSAFE_DELAYS = (0, 5, 20)
RECENT_SECONDS = 600       # cuánto se muestra el resultado del último emparejamiento           # reintentos del cierre al arrancar (bluetoothd tarda)

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|[\x01\x02]")
_DEVICE_LINE_RE = re.compile(r"^Device ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?: (.*))?$")
_PROMPT_RE = re.compile(r"\(yes/no\):?")
_PIN_RE = re.compile(r"Enter (?:PIN code|passkey)", re.IGNORECASE)
_PAIRED_EVENT_RE = re.compile(
    r"\[CHG\] Device ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}) (?:Paired|Bonded): yes")
_FAILURE_RE = re.compile(r"Failed|Error|not available|No default controller", re.IGNORECASE)

# Lo único que Groove escribe al agente. "discoverable-timeout" lleva un entero.
AGENT_COMMANDS = frozenset({"default-agent", "pairable on", "pairable off", "discoverable on",
                            "discoverable off", "yes", "no", "quit"})
_TIMEOUT_COMMAND_RE = re.compile(r"^discoverable-timeout \d{1,4}$")

# Estados de la ventana de emparejamiento.
ACTIVE, PAIRED, TIMEOUT, CANCELLED, FAILED = "active", "paired", "timeout", "cancelled", "failed"


class InvalidMac(ValueError):
    """La MAC no tiene el formato AA:BB:CC:DD:EE:FF."""


class BluetoothError(Exception):
    """Fallo de una operación. `str(exc)` es un mensaje apto para la interfaz;
    `detail` es el texto técnico (solo para el log)."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


class BluetoothUnavailable(BluetoothError):
    """No hay bluetoothctl, adaptador o BlueZ (o no es Linux)."""


def normalize_mac(text):
    """"aa:bb:cc:dd:ee:ff" -> "AA:BB:CC:DD:EE:FF". Lanza InvalidMac."""
    if not isinstance(text, str) or not MAC_RE.fullmatch(text):
        raise InvalidMac("MAC no válida")
    return text.upper()


def strip_ansi(text):
    return _ANSI_RE.sub("", text or "").replace("\r", "")


def clean_name(value, mac=""):
    """Nombre legible y seguro de mostrar: una línea, sin controles, recortado."""
    text = "".join(ch if ch.isprintable() else " " for ch in (value or ""))
    text = " ".join(text.split())[:MAX_NAME].rstrip()
    return text or mac


# --- Datos -----------------------------------------------------------------

@dataclass
class Adapter:
    available: bool
    powered: bool = False
    discoverable: bool = False
    pairable: bool = False
    alias: str = ""

    @property
    def private(self):
        return not self.discoverable and not self.pairable

    def as_dict(self):
        return {"available": self.available, "powered": self.powered,
                "discoverable": self.discoverable, "pairable": self.pairable,
                "private": self.private, "alias": self.alias}


@dataclass
class Device:
    mac: str
    name: str
    paired: bool = False
    trusted: bool = False
    connected: bool = False

    def as_dict(self):
        return {"mac": self.mac, "name": self.name, "paired": self.paired,
                "trusted": self.trusted, "connected": self.connected}


@dataclass
class PairingSession:
    started_at: datetime
    expires_at: datetime
    deadline: float                  # reloj monotónico
    state: str = ACTIVE
    ended_at: datetime = None
    devices: list = field(default_factory=list)   # Device emparejados en la ventana
    private_again: bool = None       # si el cierre dejó a Groove en privado

    @property
    def active(self):
        return self.state == ACTIVE

    def remaining(self, now):
        return max(0, int(round(self.deadline - now))) if self.active else 0

    def as_dict(self, now):
        return {"state": self.state, "active": self.active,
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "expires_at": self.expires_at.isoformat(timespec="seconds"),
                "ended_at": self.ended_at.isoformat(timespec="seconds") if self.ended_at else None,
                "remaining": self.remaining(now),
                "devices": [d.as_dict() for d in self.devices],
                "private_again": self.private_again}


# --- Parsing de la salida de bluetoothctl ------------------------------------

def parse_show(text):
    """Salida de `bluetoothctl show` -> Adapter."""
    text = strip_ansi(text)
    if "No default controller" in text or "Controller " not in text:
        return Adapter(available=False)
    values = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key in ("Alias", "Name", "Powered", "Discoverable", "Pairable"):
            values.setdefault(key, value.strip())
    return Adapter(available=True,
                   powered=values.get("Powered") == "yes",
                   discoverable=values.get("Discoverable") == "yes",
                   pairable=values.get("Pairable") == "yes",
                   alias=clean_name(values.get("Alias") or values.get("Name") or ""))


def parse_devices(text):
    """Salida de `bluetoothctl devices [...]` -> {MAC: nombre} (en orden)."""
    found = {}
    for line in strip_ansi(text).splitlines():
        match = _DEVICE_LINE_RE.match(line.strip())
        if match:
            mac = match.group(1).upper()
            found[mac] = clean_name(match.group(2), mac)
    return found


# --- Agente persistente --------------------------------------------------------

class BluetoothctlAgent:
    """`bluetoothctl --agent NoInputNoOutput` vivo en un pty durante la ventana.

    El pty hace que bluetoothctl se comporte como en una terminal: registra el
    agente y muestra las preguntas "(yes/no)" que Groove contesta. Toda la
    salida se pasa a `on_output` desde un hilo lector.
    """

    def __init__(self, on_output, popen=subprocess.Popen, binary=BLUETOOTHCTL):
        self._on_output = on_output
        self._popen = popen
        self._binary = binary
        self._proc = None
        self._master = None
        self._buffer = ""
        self._cond = threading.Condition()

    def start(self):
        import pty  # solo existe en Unix; el gestor solo se usa en Linux

        master, slave = pty.openpty()
        try:
            self._proc = self._popen([self._binary, "--agent", AGENT_CAPABILITY],
                                     stdin=slave, stdout=slave, stderr=slave,
                                     close_fds=True, start_new_session=True)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._master = master
        threading.Thread(target=self._read, name="bluetooth-agent", daemon=True).start()

    def _read(self):
        while True:
            try:
                data = os.read(self._master, 1024)
            except OSError:
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            with self._cond:
                self._buffer = (self._buffer + strip_ansi(text))[-8192:]
                self._cond.notify_all()
            try:
                self._on_output(text)
            except Exception:
                logger.exception("Bluetooth: error procesando la salida del agente")

    def wait_for(self, marker, timeout):
        """True si `marker` aparece en la salida antes de `timeout` segundos."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while marker not in self._buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive():
                    return False
                self._cond.wait(min(remaining, 0.2))
            return True

    def send(self, command):
        check_agent_command(command)
        if self._master is None:
            return
        try:
            os.write(self._master, (command + "\n").encode())
        except OSError:
            logger.warning("Bluetooth: el agente ya no acepta órdenes")

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout=3):
        if self._proc is None:
            return
        if self.alive():
            self.send("quit")
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = None


def check_agent_command(command):
    """Solo se escriben al agente órdenes de la lista cerrada."""
    if command not in AGENT_COMMANDS and not _TIMEOUT_COMMAND_RE.match(command):
        raise ValueError(f"Orden no permitida para el agente: {command!r}")


# --- Gestor ----------------------------------------------------------------------

class BluetoothManager:
    """Estado, dispositivos y emparejamiento de BlueZ. Seguro entre hilos.

    Todo lo externo es inyectable para los tests: `run` (subprocess.run),
    `agent_factory`, `clock` (monotónico), `now` (datetime) y `sleep`.
    Con `start_watcher=False` no se crea el hilo vigilante: los tests llaman
    a `tick()` a mano.
    """

    available = True

    def __init__(self, run=subprocess.run, agent_factory=BluetoothctlAgent,
                 clock=time.monotonic, now=datetime.now, sleep=time.sleep,
                 pairing_seconds=PAIRING_SECONDS, poll_seconds=POLL_SECONDS,
                 start_watcher=True):
        self._run = run
        self._agent_factory = agent_factory
        self._clock = clock
        self._now = now
        self._sleep = sleep
        self.pairing_seconds = pairing_seconds
        self.poll_seconds = poll_seconds
        self.start_watcher = start_watcher
        self._lock = threading.RLock()
        # La salida del agente se procesa con su propio lock: start_pairing()
        # espera esa salida con self._lock tomado.
        self._output_lock = threading.Lock()
        self._session = None
        self._agent = None
        self._snapshot = set()           # emparejados al abrir la ventana
        self._events = set()             # MAC con "[CHG] ... Paired: yes" en la ventana
        self._pending = ""               # salida del agente aún sin procesar
        self._wake = threading.Event()
        self._next_poll = 0.0

    # Consultas

    def adapter(self):
        ok, output = self._ctl("show")
        adapter = parse_show(output)
        if not ok and not adapter.available:
            return Adapter(available=False)
        return adapter

    def devices(self):
        """Dispositivos emparejados o de confianza (los "tuyos"), conectados primero."""
        _, known_text = self._ctl("devices")
        known = parse_devices(known_text)
        paired = parse_devices(self._ctl("devices", "Paired")[1])
        trusted = parse_devices(self._ctl("devices", "Trusted")[1])
        connected = parse_devices(self._ctl("devices", "Connected")[1])
        names = {**connected, **trusted, **paired, **known}
        result = [Device(mac=mac, name=names[mac], paired=mac in paired,
                         trusted=mac in trusted, connected=mac in connected)
                  for mac in names if mac in paired or mac in trusted or mac in connected]
        result.sort(key=lambda d: (not d.connected, d.name.lower()))
        return result

    def device(self, mac):
        """El dispositivo conocido con esa MAC. Lanza InvalidMac o BluetoothError."""
        mac = normalize_mac(mac)
        for device in self.devices():
            if device.mac == mac:
                return device
        raise BluetoothError("Ese dispositivo no está entre tus dispositivos Bluetooth.")

    # Acciones sobre dispositivos (solo MAC validadas y conocidas)

    def connect(self, mac):
        device = self.device(mac)
        self._action(device, "connect", CONNECT_TIMEOUT, "Connection successful",
                     "No se pudo conectar. Comprueba que el dispositivo está encendido y cerca.")
        logger.info("Bluetooth: conectado %s (%s)", device.name, device.mac)
        return device

    def disconnect(self, mac):
        device = self.device(mac)
        self._action(device, "disconnect", COMMAND_TIMEOUT, "Successful disconnected",
                     f"No se pudo desconectar «{device.name}». Inténtalo de nuevo.")
        logger.info("Bluetooth: desconectado %s (%s); sigue emparejado", device.name, device.mac)
        return device

    def trust(self, mac):
        device = self.device(mac)
        self._action(device, "trust", COMMAND_TIMEOUT, "trust succeeded",
                     f"No se pudo marcar «{device.name}» como de confianza.")
        logger.info("Bluetooth: %s (%s) marcado como de confianza", device.name, device.mac)
        return device

    def forget(self, mac):
        device = self.device(mac)
        self._action(device, "remove", COMMAND_TIMEOUT, "Device has been removed",
                     f"No se pudo olvidar «{device.name}». Inténtalo de nuevo.")
        logger.info("Bluetooth: olvidado %s (%s)", device.name, device.mac)
        return device

    def _action(self, device, verb, timeout, success, message):
        ok, output = self._ctl(verb, device.mac, timeout=timeout)
        if ok and (success in output or not _FAILURE_RE.search(output)):
            return
        detail = " ".join(strip_ansi(output).split())[:300]
        logger.warning("Bluetooth: %s %s falló: %s", verb, device.mac, detail or "sin salida")
        raise BluetoothError(message, detail)

    # Modo privado

    def ensure_private(self):
        """discoverable off + pairable off. True si ambos se aplicaron. Nunca lanza."""
        results = []
        for setting in ("discoverable", "pairable"):
            try:
                ok, output = self._ctl(setting, "off")
            except BluetoothError as exc:
                ok, output = False, exc.detail or str(exc)
            ok = ok and not _FAILURE_RE.search(output)
            if not ok:
                logger.warning("Bluetooth: no se pudo aplicar %s off: %s", setting,
                               " ".join(strip_ansi(output).split())[:200] or "sin salida")
            results.append(ok)
        return all(results)

    # Ventana de emparejamiento

    @property
    def session(self):
        return self._session

    def session_view(self):
        with self._lock:
            return self._session.as_dict(self._clock()) if self._session else None

    def recent_view(self, max_age=RECENT_SECONDS):
        """La última ventana si terminó hace menos de `max_age` s (para la página)."""
        with self._lock:
            session = self._session
            if (session is None or session.active or session.ended_at is None
                    or (self._now() - session.ended_at).total_seconds() > max_age):
                return None
            return session.as_dict(self._clock())

    def start_pairing(self):
        """Abre la ventana (o devuelve la que ya está abierta). Lanza BluetoothError."""
        with self._lock:
            if self._session and self._session.active:
                return self._session, False
            self._session = None
            adapter = self.adapter()
            if not adapter.available:
                raise BluetoothUnavailable("No se encuentra el adaptador Bluetooth de Groove.")
            if not adapter.powered:
                raise BluetoothError("El adaptador Bluetooth está apagado.")
            self._snapshot = set(parse_devices(self._ctl("devices", "Paired")[1]))
            with self._output_lock:
                self._events = set()
                self._pending = ""
            agent = self._agent_factory(self._on_agent_output)
            self._agent = agent
            try:
                agent.start()
                if not agent.wait_for("Agent registered", AGENT_TIMEOUT):
                    raise BluetoothError("No se pudo preparar el emparejamiento.",
                                         "el agente no se registró")
                agent.send("default-agent")
                if not agent.wait_for("Default agent request successful", AGENT_TIMEOUT):
                    raise BluetoothError("No se pudo preparar el emparejamiento.",
                                         "default-agent no respondió")
                # La sesión existe antes de ser visibles: el agente ya acepta peticiones.
                now = self._now()
                self._session = PairingSession(
                    started_at=now, expires_at=now + timedelta(seconds=self.pairing_seconds),
                    deadline=self._clock() + self.pairing_seconds)
                # Red de seguridad: BlueZ oculta a Groove solo, aunque Groove muera.
                agent.send(f"discoverable-timeout {self.pairing_seconds}")
                agent.send("pairable on")
                agent.send("discoverable on")
                if not self._wait_visible():
                    raise BluetoothError("No se pudo hacer visible a Groove.",
                                         "pairable/discoverable no se activaron")
            except Exception as exc:
                logger.warning("Bluetooth: no se pudo abrir el emparejamiento: %s",
                               getattr(exc, "detail", "") or exc)
                if self._session is not None and self._session.active:
                    self._close(FAILED)
                else:
                    self._agent = None
                    self._stop_agent(agent)
                    self.ensure_private()
                if isinstance(exc, BluetoothError):
                    raise
                raise BluetoothError("No se pudo preparar el emparejamiento.", str(exc)) from exc
            self._next_poll = self._clock() + self.poll_seconds
            logger.info("Bluetooth: emparejamiento abierto durante %s s (Groove visible)",
                        self.pairing_seconds)
            if self.start_watcher:
                threading.Thread(target=self._watch, name="bluetooth-pairing",
                                 daemon=True).start()
            return self._session, True

    def cancel_pairing(self):
        with self._lock:
            if not (self._session and self._session.active):
                return None
            self._close(CANCELLED)
            logger.info("Bluetooth: emparejamiento cancelado desde Groove")
            return self._session

    def tick(self):
        """Una revisión de la ventana: caducidad, agente vivo y emparejados nuevos."""
        with self._lock:
            session = self._session
            if not (session and session.active):
                return
            now = self._clock()
            woken = self._wake.is_set()
            self._wake.clear()
            if woken or now >= self._next_poll:
                self._next_poll = now + self.poll_seconds
                if self._check_new_devices():
                    return
            if now >= session.deadline:
                self._close(TIMEOUT)
                logger.info("Bluetooth: emparejamiento cerrado por tiempo (%s s)",
                            self.pairing_seconds)
            elif self._agent is not None and not self._agent.alive():
                self._close(FAILED)
                logger.warning("Bluetooth: el agente terminó inesperadamente; "
                               "emparejamiento cerrado")

    def _watch(self):
        while True:
            self._wake.wait(1.0)
            try:
                self.tick()
            except Exception:
                logger.exception("Bluetooth: error vigilando el emparejamiento")
                with self._lock:
                    if self._session and self._session.active:
                        self._close(FAILED)
            with self._lock:
                if not (self._session and self._session.active):
                    return

    def _check_new_devices(self):
        """Confía en lo emparejado durante la ventana y la cierra. True si cerró."""
        paired = parse_devices(self._ctl("devices", "Paired")[1])
        with self._output_lock:
            events = set(self._events)
        new = [mac for mac in paired if mac not in self._snapshot or mac in events]
        if not new:
            return False
        for mac in new:
            ok = True
            try:
                self._ctl_checked("trust", mac)
            except BluetoothError as exc:
                ok = False
                logger.warning("Bluetooth: %s emparejado pero no se pudo confiar en él: %s",
                               mac, exc.detail or exc)
            device = Device(mac=mac, name=paired[mac], paired=True, trusted=ok)
            self._session.devices.append(device)
            logger.info("Bluetooth: nuevo dispositivo emparejado %s (%s)%s", device.name, mac,
                        " y de confianza" if ok else "")
        self._close(PAIRED)
        logger.info("Bluetooth: emparejamiento cerrado tras emparejar un dispositivo")
        return True

    def _close(self, state):
        session = self._session
        session.state = state
        session.ended_at = self._now()
        agent, self._agent = self._agent, None
        if agent is not None:
            for command in ("discoverable off", "pairable off"):
                try:
                    agent.send(command)
                except Exception:
                    pass
            self._stop_agent(agent)
        # Siempre, aunque el agente ya lo haya hecho: es idempotente.
        session.private_again = self.ensure_private()
        if session.private_again:
            logger.info("Bluetooth: Groove vuelve a modo privado")
        else:
            logger.warning("Bluetooth: no se pudo confirmar el modo privado (se reintentará "
                           "al arrancar Groove)")
        self._wake.set()

    def _stop_agent(self, agent):
        try:
            agent.stop()
        except Exception:
            logger.exception("Bluetooth: error cerrando el agente")

    def _wait_visible(self, attempts=10, delay=0.3):
        for _ in range(attempts):
            adapter = self.adapter()
            if adapter.discoverable and adapter.pairable:
                return True
            self._sleep(delay)
        return False

    def _on_agent_output(self, text):
        """Contesta a las preguntas del agente y detecta emparejamientos.

        Corre en el hilo lector del agente; no toma self._lock.
        """
        with self._output_lock:
            self._pending += strip_ansi(text)
            while True:
                match = _PROMPT_RE.search(self._pending)
                if not match:
                    break
                head, self._pending = self._pending[:match.end()], self._pending[match.end():]
                self._scan_lines(head)
                session, agent = self._session, self._agent
                accept = bool(session and session.active)
                # Sin registrar claves ni MAC del texto: solo el tipo de petición.
                kind = head.rsplit("\n", 1)[-1].split("(")[0].replace("[agent]", "")
                kind = re.sub(r"[0-9A-Fa-f:]{6,}|\d{4,}", "…", kind).strip()[:60]
                logger.info("Bluetooth: el agente %s «%s»", "acepta" if accept else "rechaza",
                            kind)
                if agent is not None:
                    agent.send("yes" if accept else "no")
            if _PIN_RE.search(self._pending):
                logger.warning("Bluetooth: el dispositivo pide un PIN; Groove no lo admite")
                self._pending = _PIN_RE.split(self._pending)[-1]
            lines = self._pending.split("\n")
            self._pending = lines.pop()[-4096:]
            self._scan_lines("\n".join(lines))

    def _scan_lines(self, text):
        for match in _PAIRED_EVENT_RE.finditer(text):
            self._events.add(match.group(1).upper())
            self._wake.set()

    # bluetoothctl de una sola orden

    def _ctl(self, *args, timeout=COMMAND_TIMEOUT):
        """Ejecuta `bluetoothctl <args>` (sin shell). Devuelve (ok, salida)."""
        command = [BLUETOOTHCTL, *args]
        try:
            completed = self._run(command, capture_output=True, text=True, timeout=timeout,
                                  check=False, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise BluetoothUnavailable("bluetoothctl no está instalado.") from None
        except subprocess.TimeoutExpired:
            raise BluetoothError("Bluetooth no respondió a tiempo.",
                                 f"bluetoothctl {args[0]} tardó más de {timeout} s") from None
        except OSError as exc:
            raise BluetoothError("No se pudo hablar con Bluetooth.", str(exc)) from None
        output = strip_ansi((completed.stdout or "") + (completed.stderr or ""))
        return completed.returncode == 0, output

    def _ctl_checked(self, *args):
        ok, output = self._ctl(*args)
        if not ok or _FAILURE_RE.search(output):
            raise BluetoothError("Bluetooth rechazó la orden.", " ".join(output.split())[:300])
        return output


class NoBluetoothManager:
    """Sin gestión de BlueZ (Windows, tests o BLUEZ_MANAGEMENT=off)."""

    available = False
    session = None

    def adapter(self):
        return Adapter(available=False)

    def devices(self):
        return []

    def session_view(self):
        return None

    def recent_view(self):
        return None

    def ensure_private(self):
        return True

    def start_pairing(self):
        raise BluetoothUnavailable("Bluetooth solo se gestiona en la Raspberry.")

    def cancel_pairing(self):
        return None

    def _unavailable(self, mac):
        normalize_mac(mac)
        raise BluetoothUnavailable("Bluetooth solo se gestiona en la Raspberry.")

    connect = disconnect = trust = forget = device = _unavailable


def create_bluetooth_manager(config, platform=None):
    platform = platform or sys.platform
    setting = str(config.get("BLUEZ_MANAGEMENT", "on")).strip().lower()
    if setting in ("", "none", "off", "false", "0") or not platform.startswith("linux"):
        return NoBluetoothManager()
    return BluetoothManager()


def run_failsafe(manager, delays=FAILSAFE_DELAYS, sleep=time.sleep):
    """Al arrancar Groove: deja Bluetooth en privado. Reintenta (bluetoothd puede
    no estar listo aún). Nunca lanza: un fallo se registra y Groove sigue."""
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            sleep(delay)
        try:
            if manager.ensure_private():
                logger.info("Bluetooth: modo privado confirmado al arrancar")
                return True
        except Exception:
            logger.exception("Bluetooth: error al asegurar el modo privado al arrancar")
        logger.warning("Bluetooth: no se pudo asegurar el modo privado (intento %s/%s)",
                       attempt, len(delays))
    return False
