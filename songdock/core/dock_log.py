"""Logging for units that keep only warnings (LogLevelMax=warning).

systemd files every line a service writes at "info", whatever it says, so such a unit
dropped its own warnings and errors too: the updater, the USB port check and the Wi-Fi
switch-back logged nothing at all (found 2026-11-06, after a real self-update left no
trace). A "<N>" at the start of a line gives it its syslog priority.
"""

import logging

PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3,
            logging.CRITICAL: 2}


class JournalFormatter(logging.Formatter):
    def format(self, record):
        prio = PRIORITY.get(record.levelno, 6)
        return "\n".join(f"<{prio}>{line}" for line in super().format(record).split("\n"))


def setup(level=logging.INFO):
    """Like basicConfig(level, "%(levelname)s %(message)s"), each line tagged with its
    priority, so LogLevelMax=warning keeps warnings and errors."""
    handler = logging.StreamHandler()
    handler.setFormatter(JournalFormatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])
