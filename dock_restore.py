"""
dock_restore.py - one count for filling a new or wiped iPod ("Restoring").

A big first sync goes in rounds: the prepare job fills the cache, a sync
copies it, the next run frees what's on the iPod and fetches more. Each
round counted only itself ("Converting 1/37,569" after "2,431/40,000"),
so a multi-day restore looked like it kept starting over. While a player
lacks at least RESTORE_MIN songs of its plan, the screens count songs on
the player against the whole plan instead, with the time left at the pace
so far (user request, 2026-10-26).

The start of a restore is kept in restore.json next to the record (it
survives restarts; the pace is measured from it) and dropped once the
player has everything. Not yet tried on hardware: a real wiped or new
iPod is needed.
"""

import json
import os
import time

import dock_record

RESTORE_MIN = 500          # songs missing for a sync to count as a restore
MIN_FOR_PACE = 25          # songs restored before a time left is shown


def state_path(record_path=None):
    return os.path.join(os.path.dirname(record_path or dock_record.DEFAULT_PATH),
                        "restore.json")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(path, state):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass


def targets(manifest):
    """Songs the player should end up with (as ipod_handler.targeted_ids)."""
    return ({t["id"] for t in manifest.get("tracks", [])}
            | {t["id"] for t in manifest.get("on_player", [])}
            | {s["id"] for s in manifest.get("skipped", [])})


def status(guid, present, manifest, now=None, path=None):
    """{"have", "total", "left" (seconds or None)} while `guid` is being
    restored, else None. `present`: Navidrome IDs the record has on it.
    Starts a restore when RESTORE_MIN or more are missing; ends it once
    nothing is."""
    if not guid or "tracks" not in (manifest or {}):
        return None
    now = time.time() if now is None else now
    path = path or state_path()
    wanted = targets(manifest)
    have, total = len(wanted & set(present)), len(wanted)
    state = _load(path)
    entry = state.get(guid)
    if have >= total:
        if entry:
            del state[guid]
            _save(path, state)
        return None
    if entry is None:
        if total - have < RESTORE_MIN:
            return None
        entry = state[guid] = {"started": now, "have_at_start": have}
        _save(path, state)
    gained, spent = have - entry["have_at_start"], now - entry["started"]
    left = (total - have) * spent / gained if gained >= MIN_FOR_PACE and spent > 0 else None
    return {"have": have, "total": total, "left": left}


def for_last_player(manifest, now=None, path=None):
    """status() for the most recently synced player (what the prepare job's
    screens are about), or None."""
    try:
        record = dock_record.Record()
        try:
            player = record.last_synced_player()
            if not player:
                return None
            present = {r["navidrome_id"] for r in record.tracks(player["guid"])}
        finally:
            record.close()
    except Exception:
        return None
    return status(player["guid"], present, manifest, now, path)


def left_text(seconds):
    """'About 2 days left' etc. ('' when there's no pace yet)."""
    if seconds is None:
        return ""
    minutes = seconds / 60
    if minutes < 90:
        return f"About {max(1, round(minutes))} min left"
    hours = minutes / 60
    if hours < 36:
        return f"About {round(hours)} h left"
    return f"About {round(hours / 24)} days left"


def screen_args(info, detail):
    """dock_status arguments for the RESTORING screen."""
    args = ["restoring", "--done", str(info["have"]), "--total", str(info["total"]),
            "--detail", detail]
    if info.get("left") is not None:
        args += ["--left", str(int(info["left"]))]
    return args
