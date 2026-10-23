"""
dock_progress.py - progress shared by the prepare job and a sync running at
the same time (concurrent batches), for the PREPARE & SYNC screen.

Two small JSON files, one per writer, so neither overwrites the other:
  prepare-progress.json   the prepare job: this run's id, tracks started /
                          total, batches handed over, and its pid
  batch-sync.json         the sync: how many of that run's tracks have been
                          copied so far, over how many syncs
They live in /run/musicdock (RAM: no SD card wear, gone after a reboot), or
next to the screen state file off the Pi. A run's progress only counts while
its prepare process is alive, so a crashed run never leaves the combined
screen behind.
"""

import json
import os

import dock_record

PREPARE_FILE = "prepare-progress.json"
SYNC_FILE = "batch-sync.json"


def folder():
    if os.path.isdir("/run/musicdock"):
        return "/run/musicdock"
    state = os.environ.get("DOCK_STATE_FILE")
    return os.path.dirname(state) if state else os.path.dirname(dock_record.DEFAULT_PATH)


def _read(name):
    try:
        with open(os.path.join(folder(), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(name, data):
    path = os.path.join(folder(), name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass                         # progress display only: never fail a job over it


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:          # someone else's process (root vs login user)
        return True
    except (OSError, TypeError):
        return False


# ------------------------------------------------------------ prepare side --
def write_prepare(run, done, total, batches):
    _write(PREPARE_FILE, {"run": run, "done": done, "total": total,
                          "batches": batches, "pid": os.getpid()})


def clear_prepare():
    try:
        os.remove(os.path.join(folder(), PREPARE_FILE))
    except OSError:
        pass


def running_prepare():
    """This run's progress while its prepare job is still going, else None."""
    p = _read(PREPARE_FILE)
    return p if p.get("total") and _alive(p.get("pid")) else None


# --------------------------------------------------------------- sync side --
def copied(run):
    """(tracks of `run` copied so far, syncs so far) by earlier batch syncs."""
    s = _read(SYNC_FILE)
    return (s.get("synced", 0), s.get("syncs", 0)) if s.get("run") == run else (0, 0)


def write_copied(run, synced, syncs):
    _write(SYNC_FILE, {"run": run, "synced": synced, "syncs": syncs})


# ------------------------------------------------------------------ screen --
def screen_args(prep, synced, syncs):
    """dock_status.py arguments for PREPARE & SYNC."""
    return ["prepsync", "--done", str(prep["done"]), "--total", str(prep["total"]),
            "--synced", str(min(synced, prep["total"])), "--batches", str(max(syncs, 1))]
