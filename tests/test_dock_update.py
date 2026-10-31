"""dock_update: announcing, signatures, and swapping folders."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401

import dock_update as u  # noqa: E402

NOW = 1_800_000_000
HOUR = 3600


class UpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.state = os.path.join(self.tmp, "update.json")
        self.app = os.path.join(self.tmp, "songdock")
        self.make_app(self.app, "SongDock 0.7.1 (abc, 2026-10-30)", "exit 0")
        with open(os.path.join(self.app, "pi", "release_signers"), "w") as f:
            f.write("x")
        patches = [mock.patch.object(u, "systemctl",
                                     return_value=mock.Mock(returncode=0, stdout="")),
                   mock.patch.object(u, "draw"), mock.patch.object(u, "blocker", return_value=None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def make_app(self, folder, version, script):
        os.makedirs(os.path.join(folder, "pi"))
        with open(os.path.join(folder, "VERSION"), "w") as f:
            f.write(version)
        path = os.path.join(folder, "pi", "install.sh")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\necho ran >> \"$(dirname \"$0\")/../installed\"\n{script}\n")
        os.chmod(path, 0o755)

    def package(self, version, script="exit 0", top="songdock"):
        src = os.path.join(self.tmp, "src", top)
        shutil.rmtree(os.path.join(self.tmp, "src"), ignore_errors=True)
        self.make_app(src, f"SongDock {version} (x, y)", script)
        out = os.path.join(self.tmp, "release.tar.gz")
        with tarfile.open(out, "w:gz") as t:
            t.add(src, arcname=top)
        return out

    def fetch(self, version="0.8.0"):
        return lambda: (u.version_of(version), "pkg", "sig")

    def go(self, now=NOW, version="0.8.0", package=None, check=lambda *a: True, **kw):
        package = package or self.package(version)

        def get(url, path):
            shutil.copy(package, path) if url == "pkg" else open(path, "w").close()
        return u.run(now=now, path=self.state, app=self.app, fetch=self.fetch(version),
                     get=get, check=check, auto=kw.pop("auto", True),
                     blocked=kw.pop("blocked", lambda: None), **kw)

    def st(self):
        return u.load_state(self.state)

    def version_here(self):
        return u.current_version(self.app)

    def test_versions(self):
        self.assertEqual(u.version_of("SongDock 0.10.2 (a, b)"), (0, 10, 2))
        self.assertGreater(u.version_of("v0.10.0"), u.version_of("0.9.9"))
        self.assertIsNone(u.version_of("nope"))

    def test_same_or_older_version_does_nothing(self):
        self.assertEqual(self.go(version="0.7.1"), "nothing to install")
        self.assertEqual(self.go(now=NOW + 2 * 86400, version="0.6.0"), "nothing to install")
        self.assertNotIn("version", self.st())

    def test_announced_for_four_hours_with_the_time_then_installed(self):
        told = []
        self.assertEqual(self.go(on_notice=lambda: told.append(1)), "waiting")
        self.assertEqual(told, [1])
        self.assertEqual(self.st()["install_after"], NOW + 4 * HOUR)
        self.assertIn("Update 0.8.0 at ", u.note(self.st(), NOW))
        self.assertEqual(self.go(now=NOW + 4 * HOUR - 60), "waiting")
        self.assertEqual(self.version_here(), (0, 7, 1))
        self.assertEqual(self.go(now=NOW + 4 * HOUR), "installed")
        self.assertEqual(self.version_here(), (0, 8, 0))
        self.assertTrue(os.path.exists(os.path.join(self.app, "installed")))
        self.assertNotIn("version", self.st())
        self.assertNotIn("installing", self.st())
        self.assertTrue(os.path.isdir(self.app + ".old"))

    def test_waits_for_a_player_or_prepare_run_and_says_so(self):
        self.go()
        when = NOW + 5 * HOUR
        self.assertEqual(self.go(now=when, blocked=lambda: "a player is connected"), "blocked")
        self.assertEqual(self.version_here(), (0, 7, 1))
        self.assertEqual(u.note(self.st(), when), "Update 0.8.0: when unplugged")
        self.assertEqual(self.go(now=when + 900), "installed")

    def test_bad_signature_is_refused_for_good(self):
        self.go()
        when = NOW + 5 * HOUR
        self.assertEqual(self.go(now=when, check=lambda *a: False), "failed")
        self.assertEqual(self.version_here(), (0, 7, 1))
        self.assertEqual(self.st()["failed"], "0.8.0")
        u.systemctl.assert_not_called()                     # syncs were never held back
        self.assertEqual(self.go(now=when + 2 * 86400), "nothing to install")
        self.assertEqual(self.go(now=when + 4 * 86400, version="0.8.1"), "waiting")

    def test_package_with_another_version_inside_is_refused(self):
        self.go()
        old = self.package("0.7.0")
        self.assertEqual(self.go(now=NOW + 5 * HOUR, package=old), "failed")
        self.assertEqual(self.version_here(), (0, 7, 1))

    def test_package_that_isnt_ours_is_refused(self):
        self.go()
        other = self.package("0.8.0", top="other")
        self.assertEqual(self.go(now=NOW + 5 * HOUR, package=other), "failed")
        self.assertEqual(self.version_here(), (0, 7, 1))

    def test_failed_installer_puts_the_old_version_back(self):
        self.go()
        bad = self.package("0.8.0", script="exit 3")
        self.assertEqual(self.go(now=NOW + 5 * HOUR, package=bad), "failed")
        self.assertEqual(self.version_here(), (0, 7, 1))
        self.assertTrue(os.path.exists(os.path.join(self.app, "installed")))  # old one re-ran
        self.assertIn("Installer failed", self.st()["failed_why"])
        u.systemctl.assert_any_call("unmask", "--runtime", u.SYNC_UNITS)
        u.systemctl.assert_any_call("start", u.PREPARE_TIMER)

    def test_something_starting_during_the_download_stops_the_swap(self):
        self.go()
        u.blocker.return_value = "a sync is running"
        self.assertEqual(self.go(now=NOW + 5 * HOUR), "blocked")
        self.assertEqual(self.version_here(), (0, 7, 1))
        u.systemctl.assert_any_call("unmask", "--runtime", u.SYNC_UNITS)
        self.assertEqual(self.st()["version"], "0.8.0")     # it tries again

    def test_syncs_are_held_back_before_the_swap(self):
        self.go()
        self.go(now=NOW + 5 * HOUR)
        names = [c.args[0] for c in u.systemctl.call_args_list]
        self.assertEqual(names[:2], ["mask", "stop"])

    def test_turned_off_only_announces(self):
        self.assertEqual(self.go(auto=False), "nothing to install")
        self.assertEqual(u.note(self.st()), "Update 0.8.0 available")
        self.go()
        self.assertEqual(self.go(now=NOW + 5 * HOUR, auto=False), "nothing to install")
        self.assertEqual(self.version_here(), (0, 7, 1))

    def test_without_a_signers_file_only_announces(self):
        os.remove(os.path.join(self.app, "pi", "release_signers"))
        self.go()
        self.assertEqual(u.note(self.st()), "Update 0.8.0 available")
        self.assertEqual(self.go(now=NOW + 5 * HOUR), "nothing to install")

    def test_release_without_a_signature_only_announces(self):
        def fetch():
            return (0, 8, 0), "pkg", None
        u.run(now=NOW, path=self.state, app=self.app, fetch=fetch, auto=True)
        self.assertNotIn("install_after", self.st())

    def test_installed_by_hand_clears_the_notice(self):
        self.go()
        with open(os.path.join(self.app, "VERSION"), "w") as f:
            f.write("0.8.0")
        self.assertEqual(self.go(now=NOW + 3600, version="0.7.1"), "nothing to install")
        self.assertNotIn("version", self.st())

    def test_a_copy_without_version_does_nothing(self):
        os.remove(os.path.join(self.app, "VERSION"))
        self.assertEqual(self.go(), "no version here")

    def test_offline_is_quiet_and_tries_again(self):
        def fetch():
            raise OSError("no route")
        self.assertEqual(u.run(now=NOW, path=self.state, app=self.app, fetch=fetch, auto=True),
                         "offline")
        self.assertEqual(self.go(), "waiting")

    def test_github_asked_once_a_day(self):
        calls = []

        def fetch():
            calls.append(1)
            return (0, 7, 1), None, None
        for now in (NOW, NOW + HOUR, NOW + 86400):
            u.run(now=now, path=self.state, app=self.app, fetch=fetch, auto=True)
        self.assertEqual(len(calls), 2)

    def test_power_cut_during_the_installer_is_finished(self):
        u.save_state({"installing": True, "checked": NOW}, self.state)
        u.run(now=NOW + 60, path=self.state, app=self.app, auto=True)
        self.assertNotIn("installing", self.st())
        self.assertTrue(os.path.exists(os.path.join(self.app, "installed")))

    def test_player_detection(self):
        usb = os.path.join(self.tmp, "usb")
        os.makedirs(os.path.join(usb, "hub"))
        with open(os.path.join(usb, "hub", "idVendor"), "w") as f:
            f.write("1d6b\n")
        self.assertFalse(u.player_connected(usb))
        os.makedirs(os.path.join(usb, "ipod"))                # ejected: no disk, still there
        with open(os.path.join(usb, "ipod", "idVendor"), "w") as f:
            f.write("05AC\n")
        self.assertTrue(u.player_connected(usb))
        shutil.rmtree(os.path.join(usb, "ipod"))
        os.makedirs(os.path.join(usb, "1-1:1.0"))
        with open(os.path.join(usb, "1-1:1.0", "bInterfaceClass"), "w") as f:
            f.write("08\n")
        self.assertTrue(u.player_connected(usb))

    def test_config_setting(self):
        path = os.path.join(self.tmp, "c.toml")
        self.assertFalse(u.auto_update_on(path))              # unreadable: don't install
        with open(path, "w") as f:
            f.write("[sync]\n")
        self.assertTrue(u.auto_update_on(path))               # the default
        with open(path, "w") as f:
            f.write("[sync]\nauto_update = false\n")
        self.assertFalse(u.auto_update_on(path))

    def test_real_signature_check(self):
        key = os.path.join(self.tmp, "k")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], check=True)
        data = self.package("0.8.0")
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key, "-n", u.SIGNER, data],
                       check=True)
        with open(key + ".pub") as f:
            pub = f.read()
        signers = os.path.join(self.tmp, "signers")
        with open(signers, "w") as f:
            f.write(f"{u.SIGNER} {pub}")
        self.assertTrue(u.signature_ok(data, data + ".sig", signers))
        with open(data, "ab") as f:
            f.write(b"x")
        self.assertFalse(u.signature_ok(data, data + ".sig", signers))


if __name__ == "__main__":
    unittest.main()
