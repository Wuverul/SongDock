#!/usr/bin/env python3
"""Tests for dock_log.py: warnings survive LogLevelMax=warning."""

import glob
import logging
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401

import dock_log  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


class JournalFormatterTests(unittest.TestCase):
    def line(self, level, msg):
        record = logging.LogRecord("t", level, __file__, 1, msg, None, None)
        return dock_log.JournalFormatter("%(levelname)s %(message)s").format(record)

    def test_each_level_gets_its_priority(self):
        self.assertEqual(self.line(logging.INFO, "checked"), "<6>INFO checked")
        self.assertEqual(self.line(logging.WARNING, "Updated"), "<4>WARNING Updated")
        self.assertEqual(self.line(logging.ERROR, "refused"), "<3>ERROR refused")

    def test_every_line_of_a_traceback_keeps_it(self):
        self.assertEqual(self.line(logging.ERROR, "a\nb"), "<3>ERROR a\n<3>b")

    def test_units_keeping_only_warnings_tag_their_lines(self):
        # 2026-11-06: such units logged nothing, errors included
        for unit in glob.glob(os.path.join(ROOT, "pi", "*.service")):
            with open(unit) as f:
                text = f.read()
            if "LogLevelMax=" not in text:
                continue
            script = re.search(r"ExecStart=\S+ \S+/songdock/(\S+\.py)", text).group(1)
            with open(os.path.join(ROOT, "songdock", script)) as f:
                self.assertIn("dock_log.setup()", f.read(), unit)


if __name__ == "__main__":
    unittest.main()
