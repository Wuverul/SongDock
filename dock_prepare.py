#!/usr/bin/env python3
"""
dock_prepare.py - prepare job: get the next sync's tracks ready.

Runs on a timer (musicdock-prepare.timer) as the login user, at low priority.
Never touches the player. Steps:
  1. Ask Navidrome for the target tracks (config `targets`): "starred"
     (starred songs plus every track of a starred album) and/or
     "playlist:<name>". A song in several targets is stored once.
  2. Remove cached tracks that are no longer targeted.
  3. Download each missing track, convert it if the player needs that, and
     move it into the cache:
       stock_ipod: MP3/AAC/ALAC kept as-is (no lossy-to-lossy re-encode);
                   FLAC and anything else -> convert_to (default MP3 V0)
       rockbox:    everything kept as-is
  4. Write manifest.json: the plan the sync job applies to the player -
     the tracks (with the targets each came from) and each playlist's
     song order, to recreate it on the player. --show-plan prints it.

Files are built in tmp/ and renamed into tracks/ only when complete, so a
power cut never leaves a half-written track that looks ready.

Cache layout (cache_dir in config.toml):
  tracks/<id>.<ext>   ready for the player
  tmp/                partial downloads/conversions (cleared every run)
  manifest.json       the sync queue
  .lock               one prepare run at a time

Screens: PREPARING while working, then IDLE ("Next sync: N/M tracks ready").
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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dock_config  # noqa: E402
from subsonic import Client, SubsonicError  # noqa: E402

log = logging.getLogger("dock_prepare")

NATIVE_FORMATS = {"stock_ipod": {"mp3", "m4a", "aac"}}   # play without conversion

# convert_to target -> file extension. MP3 is the default: ffmpeg's built-in
# AAC encoder adds audible high-frequency artifacts (measured; see CLAUDE.md),
# and the Pi has no better AAC encoder. LAME measured clean and is ~4x faster
# on the Pi Zero.
EXTENSIONS = {"mp3": "mp3", "alac": "m4a", "aac": "m4a"}


def encoding(device_cfg):
    """Short description of the current conversion settings, e.g. "mp3 v0".

    Stored per track in the manifest, so changing a setting (like
    mp3_quality) re-converts tracks made with the old one.
    """
    target = device_cfg["convert_to"]
    if target == "mp3":
        return f"mp3 {device_cfg['mp3_quality'].lower()}"
    if target == "aac":
        return f"aac {device_cfg['aac_bitrate']}"
    return target


def audio_options(device_cfg):
    """ffmpeg audio options for the current conversion settings."""
    target = device_cfg["convert_to"]
    if target == "mp3":
        q = device_cfg["mp3_quality"].lower()
        rate = ["-q:a", q[1:]] if q.startswith("v") else ["-b:a", q]   # VBR or CBR
        return ["-c:a", "libmp3lame", *rate,
                "-id3v2_version", "3"]          # ID3v2.3: what old iPods read
    if target == "alac":
        return ["-c:a", "alac"]
    return ["-c:a", "aac", "-b:a", device_cfg["aac_bitrate"]]
MAX_DISK_FRACTION = 0.80     # keep the SD card under 80% full (CLAUDE.md)
DRAW_OVER = "none,idle,preparing"     # never over syncing/safe/failed/boot


class PrepareError(Exception):
    pass


# --------------------------------------------------------------- targets ----
def starred_songs(client):
    """Starred songs plus every track of a starred album."""
    starred = client.call("getStarred2").get("starred2", {})
    songs = list(starred.get("song", []))
    for album in starred.get("album", []):
        songs += client.call("getAlbum", id=album["id"]).get("album", {}).get("song", [])
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


def target_tracks(client, targets, known_playlists=None, favorites_name="Favorites",
                  tag="", previously_tagged=()):
    """Resolve the configured targets into one de-duplicated set of tracks.

    targets: e.g. ["starred", "playlist:Mel Test"].
    known_playlists: {target: playlist id} from the last run (for renames).
    favorites_name: the player playlist made from "starred" ("" for none).
    tag: playlists whose Navidrome comment contains this (e.g. "#ipod") are
    added too. previously_tagged: playlist IDs tagged last run, so an empty
    playlist listing (server trouble) stops the run instead of pruning.
    Returns (tracks in artist/album/track order, {song id: [targets]},
    [playlist records for the manifest, in target order]).
    """
    known_playlists = known_playlists or {}
    tracks, sources, playlists = {}, {}, []
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
                              "changed": pl.get("changed"), "via": "tag",
                              "song_ids": [s["id"] for s in songs]})
            add(key, songs)
            continue
        if target == "starred":
            songs = starred_songs(client)
            if favorites_name:
                playlists.append({"target": target, "id": None, "name": favorites_name,
                                  "changed": None, "via": "starred",
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
                              "changed": pl.get("changed"), "via": "config",
                              "song_ids": [s["id"] for s in songs]})
        else:
            raise PrepareError(f"unknown target {target!r} "
                               "(use \"starred\" or \"playlist:<name>\")")
        add(target, songs)
    return sorted(tracks.values(), key=library_order), sources, playlists


def needs_conversion(suffix, device_type):
    if device_type == "rockbox":
        return False
    return (suffix or "").lower() not in NATIVE_FORMATS["stock_ipod"]


def output_name(track, device_type, target="mp3"):
    suffix = (track.get("suffix") or "bin").lower()
    ext = EXTENSIONS[target] if needs_conversion(suffix, device_type) else suffix
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", track["id"])
    return f"{safe_id}.{ext}"


def label(track):
    return f"{track.get('artist') or 'Unknown'} - {track.get('title') or track['id']}"


# ----------------------------------------------------------------- files ----
COVER_MAX = 600            # px. The Nano's largest thumbnail is 240px; keep margin
COVER_MAX_BYTES = 300_000  # for other players (e.g. Rockbox's cover.jpg).
_warned_no_cover_tools = False


def shrink_cover(path, cache=None):
    """Replace an oversized embedded cover with a <=COVER_MAX JPEG.

    Only the tag block changes (mutagen rewrites tags in place); the audio
    data is untouched. Big covers made every track take 30-90 s to add to
    the iPod on a Pi Zero (pygpod resizes the art per track) and wasted
    megabytes per track. `cache` maps a cover's hash to its replacement, so
    an album's shared cover is decoded once.

    Returns "none" (no cover), "kept" (already small), "shrunk", or
    "unchecked" (mutagen/Pillow not available - run under the venv).
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
        """Replacement JPEG bytes, or None if the cover is already fine."""
        key = hashlib.sha1(data).hexdigest()
        if cache is not None and key in cache:
            return cache[key]
        im = Image.open(io.BytesIO(data))
        if (max(im.size) <= COVER_MAX and len(data) <= COVER_MAX_BYTES
                and im.format == "JPEG"):
            new = None
        else:
            im.draft("RGB", (COVER_MAX, COVER_MAX))   # JPEG: decode at reduced scale
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
        if new is None and len(f.pictures) == 1:
            return "kept"
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg" if new else front.mime, new or front.data
        f.clear_pictures()
        f.add_picture(pic)
        f.save()
        return "shrunk" if new else "kept"
    return "none"


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
    base = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", src, *options, "-map_metadata", "0"]
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


def dir_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


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
def prepare(client, cfg, cache_dir, show=None, convert_fn=convert,
            dry_run=False, disk_usage=shutil.disk_usage):
    """Bring the cache in line with the target. Returns a summary dict.

    show(screen, *args) draws a progress screen (optional).
    """
    device = cfg["device"]["type"]
    fmt = cfg["device"]["convert_to"]
    current = encoding(cfg["device"])
    options = audio_options(cfg["device"])
    max_bytes = int(float(cfg["sync"]["max_cache_gb"]) * 1e9)
    tracks_dir = os.path.join(cache_dir, "tracks")
    tmp_dir = os.path.join(cache_dir, "tmp")

    previous = read_manifest(cache_dir)
    known = {p["target"]: p["id"] for p in previous.get("playlists", []) if p.get("id")}
    previously_tagged = [p["id"] for p in previous.get("playlists", [])
                         if p.get("via") == "tag"]
    targets, sources, playlists = target_tracks(
        client, cfg["sync"]["targets"], known,
        cfg["sync"].get("favorites_playlist", "Favorites"),
        cfg["sync"].get("playlist_tag", ""), previously_tagged)
    wanted = {output_name(t, device, fmt): t for t in targets}
    existing = set(os.listdir(tracks_dir)) if os.path.isdir(tracks_dir) else set()
    to_remove = sorted(existing - wanted.keys())

    # Cached conversions made with different settings get re-done. Entries
    # from before encodings were recorded are assumed current.
    made_with = {t["id"]: t.get("encoding") for t in previous.get("tracks", [])}
    stale = {name for name, t in wanted.items()
             if name in existing and needs_conversion(t.get("suffix"), device)
             and made_with.get(t["id"]) not in (None, current)}
    missing = [(name, t) for name, t in wanted.items()
               if name not in existing or name in stale]

    log.info("Target: %d tracks; %d cached, %d to get (%d to re-convert as %s), "
             "%d to remove", len(wanted), len(wanted) - len(missing), len(missing),
             len(stale), current, len(to_remove))
    if dry_run:
        for name, t in missing:
            how = "convert" if needs_conversion(t.get("suffix"), device) else "copy"
            log.info("  would get (%s, %s): %s", t.get("suffix"), how, label(t))
        for name in to_remove:
            log.info("  would remove: %s", name)
        return {"total": len(wanted), "ready": len(wanted) - len(missing),
                "added": 0, "removed": 0, "skipped": []}

    os.makedirs(tracks_dir, exist_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)    # leftovers from a cut-off run
    os.makedirs(tmp_dir)

    for name in to_remove:
        log.info("Removing (no longer targeted): %s", name)
        os.remove(os.path.join(tracks_dir, name))

    # Shrink covers on tracks cached before covers were checked (one-time pass;
    # each file is edited in tmp/ and swapped in whole).
    cover_cache, covers = {}, {}
    checked = {t["id"]: t.get("cover") for t in previous.get("tracks", [])
               if t.get("cover_max") == COVER_MAX}
    missing_names = {n for n, _ in missing}
    unchecked = [(n, t) for n, t in wanted.items()
                 if n in existing and n not in missing_names and t["id"] not in checked]
    if unchecked:
        log.info("Checking covers on %d cached tracks", len(unchecked))
    for i, (name, t) in enumerate(unchecked):
        if show and i % 10 == 0:
            show("preparing", "--done", str(i), "--total", str(len(unchecked)),
                 "--detail", f"Shrinking covers: {label(t)}")
        tmp = os.path.join(tmp_dir, f"cover-{name}")
        try:
            shutil.copy2(os.path.join(tracks_dir, name), tmp)
            result = shrink_cover(tmp, cover_cache)
            if result == "shrunk":
                fsync_file(tmp)
                os.replace(tmp, os.path.join(tracks_dir, name))
            covers[t["id"]] = result
        except Exception:
            log.exception("Couldn't check the cover of %s (left as is)", label(t))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    covers.update(checked)

    added, skipped = 0, []
    for i, (name, t) in enumerate(missing):
        conv = needs_conversion(t.get("suffix"), device)
        size = int(t.get("size") or 0)
        reason = no_room(cache_dir, size * (2 if conv else 1), max_bytes, disk_usage)
        if reason:
            log.warning("Stopping early: %s", reason)
            skipped += [{"id": t2["id"], "title": label(t2), "reason": reason,
                         "error": False} for _, t2 in missing[i:]]
            break
        if show:
            show("preparing", "--done", str(i), "--total", str(len(missing)),
                 "--detail", f"{'Converting' if conv else 'Downloading'}: {label(t)}")
        try:
            src = os.path.join(tmp_dir, f"src-{name}.{(t.get('suffix') or 'bin').lower()}")
            log.info("Downloading %s (%s, %.1f MB)", label(t), t.get("suffix"), size / 1e6)
            client.download(t["id"], src)
            if conv:
                out = os.path.join(tmp_dir, name)
                log.info("Converting (%s)", current)
                convert_fn(src, out, options, t.get("duration"))
                os.remove(src)
            else:
                out = src
            try:
                covers[t["id"]] = shrink_cover(out, cover_cache)
            except Exception:
                log.exception("Couldn't shrink the cover of %s (kept as is)", label(t))
            fsync_file(out)
            os.replace(out, os.path.join(tracks_dir, name))
            added += 1
        except Exception as e:      # one bad track shouldn't block the rest
            log.exception("Couldn't prepare %s", label(t))
            skipped.append({"id": t["id"], "title": label(t), "reason": str(e),
                            "error": True})

    shutil.rmtree(tmp_dir, ignore_errors=True)

    ready = []
    for name, t in wanted.items():
        path = os.path.join(tracks_dir, name)
        if os.path.exists(path):
            ready.append({
                "id": t["id"], "file": f"tracks/{name}",
                "title": t.get("title"), "artist": t.get("artist"),
                "album": t.get("album"), "track": t.get("track"),
                "disc": t.get("discNumber"), "year": t.get("year"),
                "duration": t.get("duration"), "size": os.path.getsize(path),
                "source_suffix": t.get("suffix"),
                "converted": needs_conversion(t.get("suffix"), device),
                "encoding": (current if needs_conversion(t.get("suffix"), device)
                             else "original"),
                "sources": sources.get(t["id"], []),
                **({"cover": covers[t["id"]], "cover_max": COVER_MAX}
                   if covers.get(t["id"], "unchecked") != "unchecked" else {}),
            })
    write_json_atomic(os.path.join(cache_dir, "manifest.json"), {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "targets": cfg["sync"]["targets"], "device": device,
        "playlist_tag": cfg["sync"].get("playlist_tag", ""),
        "tracks": ready, "playlists": playlists, "skipped": skipped,
    })
    return {"total": len(wanted), "ready": len(ready), "added": added,
            "removed": len(to_remove), "skipped": skipped}


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
    if manifest.get("skipped"):
        lines.append(f"  {len(manifest['skipped'])} not ready (see manifest 'skipped')")
    for pl in manifest.get("playlists", []):
        ids = pl["song_ids"]
        albums = []
        for i in ids:
            a = tracks.get(i, {}).get("album") or "?"
            if a not in albums:
                albums.append(a)
        missing = sum(1 for i in ids if i not in tracks)
        via = {"tag": " [tagged]", "config": " [config]"}.get(pl.get("via"), "")
        lines.append(f"  iPod playlist {pl['name']!r}{via}: {len(ids)} songs from "
                     f"{len(albums)} album(s): {', '.join(albums)}"
                     + (f" ({missing} not ready)" if missing else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ main ----
def main(argv=None):
    p = argparse.ArgumentParser(description="Music dock prepare job")
    p.add_argument("--config", help="config.toml path (default: %s)" % dock_config.DEFAULT_PATH)
    p.add_argument("--cache-dir", help="override cache_dir from the config")
    p.add_argument("--dry-run", action="store_true", help="show the plan only")
    p.add_argument("--show-plan", action="store_true",
                   help="print what the next sync would put on the player, from "
                        "the last run's manifest (no network)")
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

    try:
        os.nice(19)          # conversion must never slow a sync down
    except OSError:
        pass

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

    def current_screen():
        if args.preview_dir:
            return None          # previews don't touch the real screen state
        import dock_status
        return dock_status.load_state().get("screen")

    def show(name, *screen_args):
        if args.dry_run:
            return
        try:
            # dock_status checks --only-over under the panel lock.
            screen.show(name, *screen_args, "--only-over", DRAW_OVER)
        except Exception:
            log.exception("Screen update failed (continuing)")

    previous = read_manifest(cache_dir)
    client = Client(cfg["server"]["url"], cfg["server"]["username"],
                    cfg["server"]["password"])
    try:
        summary = prepare(client, cfg, cache_dir, show=show, dry_run=args.dry_run)
    except (SubsonicError, PrepareError) as e:
        log.error("Prepare failed: %s", e)
        return 1

    prev_ready = len(previous.get("tracks", []))
    changed = summary["added"] or summary["removed"] or prev_ready != summary["ready"]
    if changed or current_screen() == "preparing":
        show("idle", "--ready", str(summary["ready"]), "--total", str(summary["total"]))
    log.info("Done: %d/%d ready (%d added, %d removed, %d skipped)",
             summary["ready"], summary["total"], summary["added"],
             summary["removed"], len(summary["skipped"]))
    # Exit non-zero on real errors so `systemctl --failed` shows them; running
    # out of room is a limit, not an error.
    return 1 if any(s["error"] for s in summary["skipped"]) else 0


if __name__ == "__main__":
    sys.exit(main())
