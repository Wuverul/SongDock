"""
dock_setup.py - one-off dock settings from a `DockSetup.txt` on a docked iPod,
and the list of known iPods written into its MediaDock.txt.

    Forget=Jo's iPod (nano)    retire that iPod (the model is optional)
    Forget=all others              retire every iPod except this one
    Keep=Jo's iPod (nano)      bring a forgotten iPod back

(see dock_players.py for what forgetting does). Several lines are fine.
Rules, as for wifi.txt:
  * Only on opted-in players and only in write mode, because success
    deletes the file.
  * All or nothing: if any line can't be done (unknown setting, no such
    iPod, two iPods with that name), nothing changes and the file stays,
    with the reason on screen and in MediaDock.txt.
"""

import logging
import os
import re
from datetime import datetime

import dock_players

log = logging.getLogger("dock_setup")

SETUP_FILE = "DockSetup.txt"
GUIDE_FILE = "MediaDock README.txt"     # the manual (dock_guide.py)
ACTIONS = {"forget": dock_players.forget, "keep": dock_players.keep}


class SetupFileError(Exception):
    pass


def find_file(root, name):
    """Path of `name` in the player's top folder (any case), or None."""
    try:
        for entry in os.listdir(root):
            if entry.lower() == name.lower():
                return os.path.join(root, entry)
    except OSError:
        pass
    return None


def parse_setup_file(text):
    """[(action, value)] from 'Key=Value' / 'Key: Value' lines; # = comment."""
    steps = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*([A-Za-z]+)\s*[:=]\s*(.*?)\s*$", line)
        if not m:
            raise SetupFileError(f"Line {n} isn't Setting=value")
        key = m.group(1).lower()
        if key not in ACTIONS:
            raise SetupFileError(f"Unknown setting {m.group(1)!r} (use Forget= or Keep=)")
        steps.append((key, m.group(2)))
    if not steps:
        raise SetupFileError("Nothing in it to do")
    return steps


def apply_setup_file(root, record, guid):
    """Handle DockSetup.txt on a mounted, writable, opted-in player.

    Returns None if there's none, else {"ok", "done": [messages], "reason"}.
    """
    path = find_file(root, SETUP_FILE)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            steps = parse_setup_file(f.read())
    except SetupFileError as e:
        log.warning("Bad %s: %s (left on the player)", SETUP_FILE, e)
        return {"ok": False, "done": [], "reason": str(e)}

    # Check every line against the record before changing anything.
    players = record.players()
    for action, value in steps:
        found, why = dock_players.match(players, value, guid)
        if not why and action == "forget" and any(p["guid"] == guid for p in found):
            why = "Can't forget the iPod being synced"
        if why:
            log.warning("%s not used: %s (left on the player)", SETUP_FILE, why)
            return {"ok": False, "done": [], "reason": why}
    done = []
    for action, value in steps:
        msgs, _ = ACTIONS[action](record, record.players(), value, guid)
        done += msgs
    for msg in done:
        log.info("%s: %s", SETUP_FILE, msg)
    os.remove(path)
    _fsync_dir(root)
    return {"ok": True, "done": done, "reason": ""}


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)                                # make the deletion stick (FAT)
    finally:
        os.close(fd)


def summary_line(result):
    """One screen line for a DockSetup.txt result."""
    if not result["ok"]:
        return result["reason"]
    changed = [m for m in result["done"] if not m.startswith("Already")]
    if len(changed) == 1:
        return changed[0]
    if not changed:
        return result["done"][0] if result["done"] else "Nothing to change"
    return f"{len(changed)} iPods updated"


# ------------------------------------------------------------ MediaDock.txt --
LAST_RESULT = "Last DockSetup.txt"


def marker_text(players, this_guid, intro, result=None, previous=""):
    """MediaDock.txt: the opt-in note, the iPods the dock knows, and how to
    use DockSetup.txt. `result`: this sync's DockSetup.txt outcome; without
    one, the last outcome is carried over from `previous` (the old text)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [intro.rstrip("\n"), "", f"iPods this dock syncs (as of {now}):"]
    for p in players:
        this = " - this iPod" if p["guid"] == this_guid else ""
        lines.append(f"  * {dock_players.label(p)}, ID ...{p['guid'][-4:]}{this}")
        if p.get("forgotten_at"):
            lines.append(f"      forgotten {dock_players.when(p['forgotten_at'])} "
                         f"(docking it again brings it back)")
        else:
            lines.append(f"      {p['present']} tracks, last synced "
                         f"{dock_players.when(p['last_sync'])}")
    lines += [
        "",
        "Replaced an iPod? Retire the old one with a DockSetup.txt, e.g.",
        "  Forget=Jo's iPod (nano)",
        "How that works, how to move the dock to another Wi-Fi network, and",
        f"more: see \"{GUIDE_FILE}\" next to this file.",
    ]
    if result is not None:
        what = "; ".join(result["done"]) if result["ok"] else \
            f"not used ({result['reason']}); the file was left in place"
        last = f"{LAST_RESULT} ({now}): {what}"
    else:
        last = next((ln for ln in previous.splitlines() if ln.startswith(LAST_RESULT)), "")
    if last:
        lines += ["", last]
    return "\n".join(lines) + "\n"


def write_marker(root, marker, players, this_guid, intro, result=None):
    """Rewrite MediaDock.txt (keeping its existing spelling) with the list."""
    path = find_file(root, marker) or os.path.join(root, marker)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            previous = f.read()
    except OSError:
        previous = ""
    text = marker_text(players, this_guid, intro, result, previous)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
