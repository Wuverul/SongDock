"""
dock_progress.py - progress shared by the prepare job and a sync running at
the same time (concurrent batches), for the PREPARE & SYNC screen.

Two small JSON files, one per writer, so neither overwrites the other:
  prepare-progress.json   the prepare job: this run's id, tracks started /
                          total, tracks that must reach the player (also
                          songs reused from the cache), batches handed
                          over, its pid, and whether it's final (conversion
                          over, its last sync asked for)
  batch-sync.json         the sync: how many of that run's tracks have been
                          copied so far, over how many syncs
They live in /run/musicdock (RAM: no SD card wear, gone after a reboot), or
next to the screen state file off the Pi. A run's progress counts while its
prepare process is alive, or - once conversion is over - while its final
sync is pending (FINAL_FOR seconds at most), so a crashed or abandoned run
never leaves the combined screen behind.
"""

import json
import os
import time

import dock_record

PREPARE_FILE = "prepare-progress.json"
FINAL_FOR = 600          # seconds a finished run waits for its last sync
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
def write_prepare(run, done, total, batches, to_copy=None):
    """total: tracks this run converts or downloads; to_copy: tracks that
    must reach the player (also re-added songs reused from the cache)."""
    _write(PREPARE_FILE, {"run": run, "done": done, "total": total,
                          "to_copy": to_copy or total, "batches": batches,
                          "pid": os.getpid(), "at": time.time()})


def mark_final():
    """Conversion is over and the run's last sync has been asked for: keep
    the run (and PREPARE & SYNC) until that sync ends. Returns False if
    there's no run to mark."""
    p = _read(PREPARE_FILE)
    if not p.get("total"):
        return False
    p.update(done=p["total"], final=True, at=time.time())
    _write(PREPARE_FILE, p)
    return True


def clear_prepare():
    try:
        os.remove(os.path.join(folder(), PREPARE_FILE))
    except OSError:
        pass


def running_prepare():
    """The run in progress - its prepare job still going, or its final sync
    pending (p["final"]) - else None."""
    p = _read(PREPARE_FILE)
    if not p.get("total"):
        return None
    if p.get("final"):
        return p if time.time() - p.get("at", 0) < FINAL_FOR else None
    return p if _alive(p.get("pid")) else None


def clear_run():
    """The run is complete: forget its progress."""
    for name in (PREPARE_FILE, SYNC_FILE):
        try:
            os.remove(os.path.join(folder(), name))
        except OSError:
            pass


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
    to_copy = max(prep.get("to_copy") or prep["total"], prep["total"])
    return ["prepsync", "--done", str(prep["done"]), "--total", str(prep["total"]),
            "--synced", str(min(synced, to_copy)), "--to-copy", str(to_copy),
            "--batches", str(max(syncs, 1))]
