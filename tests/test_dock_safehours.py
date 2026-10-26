"""Tests for Safe Hours (no syncs in a daily window).

Run: python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_safehours as sh  # noqa: E402

MORNING = (7 * 60, 10 * 60)


class SafeHoursTests(unittest.TestCase):
    def test_formats(self):
        for text in ("07:00-10:00", "7-10", "7:00 - 10", " 07-10:00 "):
            self.assertEqual(sh.parse(text), MORNING, text)
        self.assertEqual(sh.parse("22:30-06:15"), (22 * 60 + 30, 6 * 60 + 15))
        for off in ("", "off", "None", None):
            self.assertIsNone(sh.parse(off))
        for bad in ("7am-10am", "25:00-10:00", "07:60-10:00", "10-10", "7"):
            with self.assertRaises(sh.SafeHoursError, msg=bad):
                sh.parse(bad)
        self.assertEqual(sh.text(sh.parse("7-10")), "07:00-10:00")

    def test_inside_and_outside(self):
        at = lambda h, m=0: datetime(2026, 10, 27, h, m)       # noqa: E731
        self.assertFalse(sh.active(MORNING, at(6, 59)))
        self.assertTrue(sh.active(MORNING, at(7, 0)))
        self.assertTrue(sh.active(MORNING, at(9, 59)))
        self.assertFalse(sh.active(MORNING, at(10, 0)))       # the end is when syncs resume
        night = sh.parse("22:00-06:00")                       # wraps past midnight
        self.assertTrue(sh.active(night, at(23)))
        self.assertTrue(sh.active(night, at(2)))
        self.assertFalse(sh.active(night, at(12)))
        self.assertFalse(sh.active(None, at(8)))

    def test_when_the_window_ends_and_next_opens(self):
        self.assertEqual(sh.window_end(MORNING, datetime(2026, 10, 27, 8, 15)),
                         datetime(2026, 10, 27, 10, 0))
        night = sh.parse("22:00-06:00")
        self.assertEqual(sh.window_end(night, datetime(2026, 10, 27, 23, 0)),
                         datetime(2026, 10, 28, 6, 0))
        self.assertEqual(sh.next_start(MORNING, datetime(2026, 10, 27, 6, 0)),
                         datetime(2026, 10, 27, 7, 0))
        self.assertEqual(sh.next_start(MORNING, datetime(2026, 10, 27, 11, 0)),
                         datetime(2026, 10, 28, 7, 0))
        self.assertIsNone(sh.next_start(None))

    def test_resync_scheduled_for_the_end_of_the_window(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stderr="")
        self.assertTrue(sh.schedule_after(MORNING, "/app/dock_resync.py",
                                          now=datetime(2026, 10, 27, 8, 0), run=run))
        argv = calls[0]
        self.assertEqual(argv[:3], ["systemd-run", "--unit", sh.AFTER_UNIT])
        self.assertIn("2026-10-27 10:00:00", argv)
        self.assertEqual(argv[-2:], ["/app/dock_resync.py", "--after-safe-hours"])
        already = lambda argv, **kw: SimpleNamespace(returncode=1,      # noqa: E731
                                                     stderr="Unit already exists")
        self.assertTrue(sh.schedule_after(MORNING, "/x", run=already))

    def test_config_checks_it(self):
        import tempfile
        import dock_config
        base = '[server]\nurl="https://x"\nusername="u"\npassword="p"\n'
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(base + '[sync]\nsafe_hours = "7am-10am"\n')
        self.addCleanup(os.remove, f.name)
        with self.assertRaisesRegex(dock_config.ConfigError, "safe hours"):
            dock_config.load(f.name)
        with open(f.name, "w") as g:
            g.write(base)
        self.assertEqual(dock_config.load(f.name)["sync"]["safe_hours"], "07:00-10:00")
        self.assertEqual(sh.from_config(dock_config.load(f.name)), MORNING)   # on by default


if __name__ == "__main__":
    unittest.main()
