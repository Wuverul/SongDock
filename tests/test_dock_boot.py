"""Tests for the boot screens. Run: python3 -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dock_boot import (boot_screen, problem, ready, starting_phase,  # noqa: E402
                       wait_for_network)


def reading(ip=None, wifi="off", clock=False):
    return {"ip": ip, "wifi": wifi, "clock": clock}


ONLINE = reading(ip="192.168.1.40", wifi=-58, clock=True)


class FakeClock:
    """Stands in for time.monotonic/time.sleep so tests don't wait."""

    def __init__(self):
        self.t = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class BootScreenTests(unittest.TestCase):
    def test_normal_boot_shows_powered_on(self):
        self.assertEqual(boot_screen("safe"), ["idle", "--boot", "--force"])

    def test_first_boot_with_no_state(self):
        self.assertEqual(boot_screen(None)[0], "idle")

    def test_recovered_after_offline_doesnt_claim_powered_on_now(self):
        self.assertNotIn("--boot", boot_screen("offline", recovered=True))

    def test_interrupted_sync_is_reported_not_hidden(self):
        argv = boot_screen("syncing")
        self.assertEqual(argv[0], "failed")
        self.assertIn("--force", argv)   # must replace the stale SYNCING


class StartingPhaseTests(unittest.TestCase):
    def run_phase(self, sequence, give_up=300, work=0):
        """work = seconds each screen update takes (the Pi is slow at boot)."""
        seq = iter(sequence)
        last = sequence[-1]
        shown = []
        fake = FakeClock()

        def show(r):
            shown.append((fake.t, r))
            fake.t += work

        result = starting_phase(show, lambda: next(seq, last),
                                sleep=fake.sleep, clock=fake.now,
                                give_up=give_up, tick=10)
        return result, shown

    def test_waits_for_clock_not_just_wifi(self):
        steps = [reading(), reading(ip="192.168.1.40", wifi=-60), ONLINE]
        (result, gave_up), shown = self.run_phase(steps)
        self.assertEqual([r for _, r in shown], steps)
        self.assertFalse(gave_up)
        self.assertTrue(result["clock"])

    def test_gives_up_after_five_minutes(self):
        (result, gave_up), shown = self.run_phase([reading()])
        self.assertTrue(gave_up)
        self.assertEqual(shown[-1][0], 300)

    def test_slow_updates_dont_stretch_the_interval(self):
        # Each update takes 4s; they should still start every 10s.
        (_, _), shown = self.run_phase([reading()], give_up=40, work=4)
        self.assertEqual([t for t, _ in shown], [0, 10, 20, 30, 40])

    def test_ready_needs_ip_signal_and_clock(self):
        self.assertFalse(ready(reading(ip="1.2.3.4", wifi="off", clock=True)))
        self.assertFalse(ready(reading(ip=None, wifi=-50, clock=True)))
        self.assertFalse(ready(reading(ip="1.2.3.4", wifi=-50, clock=False)))
        self.assertTrue(ready(reading(ip="1.2.3.4", wifi=-50, clock=True)))
        self.assertTrue(ready(reading(ip="1.2.3.4", wifi=None, clock=True)))  # not a Pi


class OfflineTests(unittest.TestCase):
    def test_problem_names_what_is_missing(self):
        self.assertEqual(problem(reading()), "wifi")
        self.assertEqual(problem(reading(ip="1.2.3.4", wifi=-50)), "clock")

    def test_redraws_only_when_problem_changes(self):
        seq = iter([reading(), reading(), reading(ip="1.2.3.4", wifi=-50),
                    reading(ip="1.2.3.4", wifi=-50), ONLINE])
        drawn = []
        result = wait_for_network(lambda: next(seq),
                                  lambda kind, r: drawn.append(kind),
                                  sleep=lambda s: None)
        self.assertEqual(drawn, ["wifi", "clock"])
        self.assertEqual(result, ONLINE)


if __name__ == "__main__":
    unittest.main()
