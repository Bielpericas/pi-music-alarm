#!/usr/bin/python3
"""Root-only, fixed-path Raspotify mode transactions. Never reads credentials.

Installed outside the checkout. The only public operations are the systemd
instances private, guest and status; check/boot are installer/root operations.
All diagnostics are fixed codes: command output and configuration stay private.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

PREFIX = "LIBRESPOT_"
KEYS = ("SYSTEM_CACHE", "CACHE", "USERNAME", "DISABLE_DISCOVERY",
        "DISABLE_CREDENTIAL_CACHE", "ACCESS_TOKEN", "PASSWORD", "ENABLE_OAUTH")
AUTH = ("ACCESS_TOKEN", "PASSWORD", "ENABLE_OAUTH")
ASSIGNMENT = re.compile(r"^\s*(LIBRESPOT_[A-Z_]+)\s*=(.*)$")
MENTION = re.compile(r"^\s*#?\s*(LIBRESPOT_[A-Z_]+)\s*=")
GUEST_GUARD = ("[Service]\n"
               "InaccessiblePaths=/var/cache/raspotify /var/lib/raspotify\n"
               "UnsetEnvironment=LIBRESPOT_USERNAME LIBRESPOT_ACCESS_TOKEN "
               "LIBRESPOT_PASSWORD LIBRESPOT_ENABLE_OAUTH LIBRESPOT_DISABLE_DISCOVERY\n")
PRIVATE_GUARD = ("[Service]\n"
                 "ReadOnlyPaths=/var/cache/raspotify/credentials.json\n"
                 "UnsetEnvironment=LIBRESPOT_ACCESS_TOKEN LIBRESPOT_PASSWORD LIBRESPOT_ENABLE_OAUTH\n")


class ModeError(Exception):
    """A fixed, non-secret diagnostic code."""


@dataclass(frozen=True)
class Paths:
    conf: Path = Path("/etc/raspotify/conf")
    state: Path = Path("/var/lib/groove-spotify")
    runtime: Path = Path("/run/groove-spotify")
    guard: Path = Path("/run/systemd/system/raspotify.service.d/90-groove-spotify-mode.conf")
    primary: Path = Path("/var/cache/raspotify")
    proc: Path = Path("/proc")

    @property
    def guest(self):
        return self.runtime / "guest"

    @property
    def backup(self):
        return self.state / "conf.original"

    @property
    def status(self):
        return self.runtime / "status.json"


def assignments(text):
    """Parse only the supported single-line EnvironmentFile assignments."""
    result = {}
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match and match[1] in {PREFIX + key for key in KEYS}:
            if line.endswith("\\") or match[1] in result:
                raise ModeError("unsupported_config")
            result[match[1]] = match[2].strip()
    return result


def value(raw):
    try:
        parts = shlex.split(raw, comments=False, posix=True)
        if len(parts) > 1:
            raise ValueError()
        return parts[0] if parts else ""
    except ValueError:
        raise ModeError("unsupported_config") from None


def render_config(current, baseline, guest, guest_path):
    """Change only cache/auth/discovery settings; keep audio and other lines."""
    wanted = {PREFIX + key: baseline.get(PREFIX + key) for key in KEYS}
    for key in AUTH:
        wanted[PREFIX + key] = None
    wanted[PREFIX + "DISABLE_DISCOVERY"] = None if guest else ""
    wanted[PREFIX + "SYSTEM_CACHE"] = '"' + (str(guest_path) if guest else "/var/cache/raspotify") + '"'
    if guest:
        wanted[PREFIX + "CACHE"] = '"' + str(guest_path) + '"'
        wanted[PREFIX + "USERNAME"] = None
        wanted[PREFIX + "DISABLE_CREDENTIAL_CACHE"] = ""
    lines, seen = [], set()
    assignments(current)  # reject ambiguous active assignments before changing anything
    for line in current.splitlines(keepends=True):
        match = MENTION.match(line)
        if match and match[1] in wanted:
            key = match[1]
            if key not in seen:
                lines.append(key + "=" + wanted[key] + "\n" if wanted[key] is not None
                             else "#" + key + "=\n")
                seen.add(key)
            else:
                # Preserve duplicate comments but never a second active assignment.
                lines.append(line if line.lstrip().startswith("#") else "#" + key + "=\n")
        else:
            lines.append(line)
    for key, raw in wanted.items():
        if key not in seen and raw is not None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(key + "=" + raw + "\n")
    return "".join(lines)


def atomic_write(path, data, mode=0o600, owner=None):
    """No partial config/status files; preserve the config owner and permissions."""
    fd, temporary = tempfile.mkstemp(prefix=".groove-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            os.fchmod(stream.fileno(), mode)
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Controller:
    def __init__(self, paths=None, run=subprocess.run, sleep=time.sleep, account=None):
        self.paths = paths or Paths()
        self.run = run
        self.sleep = sleep
        self.account = account  # injected only by unit tests

    def command(self, *args, check=True):
        try:
            completed = self.run(list(args), capture_output=True, text=True, timeout=20,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
        except (OSError, subprocess.SubprocessError):
            raise ModeError("command_failed") from None
        if check and completed.returncode:
            raise ModeError("command_failed")
        return completed

    def systemctl(self, *args, check=True):
        return self.command("/usr/bin/systemctl", "--no-ask-password", *args, check=check)

    def property(self, name):
        return self.systemctl("show", "raspotify.service", "--property=" + name,
                              "--value").stdout.strip()

    def identity(self):
        if self.account is not None:
            return self.account
        import pwd
        import grp
        if self.property("DynamicUser") != "no":
            # A dynamic UID can disappear/change between stop and start. It
            # cannot own this manually managed guest directory safely.
            raise ModeError("unsupported_dynamic_user")
        # A system unit with no User= executes as root. Raspotify's upstream
        # unit uses this default; preserve its existing identity and sandbox.
        user = self.property("User") or "root"
        group = self.property("Group")
        try:
            entry = pwd.getpwuid(int(user)) if user.isdecimal() else pwd.getpwnam(user)
        except KeyError:
            raise ModeError("unknown_service_user") from None
        try:
            gid = int(group) if group.isdecimal() else grp.getgrnam(group).gr_gid if group else entry.pw_gid
        except KeyError:
            raise ModeError("unknown_service_group") from None
        return entry.pw_uid, gid

    def read_conf(self, path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ModeError("unsafe_config")
        return path.read_text(encoding="utf-8")

    def baseline(self):
        original = assignments(self.read_conf(self.paths.backup))
        if (value(original.get(PREFIX + "SYSTEM_CACHE", "")) != "/var/cache/raspotify"
                or not value(original.get(PREFIX + "USERNAME", ""))
                or any(PREFIX + key in original for key in AUTH)):
            raise ModeError("unsupported_primary_config")
        return original

    def check(self):
        """Inspect the installed template AND binary AND actual service mapping."""
        current = self.read_conf(self.paths.conf)
        mentioned = {match[1] for line in current.splitlines()
                     if (match := MENTION.match(line))}
        required = {PREFIX + key for key in KEYS[:5]}
        if not required <= mentioned or "LIBRESPOT_" not in current or "foo-bar" not in current:
            raise ModeError("unsupported_template")
        help_text = self.command("/usr/bin/librespot", "--help").stdout
        if any("--" + key.lower().replace("_", "-") not in help_text for key in KEYS[:5]):
            raise ModeError("unsupported_librespot")
        files = self.property("EnvironmentFiles")
        start = self.property("ExecStart")
        if files not in ("/etc/raspotify/conf (ignore_errors=yes)",
                         "/etc/raspotify/conf (ignore_errors=no)"):
            raise ModeError("unsupported_environment_files")
        if "argv[]=/usr/bin/librespot ;" not in start:
            raise ModeError("unsupported_exec_start")
        self.identity()
        # A real, existing primary credential file is required, but never opened.
        credentials = self.paths.primary / "credentials.json"
        if self.paths.primary.is_symlink() or not stat.S_ISREG(credentials.lstat().st_mode):
            raise ModeError("missing_primary_credentials")
        if self.paths.backup.exists():
            self.baseline()
        else:
            parsed = assignments(current)
            if (value(parsed.get(PREFIX + "SYSTEM_CACHE", "")) != "/var/cache/raspotify"
                    or not value(parsed.get(PREFIX + "USERNAME", ""))
                    or PREFIX + "DISABLE_DISCOVERY" not in parsed
                    or any(PREFIX + key in parsed for key in AUTH)):
                raise ModeError("unsupported_primary_config")
        return current

    def initialize(self):
        current = self.check()  # no modifications before compatibility validation
        if not self.paths.backup.exists():
            atomic_write(self.paths.backup, current)

    def write_conf(self, text):
        info = self.paths.conf.lstat()
        atomic_write(self.paths.conf, text, stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid))

    def purge_guest(self):
        # Parent is root-owned, never writable by Raspotify/web. Never follow a link.
        guest = self.paths.guest
        if guest.is_symlink():
            guest.unlink()
        elif guest.exists():
            shutil.rmtree(guest)

    def prepare(self, guest):
        if guest:
            uid, gid = self.identity()
            self.purge_guest()
            self.paths.guest.mkdir(mode=0o700)
            os.chown(self.paths.guest, uid, gid)
        else:
            self.purge_guest()
        self.paths.guard.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.paths.guard, GUEST_GUARD if guest else PRIVATE_GUARD, 0o644)

    def actual(self):
        """Read the running process, not just the desired config. Return no secrets."""
        active = self.systemctl("is-active", "raspotify.service", check=False)
        result = {"enabled": None, "active": active.returncode == 0 and active.stdout.strip() == "active"}
        if not result["active"]:
            return result
        pid = self.property("MainPID")
        if not pid.isdigit() or int(pid) <= 0:
            return result
        try:
            raw = (self.paths.proc / pid / "environ").read_bytes()
            env = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
            baseline = self.baseline()
            guest = PREFIX.encode() + b"DISABLE_DISCOVERY" not in env
            current = self.read_conf(self.paths.conf)
            configured = assignments(current)
            expected = assignments(render_config(current, baseline,
                                                  guest, self.paths.guest))
            for key in KEYS:
                name = PREFIX + key
                if name == PREFIX + "CACHE" and not guest and name not in expected:
                    continue  # Raspotify's normal audio cache may come from the unit
                found = env.get(name.encode())
                desired = value(expected[name]).encode() if name in expected else None
                on_disk = value(configured[name]).encode() if name in configured else None
                if on_disk != desired:
                    return result
                if found != desired:
                    return result
            if self.paths.guard.read_text(encoding="utf-8") != (GUEST_GUARD if guest else PRIVATE_GUARD):
                return result
            if guest:
                info = self.paths.guest.lstat()
                if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                        or (info.st_uid, info.st_gid) != self.identity()):
                    return result
            elif self.paths.guest.exists() or self.paths.guest.is_symlink():
                return result
            result["enabled"] = guest
        except (OSError, ValueError, ModeError):
            pass
        return result

    def publish(self, error=None):
        result = self.actual()
        result["error"] = error
        atomic_write(self.paths.status, json.dumps(result) + "\n", 0o644)
        return result

    def verify(self, guest):
        # A Type=simple restart can succeed before librespot fails during startup.
        for _ in range(3):
            self.sleep(1)
            state = self.actual()
            if state["enabled"] is not guest or not state["active"]:
                raise ModeError("start_failed")

    def restart(self, guest):
        self.systemctl("daemon-reload")
        self.systemctl("restart", "raspotify.service")
        self.verify(guest)

    def boot(self):
        """Before every boot's Raspotify start: always private, no DB/secrets read."""
        self.initialize()
        self.prepare(False)
        self.write_conf(render_config(self.read_conf(self.paths.conf), self.baseline(),
                                      False, self.paths.guest))
        # Runtime drop-in created after systemd's initial unit load.
        self.systemctl("daemon-reload")

    def change(self, guest):
        self.check()  # installed binary/unit may have changed since installation
        self.baseline()
        previous = self.actual()
        if previous["enabled"] is guest and previous["active"]:
            return self.publish()
        old_conf = self.read_conf(self.paths.conf)
        # Unknown/mismatched state always rolls back to private, never to guests.
        old_guest = previous["enabled"] is True
        try:
            self.systemctl("stop", "raspotify.service")
            self.prepare(guest)
            self.write_conf(render_config(old_conf, self.baseline(), guest, self.paths.guest))
            self.restart(guest)
        except (ModeError, OSError, ValueError):
            try:
                self.systemctl("stop", "raspotify.service")
                self.prepare(old_guest)
                self.write_conf(old_conf if previous["enabled"] is not None else
                                render_config(old_conf, self.baseline(), False, self.paths.guest))
                self.restart(old_guest)
                self.publish("change_failed_rolled_back")
            except (ModeError, OSError, ValueError):
                # No confirmed recovery: leave private on disk, erase guest session,
                # and stop the service rather than claim that it is running safely.
                self.systemctl("stop", "raspotify.service", check=False)
                self.write_conf(render_config(old_conf, self.baseline(), False, self.paths.guest))
                self.prepare(False)
                self.systemctl("daemon-reload", check=False)
                self.publish("rollback_failed")
            raise ModeError("change_failed") from None
        return self.publish()


@contextlib.contextmanager
def locked(paths):
    import fcntl
    for path, mode in ((paths.state, 0o700), (paths.runtime, 0o755)):
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
            raise ModeError("unsafe_directory")
    fd = os.open(paths.runtime / "transition.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def main():
    if os.geteuid() != 0 or len(sys.argv) != 2 or sys.argv[1] not in {"check", "boot", "guest", "private", "status"}:
        print("spotify_mode: invalid_invocation", file=sys.stderr)
        return 1
    controller = Controller()
    try:
        with locked(controller.paths):
            action = sys.argv[1]
            if action == "check":
                controller.check()
            elif action == "boot":
                controller.boot()
            elif action == "status":
                controller.publish()
            else:
                controller.change(action == "guest")
        return 0
    except ModeError as exc:
        print("spotify_mode: " + str(exc), file=sys.stderr)  # fixed codes only
        return 1
    except (OSError, ValueError):
        # Never stringify exceptions: they can contain config, usernames or tokens.
        print("spotify_mode: operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
