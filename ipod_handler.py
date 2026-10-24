"""
ipod_handler.py - the stock-firmware iPod handler (pygpod; needs the venv).

One sync session (sync_player): snapshot the play data and queue it, send
it, then - in write mode - make sure SysInfo is complete, back up the
database, reconcile the record with the iPod, and apply the prepare job's
plan: removals, additions in batches of 25 (the database saved after each),
names and genres, album art, playlists. Also here: the iPod's browse-list
order, one album-art image per album (Classic / nano 3G-4G), a memory-safe
album-art compaction that a failed sync or crash can't leave half-done,
and the copy of the record kept on the iPod.

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

import gc
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


def plays_from_snapshot(workdir, record, guid, now=None, tracks=None):
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
    if tracks is None:        # the snapshot's tracks, if the caller has them loaded
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
    0x1209: "xA002",       # iPod video (5th/5.5th generation)
    0x120A: "xA004",       # iPod nano 1st generation
    0x1260: "xA426",       # iPod nano 2nd generation
    0x1261: "xB029",       # iPod classic (all three generations)
    0x1262: "xA980",       # iPod nano 3rd generation
    0x1263: "xB903",       # iPod nano 4th generation
}
# Generations the dock writes to (pygpod IpodGeneration names). The firmware
# of the first group rejects a database without a hash58 signature; the
# second checks no signature. pygpod 0.0.6 signs those too (libgpod
# doesn't); the extra header bytes are ignored. Nano 5G (hash72), nano 6G/7G
# (hashAB), shuffles, touch and pre-video iPods are refused.
SIGNED_GENERATIONS = {"CLASSIC_1", "CLASSIC_2", "CLASSIC_3", "NANO_3", "NANO_4"}
UNSIGNED_GENERATIONS = {"VIDEO_1", "VIDEO_2", "NANO_1", "NANO_2"}
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


def model_from_serial(block_device, read_serial=None):
    """("x<model>", serial) from the iPod's real serial number, or None.

    The USB serial is the FireWire GUID; the real serial comes from SCSI VPD
    page 0x80 (needs root - the sync job has it). Its last 3 characters name
    the exact model (libgpod's table, via pygpod)."""
    try:
        from pygpod.device.models import lookup_model_by_serial
        from pygpod.device.usb import _read_scsi_serial
    except ImportError:
        return None
    try:
        serial = (read_serial or _read_scsi_serial)(block_device)
    except Exception as e:
        log_info(f"Couldn't read the iPod's serial number: {e}")
        return None
    info = lookup_model_by_serial(serial) if serial else None
    if info and info.model_number and info.model_number != "Unknown":
        return "x" + info.model_number, serial
    return None


def ensure_sysinfo(mount, guid, record, usb_product=None, block_device=None,
                   read_serial=None):
    """Make sure SysInfo has the model number and FireWire GUID.

    Without them pygpod can't tell the player needs a signed database and
    writes one the iPod rejects. A factory restore empties SysInfo, so the
    model is remembered in the record per GUID. Returns the model used.
    """
    values, lines = read_sysinfo(mount)
    known = record.player(guid) or {}
    model = values.get("ModelNumStr") or known.get("model")
    if not model and block_device:
        found = model_from_serial(block_device, read_serial)
        if found:
            model = found[0]
            log_info(f"iPod model {model} worked out from its serial number ({found[1]})")
    model = model or REPRESENTATIVE_MODELS.get(usb_product)
    if not model:
        log_info("Unknown iPod model: SysInfo is empty, the serial number didn't help, "
                 "and this player hasn't been seen before. Add a line "
                 f"'ModelNumStr: x<model>' to {sysinfo_path(mount)} (model from "
                 "Settings > About, e.g. MB903 -> xB903), then dock again.")
        raise HandlerError("Unknown iPod model (see the guide)")
    check_supported(model)
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


def check_supported(model):
    """Raise HandlerError unless the dock can write this model's database.
    Runs before SysInfo or anything else on the player is changed."""
    from pygpod.device.models import get_generation_name, lookup_model
    gen = lookup_model(model).generation
    if gen.name in SIGNED_GENERATIONS | UNSIGNED_GENERATIONS:
        return
    if gen.name == "UNKNOWN":
        log_info(f"Model {model} isn't in pygpod's table; can't tell how to "
                 "sign its database")
        raise HandlerError(f"Unknown iPod model {model} (see the guide)")
    log_info(f"{get_generation_name(gen)} ({model}) isn't supported: its database "
             "needs a signature the dock can't make, or a different format")
    raise HandlerError(f"{get_generation_name(gen)} isn't supported")


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
    undo_compaction(mount)            # the backup's ArtworkDB points into the old art
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
# The iPod browses by pre-sorted lists stored in the iTunesDB. pygpod 0.0.6
# sorts each by one field (album, artist, ...) and leaves ties in the order
# tracks were added, so an album's tracks came out in add order: a title
# track added first (when its album name still differed) showed as track 1
# (2026-10-19). Ties are broken like iTunes/libgpod: by album, disc, track.
SORT_ORDER_VERSION = "1"
SORT_ORDER_MARKER = os.path.join("iPod_Control", "musicdock", "sort-order")


def _sort_text(value):
    value = (value or "").upper()                  # as pygpod's own sort does
    return value[4:] if value.startswith("THE ") else value


def _master_track_ids(db):
    """Track IDs of the master playlist, in its order (pygpod internals)."""
    for mhsd in db._root.children:
        if mhsd.fields.get("mhsd_type") in (2, 3) and mhsd.children:
            for mhyp in mhsd.children[0].children:
                if mhyp.fields.get("playlist_type") == 1:
                    return [c.fields.get("track_id") for c in mhyp.children
                            if c.magic == b"mhip"]
    return []


def install_sort_order():
    """Make pygpod's browse lists order an album's tracks by disc and track.

    pygpod sorts each list with a stable sort on one field, so handing it the
    tracks already ordered by the full key gives the right result. Applied
    once per process; checked_save calls it before every save."""
    from pygpod.model import database as pdb
    original = pdb.Database._build_sort_pair
    if getattr(original, "_musicdock", False):
        return

    def build_sort_pair(self, sort_key, field_name, tracks_meta):
        ids = _master_track_ids(self)

        def numbers(meta):
            i = meta["index"]
            t = self._track_lookup.get(ids[i]) if i < len(ids) else None
            return (t.cd_number or 0, t.track_number or 0) if t else (0, 0)

        def key(meta):
            album, artist = _sort_text(meta["album"]), _sort_text(meta["artist"])
            title = _sort_text(meta["title"])
            if sort_key == 4:                                   # albums
                return (album, *numbers(meta), title)
            if sort_key == 5:                                   # artists
                return (artist, album, *numbers(meta), title)
            if sort_key == 7:                                   # genres
                return (_sort_text(meta["genre"]), artist, album, *numbers(meta), title)
            return (_sort_text(meta[field_name]), title)
        return original(self, sort_key, field_name, sorted(tracks_meta, key=key))

    build_sort_pair._musicdock = True
    pdb.Database._build_sort_pair = build_sort_pair


def sort_order_outdated(mount):
    """True if the player's browse lists were last written without the
    disc/track ordering (so the next write sync re-saves them once)."""
    try:
        with open(os.path.join(mount, SORT_ORDER_MARKER)) as f:
            return f.read().strip() != SORT_ORDER_VERSION
    except OSError:
        return True


def mark_sort_order(mount):
    path = os.path.join(mount, SORT_ORDER_MARKER)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(SORT_ORDER_VERSION + "\n")


# pygpod 0.0.6 compacts the album-art files (.ithmb, one per thumbnail
# size) after any artwork removal by reading every image of a size into
# memory - 169 MB per size on the Nano with 1465 tracks, which ran the Pi
# Zero 2 W out of memory mid-save (2026-10-23) - and rewriting all of them
# (460 MB over USB, minutes per sync). Instead: copy one image at a time to
# a new file, swap it in, and only once removed art leaves this share of a
# file unused (until then the gaps just stay; new art goes at the end).
COMPACT_WASTE = 0.25


def install_artwork_compaction():
    """Replace pygpod's .ithmb compaction (see COMPACT_WASTE). Applied once
    per process; checked_save calls it before every save."""
    from pygpod.model import artwork_manager as am
    if getattr(am.ArtworkManager._rebuild_ithmb, "_musicdock", False):
        return

    def rebuild_ithmb(self):
        by_format = {}                      # format id -> [(mhni, offset, size)]
        for mhii in self._get_or_create_mhli().children:
            for child in mhii.children:
                if child.magic == am.MHNI_MAGIC:
                    mhnis = [child]
                elif child.magic == am.MHOD_MAGIC:
                    mhnis = [c for c in child.children if c.magic == am.MHNI_MAGIC]
                else:
                    continue
                for mhni in mhnis:
                    fmt, size = mhni.fields.get("format_id"), mhni.fields.get("image_size", 0)
                    if fmt is not None and size > 0:
                        by_format.setdefault(fmt, []).append(
                            (mhni, mhni.fields.get("ithumb_offset", 0), size))
        for fmt, entries in by_format.items():
            path = os.path.join(self._artwork_dir, f"F{fmt}_1.ithmb")
            if not os.path.isfile(path):
                continue
            total = os.path.getsize(path)
            if total - sum(size for _, _, size in entries) <= total * COMPACT_WASTE:
                continue                    # a few gaps: not worth rewriting the file
            tmp, moved, offset = path + COMPACT_SUFFIX, [], 0
            with open(path, "rb") as src, open(tmp, "wb") as dst:
                for mhni, old, size in entries:      # one image in memory at a time
                    src.seek(old)
                    dst.write(src.read(size))
                    moved.append((mhni, offset))
                    offset += size
                dst.flush()
                os.fsync(dst.fileno())
            for mhni, new in moved:      # the ArtworkDB about to be written uses these
                header = bytearray(mhni.raw_header)
                am.put32lint(header, 0x14, new)
                mhni.raw_header = bytes(header)
                mhni.fields["ithumb_offset"] = new
            self._ithmb_offsets[fmt] = offset
            self._musicdock_swaps = getattr(self, "_musicdock_swaps", []) + [(path, tmp)]
            log_info(f"Compacted album art (format {fmt}): {total / 1e6:.0f} -> "
                     f"{offset / 1e6:.0f} MB")
        if os.path.isdir(self._artwork_dir):        # sizes with no art left (as pygpod)
            for name in os.listdir(self._artwork_dir):
                parts = name[1:].replace(".ithmb", "").split("_")
                if name.startswith("F") and name.endswith(".ithmb") and len(parts) == 2:
                    try:
                        if int(parts[0]) not in by_format:
                            os.unlink(os.path.join(self._artwork_dir, name))
                            self._ithmb_offsets.pop(int(parts[0]), None)
                    except ValueError:
                        pass

    def save(self):
        """pygpod's save, with compacted files swapped in together with the
        ArtworkDB that matches them; the old files stay as .before-compact
        until the sync succeeds (see finish_compaction / undo_compaction)."""
        if self._root is None:
            return
        if getattr(self, "_needs_rebuild", False):
            self._rebuild_ithmb()
            self._needs_rebuild = False
        data = am.write_artworkdb(self._root)
        tmp = self._artworkdb_path + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for path, compacted in getattr(self, "_musicdock_swaps", []):
            os.replace(path, path + BEFORE_COMPACT)
            os.replace(compacted, path)
        self._musicdock_swaps = []
        os.replace(tmp, self._artworkdb_path)

    # pygpod's ArtworkDB writer emits a record's original bytes when it has
    # no children - right for leaves, wrong for lists: an emptied image list
    # was written back with its old entries, so the last image ever removed
    # came back, pointing into deleted .ithmb files. Empty lists are written
    # as their header with a count of 0.
    from pygpod.db import artwork_writer as aw
    original_write = aw._write_artwork_record

    def write_artwork_record(rec):
        if not rec.children and rec.magic in aw._COUNT_MAGICS:
            header = bytearray(rec.raw_header)
            aw.put32lint(header, 8, 0)
            return bytes(header)
        return original_write(rec)

    aw._write_artwork_record = write_artwork_record

    rebuild_ithmb._musicdock = True
    am.ArtworkManager._rebuild_ithmb = rebuild_ithmb
    am.ArtworkManager.save = am.ArtworkManager._save = save


COMPACT_SUFFIX = ".compact"
BEFORE_COMPACT = ".before-compact"


def _artwork_dir(mount):
    return os.path.join(mount, "iPod_Control", "Artwork")


def _ithmb_leftovers(mount, suffix):
    folder = _artwork_dir(mount)
    try:
        return [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(suffix)]
    except OSError:
        return []


def finish_compaction(mount):
    """The sync succeeded: the art from before compaction can go."""
    for old in _ithmb_leftovers(mount, BEFORE_COMPACT):
        os.remove(old)


def undo_compaction(mount):
    """The sync failed and its backup ArtworkDB is going back: put back the
    art files it points into."""
    for old in _ithmb_leftovers(mount, BEFORE_COMPACT):
        os.replace(old, old[:-len(BEFORE_COMPACT)])
    for tmp in _ithmb_leftovers(mount, COMPACT_SUFFIX):
        os.remove(tmp)


def art_files_consistent(mount):
    """True if every image the ArtworkDB lists lies within its .ithmb file."""
    from pygpod.db.artwork_parser import parse_artworkdb
    path = os.path.join(_artwork_dir(mount), "ArtworkDB")
    try:
        with open(path, "rb") as f:
            root = parse_artworkdb(f.read())
    except (OSError, ValueError):
        return False
    sizes = {}

    def walk(record):
        for child in getattr(record, "children", None) or []:
            if child.magic == b"mhni":
                fmt = child.fields.get("format_id")
                if fmt not in sizes:
                    try:
                        sizes[fmt] = os.path.getsize(
                            os.path.join(_artwork_dir(mount), f"F{fmt}_1.ithmb"))
                    except OSError:
                        sizes[fmt] = -1
                end = child.fields.get("ithumb_offset", 0) + child.fields.get("image_size", 0)
                if end > sizes[fmt]:
                    raise ValueError(fmt)
            walk(child)
    try:
        walk(root)
        return True
    except ValueError:
        return False


def recover_compaction(mount):
    """At the start of a sync: art files left over from a compaction that a
    crash interrupted. Keep whichever set the ArtworkDB on the player fits."""
    for tmp in _ithmb_leftovers(mount, COMPACT_SUFFIX):
        os.remove(tmp)
    if not _ithmb_leftovers(mount, BEFORE_COMPACT):
        return None
    if art_files_consistent(mount):
        finish_compaction(mount)
        return "kept compacted art"
    undo_compaction(mount)
    log_info("Album art: the last compaction was interrupted; put the old files back")
    return "restored art from before compaction"


# One cover per album instead of per track (2026-10-23). pygpod gives every
# track its own image in all 6 thumbnail sizes: 1413 tracks on 95 albums
# made 451 MB of .ithmb on the Nano, a 24 MB ArtworkDB in memory, and a
# compaction that ran the Pi out of memory. iTunes shares one image per
# album ("sparse artwork": the track's mhii_link points at it). Verified on
# the Nano 4G: covers right in the album list, Now Playing and per-track
# variants - but only after a restart: straight after the one-time
# conversion (docked through the sync) the album list showed no art and
# playing a song crashed the iPod. Older generations (video, nano 1G/2G)
# find art per track and keep one image each.
SHARED_ART_GENERATIONS = SIGNED_GENERATIONS
SHARED_ART_VERSION = "1"
SHARED_ART_MARKER = os.path.join("iPod_Control", "musicdock", "shared-art")


def _art_key(album, size):
    """Tracks share an image when their album name and source cover size
    match: the prepare job encodes an album's cover once, so its tracks'
    covers are byte-identical, and a different cover is all but certain to
    differ in size."""
    return ((album or "").strip().casefold(), int(size or 0))


def _shares_art(db):
    d = db._device
    return bool(d) and d.generation.name in SHARED_ART_GENERATIONS


def _link_art(mhit, image_id, size):
    """Point a track at an image, as pygpod does when it adds one."""
    from pygpod.utils.compat import put8int, put16lint, put32lint
    header = bytearray(mhit.raw_header)
    if len(header) > 0x7D:
        put16lint(header, 0x7C, 1)                  # artwork_count
    if len(header) > 0x83:
        put32lint(header, 0x80, size)               # artwork_size
    if len(header) > 0xA4:
        put8int(header, 0xA4, 1)                    # has_artwork
    if len(header) > 0x163:
        put32lint(header, 0x160, image_id)          # mhii_link
    mhit.raw_header = bytes(header)
    mhit.fields.update(artwork_count=1, artwork_size=size, has_artwork=1,
                       mhii_link=image_id)


def _images(db):
    """{image_id: mhii record} in the ArtworkDB ({} if it has none yet)."""
    mgr = db._get_artwork_manager()
    if mgr._root is None:
        return {}
    return {m.fields.get("image_id"): m for m in mgr._get_or_create_mhli().children}


def _set_owner(mhii, dbid):
    """The track an image records as its own (mhii song_id)."""
    from pygpod.utils.compat import put64lint
    header = bytearray(mhii.raw_header)
    put64lint(header, 0x14, dbid)
    mhii.raw_header = bytes(header)
    mhii.fields["song_id"] = dbid


def _art_index(db):
    """{_art_key: image_id} for art already on the player (built once)."""
    index = getattr(db, "_musicdock_art", None)
    if index is None:
        images = _images(db)
        index = {}
        for t in db.tracks:
            if t.has_artwork and t.mhii_link in images:
                index.setdefault(_art_key(t.album, t.artwork_size), t.mhii_link)
        db._musicdock_art = index
    return index


def install_shared_artwork():
    """Make pygpod reuse an album's image for its other tracks, and never
    delete an image another track still shows. Applied once per process."""
    from pygpod.model import database as pdb
    if getattr(pdb.Database._add_artwork_for_track, "_musicdock", False):
        return
    original_add, original_remove = pdb.Database._add_artwork_for_track, pdb.Database.remove_track

    def add_artwork_for_track(self, mhit, dbid, filepath, cover_art=None):
        if not self._mountpoint or not _shares_art(self):
            return original_add(self, mhit, dbid, filepath, cover_art)
        try:
            art = pdb.extract_artwork(filepath)
            if not art and cover_art and os.path.isfile(cover_art):
                with open(cover_art, "rb") as f:
                    art = f.read()
        except Exception:
            art = None
        if not art:
            return original_add(self, mhit, dbid, filepath, cover_art)
        track = next((t for t in reversed(self._tracks) if t.dbid == dbid), None)
        key = _art_key(track.album if track else None, len(art))
        index = _art_index(self)
        if index.get(key) in _images(self):
            _link_art(mhit, index[key], len(art))           # the album's image
            return
        original_add(self, mhit, dbid, filepath, cover_art)
        if mhit.fields.get("mhii_link"):
            index[key] = mhit.fields["mhii_link"]

    def remove_track(self, track, delete_file=False):
        link = track.mhii_link if track.has_artwork else 0
        if link and self._mountpoint:
            others = [t for t in self._tracks if t.track_id != track.track_id
                      and t.has_artwork and t.mhii_link == link]
            if others:
                # Still shown by other tracks: keep the image (pygpod would
                # delete it) and hand it to one of them if this track owned it.
                track.record.fields["mhii_link"] = 0
                mhii = _images(self).get(link)
                if mhii is not None and mhii.fields.get("song_id") == track.dbid:
                    _set_owner(mhii, others[0].dbid)
            else:
                index = getattr(self, "_musicdock_art", None) or {}
                for k in [k for k, v in index.items() if v == link]:
                    del index[k]
        return original_remove(self, track, delete_file)

    add_artwork_for_track._musicdock = True
    pdb.Database._add_artwork_for_track = add_artwork_for_track
    pdb.Database.remove_track = remove_track


def share_existing_artwork(db):
    """One cover per album for art added before sharing: point each album's
    tracks at one of its images and drop the rest (compaction then shrinks
    the .ithmb files). Returns how many images were dropped."""
    if not _shares_art(db):
        return 0
    images = _images(db)
    keep, dropped = {}, set()
    for t in db.tracks:
        if not (t.has_artwork and t.mhii_link in images):
            continue
        key = _art_key(t.album, t.artwork_size)
        image = keep.setdefault(key, t.mhii_link)
        if t.mhii_link != image:
            dropped.add(t.mhii_link)
            _link_art(t.record, image, t.artwork_size)
    still_shown = {t.mhii_link for t in db.tracks if t.has_artwork}
    owners = {t.dbid for t in db.tracks}
    mgr = db._get_artwork_manager()
    for image_id in dropped - still_shown:
        mgr.remove_artwork(image_id)
    for image_id in set(keep.values()):           # each kept image owned by a track it shows
        mhii = images[image_id]
        if mhii.fields.get("song_id") not in owners or not any(
                t.dbid == mhii.fields.get("song_id") and t.mhii_link == image_id
                for t in db.tracks):
            shown_by = next(t for t in db.tracks if t.has_artwork and t.mhii_link == image_id)
            _set_owner(mhii, shown_by.dbid)
    db._musicdock_art = {k: v for k, v in keep.items()}
    return len(dropped - still_shown)


def shared_art_outdated(mount):
    try:
        with open(os.path.join(mount, SHARED_ART_MARKER)) as f:
            return f.read().strip() != SHARED_ART_VERSION
    except OSError:
        return True


def mark_shared_art(mount):
    path = os.path.join(mount, SHARED_ART_MARKER)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(SHARED_ART_VERSION + "\n")


def checked_save(db, guid):
    """Save only if pygpod will sign the DB the way this player needs."""
    d = db._device
    gen = d.generation.name if d else None
    if gen in SIGNED_GENERATIONS:
        ok = d.requires_hash and d.checksum_type.name == "HASH58"
    else:
        ok = gen in UNSIGNED_GENERATIONS and d.checksum_type.name in ("NONE", "HASH58")
    if not ok:
        raise HandlerError(f"refusing to save: player not identified as a model the "
                           f"dock can sign for ({d.model if d else 'no device'})")
    if (d.firewire_guid or "").upper() != guid.upper():
        raise HandlerError("refusing to save: SysInfo FireWire GUID doesn't match the player")
    install_sort_order()
    install_artwork_compaction()
    install_shared_artwork()
    db.save()


def start_artwork_ids_at(db, first=ARTWORK_FIRST_ID):
    """New artwork gets image IDs from `first` up (pygpod 0.0.6 internals)."""
    mgr = db._get_artwork_manager()
    if mgr._next_image_id < first:
        mgr._next_image_id = first


def targeted_ids(manifest):
    """Songs the player should hold: ready tracks plus targeted ones that
    aren't cached right now (manifest "skipped": download failed, cache
    full, ...; "on_player": dropped from a full cache because the player
    has them). Only songs outside this set are ever removed."""
    return ({t["id"] for t in manifest.get("tracks", [])}
            | {t["id"] for t in manifest.get("on_player", [])}
            | {s["id"] for s in manifest.get("skipped", [])})


def pending_changes(manifest, present_ids):
    """(to_add, to_remove) for a player holding `present_ids`, as apply_plan
    would do it (not counting re-encode replacements)."""
    ready = {t["id"] for t in manifest.get("tracks", [])}
    return len(ready - set(present_ids)), len(set(present_ids) - targeted_ids(manifest))


def fill_missing_names(track, t):
    """Use Navidrome's title/artist where the file had none: pygpod then
    titles the track with the file name (the Navidrome ID) and no artist.
    The album always follows Navidrome (as when the track was added), so a
    corrected album name in the plan reaches tracks already on the player.
    So does the genre: the plan's simplified one (dock_genres), not the
    file's tags. Returns the names of the fields changed (empty: none)."""
    changed = set()
    if t.get("album") and track.album != t["album"]:
        track.album = t["album"]
        changed.add("album")
    if t.get("title") and (not track.title or track.title == t["id"]):
        track.title = t["title"]
        changed.add("title")
    if t.get("artist") and not track.artist:
        track.artist = t["artist"]
        changed.add("artist")
    if t.get("genre") and track.genre != t["genre"]:
        track.genre = t["genre"]
        changed.add("genre")
    return changed


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
    if "tracks" not in manifest:
        raise ValueError("No staged plan: refusing to apply it (it would remove every track)")
    install_shared_artwork()
    wanted = {t["id"]: t for t in manifest["tracks"]}
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
    keep = targeted_ids(manifest)        # a song not cached right now stays put
    removals = [r for nid, r in present.items() if nid not in keep or nid in changed]

    # Tracks already on the player that were added without tags, or whose
    # album name or genre changed in the plan (fixed in the iTunesDB; no
    # copy needed). Includes songs the cache no longer holds ("on_player").
    planned = {t["id"]: t for t in manifest.get("on_player", [])}
    planned.update(wanted)
    renamed = regenred = 0
    for nid, r in present.items():
        if nid not in planned or nid in changed:
            continue
        t = planned[nid]
        fields = fill_missing_names(on_player[int(r["dbid"])], t)
        if fields - {"genre"}:
            log_info(f"Updated names from Navidrome: {t.get('artist')} - "
                     f"{t.get('title')} ({t.get('album')})")
        renamed += bool(fields)
        regenred += "genre" in fields
    if regenred:
        log_info(f"Genre updated on {regenred} track(s) (the [genres] list)")
    dirty = dirty or bool(renamed)
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
    # Tracks added before the prepare job embedded Navidrome's cover (the
    # source had none): add the art to the track in place, no re-copy. The
    # iPod builds thumbnails only when art is added, never from the file.
    art_added = 0
    for nid, r in present.items():
        t = wanted.get(nid)
        if not t or nid in changed or t.get("cover") != "navidrome":
            continue
        track = on_player[int(r["dbid"])]
        path = os.path.join(cache_dir, t["file"])
        if track.has_artwork or not os.path.exists(path):
            continue
        db._add_artwork_for_track(track.record, track.dbid, path)   # pygpod 0.0.6
        if track.has_artwork:
            art_added += 1
    if art_added:
        log_info(f"Added album art to {art_added} track(s) already on the player")
        dirty = True
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
            fill_missing_names(track, t)
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
    return {"added": added, "removed": removed, "replaced": len(changed), "renamed": renamed,
            "art_added": art_added, "not_added_full": skipped_full,
            "playlists_changed": playlists_changed}


def sync_playlists(db, record, guid, manifest):
    """Create/update the dock's playlists (Favorites, tagged, configured) and
    delete ones it made that are no longer wanted. Playlists the dock didn't
    make are never touched. Returns how many changed."""
    by_nid = {}
    for r in record.tracks(guid):
        by_nid.setdefault(r["navidrome_id"], int(r["dbid"]))
    known = {int(r["dbid"]) for r in record.tracks(guid)}
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
            # Made by the dock but not in its record (e.g. a new dock or a
            # lost record): adopt a playlist that holds only the dock's
            # tracks - known to the record, or carrying a Navidrome ID - or
            # nothing at all (emptied, e.g. by a mass removal; stock iPods
            # can't make named playlists themselves). A playlist with any
            # other track is the user's: hands off.
            if all(t.dbid in known or navidrome_id_from(t)
                   for t in existing[name].tracks):
                log_info(f"Adopting playlist {name!r} (all its tracks are the dock's)")
                current = existing[name]
            else:
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
                write=False, client=None, usb_product=None, step=None, progress=None,
                block_device=None):
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
    name = None                        # the name given to the iPod (DockSetup.txt uses it)
    if "iTunesDB" in copied:
        snap = pygpod.Database.from_file(os.path.join(work_dir, "iTunesDB"))
        name = getattr(snap.master_playlist, "name", None)
        reconcile(snap, record, guid, mount, delete_orphans=False, manifest=manifest)
        plays = plays_from_snapshot(work_dir, record, guid, tracks=snap.tracks)
        # Let the snapshot go before the live database is opened: two full
        # copies at once was ~37 MB extra at 1443 tracks (sync peak 139 vs
        # 102 MB measured on the Pi, 2026-10-23).
        del snap
        gc.collect()
        summary["plays_new"] = record.queue_plays(guid, plays)
        record.rematch_plays(guid)
        if summary["plays_new"]:
            log_info(f"Queued {summary['plays_new']} new play(s)")

    if client is not None and record.unsent_count()[0]:
        step(3, f"Sending {record.unsent_count()[0]} plays")
        summary["plays_sent"] = dock_plays.upload_plays(client, record)[0]
    summary["plays_queued"] = record.unsent_count()[0]

    # No staged plan (fresh install: the first prepare run hasn't finished)
    # is not an empty plan: applying {} would remove every track. Plays only.
    if write and "tracks" not in (manifest or {}):
        log_info("No staged plan yet: plays only, the player's music is left as is")
        write = False

    if write:
        step(4, "Backing up player")
        ensure_sysinfo(mount, guid, record, usb_product, block_device)
        recover_compaction(mount)        # before the backup, so it matches the art files
        os.makedirs(backup_root, exist_ok=True)
        backup = backup_player(mount, backup_root)
        try:
            install_shared_artwork()
            db = pygpod.Database(mount)
            report = reconcile(db, record, guid, mount, manifest=manifest)
            if report["reset"]:
                log_info("Player appears to have been reset: rebuilding it")
            step(5, "Copying tracks")
            resort = sort_order_outdated(mount)
            if resort:
                log_info("Re-sorting the player's album lists by disc and track")
            reshare = _shares_art(db) and shared_art_outdated(mount)
            if reshare:
                dropped = share_existing_artwork(db)
                log_info(f"Album art: one cover per album ({dropped} duplicate images dropped)")
            result = apply_plan(db, record, guid, mount, manifest, cache_dir,
                                progress=progress, dirty=report["dirty"] or resort or reshare)
            if resort:
                mark_sort_order(mount)
            if reshare:
                mark_shared_art(mount)
            finish_compaction(mount)
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
    player = record.player(guid)
    if player:
        if player.get("forgotten_at"):
            log_info("This iPod was forgotten (musicdock-players forget); "
                     "docking it brings it back")
        record.set_player_name(guid, name)
        record.set_player_kind(guid, "stock")
        record.mark_synced(guid)
    return summary
