"""
dock_paths.py - lets the code find itself. The modules live in folders by
purpose but import each other by plain name (import dock_record). Importing this
puts every folder on the path. A file that's run directly does this first,
since Python only adds the running file's own folder:

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import dock_paths
"""

import os
import sys

CODE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(CODE)          # VERSION, vendor/, pi/
FOLDERS = ("core", "jobs", "players", "music", "screen", "report", "setup")

for folder in FOLDERS:
    path = os.path.join(CODE, folder)
    if path not in sys.path:
        sys.path.insert(0, path)


def script(name):
    """The full path of a module, to run it in its own process."""
    for folder in FOLDERS:
        path = os.path.join(CODE, folder, name)
        if os.path.exists(path):
            return path
    raise FileNotFoundError(name)
