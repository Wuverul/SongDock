"""
ipod_handler.py - the stock-firmware iPod handler (pygpod; needs the venv).

This file grows through Phase 4. So far: reading play data safely.

Play data, and why the order of operations matters:
  * The iPod logs plays in iPod_Control/iTunes/"Play Counts" (newer models:
    iTunesStats or PlayCounts.plist): one entry per track *by position* in
    iTunesDB, with a count since the last DB write and the last play time.
  * pygpod deletes those files whenever it opens the iPod folder, and after
    any DB change the positions no longer match. So: snapshot Play Counts and
    iTunesDB, map positions to tracks from the *snapshot* (Database.from_file
    only reads), queue the plays in the record, and only then let pygpod open
    the live database. (CLAUDE.md safety rules.)
"""

import os
import shutil
import warnings
from datetime import datetime, timedelta

# pygpod warns at import that pyusb (its USB auto-detection) is missing; the
# dock reads the player's identity from udev instead, so it isn't needed.
warnings.filterwarnings("ignore", message="pygpod: optional dependencies not installed")

PLAY_FILES = ("Play Counts", "iTunesStats", "PlayCounts.plist")
MAC_EPOCH = datetime(1904, 1, 1)
REPEAT_GAP = 2           # seconds assumed between back-to-back repeat plays


def itunes_dir(mount):
    return os.path.join(mount, "iPod_Control", "iTunes")


def mac_to_unix(mac_seconds):
    """iPod time (seconds since 1904, in the iPod's local time, no zone) to
    Unix time. Read as the Pi's local time, so the Pi's timezone should match
    the one the iPod's clock is set to."""
    if not mac_seconds:
        return None
    return int((MAC_EPOCH + timedelta(seconds=mac_seconds)).timestamp())


def snapshot_play_data(mount, workdir):
    """Copy the play files and iTunesDB off the player (read-only for the
    player). Returns the names copied (iTunesDB plus any play files)."""
    os.makedirs(workdir, exist_ok=True)
    copied = []
    for name in ("iTunesDB",) + PLAY_FILES:
        src = os.path.join(itunes_dir(mount), name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(workdir, name))
            copied.append(name)
    return copied


def read_play_entries(folder):
    """Parse whichever play file exists in `folder` (pygpod's parsers)."""
    from pygpod.db.playcounts import (parse_itunesstats, parse_play_counts,
                                      parse_playcounts_plist)
    path = os.path.join(folder, "Play Counts")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return parse_play_counts(f.read())
    path = os.path.join(folder, "iTunesStats")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return parse_itunesstats(f.read().decode("utf-8", "replace"))
    path = os.path.join(folder, "PlayCounts.plist")
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return parse_playcounts_plist(f.read())
    return []


def plays_from_snapshot(workdir, record, guid, now=None):
    """Turn a snapshot into play dicts for Record.queue_plays.

    Each logged play becomes one row; for a count of N, seq 0 is the last
    play, with the iPod's exact time, and seq 1..N-1 are earlier plays
    estimated by stepping back one track length each (approx=True). Tracks the dock didn't add
    get navidrome_id None and keep artist/title for later matching.
    """
    import pygpod
    entries = read_play_entries(workdir)
    if not entries:
        return []
    tracks = pygpod.Database.from_file(os.path.join(workdir, "iTunesDB")).tracks
    plays = []
    for entry, track in zip(entries, tracks):
        count = entry.play_count
        if count <= 0:
            continue
        known = record.track(guid, track.dbid) if record else None
        last = mac_to_unix(entry.time_played)
        exact = last is not None
        if last is None:
            last = int(now or datetime.now().timestamp())
        step = int(round(track.duration or 0)) + REPEAT_GAP
        for k in range(count):
            plays.append({
                "dbid": track.dbid,
                "seq": k,
                "played_at": last - k * step,
                "approx": k > 0 or not exact,
                "navidrome_id": known["navidrome_id"] if known else None,
                "artist": track.artist, "album": track.album, "title": track.title,
            })
    return plays


# ================================================================== setup ====
class HandlerError(Exception):
    pass


# USB product ID -> a model number pygpod knows, for players whose SysInfo is
# empty (fresh restore) and that the record hasn't seen yet. Any model of the
# same generation gives the same signature type and artwork sizes.
REPRESENTATIVE_MODELS = {
    0x1263: "xB903",       # iPod nano 4th generation
}
NAVIDROME_TAG = "navidrome:"   # prefix in a track's comment field (mapping backup)
FREE_SPACE_MARGIN = 200 * 1024 * 1024
ARTWORK_FIRST_ID = 100     # pygpod starts at 1, which clashed with game icons


def sysinfo_path(mount):
    return os.path.join(mount, "iPod_Control", "Device", "SysInfo")


def read_sysinfo(mount):
    values, lines = {}, []
    try:
        with open(sysinfo_path(mount)) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        pass
    for line in lines:
        key, sep, val = line.partition(":")
        if sep:
            values[key.strip()] = val.strip()
    return values, lines


def ensure_sysinfo(mount, guid, record, usb_product=None):
    """Make sure SysInfo has the model number and FireWire GUID.

    Without them pygpod can't tell the player needs a signed database and
    writes one the iPod rejects. A factory restore empties SysInfo, so the
    model is remembered in the record per GUID. Returns the model used.
    """
    values, lines = read_sysinfo(mount)
    known = record.player(guid) or {}
    model = (values.get("ModelNumStr") or known.get("model")
             or REPRESENTATIVE_MODELS.get(usb_product))
    if not model:
        raise HandlerError(
            "unknown iPod model: SysInfo is empty and this player hasn't been seen "
            f"before. Add a line 'ModelNumStr: x<model>' to {sysinfo_path(mount)} "
            "(model from Settings > About, e.g. MB903 -> xB903).")
    add = []
    if "ModelNumStr" not in values:
        add.append(f"ModelNumStr: {model}")
    if "FirewireGuid" not in values:
        add.append(f"FirewireGuid: 0x{guid}")
    if add:
        os.makedirs(os.path.dirname(sysinfo_path(mount)), exist_ok=True)
        with open(sysinfo_path(mount), "w") as f:
            f.write("\n".join(lines + add) + "\n")
            f.flush()
            os.fsync(f.fileno())
    record.upsert_player(guid, model=model)
    return model


# ================================================================= backup ====
BACKED_UP = (("iPod_Control", "iTunes"), ("iPod_Control", "Artwork", "ArtworkDB"))


def backup_player(mount, dest_root, keep=3):
    """Copy iTunes/ (iTunesDB, play files) and ArtworkDB off the player before
    a write. Keeps the newest `keep` backups. Returns the backup folder.
    (.ithmb thumbnail files are large and not backed up.)"""
    dest = os.path.join(dest_root, datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    for parts in BACKED_UP:
        src = os.path.join(mount, *parts)
        out = os.path.join(dest, *parts)
        if os.path.isdir(src):
            shutil.copytree(src, out)
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(src, out)
    old = sorted(d for d in os.listdir(dest_root) if os.path.isdir(os.path.join(dest_root, d)))
    for d in old[:-keep]:
        shutil.rmtree(os.path.join(dest_root, d), ignore_errors=True)
    return dest


def restore_backup(mount, backup):
    """Put a backup's iTunesDB/ArtworkDB back (after a failed write)."""
    for parts in BACKED_UP:
        src = os.path.join(backup, *parts)
        out = os.path.join(mount, *parts)
        if os.path.isdir(src):
            for name in os.listdir(src):
                shutil.copy2(os.path.join(src, name), os.path.join(out, name))
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(src, out)


# ============================================================== reconcile ====
def music_files(mount):
    root = os.path.join(mount, "iPod_Control", "Music")
    out = set()
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.startswith("._"):
                out.add(os.path.relpath(os.path.join(dirpath, name), mount))
    return out


def ipod_path_to_rel(ipod_path):
    return ipod_path.lstrip(":").replace(":", os.sep)


def navidrome_id_from(track):
    comment = track.comment or ""
    return comment[len(NAVIDROME_TAG):] if comment.startswith(NAVIDROME_TAG) else None


def _key(title, artist):
    return ((title or "").strip().casefold(), (artist or "").strip().casefold())


def reconcile(db, record, guid, mount, delete_orphans=True, manifest=None):
    """Compare the live player with the record before changing anything.

    - Tracks the record has but the player doesn't: marked removed (all of
      them at once = the player was reset; logged as such).
    - Tracks on the player the record doesn't have: adopted if their comment
      carries a Navidrome ID (e.g. a sync that saved the DB but crashed
      before updating the record), or if they match exactly one staged track
      by title, artist and length (+-2 s) - e.g. tracks copied by hand before
      the dock managed this player. Adopting by match writes the Navidrome
      ID into the track's comment (report["dirty"]: the DB needs a save).
      Anything else is "foreign" and left alone.
    - Files in Music/ that no track points to (orphans): deleted.
    Returns a report dict.
    """
    on_player = {t.dbid: t for t in db.tracks}
    known = {int(r["dbid"]): r for r in record.tracks(guid)}
    missing = [r for d, r in known.items() if d not in on_player]
    reset = bool(known) and len(missing) == len(known)
    for r in missing:
        record.mark_removed(guid, r["dbid"], "player appears to have been reset"
                            if reset else "missing from player")
    candidates = {}
    for m in (manifest or {}).get("tracks", []):
        candidates.setdefault(_key(m.get("title"), m.get("artist")), []).append(m)
    taken = {r["navidrome_id"] for r in record.tracks(guid)}
    adopted, foreign, dirty = 0, 0, False
    for dbid, t in on_player.items():
        if dbid in known:
            continue
        nid, entry = navidrome_id_from(t), None
        if not nid:
            matches = [m for m in candidates.get(_key(t.title, t.artist), [])
                       if abs((m.get("duration") or 0) - (t.duration or 0)) <= 2
                       and m["id"] not in taken]
            if len(matches) == 1:
                entry = matches[0]
                nid = entry["id"]
                t.comment = NAVIDROME_TAG + nid
                dirty = True
        if nid:
            encoding = None
            if entry:
                # Same format as the cached file: adopt as is. Different (e.g.
                # an old AAC copy of a track now cached as MP3): adopt, but
                # mark it so apply_plan replaces it with the current file.
                same = (os.path.splitext(t.ipod_path)[1].lower()
                        == os.path.splitext(entry["file"])[1].lower())
                encoding = entry.get("encoding") if same else "unknown (replace)"
            record.add_track(guid, dbid, ipod_path=t.ipod_path, navidrome_id=nid,
                             artist=t.artist, album=t.album, title=t.title,
                             encoding=encoding)
            taken.add(nid)
            adopted += 1
        else:
            foreign += 1
    referenced = {ipod_path_to_rel(t.ipod_path) for t in db.tracks if t.ipod_path}
    orphans = sorted(music_files(mount) - referenced)
    if delete_orphans:
        for rel in orphans:
            os.remove(os.path.join(mount, rel))
    return {"missing": len(missing), "reset": reset, "adopted": adopted,
            "foreign": foreign, "orphans": len(orphans), "dirty": dirty}


# ================================================================== apply ====
def checked_save(db, guid):
    """Save only if pygpod will sign the DB the way this player needs."""
    d = db._device
    if not d or not d.requires_hash or d.checksum_type.name != "HASH58":
        raise HandlerError(f"refusing to save: player not identified as needing a "
                           f"hash58-signed database ({d.model if d else 'no device'})")
    if (d.firewire_guid or "").upper() != guid.upper():
        raise HandlerError("refusing to save: SysInfo FireWire GUID doesn't match the player")
    db.save()


def start_artwork_ids_at(db, first=ARTWORK_FIRST_ID):
    """New artwork gets image IDs from `first` up (pygpod 0.0.6 internals)."""
    mgr = db._get_artwork_manager()
    if mgr._next_image_id < first:
        mgr._next_image_id = first


def apply_plan(db, record, guid, mount, manifest, cache_dir, batch_size=25,
               progress=None, save=checked_save, dirty=False):
    """Make the player match the staged plan (prepare job's manifest).

    Order: remove tracks that left every target (with their files), add
    missing ones in batches - saving the DB after each batch and only then
    recording them as present - and rebuild the dock's playlists. A crash
    loses at most the current batch; the next sync carries on.
    `dirty`: the DB was changed before this call (e.g. reconcile adopted
    tracks) and must be saved even if the plan changes nothing.
    Returns a summary dict.
    """
    wanted = {t["id"]: t for t in manifest.get("tracks", [])}
    saved = False
    on_player = {t.dbid: t for t in db.tracks}
    present = {}
    for r in record.tracks(guid):
        if int(r["dbid"]) in on_player:
            present.setdefault(r["navidrome_id"], r)

    # Tracks whose cached file changed encoding (e.g. mp3_quality) are replaced.
    changed = {nid for nid, r in present.items()
               if nid in wanted and r.get("encoding")
               and wanted[nid].get("encoding") and r["encoding"] != wanted[nid]["encoding"]}
    removals = [r for nid, r in present.items() if nid not in wanted or nid in changed]
    additions = [t for nid, t in wanted.items() if nid not in present or nid in changed]

    # Remove from the DB, save, and only then delete the files: if the save
    # fails, restoring the backup finds every file still there. (pygpod's
    # delete_file=True would delete before the save.) A crash after the save
    # leaves orphan files, which the next reconcile removes.
    removed, doomed = 0, []
    for r in removals:
        track = on_player[int(r["dbid"])]
        doomed.append(os.path.join(mount, ipod_path_to_rel(track.ipod_path)))
        db.remove_track(track, delete_file=False)
    if removals:
        save(db, guid)
        saved = True
        for r in removals:
            reason = ("re-encoded" if r["navidrome_id"] in changed
                      else "no longer in any synced playlist")
            record.mark_removed(guid, r["dbid"], reason)
            log_info(f"Removed {r['artist']} - {r['title']} ({reason})")
            removed += 1
        for path in doomed:
            if os.path.exists(path):
                os.remove(path)

    start_artwork_ids_at(db)
    added, skipped_full = 0, 0
    for i in range(0, len(additions), batch_size):
        batch = []
        for t in additions[i:i + batch_size]:
            path = os.path.join(cache_dir, t["file"])
            if not os.path.exists(path):
                # The prepare job pruned or is replacing it; next sync gets it.
                log_info(f"Skipping {t.get('title')}: not in the cache right now")
                continue
            if shutil.disk_usage(mount).free - os.path.getsize(path) < FREE_SPACE_MARGIN:
                skipped_full = len(additions) - (i + len(batch))
                break
            if progress:
                progress(added + len(batch), len(additions), t)
            # Navidrome's album name, not the file's tag: they can differ
            # (e.g. "Inertia (deluxe digital)" vs "Inertia"), and the player
            # should match what the user sees in Navidrome.
            overrides = {"album": t["album"]} if t.get("album") else {}
            track = db.add_track(path, **overrides)
            track.comment = NAVIDROME_TAG + t["id"]
            batch.append((track, t))
        if batch:
            save(db, guid)                          # DB first ...
            saved = True
            with record.transaction():              # ... then the record
                for track, t in batch:
                    record.add_track(guid, track.dbid, ipod_path=track.ipod_path,
                                     navidrome_id=t["id"], artist=t.get("artist"),
                                     album=t.get("album"), title=t.get("title"),
                                     track_no=t.get("track"), duration=t.get("duration"),
                                     encoding=t.get("encoding"))
            added += len(batch)
        if skipped_full:
            log_info(f"Player is full: {skipped_full} track(s) not added")
            break

    playlists_changed = sync_playlists(db, record, guid, manifest)
    if playlists_changed or (dirty and not saved):
        save(db, guid)
    return {"added": added, "removed": removed, "replaced": len(changed),
            "not_added_full": skipped_full, "playlists_changed": playlists_changed}


def sync_playlists(db, record, guid, manifest):
    """Create/update the dock's playlists (Favorites, tagged, configured) and
    delete ones it made that are no longer wanted. Playlists the dock didn't
    make are never touched. Returns how many changed."""
    by_nid = {}
    for r in record.tracks(guid):
        by_nid.setdefault(r["navidrome_id"], int(r["dbid"]))
    tracks = {t.dbid: t for t in db.tracks}
    managed = record.managed_playlists(guid)
    existing = {p.name: p for p in db.playlists if not p.is_master}
    changed, now_managed = 0, {}
    for pl in manifest.get("playlists", []):
        name, target = pl["name"], pl["target"]
        want = [tracks[by_nid[i]] for i in pl["song_ids"]
                if i in by_nid and by_nid[i] in tracks]
        old_name = managed.get(target)
        current = existing.get(old_name) if old_name else None
        if current is None and name in existing and name not in managed.values():
            log_info(f"Not touching the player's own playlist {name!r}")
            continue
        if current is not None and current.name == name and \
                [t.dbid for t in current.tracks] == [t.dbid for t in want]:
            now_managed[target] = name
            continue
        if current is not None:
            db.delete_playlist(current)
        new = db.create_playlist(name)
        for t in want:
            db.add_track_to_playlist(new, t)
        now_managed[target] = name
        changed += 1
    for target, name in managed.items():
        if target not in now_managed and name in existing:
            db.delete_playlist(existing[name])
            changed += 1
    record.set_managed_playlists(guid, now_managed)
    return changed


def log_info(message):
    import logging
    logging.getLogger("ipod_handler").info(message)


# ================================================================ session ====
RECORD_COPY = os.path.join("iPod_Control", "musicdock", "dock.db")


def sync_player(mount, guid, record, manifest, cache_dir, work_dir, backup_root,
                write=False, client=None, usb_product=None, step=None, progress=None):
    """One dock session for a stock iPod (the sync job's steps 2-5).

    2. Snapshot the play data and DB, map plays to songs (reconciling the
       record against the snapshot first, so hand-copied tracks are matched),
       and queue them - committed before anything can clear them.
    3. Send queued plays if the server is reachable (else they wait).
    With write=True (config [device] write), also:
    4. Make sure SysInfo is complete, back up iTunesDB/ArtworkDB to the Pi.
    5. Open the live DB (pygpod clears Play Counts here - already queued),
       reconcile, apply the staged plan. On any error the backup is restored
       and the error re-raised.
    Then a copy of the record is saved onto the player. Returns a summary.
    """
    import pygpod

    import dock_plays
    step = step or (lambda n, detail: None)
    summary = {"plays_new": 0, "plays_sent": 0, "plays_queued": 0,
               "added": 0, "removed": 0, "wrote": False}

    step(2, "Reading plays")
    shutil.rmtree(work_dir, ignore_errors=True)
    copied = snapshot_play_data(mount, work_dir)
    if "iTunesDB" in copied:
        snap = pygpod.Database.from_file(os.path.join(work_dir, "iTunesDB"))
        reconcile(snap, record, guid, mount, delete_orphans=False, manifest=manifest)
        plays = plays_from_snapshot(work_dir, record, guid)
        summary["plays_new"] = record.queue_plays(guid, plays)
        record.rematch_plays(guid)
        if summary["plays_new"]:
            log_info(f"Queued {summary['plays_new']} new play(s)")

    if client is not None and record.unsent_count()[0]:
        step(3, f"Sending {record.unsent_count()[0]} plays")
        summary["plays_sent"] = dock_plays.upload_plays(client, record)[0]
    summary["plays_queued"] = record.unsent_count()[0]

    if write:
        step(4, "Backing up player")
        ensure_sysinfo(mount, guid, record, usb_product)
        os.makedirs(backup_root, exist_ok=True)
        backup = backup_player(mount, backup_root)
        try:
            db = pygpod.Database(mount)
            report = reconcile(db, record, guid, mount, manifest=manifest)
            if report["reset"]:
                log_info("Player appears to have been reset: rebuilding it")
            step(5, "Copying tracks")
            result = apply_plan(db, record, guid, mount, manifest, cache_dir,
                                progress=progress, dirty=report["dirty"])
        except Exception:
            log_info("Sync failed: restoring the player's database from the backup")
            restore_backup(mount, backup)
            raise
        summary.update(result, reconcile=report, wrote=True)

    try:
        dest = os.path.join(mount, RECORD_COPY)
        if write:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            record.backup_to(dest)
    except OSError as e:
        log_info(f"Couldn't save a copy of the record on the player: {e}")
    record.mark_synced(guid) if record.player(guid) else None
    return summary
