"""Root helper transactions on a temporary POSIX filesystem, fake systemd/proc.

Run with Python on Linux (also WSL). No real services or credentials are touched.
"""
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spotify_mode_helper", ROOT / "deploy/spotify-mode-helper.py")
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)

CONF = '''# option foo-bar becomes LIBRESPOT_FOO_BAR
LIBRESPOT_NAME="Groove"
LIBRESPOT_SYSTEM_CACHE="/var/cache/raspotify"
#LIBRESPOT_CACHE=
LIBRESPOT_USERNAME="private-user-secret"
LIBRESPOT_DISABLE_DISCOVERY=
#LIBRESPOT_DISABLE_CREDENTIAL_CACHE=
#LIBRESPOT_ACCESS_TOKEN=
#LIBRESPOT_PASSWORD=
#LIBRESPOT_ENABLE_OAUTH=
LIBRESPOT_BACKEND="alsa"
LIBRESPOT_DEVICE="hw:Device"
LIBRESPOT_BITRATE=160
LIBRESPOT_INITIAL_VOLUME=37
'''


class ConfigTest(unittest.TestCase):
    def test_guest_and_private_change_only_managed_keys(self):
        baseline = helper.assignments(CONF)
        guest = helper.render_config(CONF, baseline, True, PurePosixPath("/run/groove-spotify/guest"))
        parsed = helper.assignments(guest)
        self.assertNotIn("LIBRESPOT_USERNAME", parsed)
        self.assertNotIn("LIBRESPOT_DISABLE_DISCOVERY", parsed)
        self.assertEqual(parsed["LIBRESPOT_DISABLE_CREDENTIAL_CACHE"], "")
        for key in ("CACHE", "SYSTEM_CACHE"):
            self.assertEqual(helper.value(parsed["LIBRESPOT_" + key]), "/run/groove-spotify/guest")
        private = helper.render_config(guest, baseline, False, PurePosixPath("/run/groove-spotify/guest"))
        self.assertEqual(helper.assignments(private), baseline)
        for text in (guest, private):
            for line in CONF.splitlines()[1:2] + CONF.splitlines()[-4:]:
                self.assertIn(line, text)
        self.assertEqual(helper.render_config(guest, baseline, True, PurePosixPath("/run/groove-spotify/guest")), guest)

    def test_duplicate_or_multiline_active_keys_rejected(self):
        for invalid in (CONF + 'LIBRESPOT_USERNAME="other"\n', CONF + 'LIBRESPOT_CACHE=\\\n/tmp\n'):
            with self.assertRaises(helper.ModeError):
                helper.assignments(invalid)


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "POSIX root fixture required (use WSL -u root)")
class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="groove-tests-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.paths = helper.Paths(root / "etc/conf", root / "state", root / "run",
                                  root / "systemd/raspotify.service.d/90-mode.conf", root / "primary", root / "proc")
        for directory in (self.paths.conf.parent, self.paths.state, self.paths.runtime, self.paths.primary):
            directory.mkdir()
        self.paths.conf.write_text(CONF)
        self.paths.conf.chmod(0o640)
        self.credentials = self.paths.primary / "credentials.json"
        self.credentials.write_bytes(b"PRIMARY-CREDENTIALS-DO-NOT-READ-OR-WRITE")
        self.active = False
        self.calls = []
        self.fail_restarts = 0
        self.fail_verification = False
        self.help_text = " ".join("--" + key.lower().replace("_", "-") for key in helper.KEYS[:5])
        self.controller = helper.Controller(self.paths, run=self.run_command, sleep=lambda _: None,
                                            account=(os.getuid(), os.getgid()))
        self.controller.boot()
        self.run_command(["/usr/bin/systemctl", "--no-ask-password", "restart", "raspotify.service"])
        self.calls.clear()

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        output, code = "", 0
        if command[0] == "/usr/bin/librespot":
            output = self.help_text
        elif "show" in command:
            properties = {"EnvironmentFiles": "/etc/raspotify/conf (ignore_errors=yes)",
                          "ExecStart": "{ path=/usr/bin/librespot ; argv[]=/usr/bin/librespot ; ignore_errors=no ; }",
                          "MainPID": "123" if self.active else "0"}
            output = properties[command[-2].split("=", 1)[1]]
        elif "stop" in command:
            self.active = False
        elif "restart" in command:
            if self.fail_restarts:
                self.fail_restarts -= 1
                self.active = False
                code = 1
            else:
                self.active = True
                parsed = helper.assignments(self.paths.conf.read_text())
                env = {name: helper.value(raw) for name, raw in parsed.items()}
                env.setdefault("LIBRESPOT_CACHE", "/var/cache/raspotify")
                proc = self.paths.proc / "123"
                proc.mkdir(parents=True, exist_ok=True)
                (proc / "environ").write_bytes(b"\0".join((name + "=" + raw).encode() for name, raw in env.items()))
        elif "is-active" in command:
            output = "active\n" if self.active else "inactive\n"
            code = 0 if self.active else 3
            if self.fail_verification and self.active:
                self.active = False
        return subprocess.CompletedProcess(command, code, output, "SECRET-COMMAND-STDERR")

    def assert_primary_unchanged(self):
        self.assertEqual(self.credentials.read_bytes(), b"PRIMARY-CREDENTIALS-DO-NOT-READ-OR-WRITE")

    def test_initial_private_active_and_immutable_backup(self):
        self.assertEqual(self.controller.actual(), {"enabled": False, "active": True})
        self.assertEqual(self.paths.backup.read_text(), CONF)
        self.assertEqual(stat.S_IMODE(self.paths.backup.stat().st_mode), 0o600)
        self.assertIn("ReadOnlyPaths=/var/cache/raspotify/credentials.json", self.paths.guard.read_text())
        self.assert_primary_unchanged()

    def test_guest_full_cycle_isolation_cleanup_and_active(self):
        self.assertTrue(self.controller.change(True)["enabled"])
        guest_config = helper.assignments(self.paths.conf.read_text())
        self.assertEqual(helper.value(guest_config["LIBRESPOT_SYSTEM_CACHE"]), str(self.paths.guest))
        self.assertEqual(helper.value(guest_config["LIBRESPOT_CACHE"]), str(self.paths.guest))
        self.assertNotIn("LIBRESPOT_DISABLE_DISCOVERY", guest_config)
        self.assertNotIn("LIBRESPOT_USERNAME", guest_config)
        self.assertEqual(stat.S_IMODE(self.paths.guest.stat().st_mode), 0o700)
        self.assertIn("InaccessiblePaths=/var/cache/raspotify /var/lib/raspotify", self.paths.guard.read_text())
        (self.paths.guest / "credentials.json").write_text("TEMPORARY-GUEST-SECRET")
        self.assert_primary_unchanged()
        self.assertFalse(self.controller.change(False)["enabled"])
        self.assertFalse(self.paths.guest.exists())
        self.assertEqual(helper.assignments(self.paths.conf.read_text()), helper.assignments(CONF))
        self.assertTrue(self.active)
        self.assert_primary_unchanged()
        self.assertEqual(self.paths.backup.read_text(), CONF)
        self.assertNotIn("private-user-secret", self.paths.status.read_text())

    def test_idempotent_requests_do_not_restart_or_purge_live_guest_session(self):
        for mode in (False, True):
            self.controller.change(mode)
            if mode:
                (self.paths.guest / "session").write_text("session")
            self.calls.clear()
            self.controller.change(mode)
            self.assertFalse(any("stop" in command or "restart" in command for command in self.calls))
            if mode:
                self.assertTrue((self.paths.guest / "session").exists())

    def test_failed_enabling_rolls_back_private_and_deletes_guest(self):
        self.fail_restarts = 1
        with self.assertRaises(helper.ModeError):
            self.controller.change(True)
        state = self.controller.actual()
        self.assertEqual(state, {"enabled": False, "active": True})
        self.assertEqual(json.loads(self.paths.status.read_text())["error"], "change_failed_rolled_back")
        self.assertFalse(self.paths.guest.exists())
        self.assert_primary_unchanged()

    def test_failed_disabling_rolls_back_observed_guest_with_fresh_session(self):
        self.controller.change(True)
        (self.paths.guest / "credentials.json").write_text("GUEST-SECRET")
        self.fail_restarts = 1
        with self.assertRaises(helper.ModeError):
            self.controller.change(False)
        self.assertEqual(self.controller.actual(), {"enabled": True, "active": True})
        self.assertFalse((self.paths.guest / "credentials.json").exists())
        self.assert_primary_unchanged()

    def test_failed_rollback_leaves_private_stopped_and_no_guest(self):
        self.fail_restarts = 2
        with self.assertRaises(helper.ModeError):
            self.controller.change(True)
        self.assertFalse(self.active)
        self.assertFalse(self.paths.guest.exists())
        self.assertIn("LIBRESPOT_DISABLE_DISCOVERY", helper.assignments(self.paths.conf.read_text()))
        self.assertEqual(json.loads(self.paths.status.read_text())["error"], "rollback_failed")
        self.assert_primary_unchanged()

    def test_process_dies_after_successful_restart_is_not_reported_success(self):
        self.fail_verification = True
        with self.assertRaises(helper.ModeError):
            self.controller.change(True)
        self.assertFalse(self.active)
        self.assertIsNone(self.controller.actual()["enabled"])

    def test_cache_initialization_failure_rolls_back_private(self):
        with mock.patch.object(helper.os, "chown", side_effect=OSError("SECRET")):
            with self.assertRaises(helper.ModeError):
                self.controller.change(True)
        self.assertFalse(self.controller.actual()["enabled"])
        self.assertFalse(self.paths.guest.exists())

    def test_boot_always_resets_to_private_before_raspotify_can_start(self):
        self.controller.change(True)
        self.active = False
        (self.paths.guest / "session").write_text("secret")
        self.controller.boot()
        self.assertFalse(self.paths.guest.exists())
        self.assertIn("LIBRESPOT_DISABLE_DISCOVERY", helper.assignments(self.paths.conf.read_text()))
        self.assertFalse(self.active)  # boot never starts/stops Raspotify, avoids dependency deadlock
        self.assert_primary_unchanged()

    def test_guest_never_reads_primary_credentials(self):
        real = Path.read_bytes
        def guarded(path):
            if path == self.credentials:
                self.fail("helper read primary credentials")
            return real(path)
        with mock.patch.object(Path, "read_bytes", guarded):
            self.controller.change(True)
            self.controller.change(False)

    def test_cleanup_does_not_follow_symlink(self):
        self.paths.guest.symlink_to(self.paths.primary, target_is_directory=True)
        self.controller.purge_guest()
        self.assertFalse(self.paths.guest.is_symlink())
        self.assert_primary_unchanged()

    def test_actual_process_config_mismatch_is_unknown(self):
        self.paths.guard.unlink()
        self.assertIsNone(self.controller.actual()["enabled"])
        self.controller.change(False)
        self.paths.conf.write_text(self.paths.conf.read_text().replace("hw:Device", "hw:Other"))
        self.controller.change(True)
        self.controller.change(False)
        self.assertIn("hw:Other", self.paths.conf.read_text())  # live audio edits survive

    def test_disk_mode_change_without_process_restart_is_not_confirmed(self):
        self.paths.conf.write_text(helper.render_config(self.paths.conf.read_text(), self.controller.baseline(),
                                                       True, self.paths.guest))
        self.assertIsNone(self.controller.actual()["enabled"])

    def test_compatibility_failure_has_no_config_or_credential_mutations(self):
        self.help_text = "--username"
        before = self.paths.conf.read_bytes()
        with self.assertRaises(helper.ModeError):
            self.controller.initialize()
        self.assertEqual(self.paths.conf.read_bytes(), before)
        self.assert_primary_unchanged()

    def test_atomic_write_failure_leaves_original_config(self):
        before = self.paths.conf.read_bytes()
        with mock.patch.object(helper.os, "replace", side_effect=OSError("SECRET")):
            with self.assertRaises(OSError):
                self.controller.write_conf("replacement")
        self.assertEqual(self.paths.conf.read_bytes(), before)
        self.assertEqual(list(self.paths.conf.parent.glob(".groove-*")), [])

    def test_unsafe_or_symlinked_configuration_is_rejected(self):
        self.paths.conf.chmod(0o666)
        with self.assertRaises(helper.ModeError):
            self.controller.change(True)
        self.paths.conf.unlink()
        self.paths.conf.symlink_to(self.paths.backup)
        with self.assertRaises(helper.ModeError):
            self.controller.change(True)
        self.assert_primary_unchanged()

    def test_inherited_authentication_is_not_reported_as_valid_guest(self):
        self.controller.change(True)
        process = self.paths.proc / "123/environ"
        process.write_bytes(process.read_bytes() + b"\0LIBRESPOT_ACCESS_TOKEN=TOKEN-SECRET")
        self.assertIsNone(self.controller.actual()["enabled"])
        self.controller.publish()
        self.assertNotIn("TOKEN-SECRET", self.paths.status.read_text())


class DeploymentSecurityTest(unittest.TestCase):
    def test_polkit_grants_only_start_for_three_fixed_instances(self):
        rule = (ROOT / "deploy/pi-music-alarm-spotify-guest.rules.template").read_text()
        self.assertIn('subject.user === "@USER@"', rule)
        self.assertIn('action.lookup("verb") === "start"', rule)
        for mode in ("guest", "private", "status"):
            self.assertIn(f'"groove-spotify-mode@{mode}.service"', rule)
        for operation in ("restart", "stop", "enable", "disable"):
            self.assertNotIn('"' + operation + '"', rule)
        self.assertNotIn("groove-spotify-mode@boot.service", rule)

    def test_service_isolates_python_and_keeps_web_privileges(self):
        unit = (ROOT / "deploy/groove-spotify-mode@.service").read_text()
        self.assertIn("/usr/bin/python3 -I /usr/local/libexec/groove-spotify-mode %i", unit)
        self.assertIn("ProtectSystem=strict", unit)
        app_unit = (ROOT / "deploy/pi-music-alarm.service.template").read_text()
        self.assertIn("NoNewPrivileges=true", app_unit)
        installer = (ROOT / "deploy/install-spotify-guest-mode.sh").read_text()
        self.assertNotIn("sudoers", installer[installer.index("set -euo"):])
        self.assertLess(installer.index('spotify-mode-helper.py" check'),
                        installer.index("sudo systemctl stop"))

    def test_system_files_have_unix_newlines(self):
        for name in ("groove-spotify-mode@.service", "groove-spotify-boot.service",
                     "raspotify-guest-mode.conf", "pi-music-alarm-spotify-guest.conf",
                     "install-spotify-guest-mode.sh", "spotify-mode-helper.py",
                     "pi-music-alarm-spotify-guest.rules.template"):
            with self.subTest(name=name):
                self.assertNotIn(b"\r\n", (ROOT / "deploy" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
