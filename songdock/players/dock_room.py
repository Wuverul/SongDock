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

By album ([sync] make_room_albums, on by default; user, 2026-11-01): a song's
date is its album's, the newest of the album's songs in the plan, and only
whole albums leave. So one recent play keeps an album, and brings a removed
one back whole. An album is its album artist and name; a song in "<Artist>
Singles" goes by its own release (folder_album), so singles stay apart.
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


def album_key(entry):
    """Which album a plan entry belongs to, or None if it has none."""
    album = entry.get("folder_album") or entry.get("album")
    if not album:
        return None
    return ((entry.get("album_artist") or entry.get("artist") or "").casefold(),
            album.casefold())


def albums(manifest):
    """{song id: album key} for every song in the plan that has an album."""
    found = {}
    for t in manifest.get("tracks", []) + manifest.get("on_player", []):
        key = album_key(t)
        if key:
            found[t["id"]] = key
    return found


class Rules:
    """What can go right now, or None from `rules` when nothing may.
    albums: {song id: album key} when whole albums go (else songs one by one)."""

    def __init__(self, dates, plays, months, now, albums=None):
        self.dates, self.plays = dates.get("songs", {}), plays
        self.cutoff = now - months * MONTH
        self.album_of = albums or {}
        self.members = {}
        for nid, key in self.album_of.items():
            self.members.setdefault(key, []).append(nid)
        self.added = {}          # {song id: added to this player}, set by the caller

    def song_date(self, nid, added_at=0):
        return max(self.dates.get(nid, 0), self.plays.get(nid) or 0,
                   added_at or self.added.get(nid) or 0)

    def date(self, nid, added_at=0):
        own = self.song_date(nid, added_at)
        key = self.album_of.get(nid)
        if key is None:
            return own
        return max([own] + [self.song_date(m) for m in self.members[key]])

    def recent(self, nid, added_at=0):
        return self.date(nid, added_at) >= self.cutoff


def rules(manifest, cache_dir, record, now):
    """The plan carries make_room_months only while the setting is on, and
    make_room_albums while songs go by album."""
    months = manifest.get("make_room_months")
    dates = read_dates(cache_dir)
    if months is None or dates is None:
        return None
    return Rules(dates, record.last_played(), months, now,
                 albums(manifest) if manifest.get("make_room_albums") else None)


def coming_back(manifest, cache_dir, record, guid, now):
    """Songs taken off `guid` to make room that its next sync brings back now:
    their date became recent, e.g. played in Navidrome's web or desktop app
    (user, 2026-11-04), or, by album, a song of their album was. They're new songs
    to that sync, so the prepare job counts them as changes for a docked player."""
    rules_now = rules(manifest, cache_dir, record, now)
    if not rules_now:
        return set()
    rules_now.added = {r["navidrome_id"]: r.get("added_at") for r in record.tracks(guid)}
    planned = {t["id"] for t in manifest.get("tracks", [])}
    return {n for n in record.room_removed(guid) if n in planned and rules_now.recent(n)}


def choose(rules, new_sizes, room, candidates, on_player):
    """pick() by song, or by whole album when rules.album_of is set. Returns the
    song ids to remove. on_player: song ids on the player now."""
    if not rules.album_of:
        return pick(new_sizes, room, candidates)
    groups, members = whole_albums(candidates, rules, on_player)
    return [nid for key in pick(new_sizes, room, groups) for nid in members[key]]


def whole_albums(candidates, rules, on_player):
    """Group candidates (date, song id, size) into albums for pick(): ([(date,
    album key, size)], {album key: [song ids]}). An album can go only if every
    song of it on the player may go now (e.g. none is waiting for a format
    swap); a song with no album is one on its own."""
    by_key = {}
    for date, nid, size in candidates:
        by_key.setdefault(rules.album_of.get(nid) or ("", nid), []).append((date, nid, size))
    groups, members = [], {}
    for key, rows in by_key.items():
        held = [n for n in rules.members.get(key, ()) if n in on_player]
        if len(rows) < len(held):
            continue
        groups.append((max(r[0] for r in rows), key, sum(r[2] for r in rows)))
        members[key] = [r[1] for r in rows]
    return groups, members


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
