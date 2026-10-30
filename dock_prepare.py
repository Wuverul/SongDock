#!/usr/bin/env python3
"""
dock_prepare.py - prepare job: get the next sync's tracks ready.

Runs on a timer (musicdock-prepare.timer) as the login user, at low priority.
Never touches the player. Steps:
  1. Ask Navidrome for the target tracks (config `targets`): "starred"
     (starred songs plus every track of a starred album) and/or
     "playlist:<name>". A song in several targets is stored once.
  2. Retire cached tracks that are no longer targeted (kept
     removal_grace_days in case they're re-added; retired.json).
  3. Download each missing track, convert it if the player needs that (in
     parallel, one worker per spare core), and move it into the cache:
       stock_ipod: MP3/AAC/ALAC kept as-is (no lossy-to-lossy re-encode);
                   FLAC and anything else -> convert_to (default MP3 V0);
                   lossless sources in a #lossless playlist -> 16-bit ALAC
       rockbox:    everything kept as-is; with flac = false, FLAC ->
                   convert_to (for players that can't play it), unless the
                   song is in a #lossless playlist
  4. Write manifest.json: the plan the sync job applies to the player -
     the tracks (with the targets each came from) and each playlist's
     song order, to recreate it on the player. --show-plan prints it.
     With 2+ cores, an interim plan is handed over every sync_batch_size
     tracks so a docked iPod syncs them while the rest convert. When the
     cache is full, files of songs already on the player make room.

Files are built in tmp/ and renamed into tracks/ only when complete, so a
power cut never leaves a half-written track that looks ready.

Cache layout (cache_dir in config.toml):
  tracks/<id>.<ext>   ready for the player
  tmp/                partial downloads/conversions (cleared every run)
  manifest.json       the sync queue
  retired.json        tracks kept for the grace period after leaving the targets
  .lock               one prepare run at a time

Screens: PREPARING while working (PREPARE & SYNC while a batch syncs), then
IDLE ("Next sync: N/M tracks ready").
Only drawn over IDLE/PREPARING: never over SYNCING, SAFE, FAILED, or the boot
screens, which carry information the user still needs.

Usage:
  dock_prepare.py                     # on the Pi (the timer runs this)
  dock_prepare.py --dry-run           # show what would change, touch nothing
  dock_prepare.py --preview-dir DIR   # save screens as PNGs instead
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dock_artists  # noqa: E402
import dock_config  # noqa: E402
import dock_genres  # noqa: E402
from subsonic import Client, SubsonicError  # noqa: E402

log = logging.getLogger("dock_prepare")

NATIVE_FORMATS = {"stock_ipod": {"mp3", "m4a", "aac"}}   # play without conversion

# convert_to target -> file extension. MP3 is the default: ffmpeg's built-in
# AAC encoder adds audible high-frequency artifacts (measured; see CLAUDE.md),
# and the Pi has no better AAC encoder. LAME measured clean and is ~4x faster
# on the Pi Zero.
EXTENSIONS = {"mp3": "mp3", "alac": "m4a", "aac": "m4a"}
# Only these become lossless output: a lossy source is never "upgraded"
# (user's rule, 2026-10-26). m4a isn't listed: it may be AAC, and an ALAC one
# plays as it is.
LOSSLESS_SOURCES = {"flac", "wav", "aif", "aiff", "ape", "wv"}
# Which players make a #lossless tag count, per cache format: without one,
# nobody gets files a cheap MP3 player may not play (user, 2026-10-30).
LOSSLESS_PLAYERS = {"stock_ipod": {"stock"}, "rockbox_no_flac": {"rockbox"}}


def encoding(device_cfg, target=None):
    """Short description of the current conversion settings, e.g. "mp3 v0".

    Stored per track in the manifest, so changing a setting (like
    mp3_quality) re-converts tracks made with the old one. target: the
    song's own format (song_format), else the dock's convert_to.
    """
    target = target or device_cfg["convert_to"]
    if target == "mp3":
        return f"mp3 {device_cfg['mp3_quality'].lower()}"
    if target == "aac":
        return f"aac {device_cfg['aac_bitrate']}"
    return "alac 16"          # "alac" = made before hi-res was brought down


def alac_rate(track):
    """Sample rate for a song's ALAC file: as it is up to 48 kHz, else the
    rate it's a multiple of (96 -> 48, 88.2 -> 44.1). Unknown = 44.1."""
    rate = int(track.get("samplingRate") or 0)
    if not rate:
        return 44100
    if rate <= 48000:
        return rate
    return 44100 if rate % 44100 == 0 else 48000


def audio_options(device_cfg, target=None, track=None):
    """ffmpeg audio options for a song's format (default: convert_to)."""
    target = target or device_cfg["convert_to"]
    if target == "mp3":
        q = device_cfg["mp3_quality"].lower()
        rate = ["-q:a", q[1:]] if q.startswith("v") else ["-b:a", q]   # VBR or CBR
        return ["-c:a", "libmp3lame", *rate,
                "-id3v2_version", "3"]          # ID3v2.3: what old iPods read
    if target == "alac":
        # Stock iPods are believed not to play ALAC above 16-bit / 48 kHz.
        # Triangular dither applies only when bits are dropped: 16-bit
        # sources stay bit-exact.
        return ["-c:a", "alac", "-af", f"aresample={alac_rate(track or {})}:osf=s16p"
                ":dither_method=triangular"]
    return ["-c:a", "aac", "-b:a", device_cfg["aac_bitrate"]]
# Ogg/Opus keep their tags on the audio stream, not in the file's global
# metadata, so ffmpeg's -map_metadata 0 drops them (2026-10-18: Leashes
# tracks reached the iPod titled by their Navidrome ID, artist unknown).
OGG_SUFFIXES = {"ogg", "oga", "opus"}
# Tags filled in from Navidrome when a prepared file has none:
# (mutagen "easy" key, Navidrome song field).
TAG_FIELDS = (("title", "title"), ("artist", "artist"), ("album", "album"),
              ("tracknumber", "track"), ("discnumber", "discNumber"), ("date", "year"))
MAX_DISK_FRACTION = 0.80     # keep the SD card under 80% full (CLAUDE.md)
MIN_FREE_FOR_KEPT = 0.25     # below 25% free, kept (removed) tracks go first
DAY = 86400
DRAW_OVER = "none,idle,preparing,restoring"   # never over syncing/safe/failed/boot/safehours


class PrepareError(Exception):
    pass


# --------------------------------------------------------------- targets ----
def starred_songs(client, album_info=None):
    """Starred songs plus every track of a starred album. Fills
    `album_info` ({album id: album fields}) from the albums it reads."""
    starred = client.call("getStarred2").get("starred2", {})
    songs = list(starred.get("song", []))
    for album in starred.get("album", []):
        info = client.call("getAlbum", id=album["id"]).get("album", {})
        if album_info is not None:
            album_info[album["id"]] = {k: v for k, v in info.items() if k != "song"}
        songs += info.get("song", [])
    if starred.get("artist"):
        log.info("Ignoring %d starred artist(s): only songs and albums are synced",
                 len(starred["artist"]))
    return songs


def find_playlist(client, name, known_id=None, listing=None):
    """Return the playlist called `name` (with its songs).

    Looked up by name; if no playlist has that name any more but we know its
    ID from an earlier run, the ID is used (so renaming doesn't break it).
    A playlist that can't be found is an error, never "empty": treating it
    as empty would delete all its tracks from the cache.
    """
    if listing is None:
        listing = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
    matches = [p for p in listing if p["name"] == name]
    if len(matches) > 1:
        raise PrepareError(f"several playlists are named {name!r}; rename one")
    if matches:
        pid = matches[0]["id"]
    elif known_id:
        log.warning("No playlist named %r; using its previous ID %s (renamed?)",
                    name, known_id)
        pid = known_id
    else:
        raise PrepareError(f"playlist {name!r} not found on the server")
    try:
        return client.call("getPlaylist", id=pid)["playlist"]
    except SubsonicError as e:
        raise PrepareError(f"playlist {name!r} couldn't be read: {e}") from None


def library_order(song):
    return ((song.get("artist") or "").lower(), (song.get("album") or "").lower(),
            song.get("discNumber") or 0, song.get("track") or 0, song.get("title") or "")


def unique_ids(songs):
    seen, ids = set(), []
    for s in songs:
        if s["id"] not in seen:
            seen.add(s["id"])
            ids.append(s["id"])
    return ids


def has_tag(comment, tag):
    """True if the playlist comment contains the tag as a whole word (any case)."""
    if not tag:
        return False
    pattern = r"(?<![\w#])" + re.escape(tag) + r"(?!\w)"
    return re.search(pattern, comment or "", re.IGNORECASE) is not None


def lossless_songs(client, tag, targets, playlists, previous_ids=()):
    """Songs to sync as lossless: those in playlists whose comment has `tag`.
    Such a playlist needn't be synced itself; it only flags songs that are.
    Returns ({song id}, [playlist ids]). Any failure raises: carrying on
    without the flags would swap these songs back to MP3."""
    if not tag:
        return set(), []
    listing = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
    if not listing and previous_ids:
        raise PrepareError(
            f"the server listed no playlists, but {len(previous_ids)} were tagged {tag} "
            "last time; stopping without changing any song's format.")
    synced = {p["id"]: p for p in playlists}
    ids, recordings, found = set(), set(), []
    for p in listing:
        if not has_tag(p.get("comment"), tag):
            continue
        found.append(p["id"])
        if p["id"] in synced:          # song_ids already follow merged recordings
            ids.update(synced[p["id"]]["song_ids"])
            continue
        try:
            songs = client.call("getPlaylist", id=p["id"])["playlist"].get("entry", [])
        except SubsonicError as e:
            raise PrepareError(f"playlist {p['name']!r} (tagged {tag}) couldn't be "
                               f"read: {e}") from None
        ids.update(s["id"] for s in songs)
        recordings.update(k for k in map(recording_key, songs) if k)
    # An unsynced list may name the copy of a recording the plan didn't keep.
    ids.update(t["id"] for t in targets if recording_key(t) in recordings)
    return ids, found


def target_tracks(client, targets, known_playlists=None, favorites_name="Favorites",
                  tag="", previously_tagged=(), album_info=None):
    """Resolve the configured targets into one de-duplicated set of tracks.

    targets: e.g. ["starred", "playlist:Mel Test"].
    known_playlists: {target: playlist id} from the last run (for renames).
    favorites_name: the player playlist made from "starred" ("" for none).
    tag: playlists whose Navidrome comment contains this (e.g. "#ipod") are
    added too. previously_tagged: playlist IDs tagged last run, so an empty
    playlist listing (server trouble) stops the run instead of pruning.
    album_info: pass a dict to keep the album records read along the way.
    Returns (tracks in artist/album/track order, {song id: [targets]},
    [playlist records for the manifest, in target order]).
    """
    known_playlists = known_playlists or {}
    tracks, sources, playlists = {}, {}, []
    album_info = {} if album_info is None else album_info
    listing = None
    if tag:
        listing = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
        if not listing and previously_tagged:
            raise PrepareError(
                f"the server listed no playlists, but {len(previously_tagged)} were "
                f"tagged {tag} last time; stopping without removing anything. (If you "
                f"really deleted them all, set playlist_tag = \"\" to continue.)")
        tagged = [p for p in listing if has_tag(p.get("comment"), tag)]
        targets = list(targets) + [("tagged", p) for p in tagged]

    def add(target, songs):
        for song in songs:
            tracks.setdefault(song["id"], song)
            sources.setdefault(song["id"], [])
            if target not in sources[song["id"]]:
                sources[song["id"]].append(target)
        log.info("Target %s: %d songs", target, len(songs))

    seen_playlists = set()
    for target in targets:
        if isinstance(target, tuple):                  # found by tag
            pid = target[1]["id"]
            if pid in seen_playlists:
                continue                               # also listed in config
            try:
                pl = client.call("getPlaylist", id=pid)["playlist"]
            except SubsonicError as e:
                raise PrepareError(f"tagged playlist {target[1]['name']!r} couldn't "
                                   f"be read: {e}") from None
            seen_playlists.add(pid)
            songs = pl.get("entry", [])
            key = f"playlist:{pl['name']}"
            playlists.append({"target": key, "id": pl["id"], "name": pl["name"],
                              "via": "tag",
                              "song_ids": [s["id"] for s in songs]})
            add(key, songs)
            continue
        if target == "starred":
            songs = starred_songs(client, album_info)
            if favorites_name:
                playlists.append({"target": target, "id": None, "name": favorites_name,
                                  "via": "starred",
                                  "song_ids": unique_ids(sorted(songs, key=library_order))})
        elif target.startswith("playlist:"):
            if listing is None:
                listing = client.call("getPlaylists").get("playlists", {}).get("playlist", [])
            pl = find_playlist(client, target.split(":", 1)[1],
                               known_playlists.get(target), listing)
            if pl["id"] in seen_playlists:
                continue
            seen_playlists.add(pl["id"])
            songs = pl.get("entry", [])
            playlists.append({"target": target, "id": pl["id"], "name": pl["name"],
                              "via": "config",
                              "song_ids": [s["id"] for s in songs]})
        else:
            raise PrepareError(f"unknown target {target!r} "
                               "(use \"starred\" or \"playlist:<name>\")")
        add(target, songs)
    merge_same_recordings(client, tracks, sources, playlists, album_info)
    unify_album_names(client, tracks.values(), album_info)
    return sorted(tracks.values(), key=library_order), sources, playlists


# Which copy of a recording found on several releases to keep: lower is
# better ("favor the album listing"; user, 2026-10-19).
RELEASE_RANK = {"album": 0, "": 1, "ep": 2, "single": 3, "live": 4, "compilation": 5,
                "soundtrack": 5, "remix": 6, "demo": 7}
SAME_LENGTH = 2              # seconds: copies of one recording are this close


def album_fields(client, album_id, album_info):
    """Navidrome's album record (name, releaseTypes, songCount...), cached in
    `album_info`; {} if it can't be read."""
    if album_id not in album_info:
        try:
            album = client.call("getAlbum", id=album_id).get("album", {})
            album_info[album_id] = {k: v for k, v in album.items() if k != "song"}
        except SubsonicError as e:
            log.warning("Couldn't look up album %s (%s)", album_id, e)
            album_info[album_id] = {}
    return album_info[album_id]


def recording_key(song):
    """What identifies a recording across releases: its MusicBrainz recording
    ID, else its ISRC. Songs without either are never merged."""
    if song.get("musicBrainzId"):
        return ("mbid", song["musicBrainzId"].lower())
    isrc = song.get("isrc")
    if isinstance(isrc, list):
        isrc = isrc[0] if isrc else None
    return ("isrc", isrc.upper()) if isrc else None


def merge_same_recordings(client, tracks, sources, playlists, album_info):
    """Keep one copy of a recording that the targets list on several releases
    (e.g. a single and its album), preferring the album.

    Two songs are one recording only if they share a MusicBrainz recording ID
    (or ISRC) and their lengths agree within SAME_LENGTH seconds; verified on
    Kitsune² tracks found on two albums (2026-10-19: spectrograms matched to
    0.999+, a studio and a live version scored 0.2). The kept copy takes over
    the other's targets and playlist places (listed once). Modifies `tracks`
    ({song id: song}), `sources` and the playlists' song_ids in place.
    """
    groups = {}
    for song in tracks.values():
        key = recording_key(song)
        if key:
            groups.setdefault(key, []).append(song)

    def rank(song):
        info = album_fields(client, song.get("albumId"), album_info) \
            if song.get("albumId") else {}
        types = dock_genres.release_types(info) or [""]
        return (min(RELEASE_RANK.get(t, 4) for t in types),
                bool(info.get("isCompilation")), -(info.get("songCount") or 0),
                song.get("year") or 9999, song["id"])

    replaced = {}
    for songs in groups.values():
        if len(songs) < 2:
            continue
        songs.sort(key=rank)
        keep = songs[0]
        for other in songs[1:]:
            if abs((other.get("duration") or 0) - (keep.get("duration") or 0)) > SAME_LENGTH:
                continue
            replaced[other["id"]] = keep["id"]
            log.info("Same recording on two releases: keeping %s - %s from %r, not %r",
                     keep.get("artist"), keep.get("title"), keep.get("album"),
                     other.get("album"))
    for old, new in replaced.items():
        del tracks[old]
        for target in sources.pop(old, []):
            if target not in sources[new]:
                sources[new].append(target)
    if replaced:
        for pl in playlists:
            pl["song_ids"] = unique_ids([{"id": replaced.get(i, i)} for i in pl["song_ids"]])


def unify_album_names(client, songs, album_info):
    """Give every song of one Navidrome album the album's own name.

    Navidrome groups an album by its ID, but a song's `album` field comes
    from that file's tags and can differ (2026-10-18: one track of "Hold
    Your Colour (2007 re-release)" said "Hold Your Colour"). The iPod groups
    by name, so that song landed in an album of its own. Only albums whose
    songs disagree are looked up; `album_info` holds albums already read.
    """
    names = {}
    for s in songs:
        if s.get("albumId"):
            names.setdefault(s["albumId"], set()).add(s.get("album"))
    for album_id, seen in names.items():
        if len(seen) < 2:
            continue
        name = album_fields(client, album_id, album_info).get("name")
        if not name:
            continue
        for s in songs:
            if s.get("albumId") == album_id and s.get("album") != name:
                log.info("Album name from Navidrome's album: %s - %s: %r -> %r",
                         s.get("artist"), s.get("title"), s.get("album"), name)
                s["album"] = name


def plan_genres(client, cfg, songs, previous, album_info=None):
    """{song id: genre for the player} from the [genres] list, and the
    release types used ({album id: [types]}, kept in the manifest so each
    album is looked up once, and only if a group uses "release:").
    album_info: album records already read this run (target_tracks)."""
    album_info = {} if album_info is None else album_info
    settings = cfg.get("genres", dock_config.DEFAULTS["genres"])
    groups = settings["groups"]
    album_types = {}
    if dock_genres.uses_release_types(groups):
        known = previous.get("album_types", {})
        for album_id in sorted({s["albumId"] for s in songs if s.get("albumId")}):
            if album_id in known:
                album_types[album_id] = known[album_id]
                continue
            info = album_fields(client, album_id, album_info)
            if info:                         # {} = lookup failed: retry next run
                album_types[album_id] = dock_genres.release_types(info)
    genres = dock_genres.assign(songs, groups, settings["unmatched"], album_types)
    return genres, album_types


def format_mode(device):
    """How the cache holds music, from config [device]: "stock_ipod",
    "rockbox" (as on the server) or "rockbox_no_flac" (FLAC converted,
    everything else as-is). `flac` only matters on a Rockbox-only dock:
    stock compatibility converts FLAC anyway."""
    if device["type"] == "rockbox" and not device.get("flac", True):
        return "rockbox_no_flac"
    return device["type"]


def needs_conversion(suffix, device_type):
    suffix = (suffix or "").lower()
    if device_type == "rockbox":
        return False
    if device_type == "rockbox_no_flac":
        return suffix == "flac"
    return suffix not in NATIVE_FORMATS["stock_ipod"]


def song_format(track, device_type, convert_to, lossless=()):
    """What a song is converted to, or None to keep the server's file.
    lossless: IDs of the songs in #lossless playlists."""
    suffix = (track.get("suffix") or "").lower()
    source_lossless = suffix in LOSSLESS_SOURCES
    if track["id"] in lossless and source_lossless:
        return "alac" if device_type == "stock_ipod" else None    # Rockbox plays FLAC
    if not needs_conversion(suffix, device_type):
        return None
    return "mp3" if convert_to == "alac" and not source_lossless else convert_to


def output_name(track, target):
    """The cache file for a song; target from song_format."""
    suffix = (track.get("suffix") or "bin").lower()
    ext = EXTENSIONS[target] if target else suffix
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", track["id"])
    return f"{safe_id}.{ext}"


def label(track):
    return f"{track.get('artist') or 'Unknown'} - {track.get('title') or track['id']}"


# ----------------------------------------------------------------- files ----
COVER_MAX = 600            # px. The Nano's largest thumbnail is 240px; keep margin
COVER_MAX_BYTES = 300_000  # for other players (e.g. Rockbox's cover.jpg).
# Decoding a cover takes ~10 bytes per pixel (a 4000x4000 cover peaked at
# 153 MB on a Pi Zero, 2026-10-16). One too big to decode safely - a huge
# PNG; JPEGs are decoded at reduced scale - is removed instead, and the
# file gets Navidrome's 600 px cover, which looks the same on the iPod. A
# quarter of the RAM: up to three conversions may decode covers at once.
COVER_DECODE_SHARE = 0.25
COVER_BYTES_PER_PIXEL = 10


def max_cover_pixels():
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        ram = 512e6
    return int(ram * COVER_DECODE_SHARE / COVER_BYTES_PER_PIXEL)
_warned_no_cover_tools = False


def shrink_cover(path, cache=None):
    """Replace an oversized embedded cover with a <=COVER_MAX JPEG.

    Only the tag block changes (mutagen rewrites tags in place); the audio
    data is untouched. Big covers made every track take 30-90 s to add to
    the iPod on a Pi Zero (pygpod resizes the art per track) and wasted
    megabytes per track. `cache` maps a cover's hash to its replacement, so
    an album's shared cover is decoded once.

    Returns "none" (no cover, or one too big to decode safely, now
    removed: the caller then adds Navidrome's), "kept" (already small),
    "shrunk", or "unchecked" (mutagen/Pillow not available - run under the
    venv).
    """
    global _warned_no_cover_tools
    try:
        import hashlib
        import io
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        from mutagen.mp4 import MP4, MP4Cover
        from PIL import Image
    except ImportError:
        if not _warned_no_cover_tools:
            log.warning("mutagen/Pillow missing: covers not shrunk (run with ~/musicdock/venv)")
            _warned_no_cover_tools = True
        return "unchecked"

    def smaller(data):
        """Replacement JPEG bytes, None if the cover is already fine, or
        False if it's too big to decode here (remove it)."""
        key = hashlib.sha1(data).hexdigest()
        if cache is not None and key in cache:
            return cache[key]
        im = Image.open(io.BytesIO(data))          # reads the header only
        if (max(im.size) <= COVER_MAX and len(data) <= COVER_MAX_BYTES
                and im.format == "JPEG"):
            new = None
        else:
            im.draft("RGB", (COVER_MAX, COVER_MAX))   # JPEG: decode at reduced scale
            width, height = im.size
            if width * height > max_cover_pixels():
                log.info("Cover too big to decode here (%dx%d): using Navidrome's instead",
                         width, height)
                new = False
                if cache is not None:
                    cache[key] = new
                return new
            im = im.convert("RGB")
            im.thumbnail((COVER_MAX, COVER_MAX), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90, optimize=True, progressive=False)
            new = buf.getvalue()
        if cache is not None:
            cache[key] = new
        return new

    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return "none"
        pics = tags.getall("APIC")
        if not pics:
            return "none"
        front = next((p for p in pics if p.type == 3), pics[0])
        new = smaller(front.data)
        if new is False:
            tags.delall("APIC")
            tags.save(path, v2_version=3)
            return "none"
        if new is None and len(pics) == 1:
            return "kept"
        tags.delall("APIC")                         # keep one cover, not several
        tags.add(APIC(encoding=3, mime="image/jpeg" if new else front.mime,
                      type=3, desc="", data=new or front.data))
        tags.save(path, v2_version=3)
        return "shrunk" if new else "kept"
    if ext in (".m4a", ".mp4"):
        f = MP4(path)
        covers = (f.tags or {}).get("covr")
        if not covers:
            return "none"
        new = smaller(bytes(covers[0]))
        if new is False:
            del f.tags["covr"]
            f.save()
            return "none"
        if new is None and len(covers) == 1:
            return "kept"
        f.tags["covr"] = [MP4Cover(new, imageformat=MP4Cover.FORMAT_JPEG)] if new else covers[:1]
        f.save()
        return "shrunk" if new else "kept"
    if ext == ".flac":
        f = FLAC(path)
        if not f.pictures:
            return "none"
        front = next((p for p in f.pictures if p.type == 3), f.pictures[0])
        new = smaller(front.data)
        if new is False:
            f.clear_pictures()
            f.save()
            return "none"
        if new is None and len(f.pictures) == 1:
            return "kept"
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg" if new else front.mime, new or front.data
        f.clear_pictures()
        f.add_picture(pic)
        f.save()
        return "shrunk" if new else "kept"
    return "none"


def add_server_cover(path, t, client, images, cache=None):
    """Embed Navidrome's cover in a file that has none, then shrink it like
    any other cover.

    Navidrome also shows covers kept beside the music (e.g. a folder's
    cover.jpg), which never reach a converted file; the iPod only uses
    embedded art. `images` maps a coverArt ID to the server's image (None:
    it has none), so an album's cover is fetched once. Returns "navidrome"
    (added), "missing" (the server has no cover either) or "none" (couldn't
    tell: no mutagen, or not a format we tag). A network error is raised,
    so the track is tried again next run.
    """
    try:
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        return "none"
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".mp3", ".m4a", ".mp4", ".flac"):
        return "none"
    cover_id = t.get("coverArt") or t.get("albumId")
    if not cover_id:
        return "missing"
    if cover_id not in images:
        try:
            images[cover_id] = client.cover_art(cover_id, size=COVER_MAX)
        except SubsonicError as e:
            if e.network:
                raise
            images[cover_id] = None
    data = images[cover_id]
    if not data:
        return "missing"
    png = data.startswith(b"\x89PNG")
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(APIC(encoding=3, mime="image/png" if png else "image/jpeg",
                      type=3, desc="", data=data))
        tags.save(path, v2_version=3)
    elif ext == ".flac":
        f = FLAC(path)
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/png" if png else "image/jpeg", data
        f.add_picture(pic)
        f.save()
    else:
        f = MP4(path)
        if f.tags is None:
            f.add_tags()
        f.tags["covr"] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG if png
                                   else MP4Cover.FORMAT_JPEG)]
        f.save()
    shrink_cover(path, cache)       # PNG or oversized -> 600px baseline JPEG
    return "navidrome"


# --------------------------------------------------------------- workers ----
HOT_C = 75.0               # above this, convert one track at a time until it cools


def resolve_workers(setting="auto", cpu_count=None):
    """How many conversions to run at once.

    "auto" = cores - 1 (at least 1): 1 on a Pi Zero W, 3 on a Pi Zero 2 W.
    Worked out every run, so moving the SD card to another board just works.
    The spare core's worth is for the sync, the screen, and the system;
    nice 19 means a sync still wins the CPU whenever it needs it.
    """
    if setting in (None, "auto"):
        return max(1, (cpu_count or os.cpu_count() or 1) - 1)
    return max(1, int(setting))


def cpu_temp():
    """SoC temperature in degrees C, or None if unknown (e.g. not a Pi)."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000
    except (OSError, ValueError):
        return None


class ConversionSlots:
    """Lets at most `limit` conversions run at once; only one while the Pi is
    hot (multi-core boards in an enclosure can reach thermal throttling)."""

    def __init__(self, limit, temp=cpu_temp, recheck=10):
        self.limit, self.temp, self.recheck = limit, temp, recheck
        self.active = 0
        self.cond = threading.Condition()
        self.warned_hot = False

    def __enter__(self):
        with self.cond:
            while True:
                t = self.temp()
                hot = t is not None and t >= HOT_C
                if hot and not self.warned_hot:
                    log.warning("Pi is at %.0f C: converting one track at a time", t)
                    self.warned_hot = True
                if self.active < (1 if hot else self.limit):
                    self.active += 1
                    return self
                self.cond.wait(timeout=self.recheck)   # re-check the temperature

    def __exit__(self, *exc):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()


def fsync_file(path):
    """Make sure a file's data is on the SD card before we rename it."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def convert(src, dst, options, duration=None):
    """FLAC (or other) -> another format (ffmpeg audio `options`), keeping
    tags and cover art.

    If the cover art can't be carried over, retries without it rather than
    failing the track.
    """
    from_stream = os.path.splitext(src)[1].lstrip(".").lower() in OGG_SUFFIXES
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src, *options, "-map_metadata", "0:s:a:0" if from_stream else "0"]
    attempts = [(["-c:v", "copy", "-disposition:v", "attached_pic"], "with cover art"),
                (["-vn"], "without cover art")]
    timeout = max(600, int(duration or 0) * 10)    # the Pi Zero is slow
    for extra, how in attempts:
        try:
            r = subprocess.run(base + extra + [dst], capture_output=True,
                               text=True, timeout=timeout)
        except FileNotFoundError:
            raise PrepareError("ffmpeg isn't installed (sudo apt install ffmpeg)") from None
        if r.returncode == 0:
            return
        log.warning("ffmpeg failed %s: %s", how, r.stderr.strip()[-400:])
    raise PrepareError("conversion failed")


def fill_missing_tags(path, t):
    """Give a prepared file Navidrome's title/artist/album/track/disc/year
    where it has none, so the player never falls back to the file name (the
    Navidrome ID). Tags the file already has are left alone. Returns True if
    the file was changed. Needs mutagen (the venv); without it, does nothing."""
    try:
        import mutagen
        from mutagen.easyid3 import EasyID3
    except ImportError:
        return False
    audio = mutagen.File(path, easy=True)
    if audio is None:
        return False
    if audio.tags is None:
        audio.add_tags()
    changed = False
    for key, field in TAG_FIELDS:
        value = t.get(field)
        if value and not audio.tags.get(key):
            audio.tags[key] = str(value)
            changed = True
    if changed:
        if isinstance(audio.tags, EasyID3):
            audio.save(v2_version=3)       # ID3v2.3: what old iPods read
        else:
            audio.save()
    return changed


def dir_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


MAX_FILE_SHARE = 0.5         # a song may use at most half the room the cache can have


def cache_room(cache_dir, max_bytes, disk_usage=shutil.disk_usage):
    """The most the cache can ever hold: max_cache_gb, or what keeping the
    SD card under 80% full leaves once everything else on it is counted,
    whichever is less. A song being converted reserves twice its size, so
    one of up to half this always fits in an emptied cache."""
    usage = disk_usage(cache_dir)
    others = usage.used - dir_bytes(cache_dir)
    return max(0, min(max_bytes, MAX_DISK_FRACTION * usage.total - others))


def no_room(cache_dir, need, max_bytes, disk_usage=shutil.disk_usage):
    """Return why `need` more bytes won't fit, or None if they will."""
    if dir_bytes(cache_dir) + need > max_bytes:
        return "cache limit (max_cache_gb) reached"
    usage = disk_usage(cache_dir)
    if (usage.used + need) / usage.total > MAX_DISK_FRACTION:
        return "SD card would be over 80% full"
    return None


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_manifest(cache_dir):
    try:
        with open(os.path.join(cache_dir, "manifest.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------- prepare ----
class KeptTracks:
    """Tracks that left every target but stay cached for the grace period
    (removal_grace_days), in case they're re-added. Stored in retired.json:
    {cache file name: {"since": epoch, **its last manifest entry}}.
    They're not in the plan, so the next sync still removes them from the
    player; re-adding one within the grace period reuses the file."""

    def __init__(self, cache_dir, tracks_dir, clock=time.time):
        self.path = os.path.join(cache_dir, "retired.json")
        self.tracks_dir = tracks_dir
        self.clock = clock
        try:
            with open(self.path) as f:
                self.items = json.load(f)
        except (OSError, ValueError):
            self.items = {}
        self.dirty = False

    def keep(self, name, entry):
        if name not in self.items:
            self.items[name] = {**(entry or {}), "since": self.clock()}
            self.dirty = True
            return True
        return False

    def reuse(self, name):
        if self.items.pop(name, None) is not None:
            self.dirty = True
            return True
        return False

    def drop(self, name, why):
        info = self.items.pop(name, {})
        self.dirty = True
        path = os.path.join(self.tracks_dir, name)
        if os.path.exists(path):
            os.remove(path)
        log.info("Deleting kept track (%s): %s - %s", why, info.get("artist"),
                 info.get("title") or name)

    def oldest(self):
        return min(self.items, key=lambda n: self.items[n]["since"]) if self.items else None

    def expire(self, grace_days):
        for name in sorted(self.items, key=lambda n: self.items[n]["since"]):
            if self.clock() - self.items[name]["since"] >= grace_days * DAY:
                self.drop(name, f"grace period of {grace_days} days over"
                          if grace_days else "no grace period")

    def save(self):
        if self.dirty:
            write_json_atomic(self.path, self.items)
            self.dirty = False


# sync_batch_size = "auto": about BATCH_GROUPS hand-overs per run, each at
# least BATCH_MIN and at most BATCH_MAX tracks (user's rule, 2026-10-26).
# Every hand-over rewrites the whole plan (~21 MB at 40,000 tracks) and the
# sync re-reads it: a few hundred per run at most. Smaller batches start
# copying sooner and leave less to copy once conversion ends.
BATCH_GROUPS, BATCH_MIN, BATCH_MAX = 20, 25, 500


def batch_size(setting, count, cores):
    """Tracks per hand-over for a run preparing `count` tracks (0 = none:
    one core, where a sync would only slow conversion, or turned off)."""
    if cores < 2:
        return 0
    if setting == "auto":
        return min(BATCH_MAX, max(BATCH_MIN, -(-count // BATCH_GROUPS)))
    return int(setting or 0)


def prepare(client, cfg, cache_dir, show=None, convert_fn=convert,
            dry_run=False, disk_usage=shutil.disk_usage, clock=time.time,
            on_player=None, on_batch=None, cpu_count=None, on_progress=None,
            player_kinds=None):
    """Bring the cache in line with the target. Returns a summary dict.

    show(screen, *args) draws a progress screen (optional).
    on_player: {song id: {encodings}} for songs confirmed on every player
    (Record.songs_on_every_player). When the cache is full, those songs'
    files make room for songs the player lacks (largest first) and aren't
    fetched again: the plan lists them under "on_player", so the sync keeps
    them. A player bigger than the cache then fills over several syncs.
    player_kinds: Record.active_player_kinds(), None if the record can't be
    read (then lossless stays as it was last run: no swapping either way).
    on_batch(manifest): called each time a batch of [sync] sync_batch_size
    new tracks is handed over mid-run (only with 2+ CPU cores).
    on_progress(started, total, batches, to_copy): as each track starts;
    to_copy also counts re-added songs reused from the cache.
    """
    device = format_mode(cfg["device"])
    current = encoding(cfg["device"])
    max_bytes = int(float(cfg["sync"]["max_cache_gb"]) * 1e9)
    tracks_dir = os.path.join(cache_dir, "tracks")
    tmp_dir = os.path.join(cache_dir, "tmp")

    previous = read_manifest(cache_dir)
    known = {p["target"]: p["id"] for p in previous.get("playlists", []) if p.get("id")}
    previously_tagged = [p["id"] for p in previous.get("playlists", [])
                         if p.get("via") == "tag"]
    album_info = {}                 # album records read this run, shared
    targets, sources, playlists = target_tracks(
        client, cfg["sync"]["targets"], known,
        cfg["sync"].get("favorites_playlist", "Favorites"),
        cfg["sync"].get("playlist_tag", ""), previously_tagged, album_info)
    unsplit_artists = set()
    if cfg["sync"].get("primary_artist", True):
        dock_artists.tidy(targets, cfg["sync"].get("artist_names", ()), unsplit_artists,
                          cfg["sync"].get("compilation_albums", ()))
    genres, album_types = plan_genres(client, cfg, targets, previous, album_info)
    lossless_on = (bool(previous.get("lossless_playlists")) if player_kinds is None else
                   bool(LOSSLESS_PLAYERS.get(device, set()) & set(player_kinds)))
    lossless, lossless_lists = lossless_songs(
        client, cfg["sync"].get("lossless_tag", "") if lossless_on else "", targets,
        playlists, previous.get("lossless_playlists", ()))

    def format_of(t):
        return song_format(t, device, cfg["device"]["convert_to"], lossless)

    def made_as(t):
        target = format_of(t)
        return encoding(cfg["device"], target) if target else "original"
    wanted = {output_name(t, format_of(t)): t for t in targets}
    existing = set(os.listdir(tracks_dir)) if os.path.isdir(tracks_dir) else set()
    to_remove = sorted(existing - wanted.keys())     # includes tracks kept earlier

    # Cached conversions made with different settings get re-done. Entries
    # from before encodings were recorded are assumed current.
    kept = KeptTracks(cache_dir, tracks_dir, clock)
    newly_removed = [n for n in to_remove if n not in kept.items]
    made_with = {t["id"]: t.get("encoding") for t in previous.get("tracks", [])}
    made_with.update({e["id"]: e.get("encoding") for e in kept.items.values() if "id" in e})
    stale = {name for name, t in wanted.items()
             if name in existing and format_of(t)
             and made_with.get(t["id"]) not in (None, made_as(t))}
    missing = [(name, t) for name, t in wanted.items()
               if name not in existing or name in stale]

    # Songs already on every player in their current encoding ("held") can
    # do without a cache file. Fetch songs the player lacks first (playlist
    # songs before the rest), then mirror held ones while there's room.
    on_player = on_player or {}
    held = {name for name, t in wanted.items()
            if on_player.get(t["id"]) and on_player[t["id"]] <= {None, made_as(t)}}
    missing.sort(key=lambda nt: (nt[0] in held, not any(
        s.startswith("playlist:") for s in sources.get(nt[1]["id"], []))))

    log.info("Target: %d tracks (%d as %s, %d lossless); %d cached, %d to get (%d to "
             "re-convert), %d to remove", len(wanted),
             sum(made_as(t) == current for t in wanted.values()), current,
             sum(format_of(t) == "alac" for t in wanted.values()),
             len(wanted) - len(missing), len(missing), len(stale), len(newly_removed))
    if dry_run:
        for name, t in missing:
            how = f"convert to {made_as(t)}" if format_of(t) else "copy"
            log.info("  would get (%s, %s): %s", t.get("suffix"), how, label(t))
        for name in newly_removed:
            log.info("  would remove: %s", name)
        return {"total": len(wanted), "ready": len(wanted) - len(missing),
                "added": 0, "removed": 0, "skipped": []}

    os.makedirs(tracks_dir, exist_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)    # leftovers from a cut-off run
    os.makedirs(tmp_dir)

    # Tracks that left every target: kept for the grace period (re-adding
    # one reuses the file), then deleted; sooner if space is short.
    grace = int(cfg["sync"].get("removal_grace_days", 7))
    prev_entries = {t["file"].split("/", 1)[-1]: t for t in previous.get("tracks", [])}
    wanted_stems = {os.path.splitext(n)[0] for n in wanted}
    for name in to_remove:
        if os.path.splitext(name)[0] in wanted_stems and name not in kept.items:
            # Same song, old format (e.g. .m4a after switching to MP3): it'll
            # never be reused, so no grace period.
            os.remove(os.path.join(tracks_dir, name))
            log.info("Deleted old format of a still-wanted track: %s", name)
            continue
        entry = prev_entries.get(name)
        if kept.keep(name, entry) and grace:
            log.info("No longer targeted, kept %d days in case it's re-added: %s - %s",
                     grace, (entry or {}).get("artist"), (entry or {}).get("title") or name)
    reused = 0             # re-added songs whose cached file is reused: no conversion,
    for name in wanted:    # but they still have to reach the iPod (PREPARE & SYNC total)
        if kept.reuse(name):
            reused += 1
            log.info("Re-added within the grace period, reusing: %s", label(wanted[name]))
    kept.expire(grace)
    while kept.items:
        usage = disk_usage(cache_dir)
        if usage.free / usage.total >= MIN_FREE_FOR_KEPT:
            break
        kept.drop(kept.oldest(), "SD card below 25% free")
    kept.save()

    # Shrink covers on tracks cached before covers were checked, and give
    # coverless ones Navidrome's cover (one-time pass per track; "none" =
    # not yet asked the server; each file is edited in tmp/ and swapped in
    # whole).
    cover_cache, covers, server_images = {}, {}, {}
    checked = {t["id"]: t.get("cover")
               for t in list(previous.get("tracks", [])) + previous.get("on_player", [])
               + list(kept.items.values())
               if t.get("cover_max") == COVER_MAX and "id" in t}
    missing_names = {n for n, _ in missing}
    unchecked = [(n, t) for n, t in wanted.items()
                 if n in existing and n not in missing_names
                 and checked.get(t["id"], "none") == "none"]
    if unchecked:
        log.info("Checking covers on %d cached tracks", len(unchecked))
    offline = False
    for i, (name, t) in enumerate(unchecked):
        if show and i % 10 == 0:
            show("preparing", "--done", str(i), "--total", str(len(unchecked)),
                 "--detail", f"Checking covers: {label(t)}")
        tmp = os.path.join(tmp_dir, f"cover-{name}")
        try:
            shutil.copy2(os.path.join(tracks_dir, name), tmp)
            result = shrink_cover(tmp, cover_cache)
            if result == "none" and not offline:
                try:
                    result = add_server_cover(tmp, t, client, server_images, cover_cache)
                except SubsonicError as e:
                    log.warning("Can't get covers from the server (%s); next run tries again", e)
                    offline = True
                if result == "navidrome":
                    log.info("Added Navidrome's cover: %s", label(t))
            if result in ("shrunk", "navidrome"):
                fsync_file(tmp)
                os.replace(tmp, os.path.join(tracks_dir, name))
            covers[t["id"]] = result
        except Exception:
            log.exception("Couldn't check the cover of %s (left as is)", label(t))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    covers.update({k: v for k, v in checked.items() if k not in covers})

    # Ogg/Opus-sourced tracks converted before tags were read from the audio
    # stream have no title/artist: fill them in from Navidrome (cheap; only
    # these few files are checked, and each is written only if incomplete).
    for name, t in wanted.items():
        if (name in existing and name not in missing_names
                and (t.get("suffix") or "").lower() in OGG_SUFFIXES):
            tmp = os.path.join(tmp_dir, f"tags-{name}")
            try:
                shutil.copy2(os.path.join(tracks_dir, name), tmp)
                if fill_missing_tags(tmp, t):
                    fsync_file(tmp)
                    os.replace(tmp, os.path.join(tracks_dir, name))
                    log.info("Filled in missing tags from Navidrome: %s", label(t))
            except Exception:
                log.exception("Couldn't check the tags of %s (left as is)", label(t))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

    # Download/convert in parallel: `workers` conversions at once, plus one
    # more thread so the next download overlaps the current conversion (helps
    # even on a single core). Space is reserved per track before it starts.
    workers = resolve_workers(cfg["sync"].get("workers", "auto"))
    slots = ConversionSlots(workers)
    lock, show_lock = threading.Lock(), threading.Lock()
    progress = {"added": 0, "started": 0, "reserved": 0, "offline": False}
    skipped = []
    # A song too big for this cache even when it's empty (a tiny SD card, a
    # long DJ mix as FLAC) would stop every run at it - "no room" ends the
    # run - and nothing after it would ever arrive. Skip such songs instead.
    room = cache_room(cache_dir, max_bytes, disk_usage)
    too_big = [(n, t) for n, t in missing if int(t.get("size") or 0) > room * MAX_FILE_SHARE]
    if too_big:
        names = {n for n, _ in too_big}
        missing = [(n, t) for n, t in missing if n not in names]
        for _, t in too_big:                  # every run, so not a warning
            log.info("Skipping %s: %.0f MB is too big for this dock's cache (%.0f MB "
                     "of room; a song may use half)", label(t),
                     int(t.get("size") or 0) / 1e6, room / 1e6)
            skipped.append({"id": t["id"], "title": label(t), "error": False,
                            "reason": "too big for this dock's cache (more than half of it)"})
    if missing:
        log.info("Getting %d tracks with %d conversion worker(s)", len(missing), workers)

    def prepare_one(name, t, need):
        conv = format_of(t)
        if progress["offline"]:
            # The server dropped out earlier in this run: don't try every
            # remaining track; the next check carries on.
            with lock:
                skipped.append({"id": t["id"], "title": label(t),
                                "reason": "server unreachable", "error": False})
                progress["reserved"] -= need
            return
        try:
            with lock:
                n = progress["started"]
                progress["started"] += 1
            if show:
                with show_lock:
                    show("preparing", "--done", str(n), "--total", str(len(missing)),
                         "--detail", f"{'Converting' if conv else 'Downloading'}: {label(t)}")
            if on_progress:
                on_progress(n, len(missing), handed["batches"], len(missing) + reused)
            src = os.path.join(tmp_dir, f"src-{name}.{(t.get('suffix') or 'bin').lower()}")
            log.info("Downloading %s (%s, %.1f MB)", label(t), t.get("suffix"),
                     int(t.get("size") or 0) / 1e6)
            client.download(t["id"], src)
            if conv:
                out = os.path.join(tmp_dir, name)
                with slots:
                    log.info("Converting %s (%s)", label(t), made_as(t))
                    convert_fn(src, out, audio_options(cfg["device"], conv, t),
                               t.get("duration"))
                os.remove(src)
            else:
                out = src
            try:
                covers[t["id"]] = shrink_cover(out, cover_cache)
                if covers[t["id"]] == "none":
                    covers[t["id"]] = add_server_cover(out, t, client, server_images,
                                                       cover_cache)
            except SubsonicError as e:
                log.warning("Couldn't get Navidrome's cover for %s (%s); next run "
                            "tries again", label(t), e)
            except Exception:
                log.exception("Couldn't shrink the cover of %s (kept as is)", label(t))
            try:
                if fill_missing_tags(out, t):
                    log.info("Filled in missing tags from Navidrome: %s", label(t))
            except Exception:
                log.exception("Couldn't check the tags of %s (kept as is)", label(t))
            fsync_file(out)
            os.replace(out, os.path.join(tracks_dir, name))
            with lock:
                progress["added"] += 1
        except SubsonicError as e:
            with lock:
                if e.network:
                    if not progress["offline"]:
                        log.warning("Server unreachable (%s); stopping this batch, "
                                    "the next check will carry on", e)
                    progress["offline"] = True
                else:
                    log.warning("Couldn't download %s: %s", label(t), e)
                skipped.append({"id": t["id"], "title": label(t), "reason": str(e),
                                "error": not e.network})
        except Exception as e:      # one bad track shouldn't block the rest
            log.exception("Couldn't prepare %s", label(t))
            with lock:
                skipped.append({"id": t["id"], "title": label(t), "reason": str(e),
                                "error": True})
        finally:
            with lock:
                progress["reserved"] -= need

    # Only tracks actually in progress hold reserved space: hand the pool a new
    # track when a thread frees up, rather than queueing (and reserving for)
    # the whole batch at once - 432 queued FLACs reserved ~47 GB and tripped
    # the cache limit with the cache at 4.7 GB (2026-10-17).
    in_flight = threading.BoundedSemaphore(workers + 1)

    def entry(t):
        return {
            "id": t["id"],
            "title": t.get("title"), "artist": t.get("artist"),
            "album_artist": t.get("displayAlbumArtist") or t.get("albumArtist"),
            "album": t.get("album"), "track": t.get("track"),
            "disc": t.get("discNumber"), "year": t.get("year"),
            "duration": t.get("duration"), "genre": genres.get(t["id"]),
            "source_suffix": t.get("suffix"),
            "converted": bool(format_of(t)),
            "encoding": made_as(t),
            "sources": sources.get(t["id"], []),
            **({"cover": covers[t["id"]], "cover_max": COVER_MAX}
               if covers.get(t["id"], "unchecked") != "unchecked" else {}),
        }

    def build_manifest(waiting=False):
        """The plan from what's cached now. waiting=True (a batch handed over
        mid-run): songs not prepared yet are listed as skipped ("waiting to
        be prepared"), so the sync keeps them on the player, and the plan is
        marked "interim": a sync holding it stays on to take the next one."""
        ready, uncached = [], []
        for name, t in wanted.items():
            path = os.path.join(tracks_dir, name)
            if os.path.exists(path):
                ready.append({"file": f"tracks/{name}", "size": os.path.getsize(path),
                              **entry(t)})
            elif name in held:
                uncached.append(entry(t))         # on the player; no file needed
        held_ids = {t["id"] for t in uncached}
        not_ready = [s for s in skipped if s["id"] not in held_ids]
        if waiting:
            listed = held_ids | {t["id"] for t in ready} | {s["id"] for s in not_ready}
            not_ready += [{"id": t["id"], "title": label(t), "reason": "waiting to be prepared",
                           "error": False} for t in wanted.values() if t["id"] not in listed]
        return {
            "targets": cfg["sync"]["targets"], "device": device,
            "playlist_tag": cfg["sync"].get("playlist_tag", ""),
            **({"lossless_playlists": lossless_lists} if lossless_lists else {}),
            **({"unsplit_artists": sorted(unsplit_artists)} if unsplit_artists else {}),
            "tracks": ready, "on_player": uncached, "playlists": playlists,
            "skipped": not_ready, "kept_after_removal": len(kept.items),
            **({"album_types": album_types} if album_types else {}),
            **({"interim": True} if waiting else {}),
        }

    # Concurrent batches (Pi with 2+ cores): every `batch` newly prepared
    # tracks, hand the plan so far to the sync (on_batch asks for a sync of a
    # docked iPod), while the remaining conversions carry on.
    batch = batch_size(cfg["sync"].get("sync_batch_size", "auto"), len(missing),
                       cpu_count or os.cpu_count() or 1)
    if batch and len(missing) > batch:
        log.info("Handing new music to a docked iPod every %d tracks", batch)
    handed = {"added": 0, "batches": 0}

    def hand_over():
        with lock:
            manifest = build_manifest(waiting=True)
            done = progress["added"]
        manifest["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json_atomic(os.path.join(cache_dir, "manifest.json"), manifest)
        handed["added"], handed["batches"] = done, handed["batches"] + 1
        log.info("Batch %d ready (%d new tracks so far): a docked iPod can sync it "
                 "while the rest are prepared", handed["batches"], done)
        if on_batch:
            on_batch(manifest)

    # Cache files of held songs, largest first: dropped only to make room for
    # a song the player lacks, never for another held one (no churn).
    evictable = sorted((n for n in held if n in existing and n not in missing_names),
                       key=lambda n: -os.path.getsize(os.path.join(tracks_dir, n)))
    evicted = 0

    def run_one(name, t, need):
        try:
            prepare_one(name, t, need)
        finally:
            in_flight.release()

    with ThreadPoolExecutor(max_workers=workers + 1) as pool:
        futures = []
        for i, (name, t) in enumerate(missing):
            conv = format_of(t)
            need = int(t.get("size") or 0) * (2 if conv else 1)
            in_flight.acquire()
            if batch and progress["added"] - handed["added"] >= batch:
                hand_over()
            if progress["offline"]:
                in_flight.release()
                with lock:
                    skipped += [{"id": t2["id"], "title": label(t2),
                                 "reason": "server unreachable", "error": False}
                                for _, t2 in missing[i:]]
                break
            with lock:
                reason = no_room(cache_dir, need + progress["reserved"], max_bytes, disk_usage)
                while reason and kept.items:        # kept tracks never block new music
                    kept.drop(kept.oldest(), "making room for new music")
                    reason = no_room(cache_dir, need + progress["reserved"], max_bytes,
                                     disk_usage)
                while reason and evictable and name not in held:
                    gone = evictable.pop(0)
                    os.remove(os.path.join(tracks_dir, gone))
                    evicted += 1
                    log.info("Cache full: dropped %s from the cache (it's on the player)",
                             label(wanted[gone]))
                    reason = no_room(cache_dir, need + progress["reserved"], max_bytes,
                                     disk_usage)
                if not reason:
                    progress["reserved"] += need
            if reason:
                in_flight.release()
                rest = [t2 for n2, t2 in missing[i:] if n2 not in held]
                if rest:
                    log.warning("Stopping early: %s (%d tracks wait for the next run)",
                                reason, len(rest))
                else:
                    log.info("Cache full: %d tracks already on the player stay uncached",
                             len(missing) - i)
                with lock:
                    skipped += [{"id": t2["id"], "title": label(t2), "reason": reason,
                                 "error": False} for t2 in rest]
                break
            futures.append(pool.submit(run_one, name, t, need))
        for f in futures:
            f.result()
    kept.save()
    added = progress["added"]

    shutil.rmtree(tmp_dir, ignore_errors=True)

    manifest = build_manifest()
    skipped = manifest["skipped"]
    # Runs every few minutes: only write the plan when it changed (spares the
    # SD card; "generated" then says when the plan last changed). After a
    # batch hand-over the file on disk is an interim plan: always replace it.
    if handed["batches"] or \
            {k: v for k, v in previous.items() if k != "generated"} != manifest:
        manifest["generated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        write_json_atomic(os.path.join(cache_dir, "manifest.json"), manifest)
    if evicted:
        log.info("Dropped %d cached tracks that are on the player, to make room", evicted)
    return {"total": len(wanted), "ready": len(manifest["tracks"]) + len(manifest["on_player"]),
            "added": added, "removed": len(newly_removed), "skipped": skipped,
            "evicted": evicted, "batches": handed["batches"]}


def show_plan(manifest):
    """Describe what the next sync would put on the player (from the manifest)."""
    if not manifest:
        return "No manifest yet: the prepare job hasn't run."
    tracks = {t["id"]: t for t in manifest.get("tracks", [])}
    sources = ", ".join(manifest.get("targets", []))
    if manifest.get("playlist_tag"):
        sources += f" + playlists tagged {manifest['playlist_tag']}"
    lines = [f"Staged {manifest.get('generated')} for {manifest.get('device')} "
             f"from {sources}:",
             f"  {len(tracks)} tracks ready, "
             f"{sum(t['size'] for t in tracks.values()) / 1e6:.0f} MB "
             f"(each stored once, however many lists it's in)"]
    combos = {}
    for t in tracks.values():
        key = " + ".join(t.get("sources") or ["?"])
        combos[key] = combos.get(key, 0) + 1
    for key, n in sorted(combos.items()):
        lines.append(f"    {n:4d} from {key}")
    if manifest.get("on_player"):
        lines.append(f"  {len(manifest['on_player'])} on the player but not cached "
                     f"(cache full; the sync keeps them)")
    if manifest.get("skipped"):
        lines.append(f"  {len(manifest['skipped'])} not ready (see manifest 'skipped')")
    if manifest.get("kept_after_removal"):
        lines.append(f"  {manifest['kept_after_removal']} removed track(s) still cached "
                     f"for the grace period")
    listed = {**{t["id"]: t for t in manifest.get("on_player", [])}, **tracks}
    for pl in manifest.get("playlists", []):
        ids = pl["song_ids"]
        albums = []
        for i in ids:
            a = listed.get(i, {}).get("album") or "?"
            if a not in albums:
                albums.append(a)
        missing = sum(1 for i in ids if i not in listed)
        via = {"tag": " [tagged]", "config": " [config]"}.get(pl.get("via"), "")
        lines.append(f"  iPod playlist {pl['name']!r}{via}: {len(ids)} songs from "
                     f"{len(albums)} album(s): {', '.join(albums)}"
                     + (f" ({missing} not ready)" if missing else ""))
    return "\n".join(lines)


def show_genres(config_path=None):
    """The --genres preview: the current targets with the [genres] list
    applied, from the server (nothing is written)."""
    try:
        cfg = dock_config.load(config_path)
    except dock_config.ConfigError as e:
        log.error("%s", e)
        return 1
    client = Client(cfg["server"]["url"], cfg["server"]["username"],
                    cfg["server"]["password"])
    previous = read_manifest(os.path.expanduser(cfg["sync"]["cache_dir"]))
    known = {p["target"]: p["id"] for p in previous.get("playlists", []) if p.get("id")}
    album_info = {}
    try:
        songs, _, _ = target_tracks(client, cfg["sync"]["targets"], known,
                                    cfg["sync"].get("favorites_playlist", "Favorites"),
                                    cfg["sync"].get("playlist_tag", ""),
                                    album_info=album_info)
        genres, _ = plan_genres(client, cfg, songs, previous, album_info)
    except (SubsonicError, PrepareError) as e:
        log.error("Couldn't read the targets: %s", e)
        return 1
    print("\n".join(dock_genres.summary(songs, genres)))
    return 0


def pending_args(manifest):
    """DOCK READY arguments saying what the next sync will do to the most
    recently synced player (+adds, -removes), or [] if none synced yet."""
    try:
        import ipod_handler
        from dock_record import Record
        record = Record()
        try:
            player = record.last_synced_player()
            if not player:
                return []
            records = record.tracks(player["guid"])
        finally:
            record.close()
        add, remove = ipod_handler.pending_changes(
            manifest, {r["navidrome_id"] for r in records})
        lossless, other = ipod_handler.pending_conversions(manifest, records)
        return ["--pending-add", str(add), "--pending-remove", str(remove),
                "--pending-lossless", str(lossless), "--pending-format", str(other),
                "--last-sync", str(int(player["last_sync"]))]
    except Exception:
        log.exception("Couldn't work out the next sync's changes")
        return []


def has_pending(idle_args):
    """True if DOCK READY's arguments say the next sync changes something."""
    a = dict(zip(idle_args[1::2], idle_args[2::2]))
    return any(int(a.get(k, 0)) for k in ("--pending-add", "--pending-remove",
                                          "--pending-lossless", "--pending-format"))


def resync_coming():
    """True if a sync of a docked iPod has been asked for and not started
    yet (a request waiting, or musicdock-resync re-plugging the iPod)."""
    import dock_resync
    if os.path.exists(dock_resync.REQUEST):
        return True
    r = subprocess.run(["systemctl", "is-active", "musicdock-resync.service"],
                       capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def sync_running():
    """True while a sync (musicdock-sync@*) is running: the iPod comes first."""
    r = subprocess.run(["systemctl", "list-units", "--type=service", "--no-legend",
                        "--state=active,activating", "musicdock-sync@*"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def wait_for_syncs(check=sync_running, sleep=time.sleep, poll=15):
    """Wait until no sync is running (a docked iPod gets the dock first).
    Returns the seconds waited."""
    waited = 0
    while check():
        if waited == 0:
            log.info("A sync is running; checking for new music once it's done")
        sleep(poll)
        waited += poll
    return waited


# ------------------------------------------------------------------ main ----
def limit_malloc_arenas(n=2):
    """Cap glibc's per-thread malloc arenas (call before starting threads).

    Each worker thread otherwise gets its own arena (up to 64 MB on 64-bit),
    which keeps freed memory: on a Zero 2 W (415 MB) a run grew to ~230 MB
    over 116 tracks and was OOM-killed (2026-10-23). Same as setting
    MALLOC_ARENA_MAX. Returns False where there's no glibc (e.g. macOS).
    """
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        return bool(libc.mallopt(-8, n))       # M_ARENA_MAX
    except (OSError, AttributeError):
        return False


def main(argv=None):
    limit_malloc_arenas()
    p = argparse.ArgumentParser(description="SongDock prepare job")
    p.add_argument("--config", help="config.toml path (default: %s)" % dock_config.DEFAULT_PATH)
    p.add_argument("--cache-dir", help="override cache_dir from the config")
    p.add_argument("--dry-run", action="store_true", help="show the plan only")
    p.add_argument("--show-plan", action="store_true",
                   help="print what the next sync would put on the player, from "
                        "the last run's manifest (no network)")
    p.add_argument("--genres", action="store_true",
                   help="show which genre each synced album gets on the player "
                        "([genres] in the config); reads the server, changes nothing")
    p.add_argument("--preview-dir", metavar="DIR",
                   help="save screens as PNGs instead of drawing on the panel")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.show_plan:
        try:
            cfg = dock_config.load(args.config)
        except dock_config.ConfigError as e:
            log.error("%s", e)
            return 1
        print(show_plan(read_manifest(os.path.expanduser(
            args.cache_dir or cfg["sync"]["cache_dir"]))))
        return 0

    if args.genres:
        return show_genres(args.config)

    try:
        os.nice(19)          # conversion must never slow a sync down
    except OSError:
        pass

    if not args.dry_run and shutil.which("systemctl"):
        wait_for_syncs()

    try:
        cfg = dock_config.load(args.config)
    except dock_config.ConfigError as e:
        log.error("%s", e)
        return 1
    cache_dir = os.path.expanduser(args.cache_dir or cfg["sync"]["cache_dir"])
    os.makedirs(cache_dir, exist_ok=True)

    lock = open(os.path.join(cache_dir, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another prepare run is in progress; exiting")
        return 0

    from dock_sync import Screen
    screen = Screen(args.preview_dir)
    previous = read_manifest(cache_dir)
    # Restoring a new or wiped iPod (dock_restore): RESTORING with one count
    # for the whole job instead of this run's PREPARING.
    import dock_restore
    restore = None if args.dry_run else dock_restore.for_last_player(previous)

    def show(name, *screen_args):
        if args.dry_run:
            return
        if restore and name == "preparing":
            a = dict(zip(screen_args[::2], screen_args[1::2]))
            name, *screen_args = dock_restore.screen_args(
                restore, f"Preparing {int(a.get('--done', 0)):,}/{int(a.get('--total', 0)):,}")
        try:
            # dock_status checks --only-over under the panel lock.
            screen.show(name, *screen_args, "--only-over", DRAW_OVER)
        except Exception:
            log.exception("Screen update failed (continuing)")

    client = Client(cfg["server"]["url"], cfg["server"]["username"],
                    cfg["server"]["password"])
    if not args.dry_run:
        # Plays queued by a sync while the server was down go out from here
        # too, so they don't wait for the next dock.
        try:
            import dock_plays
            from dock_record import Record
            record = Record()
            if record.unsent_count()[0]:
                dock_plays.upload_plays(client, record)
            record.close()
        except Exception:
            log.exception("Couldn't send queued plays (they stay queued)")

    # What every player already holds (its cache files can go when space is
    # short). Unreadable record = nothing held: the cache just stops filling.
    try:
        from dock_record import Record
        record = Record()
        try:
            on_player = record.songs_on_every_player()
            player_kinds = record.active_player_kinds()
        finally:
            record.close()
    except Exception:
        log.exception("Couldn't read what's on the players (keeping every cached track)")
        on_player, player_kinds = {}, None

    def ask_for_sync(manifest, batch=False):
        """A docked (ejected) iPod gets new music without being re-plugged by
        hand: musicdock-resync (root) re-plugs it if one is there. Only when
        the plan changes something for the last-synced player. While a sync
        is still running, a mid-run batch skips it (the next batch asks
        again); the end of the run waits for that sync, then asks."""
        if args.dry_run or args.preview_dir or not cfg["device"].get("auto_resync", True):
            return
        if not has_pending(["idle"] + pending_args(manifest)):
            return
        if shutil.which("systemctl") and sync_running():
            if batch:
                log.info("A sync is still running: this batch goes with the next one")
                return
            # End of the run: no later batch would ask again, so wait for the
            # running sync, then ask if the full plan still has changes -
            # unless that sync already asked for the next one (its follow-up).
            wait_for_syncs()
            if resync_coming():
                log.info("The next sync is already on its way")
                return True
            if not has_pending(["idle"] + pending_args(manifest)):
                return False
        try:
            import dock_resync
            dock_resync.request()
            log.info("New changes for the iPod: a docked iPod will sync now")
            return True
        except OSError as e:
            log.warning("Couldn't ask for a sync of a docked iPod: %s", e)
        return False

    # PREPARE & SYNC: while a batch sync runs, the sync draws the combined
    # screen; this keeps its "Converting n/total" current between the
    # sync's own redraws (dock_status rate-limits both).
    import dock_progress
    run_id = int(time.time())

    def on_progress(done, total, batches, to_copy=None):
        if args.dry_run or args.preview_dir:
            return
        dock_progress.write_prepare(run_id, done, total, batches, to_copy)
        try:
            import dock_status
            state = dock_status.load_state()
            if state.get("screen") != "prepsync" or \
                    time.time() - state.get("last_draw", 0) < dock_status.MIN_PROGRESS_INTERVAL:
                return
            synced, syncs = dock_progress.copied(run_id)
            extra = (["--restored", str(restore["have"] + synced),
                      "--restore-total", str(restore["total"])] if restore else [])
            screen.show(*dock_progress.screen_args(dock_progress.running_prepare() or
                                                   {"done": done, "total": total},
                                                   synced, syncs),
                        *extra, "--only-over", "prepsync")
        except Exception:
            log.exception("PREPARE & SYNC redraw failed (continuing)")

    idle_now = None

    def release_run_screen():
        """The run is over without a last sync to end it: put DOCK READY over
        a PREPARE & SYNC left on screen (never over a running sync's)."""
        if args.dry_run or args.preview_dir or not shutil.which("systemctl"):
            return
        try:
            import dock_status
            if dock_status.load_state().get("screen") == "prepsync" and not sync_running():
                screen.show(*(idle_now or ["idle"]), "--only-over", "prepsync", "--force")
        except Exception:
            log.exception("Couldn't take PREPARE & SYNC down (continuing)")

    # A PREPARE & SYNC from a run that ended without its last sync (e.g. the
    # iPod was unplugged): nothing else will end it, so this run does.
    if not dock_progress.running_prepare():
        dock_progress.clear_run()
        release_run_screen()

    summary = None
    try:
        summary = prepare(client, cfg, cache_dir, show=show, dry_run=args.dry_run,
                          on_player=on_player, on_progress=on_progress, player_kinds=player_kinds,
                          on_batch=lambda m: ask_for_sync(m, batch=True))
    except SubsonicError as e:
        if e.network:
            # Wi-Fi or server down: expected now and then, not a failure.
            log.warning("Server unreachable (%s); trying again at the next check", e)
            return 0
        log.error("Prepare failed: %s", e)
        return 1
    except PrepareError as e:
        log.error("Prepare failed: %s", e)
        return 1
    finally:
        if summary is None:                 # failed or stopped: no run to show
            dock_progress.clear_run()
            release_run_screen()

    idle = ["idle", "--ready", str(summary["ready"]), "--total", str(summary["total"])]
    if not args.dry_run:
        idle += pending_args(read_manifest(cache_dir))
    pending = list(idle)                    # DOCK READY's arguments: what's left to sync
    if not args.dry_run:
        info = dock_restore.for_last_player(read_manifest(cache_dir))
        if info:
            a = dict(zip(idle[1::2], idle[2::2]))
            adds = int(a.get("--pending-add", 0))
            idle = dock_restore.screen_args(
                info, f"Next sync: +{adds:,}" if adds else "Preparing the next part")
    prev_ready = len(previous.get("tracks", [])) + len(previous.get("on_player", []))
    changed = summary["added"] or summary["removed"] or prev_ready != summary["ready"]
    if args.preview_dir:
        if changed:
            show(*idle)
    elif not args.dry_run:
        # Redraw only when what DOCK READY would say differs from what's up
        # (dock_status checks --only-over under the panel lock).
        import dock_status
        state = dock_status.load_state()
        if state.get("screen") in (None, "idle", "preparing", "restoring") and \
                state.get("argv") != idle:
            show(*idle)
    log.info("Done: %d/%d ready (%d added, %d removed, %d skipped)",
             summary["ready"], summary["total"], summary["added"],
             summary["removed"], len(summary["skipped"]))
    # A run with concurrent batches stays one job on screen until its last
    # sync ends: mark it final before asking for that sync (the sync then
    # shows PREPARE & SYNC and finishes with SAFE TO REMOVE). Otherwise the
    # run ends here.
    idle_now = idle
    batched = bool(summary.get("batches")) and dock_progress.running_prepare() is not None
    asked = False
    if changed and not args.dry_run and not args.preview_dir and has_pending(pending):
        if batched:
            dock_progress.mark_final()
        asked = ask_for_sync(read_manifest(cache_dir))
    if not (asked and batched):
        dock_progress.clear_run()
        release_run_screen()
    # Exit non-zero on real errors so `systemctl --failed` shows them; running
    # out of room is a limit, not an error.
    return 1 if any(s["error"] for s in summary["skipped"]) else 0


if __name__ == "__main__":
    sys.exit(main())
