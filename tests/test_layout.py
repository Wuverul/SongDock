"""The folder layout: every service and script can find what it runs."""

import glob
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "songdock"))
import dock_paths  # noqa: E402

CODE = glob.glob(os.path.join(ROOT, "songdock", "*", "*.py"))
RUN_DIRECTLY = ("dock_sync", "dock_prepare", "dock_boot", "dock_resync", "dock_timer",
                "dock_update", "dock_usbport", "dock_wifiprefer", "dock_status",
                "dock_report", "dock_players")


class LayoutTests(unittest.TestCase):
    def test_every_path_the_services_and_installer_run_exists(self):
        found = []
        for path in glob.glob(os.path.join(ROOT, "pi", "*")):
            with open(path, errors="replace") as f:
                text = f.read()
            found += re.findall(r"(?:@APP_DIR@|\$APP_DIR)/([\w./-]+\.py)", text)
        self.assertGreater(len(found), 8)
        for rel in found:
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)), rel)

    def test_no_code_left_at_the_top(self):
        self.assertEqual(glob.glob(os.path.join(ROOT, "*.py")), [])

    def test_module_names_are_unique(self):
        names = [os.path.basename(p) for p in CODE]
        self.assertEqual(len(names), len(set(names)))

    def test_paths_the_code_relies_on(self):
        self.assertEqual(dock_paths.ROOT, ROOT)
        for rel in ("VERSION", "vendor", "pi/install.sh", "pi/release_signers",
                    "config.example.toml"):
            self.assertTrue(os.path.exists(os.path.join(dock_paths.ROOT, rel)), rel)
        for name in ("dock_status.py", "dock_resync.py"):
            self.assertTrue(os.path.isfile(dock_paths.script(name)))

    def test_files_that_run_directly_find_the_rest_from_their_own_folder(self):
        # As python does for a script: only its own folder on the path.
        for name in RUN_DIRECTLY:
            path = next(p for p in CODE if os.path.basename(p) == name + ".py")
            with open(path) as f:
                self.assertIn("import dock_paths", f.read(), name)
            code = ("import sys, runpy; sys.path[0] = sys.argv[1]; "
                    f"runpy.run_path(sys.argv[2], run_name='not_main')")
            r = subprocess.run([sys.executable, "-c", code, os.path.dirname(path), path],
                               capture_output=True, text=True, cwd="/")
            if r.returncode:                 # third-party packages may be missing off the Pi
                self.assertRegex(r.stderr, r"No module named '(?!dock_|ipod_|rockbox_|subsonic)",
                                 f"{name}: {r.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()
