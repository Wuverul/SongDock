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
from datetime import datetime, timedelta

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
