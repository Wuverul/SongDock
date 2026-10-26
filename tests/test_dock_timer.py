"""Tests for applying check_interval_minutes to the prepare timer.

Run: python3 -m unittest discover -s tests -v
"""

import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_timer  # noqa: E402

logging.disable(logging.CRITICAL)


class TimerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = os.path.join(tmp.name, "config.toml")
        self.dropins = os.path.join(tmp.name, "musicdock-prepare.timer.d")
        self.commands = []

    def apply(self, text=None):
        if text is not None:
            with open(self.config, "w") as f:
                f.write(text)
        return dock_timer.apply(self.config, self.dropins,
                                run=lambda cmd, check: self.commands.append(cmd))

    def dropin(self):
        with open(os.path.join(self.dropins, dock_timer.DROPIN)) as f:
            return f.read()

    def test_writes_the_interval_and_restarts_the_timer_once(self):
        self.assertEqual(self.apply("[sync]\ncheck_interval_minutes = 15\n"), (15, True))
        # The empty line clears every trigger, so the after-boot one comes back.
        self.assertIn("OnUnitInactiveSec=\nOnBootSec=1min\nOnUnitInactiveSec=15min\n",
                      self.dropin())
        self.assertEqual(self.commands, [["systemctl", "daemon-reload"],
                                         ["systemctl", "restart", "musicdock-prepare.timer"]])
        self.assertEqual(self.apply(), (15, False))                # unchanged: no reload
        self.assertEqual(len(self.commands), 2)

    def test_an_older_dropin_without_the_boot_trigger_is_replaced(self):
        os.makedirs(self.dropins)
        with open(os.path.join(self.dropins, dock_timer.DROPIN), "w") as f:
            f.write("[Timer]\nOnUnitInactiveSec=\nOnUnitInactiveSec=5min\n")
        self.assertEqual(self.apply(), (5, True))
        self.assertIn("OnBootSec=1min", self.dropin())

    def test_missing_or_bad_values_mean_five_minutes(self):
        self.assertEqual(self.apply()[0], 5)                       # no config file at all
        for text in ("[sync]\n", "[sync]\ncheck_interval_minutes = 0\n",
                     "[sync]\ncheck_interval_minutes = true\n", "not = [valid toml\n"):
            self.assertEqual(self.apply(text)[0], 5, text)
        self.assertIn("OnUnitInactiveSec=5min", self.dropin())


if __name__ == "__main__":
    unittest.main()
