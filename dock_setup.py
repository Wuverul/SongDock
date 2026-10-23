"""
dock_setup.py - one-off dock settings from a `DockSetup.txt` on a docked iPod,
and the list of known iPods written into its MediaDock.txt.

    Forget=Jo's iPod (nano)      retire that iPod (the model is optional)
    Forget=all others            retire every iPod except this one
    Keep=Jo's iPod (nano)        bring a forgotten iPod back
    PlaylistTag=#car             sync playlists tagged #car instead ("none" = off)
    SyncStarred=no               don't sync starred songs (also SyncFavorites=)
    Server=https://music.example.com      a new Navidrome address; needs
    Username=jo                           Username= and Password= with it
    Password=...                          (each also works alone, but Server
    NewLibrary=yes                        never does); NewLibrary=yes when
                                          the new server has other music

(see dock_players.py for what forgetting does). Several lines are fine.
Rules, as for wifi.txt:
  * Only on opted-in players and only in write mode, because success
    deletes the file (and with it any password in it).
  * All or nothing: if any line can't be done (unknown setting, no such
    iPod, a sign-in the server refuses, ...), nothing changes and the file
    stays, with the reason on screen and in MediaDock.txt.
  * Settings go into config.toml (dock_config.update: comments kept, mode
    600), read by the next prepare run. A new sign-in is tried first, and
    Server= never reuses the current username/password, so the dock's
    sign-in is never sent to an address someone else typed. If the new
    server doesn't have this iPod's songs (another library: every song
    would be replaced), it also needs NewLibrary=yes, and no plays may be
    waiting for the old server.
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
SETTINGS = {"playlisttag": "PlaylistTag", "syncstarred": "SyncStarred",
            "syncfavorites": "SyncStarred", "server": "Server", "username": "Username",
            "password": "Password", "newlibrary": "NewLibrary"}
YES, NO = {"yes", "true", "on", "1"}, {"no", "false", "off", "0"}
SAMPLE_SONGS = 5                        # songs checked to tell if a server has this library


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
    """([(action, value)], {setting: value}) from 'Key=Value' / 'Key: Value'
    lines; # starts a comment line. Settings are keyed by their canonical
    name (SyncFavorites = SyncStarred) and may appear once."""
    steps, settings = [], {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*([A-Za-z]+)\s*[:=]\s*(.*?)\s*$", line)
        if not m:
            raise SetupFileError(f"Line {n} isn't Setting=value")
        key = m.group(1).lower()
        if key in ACTIONS:
            steps.append((key, m.group(2)))
        elif key in SETTINGS:
            name = SETTINGS[key]
            if name in settings:
                raise SetupFileError(f"{name} is given twice")
            settings[name] = m.group(2)
        else:
            raise SetupFileError(f"Unknown setting {m.group(1)!r}")
    if not steps and not settings:
        raise SetupFileError("Nothing in it to do")
    return steps, settings


def yes_no(name, value):
    v = value.strip().lower()
    if v in YES | NO:
        return v in YES
    raise SetupFileError(f"{name} must be yes or no")


def plan_settings(settings, cfg):
    """Check the settings lines against the current config. Returns
    ({(section, key): value} for config.toml, [messages], server sign-in to
    try or None, new_library). Raises SetupFileError."""
    changes, done = {}, []
    sync = cfg["sync"]
    tag, targets = sync.get("playlist_tag", ""), list(sync.get("targets", []))
    if "PlaylistTag" in settings:
        value = settings["PlaylistTag"]
        if not value:
            raise SetupFileError('PlaylistTag needs a tag, e.g. #ipod (or "none")')
        tag = "" if value.lower() in ("none", "off") else value
        if any(c.isspace() for c in tag):
            raise SetupFileError("PlaylistTag must be one word, e.g. #car")
        changes[("sync", "playlist_tag")] = tag
        done.append(f"Playlists tagged {tag} sync" if tag else "Tagged playlists don't sync")
    if "SyncStarred" in settings:
        on = yes_no("SyncStarred", settings["SyncStarred"])
        targets = ([t for t in targets if t != "starred"] if not on else
                   targets if "starred" in targets else ["starred"] + targets)
        changes[("sync", "targets")] = targets
        done.append("Starred songs sync" if on else "Starred songs don't sync")
    if not targets and not tag:
        raise SetupFileError("Nothing would sync (no starred songs, no playlist tag)")

    server = cfg["server"]
    login = None
    given = {k: settings[k] for k in ("Server", "Username", "Password") if k in settings}
    if given:
        if "Server" in given and len(given) < 3:
            raise SetupFileError("Server= needs Username= and Password= with it")
        if any(not v for v in given.values()):
            raise SetupFileError(f"{next(k for k, v in given.items() if not v)}= is empty")
        url = given.get("Server", server["url"]).rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", url):
            raise SetupFileError("Server must start with http:// or https://")
        login = {"url": url, "username": given.get("Username", server["username"]),
                 "password": given.get("Password", server["password"]),
                 "moved": url != server["url"].rstrip("/")}
        for key, value in (("url", url), ("username", login["username"]),
                           ("password", login["password"])):
            if value != server.get(key):
                changes[("server", key)] = value
        if login["moved"]:
            done.append(f"Server is now {url}")
        if "Username" in given:
            done.append(f"Signed in as {login['username']}")
        elif "Password" in given:
            done.append("Password changed")
    new_library = yes_no("NewLibrary", settings["NewLibrary"]) \
        if "NewLibrary" in settings else False
    return changes, done, login, new_library


def check_login(login, record, guid, new_library, connect):
    """None if the dock may switch to `login`, else why not. Tries the
    sign-in; for a new address, also whether it has this iPod's songs."""
    from subsonic import SubsonicError
    client = connect(login["url"], login["username"], login["password"], timeout=20)
    try:
        client.call("ping")
    except SubsonicError as e:
        if e.network:
            return f"Can't reach {login['url']}"
        if e.code in (40, 41):
            return "The server refused that username and password"
        return f"Server error: {e}"[:60]
    if not login["moved"]:
        return None
    ids = [r["navidrome_id"] for r in record.tracks(guid)]
    sample = ids[::max(1, len(ids) // SAMPLE_SONGS)][:SAMPLE_SONGS]
    for nid in sample:
        try:
            client.call("getSong", id=nid)
            return None                                 # same library
        except SubsonicError as e:
            if e.network:
                return f"Can't reach {login['url']}"
    if not sample:
        return None
    if not new_library:
        return "That server has other music: add NewLibrary=yes"
    waiting = record.unsent_count()[0]
    if waiting:
        return f"{waiting} plays not yet sent to the old server"
    return None


def apply_setup_file(root, record, guid, cfg=None, config_path=None, connect=None):
    """Handle DockSetup.txt on a mounted, writable, opted-in player.
    `cfg`: the dock's loaded config (needed for settings lines).

    Returns None if there's none, else {"ok", "done": [messages], "reason"}.
    """
    path = find_file(root, SETUP_FILE)
    if not path:
        return None

    def refuse(why):
        log.warning("%s not used: %s (left on the player)", SETUP_FILE, why)
        return {"ok": False, "done": [], "reason": why}

    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            steps, settings = parse_setup_file(f.read())
        if settings and cfg is None:
            raise SetupFileError("Settings can't be changed here")
        changes, done, login, new_library = (plan_settings(settings, cfg) if settings
                                             else ({}, [], None, False))
    except SetupFileError as e:
        return refuse(str(e))

    # Check every line before changing anything.
    players = record.players()
    for action, value in steps:
        found, why = dock_players.match(players, value, guid)
        if not why and action == "forget" and any(p["guid"] == guid for p in found):
            why = "Can't forget the iPod being synced"
        if why:
            return refuse(why)
    if login:
        if connect is None:
            from subsonic import Client as connect
        why = check_login(login, record, guid, new_library, connect)
        if why:
            return refuse(why)
    if changes:
        import dock_config
        try:
            dock_config.update(changes, config_path)
        except (dock_config.ConfigError, OSError) as e:
            log.error("Couldn't change config.toml: %s", e)
            return refuse("Couldn't save the settings (see the log)")
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
    if all(m.startswith(("Forgot", "Brought back")) for m in changed):
        return f"{len(changed)} iPods updated"
    return f"{len(changed)} changes made"


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
