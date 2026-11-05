"""Keep the tests away from a live dock's shared state.

Imported by every test module that runs prepare or sync code. On the Pi, the
progress files of a real prepare run live in /run/musicdock, and the tests used
to read and rewrite them there (2026-11-04: a test saw the dock's real run and
left a wrong "synced" count). DOCK_STATE_FILE, set here to a temporary folder,
moves them (dock_progress.folder) for the whole test run.
"""

import atexit
import os
import shutil
import tempfile

if not os.environ.get("DOCK_STATE_FILE"):
    _folder = tempfile.mkdtemp(prefix="songdock-tests-")
    atexit.register(shutil.rmtree, _folder, ignore_errors=True)
    os.environ["DOCK_STATE_FILE"] = os.path.join(_folder, "dock_status.json")
