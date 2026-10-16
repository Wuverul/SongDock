#!/usr/bin/env python3
"""
dock_prepare.py - prepare job: get the next sync's tracks ready.

Runs on a timer (musicdock-prepare.timer) as the login user, at low priority.
Never touches the player. Steps:
  1. Ask Navidrome for the target tracks: starred songs plus every track of
     a starred album.
  2. Remove cached tracks that are no longer targeted.
  3. Download each missing track, convert it if the player needs that, and
     move it into the cache:
       stock_ipod: MP3/AAC/ALAC kept as-is (no lossy-to-lossy re-encode);
                   FLAC and anything else -> convert_to (default MP3 V0)
       rockbox:    everything kept as-is
  4. Write manifest.json: the queue the sync job copies to the player.

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
def target_tracks(client, target):
    """Return the tracks to sync, de-duplicated, in artist/album/track order."""
    if target != "starred":
        raise PrepareError(f"target {target!r} isn't supported yet (only 'starred')")
    starred = client.call("getStarred2").get("starred2", {})
    tracks = {}
    for song in starred.get("song", []):
        tracks.setdefault(song["id"], song)
    for album in starred.get("album", []):
        for song in client.call("getAlbum", id=album["id"]).get("album", {}).get("song", []):
            tracks.setdefault(song["id"], song)
    if starred.get("artist"):
        log.info("Ignoring %d starred artist(s): only songs and albums are synced",
                 len(starred["artist"]))
    return sorted(tracks.values(), key=lambda s: (
        (s.get("artist") or "").lower(), (s.get("album") or "").lower(),
        s.get("discNumber") or 0, s.get("track") or 0, s.get("title") or ""))


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
    target = cfg["device"]["convert_to"]
    current = encoding(cfg["device"])
    options = audio_options(cfg["device"])
    max_bytes = int(float(cfg["sync"]["max_cache_gb"]) * 1e9)
    tracks_dir = os.path.join(cache_dir, "tracks")
    tmp_dir = os.path.join(cache_dir, "tmp")

    targets = target_tracks(client, cfg["sync"]["target"])
    wanted = {output_name(t, device, target): t for t in targets}
    existing = set(os.listdir(tracks_dir)) if os.path.isdir(tracks_dir) else set()
    to_remove = sorted(existing - wanted.keys())

    # Cached conversions made with different settings get re-done. Entries
    # from before encodings were recorded are assumed current.
    made_with = {t["id"]: t.get("encoding")
                 for t in read_manifest(cache_dir).get("tracks", [])}
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
            })
    write_json_atomic(os.path.join(cache_dir, "manifest.json"), {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": cfg["sync"]["target"], "device": device,
        "tracks": ready, "skipped": skipped,
    })
    return {"total": len(wanted), "ready": len(ready), "added": added,
            "removed": len(to_remove), "skipped": skipped}


# ------------------------------------------------------------------ main ----
def main(argv=None):
    p = argparse.ArgumentParser(description="Music dock prepare job")
    p.add_argument("--config", help="config.toml path (default: %s)" % dock_config.DEFAULT_PATH)
    p.add_argument("--cache-dir", help="override cache_dir from the config")
    p.add_argument("--dry-run", action="store_true", help="show the plan only")
    p.add_argument("--preview-dir", metavar="DIR",
                   help="save screens as PNGs instead of drawing on the panel")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

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
