"""
dock_share.py - a report to paste into a bug report or send a friend helping out:

    musicdock-status --share

It prints and sends nothing anywhere. Most of it is a fixed list of facts,
each chosen on purpose, so a new setting or field can't slip in by itself.
Only the error lines are free text, so those lose the library's names and
everything goes through scrub(): the server's address, sign-in details, Wi-Fi
names, iPod names, playlist names, addresses and device IDs.
"""

import collections
import os
import platform
import re
import shutil
import time

import dock_limits
import dock_report as report

SPEED_SINCE = "30 days ago"
MAX_ERRORS = 10
SHARED_SETTINGS = {
    "device": ("type", "convert_to", "mp3_quality", "flac", "write", "auto_resync"),
    "sync": ("max_cache_gb", "workers", "sync_batch_size", "check_interval_minutes",
             "removal_grace_days", "recent_play_months", "make_room", "favorite_rating", "auto_update",
             "safe_hours", "primary_artist"),
    "display": ("model", "theme", "invert"),
}
CONVERSION = re.compile(r"Conversion: ([\d.]+) s for ([\d.]+) min of audio \((.+)\)")
COPIED = re.compile(r"Copied (\d+) tracks, ([\d.]+) MB in ([\d.]+) s")


def private_words(cfg, manifest, players, extra=()):
    """Strings that identify this dock's owner, longest first."""
    words = set(extra)
    server = (cfg or {}).get("server", {})
    urls = server.get("url") or []
    for url in [urls] if isinstance(urls, str) else urls:
        words.add(url)
        words.add(re.sub(r"^\w+://|[:/].*$", "", url))
    words.update(str(server.get(k) or "") for k in ("username", "password"))
    words.update(str(p.get("name") or "") for p in players)
    words.update(str(p.get("name") or "") for p in (manifest or {}).get("playlists", []))
    return sorted((w for w in words if len(w) >= 2), key=len, reverse=True)


def scrub(text, words=()):
    """The text without private words, web and network addresses or device IDs."""
    for word in words:
        text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", "<private>", text)
    text = re.sub(r"\b\w+://\S+", "<address>", text)
    text = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<ip>", text)
    text = re.sub(r"\b[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}\b", "<mac>", text)
    text = re.sub(r"\b[0-9a-fA-F]{16}\b", "<id>", text)
    return re.sub(r"/home/[^/\s]+", "~", text)


def song_names(manifest):
    """Titles, artists and albums, so error lines can lose them."""
    names = set()
    for key in ("tracks", "on_player"):
        for t in (manifest or {}).get(key, []):
            names.update(str(t.get(k) or "") for k in ("title", "artist", "album", "album_artist"))
    for s in (manifest or {}).get("skipped", []):
        names.add(str(s.get("title") or ""))
    return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)


def clean_error(text, names):
    """An error line without library names, shortened."""
    for name in names:
        if name in text:
            text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", "<song>", text)
    return text[:200]


def speeds(lines):
    """Lines about conversion and copying speed, from the log's measurements."""
    by_format, copies = collections.defaultdict(lambda: [0.0, 0.0, 0]), []
    for line in lines:
        m = CONVERSION.search(line)
        if m:
            entry = by_format[m[3]]
            entry[0] += float(m[1])
            entry[1] += float(m[2])
            entry[2] += 1
        m = COPIED.search(line)
        if m:
            copies.append((int(m[1]), float(m[2]), float(m[3])))
    out = []
    for fmt, (seconds, minutes, count) in sorted(by_format.items()):
        if minutes:
            out.append(f"Conversion to {fmt}: {seconds / minutes:.1f} s per minute of audio "
                       f"({count} songs)")
    songs, mb, seconds = (sum(c[i] for c in copies) for i in range(3))
    if seconds:
        out.append(f"Copying to players: {mb / seconds:.1f} MB/s, {seconds / max(songs, 1):.1f} s "
                   f"per song ({int(songs)} songs)")
    return out or ["No measurements yet"]


def system_lines():
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\0\n ")
    except OSError:
        model = platform.machine()
    os_name = ""
    try:
        with open("/etc/os-release") as f:
            os_name = dict(line.rstrip().split("=", 1) for line in f if "=" in line).get(
                "PRETTY_NAME", "").strip('"')
    except (OSError, ValueError):
        pass
    disk = shutil.disk_usage("/")
    _, facts = report.system_section()
    memory = {}
    try:
        with open("/proc/meminfo") as f:
            memory = {line.split(":")[0]: int(line.split()[1]) // 1024 for line in f}
    except (OSError, ValueError, IndexError):
        pass
    signal = [line.rsplit(":", 1)[-1] for line in report.run(
        "nmcli", "-t", "-f", "ACTIVE,SIGNAL", "device", "wifi").splitlines()
        if line.startswith("yes:")]
    return [f"Pi: {model} ({platform.machine()})",
            f"System: {os_name or platform.system()}, kernel {platform.release()}, "
            f"Python {platform.python_version()}",
            f"Memory: {memory.get('MemAvailable', '?')} MB free of "
            f"{memory.get('MemTotal', '?')} MB",
            f"SD card: {report.human_size(disk.used)} of {report.human_size(disk.total)}",
            f"Temperature {facts.get('temp')} C, throttled {facts.get('throttled')}",
            f"Wi-Fi signal: {signal[0] + '%' if signal else 'not connected'}"], facts


def player_lines(record_path, now):
    """One line per player, and the rows, read without opening the record for writing."""
    db = report.open_record(record_path)
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM tracks t WHERE t.guid = p.guid AND "
            "t.state = 'present') AS present FROM players p ORDER BY p.last_sync DESC")]
        waiting = db.execute("SELECT SUM(sent_at IS NULL AND navidrome_id IS NOT NULL), "
                             "SUM(sent_at IS NULL AND navidrome_id IS NULL) "
                             "FROM pending_plays").fetchone()
    finally:
        db.close()
    out = []
    for p in rows:
        capacity = report.human_size(p["capacity"]) if p.get("capacity") else "size unknown"
        line = (f"{p.get('kind') or 'kind unknown'}, model {p.get('model') or '?'}, {capacity}, "
                f"{p['present']} songs, last sync {report.ago(p.get('last_sync'), now)}")
        line += ", forgotten" if p.get("forgotten_at") else ""
        line += ", full" if p.get("full") else ""
        if p.get("swaps_no_room"):
            line += f", {p['swaps_no_room']} format changes waiting for room"
        out.append(line)
    out.append(f"Plays waiting to send: {waiting[0] or 0}, unmatched: {waiting[1] or 0}")
    return out, rows


def plan_lines(manifest, cache_dir):
    if not manifest:
        return ["No plan yet"]
    planned = [e for e, _ in dock_limits.planned_songs(manifest)]
    formats = collections.Counter(e.get("encoding") or "?" for e in planned)
    sources = collections.Counter((e.get("source_suffix") or "?").lower() for e in planned)
    total = dock_limits.plan_bytes(manifest)
    reasons = collections.Counter(re.sub(r"[\d.]+", "n", str(s.get("reason"))[:60])
                                  for s in manifest.get("skipped", []))
    lines = [f"Songs planned: {len(planned)}"
             + (f", about {report.human_size(total)}" if total else ""),
             "Output formats: " + ", ".join(f"{k} {v}" for k, v in formats.most_common()),
             "Source formats: " + ", ".join(f"{k} {v}" for k, v in sources.most_common()),
             f"Playlists synced: {len(manifest.get('playlists', []))}, "
             f"lossless playlists: {len(manifest.get('lossless_playlists', []))}",
             f"Cached: {len(manifest.get('tracks', []))}, player only: "
             f"{len(manifest.get('on_player', []))}, not ready: {len(manifest.get('skipped', []))}"]
    lines += [f"Not ready because: {k} ({v})" for k, v in reasons.most_common(5)]
    return lines


def settings_lines(cfg):
    if not cfg:
        return ["Settings couldn't be read"]
    lines = []
    for section, keys in SHARED_SETTINGS.items():
        values = cfg.get(section, {})
        shown = [f"{k} = {values[k]}" for k in keys if k in values]
        if shown:
            lines.append(f"{section}: " + ", ".join(shown))
    urls = cfg.get("server", {}).get("url") or []
    lines.append(f"server addresses: {1 if isinstance(urls, str) else len(urls)}")
    return lines


def build(cfg, manifest, cache_dir, record_path, version, since, now=None, screen=None):
    """The whole report as text."""
    now = now or time.time()
    out = [f"SongDock report: {version}, {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(now))}",
           f"Logs since: {since}"]
    facts, rows = {}, []

    def section(title, fn):
        nonlocal out
        out += ["", title]
        try:
            out += [f"  {line}" for line in fn()]
        except Exception as e:      # one unreadable part shouldn't lose the rest
            out.append(f"  (couldn't read this part: {type(e).__name__})")

    def dock():
        lines, found = system_lines()
        facts.update(found)
        return lines + ([f"Screen: {screen}"] if screen else [])

    def players():
        nonlocal rows
        lines, rows = player_lines(record_path, now)
        return lines

    section("Dock", dock)
    section("Players", players)
    section("Music", lambda: plan_lines(manifest, cache_dir))
    section("Settings", lambda: settings_lines(cfg))
    section("Speed (last 30 days)", lambda: speeds(report.run(
        "journalctl", "--no-pager", "-o", "cat", "--since", SPEED_SINCE,
        "-g", "Conversion: |Copied .* MB in", "-u", "musicdock-prepare.service",
        "-u", "musicdock-sync@*").splitlines()))

    def activity():
        lines, found = report.activity_section(since, now)
        facts.update(found)
        return [line for line in lines if not line.startswith("Dock ")]
    section("Activity", activity)

    names = song_names(manifest)

    def problems():
        import dock_update
        update = dock_update.load_state()
        if update.get("failed"):
            facts["update_failed"] = f"Update {update['failed']} not installed: " \
                                     f"{update.get('failed_why')}"
        return [clean_error(p, names) for p in report.problems(facts, now)] or ["none"]
    section("Problems", problems)

    def errors():
        found = facts.get("errors", [])
        lines = [f"{time.strftime('%m-%d %H:%M', time.localtime(t))} {clean_error(m, names)}"
                 for t, m in found[-MAX_ERRORS:]]
        return ([f"{len(found)} in all, the last {len(lines)}:"] if found else ["none"]) + lines
    section("Errors", errors)

    ssids = report.run("nmcli", "-t", "-f", "NAME", "connection", "show").splitlines()
    words = private_words(cfg, manifest, rows, [
        *ssids, platform.node(), os.environ.get("USER") or "",
        *(p.replace("musicdock-", "", 1) for p in ssids)])
    return scrub("\n".join(out), words) + "\n"
