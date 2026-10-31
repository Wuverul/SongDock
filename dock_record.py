"""
dock_record.py - the Pi's record of what is on each player, and queued plays.

SQLite (standard library; transactions survive power cuts) at
/var/lib/musicdock/dock.db on the Pi. One file, shared by the sync job (root)
and the prepare job (login user).

Tables:
  players          one row per player, keyed by FireWire GUID (= USB serial):
                   model number (to rewrite SysInfo after a reset), its name,
                   its kind ("stock" / "rockbox"), last sync,
                   and when it was forgotten (retired: it no longer holds
                   songs in the cache; docking it again brings it back).
  tracks           what the dock put on each player: GUID + dbid (the iPod's
                   persistent track ID) -> file path, Navidrome song ID,
                   MusicBrainz ID, metadata, encoding, and state:
                   "present" (confirmed on the player) or "removed". Songs
                   present on every player may leave the cache when it's
                   full (songs_on_every_player).
  pending_plays    plays read from a player, waiting for Navidrome. A play is
                   removed from the queue only by marking it sent after the
                   server confirms; sent rows are kept as history. Each play
                   has a unique fingerprint (GUID, dbid, time, repeat number),
                   so re-reading the same Play Counts after a crash adds nothing.
  playlists        playlists the dock manages on each player (so it only ever
                   changes or deletes its own).
"""

import contextlib
import json
import os
import sqlite3
import time

SHARED_DIR = "/var/lib/musicdock"
DEFAULT_PATH = os.environ.get("MUSICDOCK_RECORD") or (
    os.path.join(SHARED_DIR, "dock.db") if os.path.isdir(SHARED_DIR)
    else os.path.expanduser("~/.cache/musicdock/dock.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    guid        TEXT PRIMARY KEY,
    model       TEXT,
    first_seen  INTEGER NOT NULL,
    last_sync   INTEGER,
    forgotten_at INTEGER,         -- set by `musicdock-players forget` / DockSetup.txt
    name        TEXT,             -- the name given to the iPod (its master playlist)
    kind        TEXT,             -- "stock" or "rockbox" (set each sync; NULL = not seen yet)
    capacity    INTEGER           -- bytes of storage on the player (set each sync)
);
CREATE TABLE IF NOT EXISTS tracks (
    guid          TEXT NOT NULL,
    dbid          TEXT NOT NULL,          -- unsigned 64-bit, kept as text
    ipod_path     TEXT,
    navidrome_id  TEXT NOT NULL,
    mbid          TEXT,
    artist        TEXT,
    album         TEXT,
    title         TEXT,
    track_no      INTEGER,
    duration      INTEGER,
    encoding      TEXT,
    genre         TEXT,
    added_at      INTEGER NOT NULL,
    state         TEXT NOT NULL DEFAULT 'present',   -- present | removed
    changed_at    INTEGER NOT NULL,
    note          TEXT,
    PRIMARY KEY (guid, dbid)
);
CREATE INDEX IF NOT EXISTS tracks_by_song ON tracks (guid, navidrome_id);
CREATE TABLE IF NOT EXISTS pending_plays (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guid          TEXT NOT NULL,
    dbid          TEXT NOT NULL,
    seq           INTEGER NOT NULL DEFAULT 0,   -- which repeat of this play
    navidrome_id  TEXT,                         -- NULL = not matched yet
    played_at     INTEGER NOT NULL,             -- Unix time
    approx        INTEGER NOT NULL DEFAULT 0,   -- 1 = estimated time
    artist        TEXT,
    album         TEXT,
    title         TEXT,
    queued_at     INTEGER NOT NULL,
    sent_at       INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    UNIQUE (guid, dbid, played_at, seq)
);
-- Files copied onto a stock iPod since its database was last saved (the
-- handler saves once an hour or at the end of a sync). After a crash the
-- next sync adds these files to the database instead of deleting them.
CREATE TABLE IF NOT EXISTS copied (
    guid          TEXT NOT NULL,
    ipod_path     TEXT NOT NULL,          -- as in the iTunesDB (":iPod_Control:Music:...")
    navidrome_id  TEXT NOT NULL,
    entry         TEXT NOT NULL,          -- the plan's track (JSON)
    copied_at     INTEGER NOT NULL,
    PRIMARY KEY (guid, ipod_path)
);
CREATE TABLE IF NOT EXISTS playlists (
    guid    TEXT NOT NULL,
    target  TEXT NOT NULL,      -- e.g. "starred" or "playlist:Mel Test"
    name    TEXT NOT NULL,      -- name on the player
    PRIMARY KEY (guid, target)
);
"""

TRACK_FIELDS = ("ipod_path", "navidrome_id", "mbid", "artist", "album", "title",
                "track_no", "duration", "encoding", "genre")


class Record:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        # Rollback journal + full sync: simplest to keep consistent through a
        # power cut on an SD card.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.executescript(SCHEMA)
        self._add_column("players", "forgotten_at INTEGER")      # added 2026-10-23
        self._add_column("players", "name TEXT")                 # added 2026-10-23
        self._add_column("tracks", "genre TEXT")                 # Rockbox re-tags (2026-10-23)
        self._add_column("players", "kind TEXT")                 # stock / rockbox (2026-10-23)
        self._add_column("players", "capacity INTEGER")          # dock_limits (2026-10-26)
        self._add_column("players", "swaps_no_room INTEGER")     # format swaps held back (2026-10-30)
        self._add_column("players", "full INTEGER")              # last sync ran out of room (2026-10-30)
        if new and os.geteuid() == 0:
            # Created by the root sync job: hand it to the folder's owner (the
            # login user) so the prepare job can write it too.
            st = os.stat(folder)
            os.chown(path, st.st_uid, st.st_gid)

    def _add_column(self, table, column):
        """Bring a record made by an older version up to date."""
        name = column.split()[0]
        if name in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
            return
        try:
            with self.db:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        except sqlite3.OperationalError:
            if name not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                raise                          # not just another job adding it first

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def transaction(self):
        """All-or-nothing: commits on success, rolls back on any error."""
        with self.db:
            yield self.db

    # ------------------------------------------------------------ players ----
    def player(self, guid):
        row = self.db.execute("SELECT * FROM players WHERE guid = ?", (guid,)).fetchone()
        return dict(row) if row else None

    def upsert_player(self, guid, model=None):
        now = int(time.time())
        with self.db:
            self.db.execute(
                "INSERT INTO players (guid, model, first_seen) VALUES (?, ?, ?) "
                "ON CONFLICT(guid) DO UPDATE SET model = COALESCE(excluded.model, model)",
                (guid, model, now))

    def last_synced_player(self):
        row = self.db.execute("SELECT * FROM players WHERE last_sync IS NOT NULL "
                              "AND forgotten_at IS NULL "
                              "ORDER BY last_sync DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def mark_synced(self, guid):
        """Record a finished sync. A forgotten player that docks is back."""
        with self.db:
            self.db.execute("UPDATE players SET last_sync = ?, forgotten_at = NULL "
                            "WHERE guid = ?", (int(time.time()), guid))

    def set_player_name(self, guid, name):
        if name:
            with self.db:
                self.db.execute("UPDATE players SET name = ? WHERE guid = ?", (name, guid))

    def set_player_kind(self, guid, kind):
        """"stock" or "rockbox": which handler synced it (see active_stock_players)."""
        with self.db:
            self.db.execute("UPDATE players SET kind = ? WHERE guid = ?", (kind, guid))

    def set_player_capacity(self, guid, capacity):
        """Bytes of storage on the player (dock_limits' ALAC suggestion)."""
        with self.db:
            self.db.execute("UPDATE players SET capacity = ? WHERE guid = ?", (capacity, guid))

    def set_swaps_no_room(self, guid, count):
        """Format swaps the last sync held back because the player had no room."""
        with self.db:
            self.db.execute("UPDATE players SET swaps_no_room = ? WHERE guid = ?", (count, guid))

    def set_player_full(self, guid, full):
        """Whether the last sync left songs out for lack of room. A later sync
        with nothing left out clears it, and the player counts again."""
        with self.db:
            self.db.execute("UPDATE players SET full = ? WHERE guid = ?", (int(bool(full)), guid))

    def active_stock_players(self):
        """Players that aren't forgotten and may run Apple's firmware: kind
        "stock", or not seen since kinds were recorded (assumed stock, so
        stock-iPod compatibility is never switched off under one)."""
        rows = self.db.execute("SELECT * FROM players WHERE forgotten_at IS NULL "
                               "AND (kind IS NULL OR kind = 'stock')")
        return [dict(r) for r in rows]

    def active_player_kinds(self):
        """The kinds of player the dock still serves, e.g. {"stock", "generic"}.
        Only kinds a finished sync recorded: a new player has none yet."""
        rows = self.db.execute("SELECT DISTINCT kind FROM players "
                               "WHERE forgotten_at IS NULL AND kind IS NOT NULL")
        return {r["kind"] for r in rows}

    def remember_player(self, guid):
        """Undo forget_player (as docking the player again would)."""
        with self.db:
            self.db.execute("UPDATE players SET forgotten_at = NULL WHERE guid = ?", (guid,))

    def players(self):
        """Every player the dock knows, most recently synced first, with
        how many tracks the dock has on it."""
        rows = self.db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM tracks t WHERE t.guid = p.guid "
            "AND t.state = 'present') AS present FROM players p "
            "ORDER BY p.last_sync IS NULL, p.last_sync DESC")
        return [dict(r) for r in rows]

    def forget_player(self, guid):
        """Retire a player: its songs no longer hold files in the cache and
        it's no longer the one DOCK READY reports on. Its history and queued
        plays stay; docking it again brings it back (mark_synced)."""
        with self.db:
            self.db.execute("UPDATE players SET forgotten_at = ? WHERE guid = ?",
                            (int(time.time()), guid))

    # ------------------------------------------------------------- tracks ----
    def add_track(self, guid, dbid, **fields):
        """Record a track the dock just put on the player (or re-added)."""
        now = int(time.time())
        cols = {k: fields.get(k) for k in TRACK_FIELDS}
        with self.db:
            self.db.execute(
                f"INSERT INTO tracks (guid, dbid, {', '.join(cols)}, added_at, state, "
                f"changed_at) VALUES (?, ?, {', '.join('?' * len(cols))}, ?, 'present', ?) "
                f"ON CONFLICT(guid, dbid) DO UPDATE SET "
                + ", ".join(f"{k} = excluded.{k}" for k in cols)
                + ", state = 'present', changed_at = excluded.changed_at, note = NULL",
                (guid, str(dbid), *cols.values(), now, now))

    def log_copied(self, guid, ipod_path, navidrome_id, entry):
        """A file is on the player but not yet in a saved database."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO copied (guid, ipod_path, navidrome_id, entry, "
                "copied_at) VALUES (?, ?, ?, ?, ?)",
                (guid, ipod_path, navidrome_id, json.dumps(entry), int(time.time())))

    def copied(self, guid):
        """[{ipod_path, navidrome_id, entry (dict)}] logged for this player."""
        rows = self.db.execute("SELECT ipod_path, navidrome_id, entry FROM copied "
                               "WHERE guid = ? ORDER BY copied_at, rowid", (guid,))
        return [{"ipod_path": p, "navidrome_id": n, "entry": json.loads(e)} for p, n, e in rows]

    def clear_copied(self, guid, paths):
        """The database holding these files was saved (or they're gone). A
        crash before this leaves entries for files the database already
        has; the next sync just clears them."""
        with self.db:
            self.db.executemany("DELETE FROM copied WHERE guid = ? AND ipod_path = ?",
                                [(guid, p) for p in paths])

    def mark_removed(self, guid, dbid, note):
        with self.db:
            self.db.execute(
                "UPDATE tracks SET state = 'removed', changed_at = ?, note = ? "
                "WHERE guid = ? AND dbid = ?", (int(time.time()), note, guid, str(dbid)))

    def tracks(self, guid, state="present"):
        rows = self.db.execute("SELECT * FROM tracks WHERE guid = ? AND state = ? "
                               "ORDER BY artist, album, track_no", (guid, state))
        return [dict(r) for r in rows]

    def songs_on_every_player(self):
        """{navidrome_id: {encodings}} for songs present on every player the
        dock knows (players table or any track row) and hasn't forgotten, so
        the prepare job can drop their cache files when space is short. A
        player with no tracks (e.g. just reset) holds nothing, so nothing is.
        A full player can't take more, so it doesn't hold back songs it lacks,
        or formats it can't swap to, while another player still has room.
        {} with no players."""
        guids = [r[0] for r in self.db.execute(
            "SELECT guid FROM players WHERE forgotten_at IS NULL UNION "
            "SELECT DISTINCT guid FROM tracks WHERE guid NOT IN (SELECT guid FROM players)")]
        full = {r[0] for r in self.db.execute("SELECT guid FROM players WHERE full = 1")}
        guids = [g for g in guids if g not in full] or guids
        held = None
        for guid in guids:
            mine = {}
            for nid, enc in self.db.execute(
                    "SELECT navidrome_id, encoding FROM tracks "
                    "WHERE guid = ? AND state = 'present'", (guid,)):
                mine.setdefault(nid, set()).add(enc)
            held = mine if held is None else {n: held[n] | e for n, e in mine.items()
                                              if n in held}
        return held or {}

    def last_played(self):
        """{navidrome_id: time of the latest play the dock has read}."""
        return dict(self.db.execute("SELECT navidrome_id, MAX(played_at) FROM pending_plays "
                                    "WHERE navidrome_id IS NOT NULL GROUP BY navidrome_id"))

    def track(self, guid, dbid):
        row = self.db.execute("SELECT * FROM tracks WHERE guid = ? AND dbid = ?",
                              (guid, str(dbid))).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- plays ----
    def queue_plays(self, guid, plays):
        """Queue plays (dicts: dbid, seq, played_at, approx, navidrome_id,
        artist, album, title) in one transaction. Returns how many were new;
        plays already queued (same fingerprint) are ignored."""
        now = int(time.time())
        with self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO pending_plays (guid, dbid, seq, navidrome_id, "
                "played_at, approx, artist, album, title, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(guid, str(p["dbid"]), p.get("seq", 0), p.get("navidrome_id"),
                  int(p["played_at"]), int(bool(p.get("approx"))), p.get("artist"),
                  p.get("album"), p.get("title"), now) for p in plays])
            return self.db.total_changes - before

    def rematch_plays(self, guid):
        """Give queued plays from tracks matched since (e.g. adopted by
        reconcile) their Navidrome ID. Returns how many were matched."""
        with self.db:
            cur = self.db.execute(
                "UPDATE pending_plays SET navidrome_id = (SELECT t.navidrome_id FROM "
                "tracks t WHERE t.guid = pending_plays.guid AND t.dbid = pending_plays.dbid) "
                "WHERE guid = ? AND navidrome_id IS NULL AND sent_at IS NULL AND EXISTS "
                "(SELECT 1 FROM tracks t WHERE t.guid = pending_plays.guid "
                "AND t.dbid = pending_plays.dbid)", (guid,))
            return cur.rowcount

    def unsent_plays(self, limit=None):
        """Plays waiting for the server that can be sent (matched to a song)."""
        sql = ("SELECT * FROM pending_plays WHERE sent_at IS NULL AND navidrome_id "
               "IS NOT NULL ORDER BY played_at, id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.db.execute(sql)]

    def unsent_count(self):
        """(sendable, unmatched) plays still waiting."""
        row = self.db.execute(
            "SELECT SUM(navidrome_id IS NOT NULL), SUM(navidrome_id IS NULL) "
            "FROM pending_plays WHERE sent_at IS NULL").fetchone()
        return (row[0] or 0, row[1] or 0)

    def mark_sent(self, ids):
        with self.db:
            self.db.executemany("UPDATE pending_plays SET sent_at = ?, last_error = NULL, "
                                "attempts = attempts + 1 WHERE id = ?",
                                [(int(time.time()), i) for i in ids])

    def mark_failed(self, ids, error):
        with self.db:
            self.db.executemany("UPDATE pending_plays SET attempts = attempts + 1, "
                                "last_error = ? WHERE id = ?",
                                [(str(error)[:300], i) for i in ids])

    # ---------------------------------------------------------- playlists ----
    def managed_playlists(self, guid):
        rows = self.db.execute("SELECT target, name FROM playlists WHERE guid = ?", (guid,))
        return {r["target"]: r["name"] for r in rows}

    def set_managed_playlists(self, guid, mapping):
        """Replace the set of playlists the dock manages on this player."""
        with self.db:
            self.db.execute("DELETE FROM playlists WHERE guid = ?", (guid,))
            self.db.executemany("INSERT INTO playlists (guid, target, name) VALUES (?, ?, ?)",
                                [(guid, t, n) for t, n in mapping.items()])

    # ------------------------------------------------------------- backup ----
    def backup_to(self, dest):
        """Consistent copy of the whole record (e.g. onto the player)."""
        tmp = dest + ".tmp"
        out = sqlite3.connect(tmp)
        try:
            self.db.backup(out)
        finally:
            out.close()
        os.replace(tmp, dest)
