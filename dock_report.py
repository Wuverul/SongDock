#!/usr/bin/env python3
"""
dock_report.py - a status report for the dock, for checking in remotely.

    musicdock-status                          (on the Pi; install.sh adds it)
    musicdock-status --since "3 days ago"
    ./status.sh                               (on the Mac: runs it over SSH)

Read-only: it looks at the record, the staged plan, the screen state, the
journal and the system, and changes nothing (the record is opened read-only).
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCREEN_NAMES = {
    "idle": "DOCK READY", "preparing": "PREPARING", "syncing": "SYNCING",
    "prepsync": "PREPARE & SYNC",
    "safe": "SAFE TO REMOVE", "failed": "SYNC FAILED", "starting": "STARTING UP",
    "offline": "NO WI-FI", "notsynced": "NOT SYNCED",
    "wifi": "WI-FI CHANGED", "wififail": "WI-FI UNCHANGED",
    "setup": "DOCK UPDATED", "setupfail": "SETUP NOT USED", "connecting": "CONNECTING",
}
# Messages that mean "the server (or network) was down", not a dock problem.
NETWORK_HINTS = ("can't reach server", "Server unreachable", "name resolution")
LEVELS = ("INFO ", "WARNING ", "ERROR ")


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ago(ts, now=None):
    if not ts:
        return "never"
    s = int((now or time.time()) - ts)
    if s < 90:
        return f"{max(s, 0)} s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s / 3600:.1f} h ago"
    return f"{s // 86400} days ago"


def duration(s):
    s = int(s)
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{max(s // 60, 1)} min"


def when(ts):
    return time.strftime("%a %H:%M", time.localtime(ts)) if ts else "never"


def human_size(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def strip_level(msg):
    for lvl in LEVELS:
        if msg.startswith(lvl):
            return lvl.strip(), msg[len(lvl):]
    return None, msg


def parse_journal(text):
    """[(unix time, message)] from `journalctl -o short-unix` output
    ("<time> <host> <ident>[<pid>]: <message>"; plain text is several times
    faster than -o json on a Pi Zero)."""
    out = []
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or ": " not in parts[2]:
            continue
        try:
            out.append((float(parts[0]), parts[2].split(": ", 1)[1]))
        except ValueError:
            pass                       # "-- No entries --", boot markers
    return out


def journal(units, since):
    """[(unix time, message)] for the given units since `since`, oldest first."""
    args = ["journalctl", "--no-pager", "-o", "short-unix", "--since", since]
    for u in units:
        args += ["-u", u]
    return parse_journal(run(*args))


# ------------------------------------------------------- journal summaries ----
def real_errors(events):
    """ERROR lines that aren't explained by the server being unreachable
    (an outage logs ERROR + traceback ending in "can't reach server")."""
    outage_times = [t for t, m in events if any(h in m for h in NETWORK_HINTS)]
    out = []
    for t, m in events:
        level, text = strip_level(m)
        if level == "ERROR" and not any(abs(t - o) <= 2 for o in outage_times):
            out.append((t, text))
    return out


def summarize_prepare(events, now=None):
    """What the prepare job has been up to, from its journal lines."""
    now = now or time.time()
    s = {"checks": 0, "failed_runs": 0, "added": 0, "removed": 0,
         "outage_runs": 0, "last_done": None, "batch": None}
    outage_in_run = False
    batch = None
    for t, m in events:
        level, text = strip_level(m)
        if text.startswith("Starting musicdock-prepare"):
            s["checks"] += 1
            outage_in_run = False
        elif text.startswith("Failed to start musicdock-prepare"):
            s["failed_runs"] += 1
        elif any(h in text for h in NETWORK_HINTS) and not outage_in_run:
            s["outage_runs"] += 1
            outage_in_run = True
        elif text.startswith("Getting "):
            batch = {"started": t, "total": int(text.split()[1]), "progress": []}
        elif text.startswith("Screen: PREPARING --done") and batch:
            parts = text.split()
            a = dict(zip(parts[2::2], parts[3::2]))
            batch["progress"].append((t, int(a.get("--done", 0)), int(a.get("--total", 0))))
        elif text.startswith("Done:"):
            nums = re.search(r"\((\d+) added, (\d+) removed", text)
            if nums:
                s["added"] += int(nums.group(1))
                s["removed"] += int(nums.group(2))
            s["last_done"] = (t, text[len("Done: "):])
            batch = None
    if batch and batch["progress"]:
        # Rate from this batch's own progress: works out the time left.
        t0, d0, _ = batch["progress"][0]
        t1, d1, total = batch["progress"][-1]
        rate = (t1 - t0) / (d1 - d0) if d1 > d0 else None
        batch.update(done=d1, total=total, per_track=rate,
                     left=rate * (total - d1) if rate else None)
        s["batch"] = batch
    elif batch:
        batch.update(done=0, per_track=None, left=None)
        s["batch"] = batch
    return s


def summarize_syncs(events):
    """Docking sessions: [(time, summary dict or None, outcome)]."""
    sessions = []
    for t, m in events:
        level, text = strip_level(m)
        if text.startswith("Starting musicdock-sync"):
            sessions.append({"started": t, "summary": None, "screen": None})
        elif not sessions:
            continue
        elif text.startswith("Session summary: "):
            try:
                import ast
                sessions[-1]["summary"] = ast.literal_eval(text[len("Session summary: "):])
            except (ValueError, SyntaxError):
                pass
        elif text.startswith("Screen: ") and text.split()[1] in (
                "SAFE", "FAILED", "NOTSYNCED", "WIFI", "WIFIFAIL"):
            sessions[-1]["screen"] = text.split()[1]
    return sessions


def describe_session(s):
    summ = s["summary"] or {}
    bits = []
    if summ.get("added") or summ.get("removed"):
        bits.append(f"+{summ.get('added', 0)} -{summ.get('removed', 0)} tracks")
    if summ.get("plays_new"):
        bits.append(f"{summ['plays_new']} plays read, {summ.get('plays_sent', 0)} sent")
    if summ and not summ.get("wrote", True):
        bits.append("read-only")
    outcome = {"SAFE": "OK", "FAILED": "FAILED", "NOTSYNCED": "not synced",
               "WIFI": "OK, Wi-Fi changed", "WIFIFAIL": "OK, Wi-Fi unchanged"}.get(
        s["screen"], "running" if s["screen"] is None else s["screen"])
    return f"{when(s['started'])}: {outcome}" + (f" ({', '.join(bits)})" if bits else "")


# ---------------------------------------------------------------- sections ----
def system_section():
    up = run("uptime", "-p").strip().replace("up ", "")
    temp = run("vcgencmd", "measure_temp").strip().split("=")[-1] or "?"
    thr = run("vcgencmd", "get_throttled").strip().split("=")[-1] or "?"
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            mem[k] = int(v.split()[0]) // 1024
    disk = shutil.disk_usage("/")
    pct = disk.used * 100 // disk.total
    power = {"0x0": "OK", "?": "?"}.get(thr, f"WARNING ({thr})")
    lines = [f"Up {up or '?'} | {temp} | power {power}",
             f"Memory {mem.get('MemAvailable', 0)} MB free of {mem.get('MemTotal', 0)} | "
             f"SD card {human_size(disk.used)} of {human_size(disk.total)} ({pct}%)"]
    wifi = run("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi")
    active = [l.rsplit(":", 1) for l in wifi.splitlines() if l.startswith("yes:")]
    ip = run("hostname", "-I").split()
    if active:
        lines.append(f"Wi-Fi \"{active[0][0][4:]}\" signal {active[0][1]}% | "
                     f"IP {ip[0] if ip else '?'}")
    else:
        lines.append("Wi-Fi: not connected")
    try:
        t = float(re.sub(r"[^0-9.]", "", temp))
    except ValueError:
        t = 0
    return lines, {"throttled": thr, "disk_pct": pct, "temp": t}


def screen_section(state_file):
    try:
        with open(state_file) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return ["Screen: unknown"], {}
    argv = st.get("argv") or [st.get("screen", "?")]
    name = SCREEN_NAMES.get(st.get("screen"), st.get("screen"))
    detail = ""
    if "--done" in argv:
        a = dict(zip(argv[1::2], argv[2::2]))
        detail = f" track {int(a.get('--done', 0)) + 1} of {a.get('--total')}"
    if "--detail" in argv:
        detail += " · " + argv[argv.index("--detail") + 1].replace("\n", " · ")
    return [f"Screen: {name}{detail} (drawn {ago(st.get('last_draw'))})"], {}


def open_record(path):
    """The record, read-only (never creates or changes it)."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def ipod_section(record_path, manifest, now=None):
    import ipod_handler
    if not os.path.exists(record_path):
        return ["iPod: never synced"], {}
    db = open_record(record_path)
    try:
        # Skip forgotten players (the column may not exist yet: read-only
        # here, and a record from an older version gains it on its next write).
        p = next((r for r in db.execute("SELECT * FROM players WHERE last_sync IS NOT "
                                        "NULL ORDER BY last_sync DESC")
                  if "forgotten_at" not in r.keys() or r["forgotten_at"] is None), None)
        if not p:
            return ["iPod: never synced"], {}
        present = [r[0] for r in db.execute(
            "SELECT navidrome_id FROM tracks WHERE guid = ? AND state = 'present'",
            (p["guid"],))]
        sent, waiting, unmatched, oldest = db.execute(
            "SELECT SUM(sent_at IS NOT NULL), "
            "SUM(sent_at IS NULL AND navidrome_id IS NOT NULL), "
            "SUM(sent_at IS NULL AND navidrome_id IS NULL), "
            "MIN(CASE WHEN sent_at IS NULL AND navidrome_id IS NOT NULL "
            "THEN queued_at END) FROM pending_plays").fetchone()
    finally:
        db.close()
    lines = [f"iPod ...{p['guid'][-4:]} ({p['model'] or 'model ?'}): last synced "
             f"{when(p['last_sync'])} ({ago(p['last_sync'], now)}), {len(set(present))} tracks"]
    if manifest:
        add, remove = ipod_handler.pending_changes(manifest, present)
        lines.append("Next sync: " + (f"+{add} -{remove} tracks" if add or remove
                                      else "nothing to change"))
    plays = f"Plays: {sent or 0} sent so far, {waiting or 0} waiting to send"
    if unmatched:
        plays += f", {unmatched} unmatched (songs the dock didn't add)"
    lines.append(plays)
    keys = p.keys()
    player = {"on_player": len(set(present)),
              "capacity": p["capacity"] if "capacity" in keys else None,
              "kind": p["kind"] if "kind" in keys else None}
    return lines, {"plays_waiting": waiting or 0, "oldest_waiting": oldest,
                   "player": player}


def music_section(manifest, cfg, cache_dir):
    if not manifest:
        return ["No plan yet (the prepare job hasn't finished a run)"], {}
    names = []
    for pl in manifest.get("playlists", []):
        names.append(pl["name"] + (" (tagged)" if pl.get("via") == "tag" else ""))
    lines = ["Syncing: " + (", ".join(names) or "nothing")]
    ready = len(manifest.get("tracks", []))
    skipped = manifest.get("skipped", [])
    kept = manifest.get("kept_after_removal", 0)
    line = f"Plan: {ready} tracks ready"
    if manifest.get("on_player"):
        line += f", {len(manifest['on_player'])} on the iPod only (cache full)"
    if skipped:
        line += f", {len(skipped)} not ready yet"
    if kept:
        line += f", {kept} removed but kept for now"
    lines.append(line)
    tracks_dir = os.path.join(cache_dir, "tracks")
    size = 0
    if os.path.isdir(tracks_dir):
        with os.scandir(tracks_dir) as it:
            size = sum(e.stat().st_size for e in it if e.is_file())
    limit = float(cfg["sync"].get("max_cache_gb") or 0) * 1e9 if cfg else 0
    cache = f"Cache: {human_size(size)}"
    if limit:
        cache += f" of {human_size(limit)} limit ({size * 100 / limit:.0f}%)"
    lines.append(cache)
    return lines, {"cache_pct": size * 100 / limit if limit else 0}


def next_check():
    """When the prepare timer fires next, as Unix time (None while running)."""
    try:
        timers = json.loads(run("systemctl", "list-timers", "musicdock-prepare.timer",
                                "-o", "json", "--no-pager") or "[]")
        nxt = timers[0].get("next") if timers else None
        return nxt / 1e6 if nxt else None
    except (ValueError, TypeError):
        return None


def activity_section(since, now=None):
    now = now or time.time()
    prep_events = journal(["musicdock-prepare.service"], since)
    sync_events = journal(["musicdock-sync@*"], since)
    prep = summarize_prepare(prep_events, now)
    running = run("systemctl", "is-active", "musicdock-prepare").strip() in (
        "active", "activating")
    lines = []
    b = prep["batch"]
    if running and b:
        line = f"Prepare job: getting tracks, {b['done']}/{b['total']} done"
        if b["per_track"]:
            line += (f", about {duration(b['left'])} left "
                     f"({b['per_track']:.0f} s per track)")
        lines.append(line)
    elif running:
        lines.append("Prepare job: checking for new music now")
    else:
        line = "Prepare job: idle"
        if prep["last_done"]:
            line += f", last check {ago(prep['last_done'][0], now)}"
        nxt = next_check()
        if nxt:
            line += f", next in {duration(max(nxt - now, 60))}"
        lines.append(line)

    sessions = summarize_syncs(sync_events)
    line = (f"Since {since}: {prep['checks']} checks, {prep['added']} tracks added, "
            f"{prep['removed']} removed, {len(sessions)} docking session"
            + ("" if len(sessions) == 1 else "s"))
    lines.append(line)
    if prep["outage_runs"]:
        lines.append(f"Server unreachable during {prep['outage_runs']} checks "
                     f"(retried automatically)")
    for s in sessions[-3:]:
        lines.append("Dock " + describe_session(s))

    failed = run("systemctl", "--failed", "--no-legend", "--plain")
    facts = {"errors": real_errors(prep_events + sync_events),
             "failed_units": [l.split()[0] for l in failed.splitlines() if "musicdock" in l],
             "failed_syncs": [s for s in sessions if s["screen"] == "FAILED"]}
    return lines, facts


def problems(facts, now=None):
    now = now or time.time()
    out = []
    if facts.get("failed_units"):
        out.append("Failed services: " + ", ".join(facts["failed_units"]))
    if facts.get("throttled") not in (None, "0x0", "?"):
        out.append(f"Power or heat warning since boot (throttled={facts['throttled']}): "
                   f"check the power supply")
    if facts.get("temp", 0) >= 70:
        out.append(f"Running hot: {facts['temp']:.0f} C")
    if facts.get("disk_pct", 0) >= 75:
        out.append(f"SD card {facts['disk_pct']}% full")
    if facts.get("cache_pct", 0) >= 90:
        out.append(f"Cache at {facts['cache_pct']:.0f}% of max_cache_gb")
    if facts.get("plays_waiting") and facts.get("oldest_waiting") and \
            now - facts["oldest_waiting"] > 86400:
        out.append(f"{facts['plays_waiting']} plays waiting over a day "
                   f"(is the server reachable?)")
    out += facts.get("size_notes", [])
    for s in facts.get("failed_syncs", []):
        out.append(f"Sync failed {when(s['started'])} (see journalctl -u 'musicdock-sync@*')")
    errors = facts.get("errors", [])
    for t, m in errors[-5:]:
        out.append(f"{when(t)}: {m[:100]}")
    if len(errors) > 5:
        out.append(f"... and {len(errors) - 5} more errors")
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="SongDock status report (read-only)")
    p.add_argument("--since", default="24 hours ago",
                   help='how far back to look in the logs (default "24 hours ago")')
    args = p.parse_args(argv)

    import dock_config
    import dock_record
    import dock_status
    try:
        cfg = dock_config.load()
    except dock_config.ConfigError as e:
        cfg = None
        print(f"(settings: {e})")
    cache_dir = cfg["sync"]["cache_dir"] if cfg else os.path.expanduser("~/musicdock/cache")
    try:
        with open(os.path.join(cache_dir, "manifest.json")) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        manifest = {}

    try:
        with open(os.path.join(HERE, "VERSION")) as f:
            version = f.read().strip()
    except OSError:
        version = "SongDock"
    print(f"{version} - status at {time.strftime('%a %d %b %H:%M')}")

    facts = {}
    sections = [("Dock", system_section),
                (None, lambda: screen_section(dock_status.STATE_FILE)),
                ("iPod", lambda: ipod_section(dock_record.DEFAULT_PATH, manifest)),
                ("Music", lambda: music_section(manifest, cfg, cache_dir)),
                ("Activity", lambda: activity_section(args.since))]
    for title, fn in sections:
        try:
            lines, found = fn()
        except Exception as e:          # one unreadable part shouldn't hide the rest
            lines, found = [f"(couldn't read this part: {e})"], {}
        facts.update(found)
        if title:
            print(f"\n{title}")
        for line in lines:
            print(f"  {line}")
    if facts.get("player"):
        import dock_limits
        import dock_restore
        pl = facts["player"]
        facts["size_notes"] = dock_limits.player_notes(
            pl["on_player"], len(dock_restore.targets(manifest)), pl["capacity"], cfg,
            pl["kind"], dock_limits.plan_bytes(manifest))
    print("\nProblems")
    for line in problems(facts) or ["none"]:
        print(f"  {line}")


if __name__ == "__main__":
    main()
