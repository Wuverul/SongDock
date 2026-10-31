"""
rockbox_handler.py - sync a player running Rockbox (detected by its .rockbox
folder), alongside ipod_handler for stock iPods. Same staged plan, same
cache (MP3 V0 etc.), same record.

The same session serves generic music-folder players (kind="generic": any
opted-in USB drive that's neither an iPod nor Rockbox, e.g. a SnowSky Echo
Mini or a cheap MP3 player): the same MediaDock/ folder and cover.jpg, but
no plays (they keep no play log: "No play count - unsupported" on screen)
and no playlist files (many can't read them: songs from tagged playlists
are simply in the library). Cheap players often report the same USB serial
on every unit, so a generic player is known by an ID the dock writes onto
it (PLAYER_ID) instead.

On the player:
  MediaDock/<album artist>/<album>/<NN title>.<ext>
                      the dock's music, and only the dock's: nothing outside
                      MediaDock/ is ever changed. Each file carries the plan's
                      names and genre plus its Navidrome ID (tag
                      MUSICDOCK_NAVIDROME_ID), so a lost record can be rebuilt
                      from the player, as with stock iPods' comment field.
  MediaDock/.../cover.jpg
                      album art as Rockbox wants it: baseline JPEG, <= 600 px.
  Playlists/<name>.m3u8
                      the dock's playlists (Favorites, tagged, configured);
                      only ones it made are changed or deleted.
  .scrobbler.log      Rockbox's play log: one line per play with its own
                      time; "S" (skipped) lines are ignored. Its plays are
                      queued on the Pi (committed) before the log is deleted,
                      and the log is only deleted in write mode.

The record's track key (dbid) is the file's path under the player's root.
Untested on a real Rockbox player (2026-10-23): built against fake players.
"""

import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone

log = logging.getLogger("rockbox_handler")

ROCKBOX_DIR = ".rockbox"
MUSIC_DIR = "MediaDock"
PLAYLIST_DIR = "Playlists"
SCROBBLER_LOG = ".scrobbler.log"
COVER_FILE = "cover.jpg"
COVER_MAX = 600
TMP_PREFIX = ".musicdock-tmp-"           # a copy in progress (removed if left over)
ID_TAG = "MUSICDOCK_NAVIDROME_ID"
AUDIO = {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".oga", ".opus"}
FREE_SPACE_MARGIN = 200 * 1024 * 1024
SAME_LENGTH = 3                          # seconds: a logged play matches a track this close
RECORD_COPY = os.path.join(".musicdock", "dock.db")
PLAYER_ID = os.path.join(MUSIC_DIR, ".musicdock", "player-id")


def player_id(mount, create=False):
    """A generic player's ID, from the file the dock wrote on it (made on the
    first write sync when `create`). None if it has none yet."""
    path = os.path.join(mount, PLAYER_ID)
    try:
        with open(path) as f:
            value = f.read().strip().upper()
        if re.fullmatch(r"[0-9A-Z]{8,32}", value):
            return value
    except OSError:
        pass
    if not create:
        return None
    import secrets
    value = "SD" + secrets.token_hex(7).upper()        # 16 characters, like a GUID
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value + "\n")
        f.flush()
        os.fsync(f.fileno())
    return value


def is_rockbox(root):
    return os.path.isdir(os.path.join(root, ROCKBOX_DIR))


def log_info(message):
    log.info(message)


# ================================================================== plays ====
def parse_scrobbler_log(text):
    """[{artist, album, title, track, length, listened, time}] from an
    Audioscrobbler 1.1 log. With "#TZ/UNKNOWN" the times are the player's
    local clock written as if UTC, so they're read as local time."""
    local = True
    entries = []
    for line in text.splitlines():
        if line.startswith("#"):
            if line.upper().startswith("#TZ/"):
                local = line[4:].strip().upper() == "UNKNOWN"
            continue
        parts = line.split("\t")
        if len(parts) < 7 or not parts[6].strip().isdigit():
            continue
        stamp = int(parts[6])
        if local:      # the clock reading, taken as local time
            naive = datetime.fromtimestamp(stamp, timezone.utc).replace(tzinfo=None)
            stamp = int(naive.timestamp())
        entries.append({"artist": parts[0], "album": parts[1], "title": parts[2],
                        "track": parts[3], "length": int(parts[4]) if parts[4].isdigit()
                        else None, "listened": parts[5].strip().upper() == "L",
                        "time": stamp})
    return entries


def _fold(text):
    return (text or "").strip().casefold()


def plays_from_log(entries, record, guid):
    """Play dicts for Record.queue_plays. A play is matched to a track the
    dock put on the player by artist + title (+ album, + length when that
    isn't unique); unmatched plays are queued without a song, like a stock
    iPod's hand-added tracks."""
    by_name = {}
    for r in record.tracks(guid):
        by_name.setdefault((_fold(r["artist"]), _fold(r["title"])), []).append(r)
    plays, seen = [], {}
    for e in entries:
        if not e["listened"]:
            continue
        found = by_name.get((_fold(e["artist"]), _fold(e["title"])), [])
        if len(found) > 1:
            found = [r for r in found if _fold(r["album"]) == _fold(e["album"])] or found
        if len(found) > 1 and e["length"]:
            found = [r for r in found if r["duration"] is None
                     or abs(r["duration"] - e["length"]) <= SAME_LENGTH] or found
        match = found[0] if len(found) == 1 else None
        dbid = match["dbid"] if match else f"log:{e['artist']}\t{e['title']}"[:300]
        seq = seen.get((dbid, e["time"]), 0)        # the same song logged twice in a second
        seen[(dbid, e["time"])] = seq + 1
        plays.append({"dbid": dbid, "seq": seq, "played_at": e["time"], "approx": False,
                      "navidrome_id": match["navidrome_id"] if match else None,
                      "artist": e["artist"], "album": e["album"], "title": e["title"]})
    return plays


def read_plays(mount, work_dir, record, guid):
    """Snapshot the play log to the Pi and queue its plays (committed before
    this returns). Returns (new plays, log path or None)."""
    path = os.path.join(mount, SCROBBLER_LOG)
    if not os.path.isfile(path):
        return 0, None
    os.makedirs(work_dir, exist_ok=True)
    copy = os.path.join(work_dir, SCROBBLER_LOG.lstrip("."))
    shutil.copy2(path, copy)
    with open(copy, encoding="utf-8", errors="replace") as f:
        entries = parse_scrobbler_log(f.read())
    return record.queue_plays(guid, plays_from_log(entries, record, guid)), path


# ================================================================== files ====
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text, limit=80):
    """A FAT-safe file or folder name."""
    name = _UNSAFE.sub("_", (text or "").strip())[:limit].rstrip(" .")
    return name.lstrip(".") or "_"


def track_path(t, ext):
    """Where a planned track goes, relative to the player's root."""
    artist = safe_name(t.get("album_artist") or t.get("artist") or "Unknown Artist")
    album = safe_name(t.get("album") or "Unknown Album")
    number = ""
    if t.get("track"):
        number = (f"{int(t['disc'])}-" if t.get("disc") and int(t["disc"]) > 1 else "") \
            + f"{int(t['track']):02d} "
    return os.path.join(MUSIC_DIR, artist, album,
                        safe_name(number + (t.get("title") or t["id"]), 120) + ext)


def _easy(path):
    """mutagen's simple tag interface for a file (MP3/MP4/FLAC/Ogg)."""
    import mutagen
    from mutagen.easyid3 import EasyID3
    from mutagen.easymp4 import EasyMP4Tags
    if ID_TAG.lower() not in EasyID3.valid_keys:
        EasyID3.RegisterTXXXKey(ID_TAG.lower(), ID_TAG)
        EasyMP4Tags.RegisterFreeformKey(ID_TAG.lower(), ID_TAG)
    f = mutagen.File(path, easy=True)
    if f is not None and f.tags is None:
        f.add_tags()
    return f


TAGGED = ("title", "artist", "album", "genre")


def write_tags(path, t):
    """Give the file the plan's names, genre and Navidrome ID."""
    f = _easy(path)
    if f is None:
        raise ValueError(f"not an audio file: {path}")
    for key in TAGGED:
        if t.get(key):
            f[key] = str(t[key])
    f[ID_TAG.lower()] = t["id"]
    f.save()


def read_navidrome_id(path):
    try:
        f = _easy(path)
        value = f.get(ID_TAG.lower()) if f is not None else None
        return value[0] if value else None
    except Exception:
        return None


def write_cover(audio_path, folder):
    """cover.jpg for an album folder from a track's embedded cover (baseline
    JPEG, <= COVER_MAX px), unless the folder has one. Returns True if made."""
    dest = os.path.join(folder, COVER_FILE)
    if os.path.exists(dest):
        return False
    import io
    import mutagen
    data = None
    f = mutagen.File(audio_path)
    if f is not None and f.tags is not None:
        if hasattr(f.tags, "getall"):                        # ID3
            pics = f.tags.getall("APIC")
            data = pics[0].data if pics else None
        elif "covr" in f.tags:                               # MP4
            data = bytes(f.tags["covr"][0])
    if data is None and getattr(f, "pictures", None):        # FLAC
        data = f.pictures[0].data
    if not data:
        return False
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if (img.format != "JPEG" or img.info.get("progressive") or img.info.get("progression")
            or max(img.size) > COVER_MAX):
        img = img.convert("RGB")
        img.thumbnail((COVER_MAX, COVER_MAX))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, progressive=False)
        data = buf.getvalue()
    tmp = os.path.join(folder, TMP_PREFIX + COVER_FILE)
    with open(tmp, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, dest)
    return True


def _remove_empty_dirs(mount, rel_dir):
    """Delete an album/artist folder the dock emptied (and its cover.jpg)."""
    folder = os.path.join(mount, rel_dir)
    top = os.path.join(mount, MUSIC_DIR)
    while os.path.isdir(folder) and os.path.abspath(folder) != os.path.abspath(top):
        left = [n for n in os.listdir(folder) if n != COVER_FILE]
        if left:
            return
        shutil.rmtree(folder)
        folder = os.path.dirname(folder)


def _music_files(mount):
    """Relative paths of audio files (and leftover temp files) in MediaDock/."""
    found, leftovers = [], []
    for root, _, names in os.walk(os.path.join(mount, MUSIC_DIR)):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), mount)
            if name.startswith(TMP_PREFIX):
                leftovers.append(rel)
            elif os.path.splitext(name)[1].lower() in AUDIO:
                found.append(rel)
    return found, leftovers


# ============================================================== reconcile ====
def reconcile(record, guid, mount):
    """Compare MediaDock/ with the record before changing anything: tracks
    whose file is gone are marked removed (all at once = reset); files with
    a Navidrome ID tag the record lacks are adopted (e.g. a lost record);
    other files are left alone; half-copied temp files are deleted."""
    known = {r["dbid"]: r for r in record.tracks(guid)}
    missing = [r for d, r in known.items() if not os.path.isfile(os.path.join(mount, d))]
    reset = bool(known) and len(missing) == len(known)
    for r in missing:
        record.mark_removed(guid, r["dbid"], "player appears to have been reset"
                            if reset else "missing from player")
    files, leftovers = _music_files(mount)
    for rel in leftovers:
        os.remove(os.path.join(mount, rel))
    adopted = foreign = 0
    for rel in files:
        if rel in known:
            continue
        nid = read_navidrome_id(os.path.join(mount, rel))
        if nid:
            f = _easy(os.path.join(mount, rel))
            first = lambda k: (f.get(k) or [None])[0]               # noqa: E731
            record.add_track(guid, rel, ipod_path=rel, navidrome_id=nid,
                             artist=first("artist"), album=first("album"),
                             title=first("title"), genre=first("genre"))
            adopted += 1
        else:
            foreign += 1
    return {"missing": len(missing), "reset": reset, "adopted": adopted,
            "foreign": foreign, "orphans": len(leftovers), "dirty": False}


# ================================================================== apply ====
def apply_plan(record, guid, mount, manifest, cache_dir, progress=None, playlists=True):
    """Make MediaDock/ and the dock's playlists match the staged plan:
    removals (only songs that left every target), re-tags for changed names
    or genres, then additions, each recorded once its file is complete."""
    import ipod_handler
    if "tracks" not in manifest:
        raise ValueError("No staged plan: refusing to apply it (it would remove every track)")
    wanted = {t["id"]: t for t in manifest["tracks"]}
    planned = {t["id"]: t for t in manifest.get("on_player", [])}
    planned.update(wanted)
    present = {}
    for r in record.tracks(guid):
        present.setdefault(r["navidrome_id"], r)
    changed = {nid for nid, r in present.items()
               if nid in wanted and r.get("encoding") and wanted[nid].get("encoding")
               and r["encoding"] != wanted[nid]["encoding"]}
    keep = ipod_handler.targeted_ids(manifest)

    # A swap deletes the old file first: only start one whose new file is here
    # and fits. The rest keep the format they have.
    def on_disk(r):
        return ipod_handler.file_size(os.path.join(mount, r["dbid"]))

    def cached(t):
        return os.path.join(cache_dir, t["file"])
    changed = {nid for nid in changed if os.path.exists(cached(wanted[nid]))}

    # Songs taken off earlier to make room, and new songs pushing out the
    # oldest allowed to go (dock_room; see ipod_handler.apply_plan).
    import dock_room
    now = time.time()
    room_rows = record.room_removed(guid)
    record.clear_room_removed(guid, [n for n in room_rows if n in present or n not in keep])
    rules = dock_room.rules(manifest, cache_dir, record, now)
    returning = {n for n in room_rows
                 if n in wanted and n not in present and not (rules and rules.recent(n))}
    new_songs = [t for nid, t in wanted.items() if nid not in present and nid not in returning]
    new_bytes = sum(ipod_handler.file_size(cached(t)) for t in new_songs)
    freed = sum(on_disk(r) for nid, r in present.items() if nid not in keep)
    free = shutil.disk_usage(mount).free
    victims = []
    if rules:
        victims = dock_room.pick(
            [ipod_handler.file_size(cached(t)) for t in new_songs],
            free - FREE_SPACE_MARGIN + freed,
            [(rules.date(nid, r.get("added_at")), nid, on_disk(r)) for nid, r in present.items()
             if nid in keep and nid not in changed and not rules.recent(nid, r.get("added_at"))])
    freed += sum(on_disk(present[nid]) for nid in victims)
    no_room = ipod_handler.swaps_without_room(
        free,
        {nid: (ipod_handler.file_size(cached(wanted[nid])), on_disk(present[nid]))
         for nid in changed},
        new_bytes, freed)
    changed -= no_room
    record.set_swaps_no_room(guid, len(no_room))
    if no_room:
        log_info(f"Player is full: {len(no_room)} song(s) keep their current format")

    removed = 0
    for nid, r in present.items():
        if nid in keep and nid not in changed and nid not in victims:
            continue
        if nid in victims:
            record.save_room_removed(guid, nid)
        path = os.path.join(mount, r["dbid"])
        if os.path.exists(path):
            os.remove(path)
        reason = ("re-encoded" if nid in changed else "made room for new songs"
                  if nid in victims else "no longer in any synced playlist")
        record.mark_removed(guid, r["dbid"], reason)
        log_info(f"Removed {r['artist']} - {r['title']} ({reason})")
        _remove_empty_dirs(mount, os.path.dirname(r["dbid"]))
        removed += 1

    renamed = 0
    for nid, r in present.items():
        t = planned.get(nid)
        if not t or nid in changed or nid not in keep or nid in victims:
            continue
        if any((t.get(k) or None) != (r.get(k) or None) for k in TAGGED if t.get(k)):
            write_tags(os.path.join(mount, r["dbid"]), t)
            from dock_record import TRACK_FIELDS
            fields = {k: r.get(k) for k in TRACK_FIELDS}
            fields.update({k: t.get(k) or r.get(k) for k in TAGGED})
            record.add_track(guid, r["dbid"], **fields)
            renamed += 1

    additions = new_songs + [wanted[nid] for nid in changed]
    additions += [wanted[nid] for nid in sorted(returning)]
    added = covers = not_added = copy_bytes = 0
    copy_began = time.monotonic()
    for i, t in enumerate(additions):
        src = os.path.join(cache_dir, t["file"])
        if not os.path.exists(src):
            log_info(f"Skipping {t.get('title')}: not in the cache right now")
            continue
        if shutil.disk_usage(mount).free - os.path.getsize(src) < FREE_SPACE_MARGIN:
            not_added = sum(a["id"] not in returning for a in additions[i:])
            log_info(f"Player is full: {not_added} track(s) not added")
            break
        if progress:
            progress(added, len(additions), t)
        rel = track_path(t, os.path.splitext(t["file"])[1].lower())
        stem, ext = os.path.splitext(rel)
        n = 2
        while os.path.exists(os.path.join(mount, rel)):      # another song's name
            rel = f"{stem} ({n}){ext}"
            n += 1
        folder = os.path.join(mount, os.path.dirname(rel))
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, TMP_PREFIX + os.path.basename(rel))
        shutil.copyfile(src, tmp)
        copy_bytes += os.path.getsize(tmp)
        write_tags(tmp, t)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(mount, rel))
        record.add_track(guid, rel, ipod_path=rel, navidrome_id=t["id"],
                         artist=t.get("artist"), album=t.get("album"), title=t.get("title"),
                         genre=t.get("genre"), track_no=t.get("track"),
                         duration=t.get("duration"), encoding=t.get("encoding"))
        if t["id"] in changed and present[t["id"]].get("added_at"):
            record.set_added_at(guid, rel, present[t["id"]]["added_at"])
        added += 1
        try:
            covers += write_cover(os.path.join(mount, rel), folder)
        except Exception:
            log.exception("Couldn't make cover.jpg for %s (continuing)", rel)

    if added:
        log_info(f"Copied {added} tracks, {copy_bytes / 1e6:.1f} MB in "
                 f"{time.monotonic() - copy_began:.1f} s")
    record.set_player_full(guid, not_added or no_room)
    playlists_changed = sync_playlists(record, guid, mount, manifest) if playlists else 0
    return {"added": added, "removed": removed, "replaced": len(changed),
            "renamed": renamed, "swaps_no_room": len(no_room), "art_added": covers,
            "not_added_full": not_added, "no_space": not_added,
            "playlists_changed": playlists_changed}


def sync_playlists(record, guid, mount, manifest):
    """Write the dock's playlists as Playlists/<name>.m3u8 (only when their
    text changes) and delete ones it made that are no longer wanted. Files
    it didn't make are never touched. Returns how many changed."""
    by_nid = {}
    for r in record.tracks(guid):
        by_nid.setdefault(r["navidrome_id"], r["dbid"])
    folder = os.path.join(mount, PLAYLIST_DIR)
    managed = record.managed_playlists(guid)
    changed, now = 0, {}
    for pl in manifest.get("playlists", []):
        name = safe_name(pl["name"]) + ".m3u8"
        path = os.path.join(folder, name)
        if os.path.exists(path) and name not in managed.values():
            # A lost record: re-adopt a playlist that only lists the dock's
            # own files (as their tags re-adopt the files); else hands off.
            with open(path, encoding="utf-8", errors="replace") as f:
                entries = [ln for ln in f.read().splitlines()
                           if ln.strip() and not ln.startswith("#")]
            if all(ln.startswith(f"/{MUSIC_DIR}/") for ln in entries):   # or empty
                log_info(f"Adopting playlist {name!r} (it only lists the dock's music)")
            else:
                log_info(f"Not touching the player's own playlist {name!r}")
                continue
        lines = ["#EXTM3U"] + ["/" + by_nid[i].replace(os.sep, "/")
                               for i in pl["song_ids"] if i in by_nid]
        text = "\n".join(lines) + "\n"
        now[pl["target"]] = name
        try:
            with open(path, encoding="utf-8") as f:
                if f.read() == text:
                    continue
        except OSError:
            pass
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        changed += 1
    for target, name in managed.items():
        if target not in now and name not in now.values():
            path = os.path.join(folder, name)
            if os.path.exists(path):
                os.remove(path)
                changed += 1
    record.set_managed_playlists(guid, now)
    return changed


# ================================================================ session ====
def player_model(mount):
    """ModelNumStr from the Apple SysInfo Rockbox iPods keep, or None."""
    try:
        with open(os.path.join(mount, "iPod_Control", "Device", "SysInfo")) as f:
            for line in f:
                key, _, val = line.partition(":")
                if key.strip() == "ModelNumStr":
                    return val.strip() or None
    except OSError:
        pass
    return None


def player_name(mount):
    """The name given to the iPod in its (Apple) database, if it has one."""
    try:
        import pygpod
        path = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
        if os.path.isfile(path):
            return getattr(pygpod.Database.from_file(path).master_playlist, "name", None)
    except Exception:
        pass
    return None


def sync_player(mount, guid, record, manifest, cache_dir, work_dir, backup_root=None,
                write=False, client=None, step=None, progress=None, kind="rockbox",
                **_ignored):
    """One dock session for a Rockbox player, or with kind="generic" a plain
    music-folder player (no plays, no playlists) - the sync job's steps 2-5,
    with the same summary keys as ipod_handler.sync_player."""
    import dock_plays
    generic = kind == "generic"
    step = step or (lambda n, detail: None)
    summary = {"plays_new": 0, "plays_sent": 0, "plays_queued": 0,
               "added": 0, "removed": 0, "wrote": False}
    if generic:
        summary["plays_supported"] = False           # no play log to read
        log_path = None
    else:
        step(2, "Reading plays")
        shutil.rmtree(work_dir, ignore_errors=True)
        summary["plays_new"], log_path = read_plays(mount, work_dir, record, guid)
        record.rematch_plays(guid)
    if summary["plays_new"]:
        log_info(f"Queued {summary['plays_new']} new play(s)")
    if write and log_path:
        # The plays are committed in the record: now the log can go.
        os.remove(log_path)
        fd = os.open(mount, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    if client is not None and record.unsent_count()[0]:
        step(3, f"Sending {record.unsent_count()[0]} plays")
        summary["plays_sent"] = dock_plays.upload_plays(client, record)[0]
    summary["plays_queued"] = record.unsent_count()[0]

    if write and "tracks" not in (manifest or {}):
        log_info("No staged plan yet: plays only, the player's music is left as is")
        write = False

    if write:
        step(4, "Checking player")
        record.upsert_player(guid, model=player_model(mount))
        report = reconcile(record, guid, mount)
        if report["reset"]:
            log_info("Player appears to have been reset: rebuilding it")
        step(5, "Copying tracks")
        result = apply_plan(record, guid, mount, manifest, cache_dir, progress=progress,
                            playlists=not generic)
        summary.update(result, reconcile=report, wrote=True)
        try:
            dest = os.path.join(mount, MUSIC_DIR, RECORD_COPY)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            record.backup_to(dest)
        except OSError as e:
            log_info(f"Couldn't save a copy of the record on the player: {e}")

    player = record.player(guid)
    if player:
        if player.get("forgotten_at"):
            log_info("This player was forgotten (musicdock-players forget); "
                     "docking it brings it back")
        record.set_player_name(guid, player_name(mount) or (
            None if player.get("name") else "Music player"))
        record.set_player_kind(guid, kind)
        record.mark_synced(guid)
    return summary
