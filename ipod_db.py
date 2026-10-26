"""
ipod_db.py - a stock iPod's iTunesDB without loading all of it (needs the venv).

pygpod parses the whole database into Python objects: about 22 KB per track
to open it and 35 KB at the peak of a save (measured 2026-10-26: 10,000
tracks peaked at 356 MB, 40,000 at 1.28 GB; the Pi Zero 2 W has ~290 MB
free, so pygpod tops out around 7,000-8,000 tracks). StreamDatabase keeps
an index instead - ids, the record's place in the file, and the names
used for sorting, a few hundred bytes per track - and saves by streaming
the previous file into a new one:

  * unchanged tracks are copied byte for byte; removed ones are skipped;
    edited ones are parsed one at a time (pygpod's parser and writer);
  * new tracks are built by pygpod (its record layout, file copy and
    album art) and held in memory only until the next save;
  * the master playlist with its browse lists, and the album and artist
    lists, are rebuilt from the index; other playlists are copied as they
    are, minus removed tracks, or rebuilt when the dock changed them;
  * the hash58 signature is computed over the finished file in chunks.

The previous file is the copy on the Pi (the sync's snapshot, then each
save's output), so a save reads from the SD card and writes the iPod once:
to a temporary name, fsynced, then renamed over the old database.

The interface follows pygpod's Database where the iPod handler uses it
(tracks, playlists, add_track, remove_track, create_playlist, ...).
Files it writes read back in pygpod (tests/test_ipod_db.py checks them
against pygpod's own saves).
"""

import hashlib
import hmac
import os
import random
import struct
import sys
import warnings

# pygpod warns at import that pyusb is missing; the dock doesn't use it.
warnings.filterwarnings("ignore", message="pygpod: optional dependencies not installed")

# The iTunesDB format's numbers (as in pygpod.db.constants; kept here so
# this module loads without pygpod, e.g. for tests that are then skipped).
DEFAULT_MHBD_HEADER_LEN = 244
DEFAULT_MHIP_HEADER_LEN = 76
DEFAULT_MHLP_HEADER_LEN = 92
DEFAULT_MHLT_HEADER_LEN = 92
DEFAULT_MHOD_HEADER_LEN = 24
DEFAULT_MHLA_HEADER_LEN = 92
DEFAULT_MHLI_HEADER_LEN = 92
DEFAULT_MHII_HEADER_LEN = 80
MHIA_ALBUM_HEADER_LEN = 88             # as libgpod (pygpod's album list)
MHOD_ID_TITLE, MHOD_ID_PATH, MHOD_ID_ALBUM, MHOD_ID_ARTIST, MHOD_ID_GENRE = 1, 2, 3, 4, 5
MHOD_ID_COMMENT, MHOD_ID_COMPOSER = 8, 12
MHOD_ID_LIBPLAYLISTINDEX, MHOD_ID_LIBPLAYLISTJUMPTABLE = 52, 53
MHOD_ID_PLAYLIST = 100
MHOD_ID_ALBUM_ALBUM, MHOD_ID_ALBUM_ARTIST, MHOD_ID_ALBUM_ARTIST_MHII = 200, 201, 300

TRACKS, PLAYLISTS, PODCASTS, ALBUMS, ARTISTS = 1, 2, 3, 4, 8
NEEDED_SECTIONS = (TRACKS, PLAYLISTS, PODCASTS, ALBUMS, ARTISTS)
NORMALIZE_MAX = 3000       # tracks pygpod may load at once to tidy a database
PLAY_FILES = ("Play Counts", "iTunesStats", "PlayCounts.plist")
CHUNK = 1 << 20
HASH58_AT, HASH58_LEN = 0x58, 20
DB_ID = slice(0x18, 0x20)

# Browse lists in the master playlist, in pygpod's order (title, artist,
# album, genre, composer).
SORT_KEYS = ((3, "title"), (5, "artist"), (4, "album"), (7, "genre"), (18, "composer"))
_STRING_FIELDS = {MHOD_ID_TITLE: "title", MHOD_ID_PATH: "ipod_path", MHOD_ID_ALBUM: "album",
                  MHOD_ID_ARTIST: "artist", MHOD_ID_GENRE: "genre", MHOD_ID_COMMENT: "comment",
                  MHOD_ID_COMPOSER: "composer"}


class DatabaseError(Exception):
    pass


def _u8(b, o):
    return b[o]


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def _decode(text, encoding):
    from pygpod.utils.encoding import decode_mhod_string
    try:
        return decode_mhod_string(text, encoding)
    except (UnicodeDecodeError, ValueError):
        return text.decode("utf-16-le", "replace")


def _sort_text(value):
    """As pygpod sorts: upper case, without a leading "The "."""
    value = (value or "").upper()
    return value[4:] if value.startswith("THE ") else value


def art_key(album, size):
    """Tracks share an image when their album name and source cover size
    match: the prepare job encodes an album's cover once, so its tracks'
    covers are byte-identical, and a different cover is all but certain to
    differ in size."""
    return ((album or "").strip().casefold(), int(size or 0))


# ================================================================== index ====
def _name_property(field):
    """A name that can be set like a pygpod Track's; the change is kept
    and written at the next save."""
    private = "_" + field

    def get(self):
        return getattr(self, private)

    def put(self, value):
        if getattr(self, private) != value:
            self.set_text(field, value)
    return property(get, put)


class TrackInfo:
    """One track: what the dock needs to know without the whole record."""

    __slots__ = ("track_id", "dbid", "_title", "_artist", "_album", "_genre", "_composer",
                 "_comment", "ipod_path", "track_number", "cd_number", "duration_ms",
                 "has_artwork", "mhii_link", "artwork_size", "play_count", "skip_count",
                 "_offset", "_length", "_strings", "_header", "_record")

    title = _name_property("title")
    artist = _name_property("artist")
    album = _name_property("album")
    genre = _name_property("genre")
    composer = _name_property("composer")
    comment = _name_property("comment")

    def __init__(self):
        self._title = self._artist = self._album = self._genre = None
        self._composer = self._comment = self.ipod_path = None
        self._strings = self._header = self._record = None

    @classmethod
    def from_bytes(cls, raw, offset, intern=sys.intern):
        """Index an mhit record (`raw` = its bytes, `offset` = where it is)."""
        t = cls()
        hl = _u32(raw, 4)
        t.track_id, t.dbid = _u32(raw, 0x10), _u64(raw, 0x70)
        t.duration_ms, t.track_number = _u32(raw, 0x28), _u32(raw, 0x2C)
        t.play_count, t.cd_number = _u32(raw, 0x50), _u32(raw, 0x5C)
        t.artwork_size = _u32(raw, 0x80)
        t.skip_count = _u32(raw, 0x9C) if hl >= 0xF4 else 0
        t.has_artwork = bool(_u8(raw, 0xA4)) if hl >= 0xF4 else False
        t.mhii_link = _u32(raw, 0x160) if hl >= 0x184 else 0
        t._offset, t._length = offset, len(raw)
        pos, end = hl, len(raw)
        for _ in range(_u32(raw, 0x0C)):
            if pos + 24 > end or raw[pos:pos + 4] != b"mhod":
                break
            mhl, mtl, mtype = _u32(raw, pos + 4), _u32(raw, pos + 8), _u32(raw, pos + 12)
            field = _STRING_FIELDS.get(mtype)
            if field:
                body = pos + mhl
                encoding, length = _u32(raw, body), _u32(raw, body + 4)
                text = raw[body + 16:body + 16 + length]
                value = _decode(text, encoding)
                if field == "ipod_path":
                    t.ipod_path = value
                else:
                    if field in ("artist", "album", "genre", "composer"):
                        value = intern(value)
                    setattr(t, "_" + field, value)
            pos += mtl
        return t

    @classmethod
    def from_record(cls, rec):
        """A new track, built by pygpod and not yet saved."""
        from pygpod.db.writer import _write_record
        t = cls.from_bytes(_write_record(rec), None)
        t._record = rec
        return t

    @property
    def duration(self):
        return (self.duration_ms or 0) / 1000.0

    def set_text(self, field, value):
        """Change a name (title, artist, album, genre, comment, composer)."""
        setattr(self, "_" + field, value)
        if self._record is not None:
            from pygpod.model.track import Track
            setattr(Track(self._record), field, value)
        else:
            if self._strings is None:
                self._strings = {}
            self._strings[field] = value

    def set_header(self, offset, fmt, value):
        """Change a number in the record's header (written at save)."""
        if self._header is None:
            self._header = {}
        self._header[offset] = (fmt, value)

    def link_art(self, image_id, size):
        """Point the track at an album-art image, as pygpod does."""
        self.set_header(0x7C, "<H", 1)              # artwork_count
        self.set_header(0x80, "<I", size)           # artwork_size
        self.set_header(0xA4, "<B", 1)              # has_artwork
        self.set_header(0x160, "<I", image_id)      # mhii_link
        self.has_artwork, self.artwork_size, self.mhii_link = True, size, image_id

    def __repr__(self):
        return f"<TrackInfo {self.track_id}: {self.artist} - {self.title}>"


class PlaylistInfo:
    """A playlist (MHSD type 2, mirrored in type 3)."""

    def __init__(self, db, playlist_id, name, is_master=False, podcast=False, track_ids=None):
        self._db = db
        self.playlist_id = playlist_id
        self.name = name
        self.is_master = is_master
        self.is_podcast = podcast
        self.track_ids = track_ids or []
        self.new = False            # made this session: written from track_ids

    @property
    def tracks(self):
        lookup = self._db._by_track_id
        if self.is_master:
            return list(self._db.tracks)
        return [lookup[i] for i in self.track_ids if i in lookup]

    def __repr__(self):
        return f"<PlaylistInfo {self.name!r}: {len(self.track_ids)} tracks>"


class _Section:
    __slots__ = ("type", "offset", "header", "length", "playlists")

    def __init__(self, type_, offset, header, length):
        self.type, self.offset, self.header, self.length = type_, offset, header, length
        self.playlists = None       # type 2/3: [(playlist_id, offset, length, is_master)]


# =============================================================== database ====
class StreamDatabase:
    """The iPod's database, indexed rather than loaded.

    StreamDatabase(mount, source, work_dir): the live player at `mount`,
    read from `source` (a copy of its iTunesDB on the Pi, e.g. the sync's
    snapshot); saves are built in `work_dir`. from_file(path): read only.
    """

    def __init__(self, mountpoint=None, source=None, work_dir=None, config=None,
                 share_art=False, read_only=False):
        from pygpod.model.config import DatabaseConfig
        self._mountpoint = mountpoint
        self._config = config or DatabaseConfig()
        self._rng = random.Random(self._config.random_seed)
        self._device = None
        self._artwork_manager = None
        self.share_art = share_art
        self._art_index = None      # art_key -> image id (shared art)
        self._art_users = None      # image id -> tracks showing it
        self._read_only = read_only
        self._work_dir = work_dir
        self._source = source
        self._removed = set()       # track ids removed since the last save
        self._play_files = False    # play counts merged: delete the files after saving
        self._dirty = False
        self.tracks = []
        self._by_track_id = {}
        self._dbids = set()
        self._playlists = []        # PlaylistInfo, type 2 order (master first)
        self._deleted = set()       # playlist ids deleted since the last save
        self._changed = set()       # playlist ids whose tracks the dock changed
        if mountpoint and not read_only:
            from pygpod.device.device import Device
            self._device = Device.from_mountpoint(mountpoint)
        self._open()

    @classmethod
    def from_file(cls, path, config=None):
        return cls(source=path, config=config, read_only=True)

    # ------------------------------------------------------------- reading ----
    def _open(self):
        if self._source is None:
            raise DatabaseError("No database to read")
        sections, tracks = self._scan(self._source, index_tracks=True)
        if not self._read_only and self._needs_normalizing(sections):
            self._normalize(len(tracks))
            sections, tracks = self._scan(self._source, index_tracks=True)
        self._sections = sections
        self.tracks = tracks
        self._by_track_id = {t.track_id: t for t in tracks}
        self._dbids = {t.dbid for t in tracks}
        self._max_track_id = max(self._by_track_id, default=0)
        self._load_playlists()

    def _scan(self, path, index_tracks):
        """(sections, tracks) of the file at `path`, reading one record at
        a time."""
        sections, tracks = [], []
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] != b"mhbd":
                raise DatabaseError(f"{path} isn't an iTunesDB")
            hl, total = _u32(head, 4), _u32(head, 8)
            f.seek(0)
            self._mhbd = bytearray(f.read(hl))
            pos = hl
            while pos + 16 <= total:
                f.seek(pos)
                head = f.read(16)
                if head[:4] != b"mhsd":
                    break
                shl, stl = _u32(head, 4), _u32(head, 8)
                f.seek(pos)
                sec = _Section(_u32(head, 12), pos, f.read(shl), stl)
                if sec.type == TRACKS and index_tracks:
                    tracks = self._scan_tracks(f, pos + shl)
                elif sec.type in (PLAYLISTS, PODCASTS):
                    sec.playlists = self._scan_playlists(f, pos + shl)
                sections.append(sec)
                pos += stl
        return sections, tracks

    def _scan_tracks(self, f, pos):
        f.seek(pos)
        head = f.read(12)
        if head[:4] != b"mhlt":
            return []
        hl, count = _u32(head, 4), _u32(head, 8)
        pos += hl
        f.seek(pos)
        tracks = []
        for _ in range(count):
            head = f.read(12)
            if head[:4] != b"mhit":
                break
            length = _u32(head, 8)
            raw = head + f.read(length - 12)
            tracks.append(TrackInfo.from_bytes(raw, pos))
            pos += length
        return tracks

    def _scan_playlists(self, f, pos):
        """[(playlist_id, offset, length, is_master, podcast, name, track_ids)]"""
        f.seek(pos)
        head = f.read(12)
        if head[:4] != b"mhlp":
            return []
        hl, count = _u32(head, 4), _u32(head, 8)
        pos += hl
        out = []
        for _ in range(count):
            f.seek(pos)
            head = f.read(12)
            if head[:4] != b"mhyp":
                break
            length = _u32(head, 8)
            raw = head + f.read(length - 12)
            out.append((pos, length) + _read_playlist(raw))
            pos += length
        return out

    def _load_playlists(self):
        sec = self._section(PLAYLISTS)
        self._playlists = []
        for pos, length, pid, is_master, podcast, name, ids in (sec.playlists if sec else []):
            self._playlists.append(PlaylistInfo(self, pid, name, is_master, podcast,
                                                None if is_master else ids))

    def _section(self, type_):
        return next((s for s in self._sections if s.type == type_), None)

    @property
    def playlists(self):
        return list(self._playlists)

    @property
    def master_playlist(self):
        return next((p for p in self._playlists if p.is_master), None)

    @property
    def device(self):
        return self._device

    @property
    def source(self):
        """The Pi's copy of the database as last read or saved."""
        return self._source

    @property
    def mountpoint(self):
        return self._mountpoint

    def get_track(self, track_id):
        return self._by_track_id.get(track_id)

    def _needs_normalizing(self, sections):
        """Databases pygpod didn't write (a fresh iTunes restore) may lack
        sections or use shorter headers than the ones copied here."""
        types = {s.type for s in sections}
        if any(t not in types for t in NEEDED_SECTIONS) or len(self._mhbd) < DEFAULT_MHBD_HEADER_LEN:
            return True
        for s in sections:
            if s.type in (PLAYLISTS, PODCASTS) and not any(p[3] for p in s.playlists or []):
                return True
        return False

    def _normalize(self, count):
        """Let pygpod tidy a small database once (it loads all of it)."""
        if count > NORMALIZE_MAX:
            raise DatabaseError(f"This iPod's database ({count} tracks) wasn't made by "
                                "the dock and is too big to convert on the Pi")
        import pygpod
        db = pygpod.Database.from_file(self._source, self._config)
        db._device = self._device
        data = db._prepare_for_save()
        tmp = self._source + ".normalized"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self._source)

    # ------------------------------------------------------------ changing ----
    def add_track(self, filepath, **kwargs):
        """Copy a file onto the iPod and add it (as pygpod's add_track)."""
        from pygpod.device.fileutils import copy_track_to_ipod
        from pygpod.exceptions import TrackError
        from pygpod.tags import check_format_supported
        if not os.path.isfile(filepath):
            raise TrackError(f"File not found: {filepath}")
        check_format_supported(filepath)
        cfg = self._config
        ipod_path = copy_track_to_ipod(
            filepath, self._mountpoint, self._device.music_dirs if self._device else 50,
            prefix=cfg.filename_prefix, rand_len=cfg.filename_rand_len,
            charset=cfg.filename_charset, rng=self._rng)
        dest = os.path.join(self._mountpoint, ipod_path.lstrip(":").replace(":", os.sep))
        _fsync_file(dest)          # on the player for sure before it's logged as copied
        _fsync_dir(os.path.dirname(dest))
        return self._add(filepath, ipod_path, kwargs)

    def add_existing(self, ipod_path, **kwargs):
        """Add a file that is already on the iPod (copied before a crash,
        never saved into the database): no copy, tags and art read from it."""
        filepath = os.path.join(self._mountpoint, ipod_path.lstrip(":").replace(":", os.sep))
        return self._add(filepath, ipod_path, kwargs)

    def _add(self, filepath, ipod_path, overrides):
        from pygpod.tags import read_tags
        tags = read_tags(filepath)
        tags.update(overrides)
        track_id = max(self._max_track_id + 1, self._config.track_id_start)
        while True:
            dbid = self._rng.getrandbits(64)
            if dbid and dbid not in self._dbids:
                break
        mhit = _builder(self)._create_mhit(track_id, dbid, tags, ipod_path, filepath)
        t = TrackInfo.from_record(mhit)
        self._max_track_id = track_id
        self._dbids.add(dbid)
        self.tracks.append(t)
        self._by_track_id[track_id] = t
        self.add_artwork(t, filepath, tags.get("cover_art"))
        self._dirty = True
        return t

    def remove_track(self, track, delete_file=False):
        """Remove a track (and its album art, unless another track shows it)."""
        if track.track_id not in self._by_track_id:
            return
        self.tracks.remove(track)
        del self._by_track_id[track.track_id]
        self._dbids.discard(track.dbid)
        if track._record is None:
            self._removed.add(track.track_id)
        for pl in self._playlists:
            if not pl.is_master and track.track_id in pl.track_ids:
                pl.track_ids = [i for i in pl.track_ids if i != track.track_id]
                self._changed.add(pl.playlist_id)
        if self._mountpoint and track.has_artwork and track.mhii_link:
            self._drop_art(track)
        if delete_file and self._mountpoint and track.ipod_path:
            try:
                os.remove(os.path.join(self._mountpoint,
                                       track.ipod_path.lstrip(":").replace(":", os.sep)))
            except OSError:
                pass
        self._dirty = True

    def create_playlist(self, name):
        pl = PlaylistInfo(self, self._rng.getrandbits(64), name)
        pl.new = True
        self._playlists.append(pl)
        self._dirty = True
        return pl

    def delete_playlist(self, playlist):
        if playlist.is_master:
            raise DatabaseError("Cannot delete the master playlist")
        self._playlists = [p for p in self._playlists if p is not playlist]
        if not playlist.new:
            self._deleted.add(playlist.playlist_id)
        self._dirty = True

    def add_track_to_playlist(self, playlist, track):
        playlist.track_ids.append(track.track_id)
        self._changed.add(playlist.playlist_id)
        self._dirty = True

    def merge_play_counts(self, entries):
        """Add the iPod's play counts (entries by track position, as read
        from Play Counts) to the tracks, as pygpod does when it opens a
        database. The play files are deleted after the next save."""
        for entry, t in zip(entries, self.tracks):
            if entry.play_count > 0:
                t.play_count += entry.play_count
                t.set_header(0x50, "<I", t.play_count)
            if entry.time_played > 0:
                t.set_header(0x58, "<I", entry.time_played)
            if entry.rating >= 0:
                t.set_header(0x1F, "<B", min(entry.rating, 255))
            if entry.skip_count > 0:
                t.skip_count += entry.skip_count
                t.set_header(0x9C, "<I", t.skip_count)
            if entry.time_skipped > 0:
                t.set_header(0xA0, "<I", entry.time_skipped)
            if entry.bookmark_time > 0:
                t.set_header(0x6C, "<I", entry.bookmark_time)
        self._play_files = True
        self._dirty = True

    # ----------------------------------------------------------- album art ----
    def artwork_manager(self):
        if self._artwork_manager is None:
            from pygpod.device.models import IpodGeneration
            from pygpod.model.artwork_manager import ArtworkManager
            gen = self._device.generation if self._device else IpodGeneration.UNKNOWN
            self._artwork_manager = ArtworkManager(self._mountpoint, generation=gen)
        return self._artwork_manager

    def images(self):
        """{image_id: mhii record} in the ArtworkDB ({} if it has none yet)."""
        mgr = self.artwork_manager()
        if mgr._root is None:
            return {}
        return {m.fields.get("image_id"): m for m in mgr._get_or_create_mhli().children}

    def art_users(self):
        """{image_id: number of tracks showing it} (built once, then kept)."""
        if self._art_users is None:
            self._art_users = {}
            for t in self.tracks:
                if t.has_artwork and t.mhii_link:
                    self._art_users[t.mhii_link] = self._art_users.get(t.mhii_link, 0) + 1
        return self._art_users

    def link_art(self, track, image_id, size):
        """Point `track` at an image (counted for removals)."""
        users = self.art_users()
        if track.has_artwork and track.mhii_link:
            users[track.mhii_link] = users.get(track.mhii_link, 1) - 1
        track.link_art(image_id, size)
        users[image_id] = users.get(image_id, 0) + 1
        self._dirty = True

    def art_index(self):
        """{art_key: image_id} for art already on the player (built once)."""
        if self._art_index is None:
            images = self.images()
            self._art_index = {}
            for t in self.tracks:
                if t.has_artwork and t.mhii_link in images:
                    self._art_index.setdefault(art_key(t.album, t.artwork_size), t.mhii_link)
        return self._art_index

    def add_artwork(self, track, filepath, cover_art=None):
        """Give a track the cover embedded in its file (or `cover_art`):
        the album's existing image when art is shared, else a new one."""
        if not self._mountpoint:
            return False
        from pygpod.tags import extract_artwork
        try:
            art = extract_artwork(filepath)
            if not art and cover_art and os.path.isfile(cover_art):
                with open(cover_art, "rb") as f:
                    art = f.read()
        except Exception:
            art = None
        if not art:
            return False
        key = art_key(track.album, len(art))
        if self.share_art:
            image = self.art_index().get(key)
            if image is not None and image in self.images():
                self.link_art(track, image, len(art))
                return True
        image = self.artwork_manager().add_artwork_data(track.dbid, art, save=False)
        if image is None:
            return False
        self.link_art(track, image, len(art))
        if self.share_art:
            self.art_index()[key] = image
        self._dirty = True
        return True

    def _drop_art(self, track):
        """`track` (already out of the list) no longer shows its image."""
        link = track.mhii_link
        users = self.art_users()
        users[link] = users.get(link, 1) - 1
        if users[link] > 0:
            # Still shown by other tracks: keep the image, and hand it to one
            # of them if this track owned it.
            mhii = self.images().get(link)
            if mhii is not None and mhii.fields.get("song_id") == track.dbid:
                other = next(t for t in self.tracks if t.has_artwork and t.mhii_link == link)
                set_image_owner(mhii, other.dbid)
            return
        del users[link]
        if self._art_index:
            for k in [k for k, v in self._art_index.items() if v == link]:
                del self._art_index[k]
        self.artwork_manager().remove_artwork(link)

    # -------------------------------------------------------------- saving ----
    def save(self):
        """Write the database to the iPod: built on the Pi, signed, then
        copied to a temporary name on the player and renamed into place."""
        if self._read_only or not self._mountpoint:
            raise DatabaseError("This database was opened read-only")
        if self._artwork_manager is not None:
            self._artwork_manager.save()            # first, as pygpod does
        out = os.path.join(self._work_dir, "iTunesDB.new")
        self.write_to(out)
        dest = os.path.join(self._mountpoint, "iPod_Control", "iTunes", "iTunesDB")
        tmp = dest + ".musicdock-new"
        with open(out, "rb") as src, open(tmp, "wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, dest)
        _fsync_dir(os.path.dirname(dest))          # the rename itself
        base = os.path.join(self._work_dir, "iTunesDB.current")
        os.replace(out, base)
        self._source = base
        if self._play_files:
            for name in PLAY_FILES:
                path = os.path.join(self._mountpoint, "iPod_Control", "iTunes", name)
                if os.path.isfile(path):
                    os.remove(path)
            self._play_files = False
        self._dirty = False

    def write_to(self, path):
        """Write the database with every change so far to `path` (signed
        when the player needs it), then read on from that file."""
        final = list(self.tracks)
        albums, artists = {}, {}
        for t in final:
            albums.setdefault((t.album or "", t.artist or ""), len(albums) + 1)
            if t.artist and t.artist not in artists:
                artists[t.artist] = len(artists) + 1
        new_offsets = {}
        with open(self._source, "rb") as src, open(path, "wb") as out:
            mhbd = bytearray(self._mhbd)
            out.write(mhbd)
            count = 0
            for sec in self._sections:
                start = out.tell()
                out.write(sec.header)
                if sec.type == TRACKS:
                    self._write_tracks(src, out, final, albums, artists, new_offsets)
                elif sec.type in (PLAYLISTS, PODCASTS):
                    self._write_playlists(src, out, sec, final)
                elif sec.type == ALBUMS:
                    _write_albums(out, albums, self._config.string_encoding)
                elif sec.type == ARTISTS:
                    _write_artists(out, artists, self._config.string_encoding)
                else:
                    _copy(src, out, sec.offset + len(sec.header), sec.length - len(sec.header))
                end = out.tell()
                out.seek(start + 8)
                out.write(struct.pack("<I", end - start))
                out.seek(end)
                count += 1
            total = out.tell()
            struct.pack_into("<I", mhbd, 8, total)
            struct.pack_into("<I", mhbd, 0x14, count)
            if self._device and self._device.db_version > _u32(mhbd, 0x10):
                struct.pack_into("<I", mhbd, 0x10, self._device.db_version)
            out.seek(0)
            out.write(mhbd)
            out.flush()
            os.fsync(out.fileno())
        if self._device and self._device.requires_hash:
            sign_hash58(path, self._device.firewire_guid)
        # The new file is the source from now on.
        for t in final:
            t._offset, t._length = new_offsets[t.track_id]
            t._strings = t._header = t._record = None
        self._removed.clear()
        self._deleted.clear()
        self._changed.clear()
        for p in self._playlists:
            p.new = False
        self._source = path
        self._sections, _ = self._scan(path, index_tracks=False)

    def _write_tracks(self, src, out, final, albums, artists, new_offsets):
        from pygpod.db.parser import _parse_mhit
        from pygpod.db.writer import _write_record
        from pygpod.model.track import Track
        sec = self._section(TRACKS)
        src.seek(sec.offset + len(sec.header))
        head = src.read(12)
        mhlt = bytearray(head + src.read(_u32(head, 4) - 12)) if head[:4] == b"mhlt" \
            else _basic_header(b"mhlt", DEFAULT_MHLT_HEADER_LEN)
        struct.pack_into("<I", mhlt, 8, len(final))
        out.write(mhlt)
        for t in final:
            if t._record is not None:
                raw = _write_record(t._record)
            else:
                src.seek(t._offset)
                raw = src.read(t._length)
                if t._strings:
                    rec = _parse_mhit(raw, 0)
                    track = Track(rec)
                    for field, value in t._strings.items():
                        setattr(track, field, value)
                    raw = _write_record(rec)
            raw = bytearray(raw)
            hl = _u32(raw, 4)
            for offset, (fmt, value) in (t._header or {}).items():
                if offset + struct.calcsize(fmt) <= hl:
                    struct.pack_into(fmt, raw, offset, value)
            album_id = albums.get((t.album or "", t.artist or ""), 0)
            if album_id and hl > 0x124:
                struct.pack_into("<I", raw, 0x120, album_id)
            artist_id = artists.get(t.artist or "", 0)
            if artist_id and hl > 0x1E4:
                struct.pack_into("<I", raw, 0x1E0, artist_id)
            new_offsets[t.track_id] = (out.tell(), len(raw))
            out.write(raw)

    def _write_playlists(self, src, out, sec, final):
        head = _read_at(src, sec.offset + len(sec.header), 12)
        mhlp = bytearray(head + src.read(_u32(head, 4) - 12)) if head[:4] == b"mhlp" \
            else _basic_header(b"mhlp", DEFAULT_MHLP_HEADER_LEN)
        old = {p[2]: p for p in sec.playlists or []}
        wanted = [p for p in self._playlists]
        count_at = out.tell() + 8
        out.write(mhlp)
        count = 0
        # Playlists in this section, in their order, then new ones; a
        # section's own extras (e.g. a podcast list only in type 3) stay.
        order = [p[2] for p in sec.playlists or []]
        by_id = {p.playlist_id: p for p in wanted}
        for pid in order + [p.playlist_id for p in wanted if p.playlist_id not in old]:
            if pid in self._deleted:
                continue
            pl = by_id.get(pid)
            entry = old.get(pid)
            if entry and entry[3]:                                  # master
                self._write_master(src, out, entry, final)
            elif pl is not None and (pl.new or pid in self._changed):
                if entry:
                    _write_playlist(out, _playlist_head(_read_at(src, entry[0], entry[1])),
                                    pl.track_ids)
                else:
                    _write_playlist(out, _new_playlist_head(self, pl.name, pid), pl.track_ids)
            elif entry:
                raw = _read_at(src, entry[0], entry[1])
                if self._removed and any(i in self._removed for i in entry[6]):
                    raw = _without_tracks(raw, self._removed)
                out.write(raw)
            else:
                continue
            count += 1
        end = out.tell()
        out.seek(count_at)
        out.write(struct.pack("<I", count))
        out.seek(end)

    def _write_master(self, src, out, entry, final):
        head = _playlist_head(_read_at(src, entry[0], entry[1]),
                              drop=(MHOD_ID_LIBPLAYLISTINDEX, MHOD_ID_LIBPLAYLISTJUMPTABLE))
        header, mhods = head
        start = out.tell()
        out.write(header)
        for m in mhods:
            out.write(m)
        sort_mhods = list(_sort_mhods(final))
        for m in sort_mhods:
            out.write(m)
        for pos, t in enumerate(final):
            out.write(_mhip(t.track_id, pos))
        _finish_playlist(out, start, len(mhods) + len(sort_mhods), len(final))


# ================================================================== files ====
def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path):
    """Make a folder's entries (a new file, a rename) durable. Not every
    system can fsync a folder (macOS, some file systems): best effort."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ============================================================ record bytes ====
def _read_at(f, offset, length):
    f.seek(offset)
    return f.read(length)


def _copy(src, out, offset, length):
    src.seek(offset)
    while length > 0:
        chunk = src.read(min(CHUNK, length))
        if not chunk:
            break
        out.write(chunk)
        length -= len(chunk)


def _basic_header(magic, length):
    header = bytearray(length)
    header[0:4] = magic
    struct.pack_into("<I", header, 4, length)
    return header


def _mhod(mhod_type, body):
    return struct.pack("<4sIII", b"mhod", DEFAULT_MHOD_HEADER_LEN,
                       DEFAULT_MHOD_HEADER_LEN + len(body), mhod_type) + bytes(8) + body


def _mhip(track_id, position):
    """A playlist entry with its position (as pygpod's _create_mhip)."""
    order = _mhod(MHOD_ID_PLAYLIST, struct.pack("<I", position) + bytes(16))
    header = bytearray(DEFAULT_MHIP_HEADER_LEN)
    struct.pack_into("<4sIII", header, 0, b"mhip", DEFAULT_MHIP_HEADER_LEN,
                     DEFAULT_MHIP_HEADER_LEN + len(order), 1)
    struct.pack_into("<I", header, 0x18, track_id)
    return bytes(header) + order


def _read_playlist(raw):
    """(playlist_id, is_master, podcast, name, track_ids) of an mhyp."""
    hl = _u32(raw, 4)
    nmhods, nmhips = _u32(raw, 0x0C), _u32(raw, 0x10)
    pid = _u64(raw, 0x1C)
    is_master = raw[0x14] == 1
    podcast = hl >= 0x2C and _u16(raw, 0x2A) == 1
    name, pos = "", hl
    for _ in range(nmhods):
        if raw[pos:pos + 4] != b"mhod":
            break
        mhl, mtl = _u32(raw, pos + 4), _u32(raw, pos + 8)
        if _u32(raw, pos + 12) == MHOD_ID_TITLE:
            body = pos + mhl
            text = raw[body + 16:body + 16 + _u32(raw, body + 4)]
            name = _decode(text, _u32(raw, body))
        pos += mtl
    ids = []
    for _ in range(nmhips):
        if raw[pos:pos + 4] != b"mhip":
            break
        ids.append(_u32(raw, pos + 0x18))
        pos += _u32(raw, pos + 8)
    return pid, is_master, podcast, name, ids


def _playlist_head(raw, drop=()):
    """(header, [mhod bytes]) of an mhyp, without its entries."""
    hl = _u32(raw, 4)
    header = bytearray(raw[:hl])
    mhods, pos = [], hl
    for _ in range(_u32(raw, 0x0C)):
        if raw[pos:pos + 4] != b"mhod":
            break
        mtl = _u32(raw, pos + 8)
        if _u32(raw, pos + 12) not in drop:
            mhods.append(raw[pos:pos + mtl])
        pos += mtl
    return header, mhods


def _new_playlist_head(db, name, pid):
    from pygpod.db.writer import _write_record
    rec = _builder(db)._create_mhyp(name, pid, is_master=False)
    raw = _write_record(rec)
    return _playlist_head(raw)


def _write_playlist(out, head, track_ids):
    header, mhods = head
    start = out.tell()
    out.write(header)
    for m in mhods:
        out.write(m)
    for pos, track_id in enumerate(track_ids):
        out.write(_mhip(track_id, pos))
    _finish_playlist(out, start, len(mhods), len(track_ids))


def _finish_playlist(out, start, nmhods, nmhips):
    end = out.tell()
    out.seek(start + 8)
    out.write(struct.pack("<III", end - start, nmhods, nmhips))
    out.seek(end)


def _without_tracks(raw, removed):
    """An mhyp minus its entries for `removed` track ids."""
    header, mhods = _playlist_head(raw)
    pos = len(header) + sum(len(m) for m in mhods)
    kept = []
    while pos < len(raw) and raw[pos:pos + 4] == b"mhip":
        length = _u32(raw, pos + 8)
        if _u32(raw, pos + 0x18) not in removed or _u32(raw, pos + 0x18) == 0:
            kept.append(raw[pos:pos + length])
        pos += length
    body = b"".join(mhods) + b"".join(kept)
    struct.pack_into("<III", header, 8, len(header) + len(body), len(mhods), len(kept))
    return bytes(header) + body


def _sort_mhods(tracks):
    """The master playlist's browse lists: for each key a sort index (52)
    and a letter jump table (53). Ties go by album, disc and track, as in
    iTunes/libgpod (pygpod's own sort left an album's tracks in add order)."""
    for sort_key, field in SORT_KEYS:
        def key(i):
            t = tracks[i]
            album, artist, title = _sort_text(t.album), _sort_text(t.artist), _sort_text(t.title)
            numbers = (t.cd_number or 0, t.track_number or 0)
            if sort_key == 4:
                return (album, *numbers, title)
            if sort_key == 5:
                return (artist, album, *numbers, title)
            if sort_key == 7:
                return (_sort_text(t.genre), artist, album, *numbers, title)
            return (_sort_text(getattr(t, field)), title)
        order = sorted(range(len(tracks)), key=key)
        body = bytearray(48 + 4 * len(order))
        struct.pack_into("<II", body, 0, sort_key, len(order))
        struct.pack_into(f"<{len(order)}I", body, 48, *order)
        yield _mhod(MHOD_ID_LIBPLAYLISTINDEX, bytes(body))
        groups = []
        for i, idx in enumerate(order):
            value = _sort_text(getattr(tracks[idx], field))
            letter = ord(value[0]) if value else 0
            if groups and groups[-1][0] == letter:
                groups[-1][2] += 1
            else:
                groups.append([letter, i, 1])
        body = bytearray(16 + 12 * len(groups))
        struct.pack_into("<II", body, 0, sort_key, len(groups))
        for j, (letter, start, n) in enumerate(groups):
            struct.pack_into("<III", body, 16 + 12 * j, letter, start, n)
        yield _mhod(MHOD_ID_LIBPLAYLISTJUMPTABLE, bytes(body))


def _write_albums(out, albums, encoding):
    """Section 4 (as pygpod's _rebuild_album_list)."""
    from pygpod.db.writer import make_string_mhod, _write_record
    header = _basic_header(b"mhla", DEFAULT_MHLA_HEADER_LEN)
    struct.pack_into("<I", header, 8, len(albums))
    out.write(header)
    for (album, artist), album_id in albums.items():
        children = []
        if album:
            children.append(_write_record(make_string_mhod(MHOD_ID_ALBUM_ALBUM, album, encoding)))
        if artist:
            children.append(_write_record(make_string_mhod(MHOD_ID_ALBUM_ARTIST, artist, encoding)))
        mhia = _basic_header(b"mhia", MHIA_ALBUM_HEADER_LEN)
        body = b"".join(children)
        struct.pack_into("<III", mhia, 8, MHIA_ALBUM_HEADER_LEN + len(body), len(children),
                         album_id)
        out.write(bytes(mhia) + body)


def _write_artists(out, artists, encoding):
    """Section 8 (as pygpod's _rebuild_artist_list)."""
    from pygpod.db.writer import make_string_mhod, _write_record
    header = _basic_header(b"mhli", DEFAULT_MHLI_HEADER_LEN)
    struct.pack_into("<I", header, 8, len(artists))
    out.write(header)
    for name, artist_id in artists.items():
        body = _write_record(make_string_mhod(MHOD_ID_ALBUM_ARTIST_MHII, name, encoding))
        mhii = _basic_header(b"mhii", DEFAULT_MHII_HEADER_LEN)
        struct.pack_into("<III", mhii, 8, DEFAULT_MHII_HEADER_LEN + len(body), 1, artist_id)
        out.write(bytes(mhii) + body)


def _builder(db):
    """A pygpod Database with no tracks, for its record builders."""
    b = getattr(db, "_pygpod", None)
    if b is None:
        import pygpod
        b = pygpod.Database(config=db._config)
        b._rng = db._rng
        b._device = db._device
        db._pygpod = b
    return b


def set_image_owner(mhii, dbid):
    """The track an image records as its own (mhii song_id)."""
    header = bytearray(mhii.raw_header)
    struct.pack_into("<Q", header, 0x14, dbid)
    mhii.raw_header = bytes(header)
    mhii.fields["song_id"] = dbid


def sign_hash58(path, guid):
    """Write the hash58 signature of the file at `path` (HMAC-SHA1 over
    the file with its database ID and the signature zeroed, as pygpod and
    libgpod compute it), reading it in chunks."""
    from pygpod.hash.hash58 import _derive_key
    mac = hmac.new(_derive_key(guid), digestmod=hashlib.sha1)
    with open(path, "r+b") as f:
        first = bytearray(f.read(CHUNK))
        first[DB_ID] = bytes(8)
        first[HASH58_AT:HASH58_AT + HASH58_LEN] = bytes(HASH58_LEN)
        mac.update(first)
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            mac.update(chunk)
        f.seek(HASH58_AT)
        f.write(mac.digest())
        f.flush()
        os.fsync(f.fileno())
