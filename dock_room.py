"""
dock_room.py - making room on a full player (user, 2026-10-30).

Songs leave a player to make room only when new songs don't fit, oldest date
first, and only as many as it takes. A song's date is the latest of its last
play (Navidrome's or the dock's own record), when it reached the player, and
when a playlist holding it last changed, so an updated playlist stays whole.
Favorites have no date of their own: they go by plays like anything else.
Nothing dated within [sync] recent_play_months can go. If that isn't enough,
the songs that don't fit stay off and the screen says so.

A song removed this way stays in the plan. It comes back only into free room,
or as a new song if its date becomes recent, so two songs never take turns.

The prepare job writes the dates to song_dates.json in the cache (not the
plan: plays would rewrite the plan and look like new music). No file means
dates aren't known, so nothing is removed.
"""

import hashlib
import json
import os

DATES_FILE = "song_dates.json"
MONTH = 30 * 86400


def read_dates(cache_dir):
    try:
        with open(os.path.join(cache_dir, DATES_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def list_hash(song_ids):
    return hashlib.sha1("\n".join(sorted(song_ids)).encode()).hexdigest()


def new_dates(previous, songs, playlists, played_at, now):
    """The dates file's new contents. Navidrome's own date for a playlist can't
    be used (it moves on every read of a smart playlist), so a list's date is
    when the dock saw its songs change. Lists already there when this first
    runs count as old, or the first run would protect all of them."""
    known = (previous or {}).get("playlists")
    lists = {}
    dates = {t["id"]: played_at(t) for t in songs}
    for pl in playlists:
        if pl.get("target") == "starred":      # starring one song dates no others
            continue
        digest = list_hash(pl["song_ids"])
        old = (known or {}).get(pl["target"])
        if old and old["hash"] == digest:
            lists[pl["target"]] = old
        else:
            lists[pl["target"]] = {"hash": digest, "date": now if known is not None else 0}
        for nid in pl["song_ids"]:
            if nid in dates:
                dates[nid] = max(dates[nid], lists[pl["target"]]["date"])
    return {"playlists": lists, "songs": {n: int(d) for n, d in dates.items() if d}}


class Rules:
    """What can go right now, or None from `rules` when nothing may."""

    def __init__(self, dates, plays, months, now):
        self.dates, self.plays = dates.get("songs", {}), plays
        self.cutoff = now - months * MONTH

    def date(self, nid, added_at=0):
        return max(self.dates.get(nid, 0), self.plays.get(nid) or 0, added_at or 0)

    def recent(self, nid, added_at=0):
        return self.date(nid, added_at) >= self.cutoff


def rules(manifest, cache_dir, record, now):
    """The plan carries make_room_months only while the setting is on."""
    months = manifest.get("make_room_months")
    dates = read_dates(cache_dir)
    if months is None or dates is None:
        return None
    return Rules(dates, record.last_played(), months, now)


def pick(new_sizes, room, candidates):
    """Which songs to remove, as (song id, ...). new_sizes: the new songs in
    the order they'll be copied. room: bytes free for them. candidates: (date,
    song id, size) of songs allowed to go. Removes nothing for a new song that
    wouldn't fit even with every candidate gone, and no more than needed."""
    candidates = sorted(candidates)
    most = room + sum(c[2] for c in candidates)
    total = 0
    for size in new_sizes:
        if total + size > most:
            break
        total += size
    gone, need = [], total - room
    for _, nid, size in candidates:
        if need <= 0:
            break
        gone.append(nid)
        need -= size
    return gone
