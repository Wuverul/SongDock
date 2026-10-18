# Music Dock

A Raspberry Pi dock that keeps an iPod's music in sync with a Navidrome server:
it uploads what was played, pulls the current weekly playlists, and charges the
player overnight. An e-ink screen shows whether it's safe to unplug.

## Scope

- **In scope:** the dock (Pi) and the player-side sync.
- **Out of scope:** the Navidrome server, music discovery, and the purge rules.
  The server is a separate project run by the server owner. The dock only reads
  playlists, reports plays, and marks keepers through the Subsonic API.
- Discovery tracks are held on the server for 1–2 months, so the dock has time
  to report plays before anything is purged.

## Hardware

- **Raspberry Pi Zero W (first gen):** ARMv6, single core, 512MB RAM, 2.4GHz
  Wi-Fi only. Runs **Raspberry Pi OS Lite (32-bit)**. Claude Code can't run on
  the Pi; develop on the Mac, deploy over SSH.
- **Display:** Waveshare 2.13" e-Paper HAT **V4** (250x122), driver
  `epd2in13_V4`. Image must be rotated **270°** (confirmed on hardware). SPI is
  enabled.
- **Network:** the Pi has a fixed IP. It needs SSH from the development machine and a route to the Navidrome server.
- **Player now:** iPod Nano 4th gen, **stock firmware** (Rockbox doesn't
  support it). 16GB. Plays AAC/MP3/ALAC, **not FLAC**. The dock converts to
  **MP3 (LAME V0)** for it (decided 2026-10-16; see Notes).
- **Player later:** possibly a modded iPod (e.g. a Classic with 512 GB of
  flash), **still on stock firmware**: the user prefers the stock UI, so a
  Rockbox conversion is unlikely (decided 2026-10-16). Plan for players whose
  storage exceeds the Pi's (see "Resilience without a mirror").
- **Connection:** Pi "USB" port → micro-USB OTG adapter → USB-A to 30-pin
  cable. The Pi powers and charges the iPod.

## Architecture

Two independent jobs share one e-ink screen:

1. **Prepare job** (scheduled, never touches the player). Fetches the target
   playlists from Navidrome, downloads tracks not yet cached, converts them to
   the player's format, and prunes tracks that left the playlists. Screens:
   `idle`, `preparing`.
2. **Sync job** (runs when the player docks). Screens: `syncing`, `safe`,
   `failed`. Steps, in order:
   1. Show `syncing` **before** mounting anything.
   2. Mount the player.
   3. Read play data, queue it on the Pi (flushed to disk), then try to
      upload the queue to Navidrome. If the server is unreachable, keep
      going: plays stay queued and the sync still copies music.
   4. Read the on-device keep list and mark those tracks on the server.
   5. Copy new tracks from the cache, remove purged ones, update playlists.
   6. Flush, unmount, show `safe` (or `failed`).

**Device handlers.** One sync engine, with one handler per player type. Only
the "write tracks / read plays" step differs:

| | Stock iPod (now) | Rockbox (later) |
|---|---|---|
| Library | `iTunesDB` (signed), via **pygpod** | Plain files + `.m3u8` playlists |
| Play data | Play-count changes since last sync | `.scrobbler.log`, timestamped |
| Formats | FLAC → MP3 LAME V0 (`convert_to`, `mp3_quality`: `v0`–`v9` or constant `128k`–`320k`; `alac`/`aac` also supported). The manifest records each track's `encoding`; changing settings re-converts on the next run | FLAC copied as-is |
| Keep signal | TBD (on-device rating?) | `Keep.m3u8` playlist |
| Album art | Thumbnails in `iPod_Control/Artwork/` (ArtworkDB), made from the embedded cover when each track is added. Ignores art inside the file. | `cover.jpg` (baseline, not progressive JPEG) in the album folder |

pygpod is **pre-alpha** and untested on the Nano 4G, so prove it with a manual
test (read DB, add one track **with album art**, play it, check the art shows,
read the play count back) before building
on it. libgpod is the fallback.

**Conversion runs on the Pi** in the prepare job, at low priority (`nice`):
```
ffmpeg -i in.flac -c:a libmp3lame -q:a 0 -id3v2_version 3 -map_metadata 0 \
  -c:v copy -disposition:v attached_pic out.mp3
```
Not `-c:a aac`: ffmpeg's built-in AAC encoder adds audible artifacts (Notes).
The cache holds only the **weekly rotation**, not the whole library. The static
core library is also loaded **through the dock** (one long first sync; faster
on a Pi Zero 2 W), never by Finder/iTunes: Finder replaces the whole iTunesDB,
wiping the dock's tracks and IDs. Keep the SD card (64GB; 57G root filesystem) under ~80% full.

**Targets** (`[sync] targets` in config): `"starred"` (starred songs; starred
albums expand to their tracks) and/or `"playlist:<name>"`. Currently
`["starred", "playlist:Mel Test"]`: Mel Test stands in for an explicit "sync to
iPod" playlist. Dynamic playlists from the service come later, as more
`playlist:` targets. A song in several targets is stored and copied once
(keyed by Navidrome song ID, which is the same via every endpoint; every
cached track also has a MusicBrainz recording ID, none shared). Playlists are
found by name, remembered by ID (renames survive); a playlist that can't be
found stops the run rather than pruning its tracks. `"starred"` also becomes a
player playlist named by `favorites_playlist` (default "Favorites"; "" for
none), in artist/album/track order. **Choosing playlists:** any
playlist whose Navidrome comment contains `playlist_tag` (default `#ipod`,
whole word, any case) is synced; removing the tag unsyncs it at the next run.
The user edits comments in the Navidrome web UI (the field is easy to miss;
Navidrome returns it from getPlaylists/getPlaylist). Config `playlist:`
entries remain for playlists the user can't edit (e.g. service-owned dynamic
ones); a playlist both tagged and configured counts once. If the server lists
no playlists at all but some were tagged last run, the run stops without
pruning. Currently: `targets = ["starred"]` + Mel Test via its `#iPod` tag.

## Safety rules (do not simplify these away)

- **Only players that opted in are touched.** An iPod opts in with a
  `MediaDock.txt` file in its top folder (any case). Without it the dock reads
  nothing (no plays) and writes nothing, and shows `notsynced` ("No
  MediaDock.txt on this iPod" / "Not an iPod"). Players the record already
  syncs get the marker created on their next write-mode sync. Every player is
  mounted read-only first and remounted read-write only after this check.

- **Never clear play data until the server confirms it received it, or it is
  durably queued on the Pi** (written and fsynced before the iPod's copy can be
  cleared). Queued plays are removed only after the server confirms them. A
  failed sync or an unreachable server must lose nothing. (User decision,
  2026-10-16: music still syncs when the server is down; plays wait.)
- **Read the iPod's play data before any database write.** `Play Counts`
  entries have no track IDs; they match tracks by position in `iTunesDB`, so
  after the DB changes they would be credited to the wrong tracks. Never leave
  a `Play Counts` file behind across a DB rewrite.
- **Show `syncing` before mounting.** A crash then leaves "do not remove" on
  screen, which is the safe failure.
- **The prepare job never draws over `syncing`** (nor `safe`, `failed`, or
  the boot screens: it passes `--only-over none,idle,preparing`). Only the sync job (or
  `--force`) can replace it. This is enforced and tested in `dock_status.py`.
  One exception: at power-on, `dock_boot.py` replaces a leftover `syncing`
  with `failed` ("Sync interrupted by restart"), because after a reboot no
  sync is running and nothing is mounted. It reports the interruption rather
  than hiding it.
- **All jobs share one screen-state file** (`/var/lib/musicdock/dock_status.json`
  on the Pi), or the check above can't see another job's screen.
- **Stock iPod: back up `iPod_Control/iTunes/` before every database write.**
  On failure, restore the backup and show `failed`.
- **Never delete from the player before its play data has been uploaded.**
- **Every e-ink screen shows the time**, so a stale screen can't pass as
  current. Exception: `starting` and `offline` (boot) show uptime until the
  clock is set from the network, because the Pi Zero has no clock battery.
- **Rate-limit e-ink redraws** and always put the panel to sleep after drawing.
  Exception: the boot screen's partial-update burst (`PartialUpdates`, every
  10s for at most 5 min) keeps the panel awake between frames, because the
  driver's `sleep()` closes SPI. It always sleeps when the burst ends, even on
  error.

## Credentials

- Config lives at `~/.config/musicdock/config.toml` on the Pi, mode `600`.
- Use Subsonic **token auth** (salted hash), not the plaintext password.
- Never commit credentials. See `.gitignore` and `config.example.toml`.
- The dock signs in as the user's own Navidrome account, because play counts
  are per user.

## Files

- `dock_status.py`: e-ink status screens (`idle`, `preparing`, `syncing`,
  `safe`, `failed`), with a Wi-Fi indicator and IP address. `--preview out.png`
  renders without hardware; `--wifi -56|off` and `--ip` fake readings for
  previews. `DOCK_STATE_FILE` overrides where screen state is kept.
- `dock_sync.py`: sync job (runs under the venv). Identifies the player from
  udev (`ID_SERIAL_SHORT` = FireWire GUID, `ID_MODEL_ID`), mounts it (read-only
  unless `[device] write = true`), runs `ipod_handler.sync_player` between
  mount and unmount (6 steps on `syncing`), unmounts/ejects, shows `safe`
  (added/removed/plays sent/queued) or `failed`. `--list-only` = the Phase 2
  check; `--write` forces a full sync (for fake devices). `--fake-device DIR` uses a folder
  and writes screen PNGs to `previews/`; `--fake-fail mount|unmount` tests
  error paths.
- `dock_boot.py`: boot screens, run once per power-on by
  `musicdock-boot.service` (not ordered after the network, so it starts
  early). Shows `starting`, updated in place every 10s as Wi-Fi, IP, and
  network time arrive, then `idle --boot` ("Powered on <time>") once all are
  ready. After 5 min without them it shows `offline` (names the SSID or the
  time server, and points to the serial console at 115200 baud) and keeps
  waiting with no time limit. Syncs are ordered after this service, so they
  wait for the network too. `--preview-dir` saves every frame as a PNG.
- `pi/`: udev rule, `musicdock-sync@.service` template (ordered after the boot
  service), `musicdock-boot.service`, `install.sh`.
- **Persistent journal:** Raspberry Pi OS ships `Storage=volatile`
  (`/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`), which
  lost the logs of the 2026-10-17 outage. install.sh adds
  `/etc/systemd/journald.conf.d/50-musicdock-persistent.conf`
  (`Storage=persistent`, `SystemMaxUse=50M`); `journalctl -b -1` then shows
  the previous boot.
- **2026-10-17 "freeze" was a Wi-Fi problem, not a crash:** the Pi roamed to an access point that associated it but passed no traffic (DHCP never completed; NetworkManager gave up after 45 s and retried the same one).
  A Pi Zero's green LED is solid when idle, so it isn't a hang sign.
- `deploy.sh [--install] user@host`: runs tests, rsyncs to `~/musicdock/app`
  on the Pi (cache stays in `~/musicdock/cache`).
- `dock_report.py`: read-only status report, installed as
  `/usr/local/bin/musicdock-status` (wrapper sets the venv and
  `MUSICDOCK_CONFIG`). Dock (uptime, temp, throttling, memory, SD, Wi-Fi),
  current screen, iPod (last sync, tracks, next sync +/-, plays), plan and
  cache, activity from the journal (`--since`, default 24 h: checks, batch
  progress + ETA, sessions, outages counted separately), and Problems
  (failed units, real ERRORs not explained by an outage, heat, disk, plays
  stuck > 1 day). Opens the record with `mode=ro`; reads the journal as
  `-o short-unix` (JSON is ~5x slower on the Zero). Not on the e-ink screen
  by the user's choice. `status.sh [user@host]` runs it from the Mac
  (`MUSICDOCK_PI`).
- `dock_prepare.py`: prepare job (`musicdock-prepare.timer`: 1 min after boot -
  in practice once the boot screens and network are done - then every 5 min
  after the previous run ends (`OnUnitInactiveSec`); only redraws/rewrites the
  manifest when something changed; it
  first waits for any running `musicdock-sync@*`, since the iPod comes first; runs as the login user at nice 19). Starred songs + starred
  albums' tracks → `cache/tracks/<id>.<ext>` (stock iPod: FLAC etc. → AAC;
  MP3/AAC/ALAC kept), prunes unstarred, writes `cache/manifest.json` (the sync
  plan the sync applies): each track's `sources` (targets it came from) and
  `encoding`, plus `playlists` (id, name, ordered `song_ids`; no `changed`:
  Navidrome smart playlists bump it on every read, which rewrote the 600 KB
  manifest every 5 min, ~55 GB/year of SD writes - fixed 2026-10-18) for
  the sync to recreate on the player. Built in `cache/tmp/`, renamed when
  complete. `--dry-run` shows what a run would do; `--show-plan` prints the
  staged plan (no network). **Covers:** embedded covers over 600px / 300 KB /
  non-JPEG are replaced with a 600px baseline JPEG via mutagen (tags only;
  audio bytes untouched), one decode per album; recorded per track as
  `cover`/`cover_max`, so cached tracks are checked once. Runs under the venv
  (`musicdock-prepare.service` uses `~/musicdock/venv/bin/python`; install.sh
  creates the venv from `pi/requirements.txt`). Without mutagen it logs a
  warning and leaves covers as they are.
  **Parallelism:** `[sync] workers = "auto"` = CPU cores - 1 (min 1), worked
  out each run (1 on the Zero W, 3 on a Zero 2 W - no reinstall after a board
  swap). A thread pool of workers+1 overlaps the next download with current
  conversions; `ConversionSlots` caps concurrent ffmpeg runs and drops to one
  at a time above 75 C. Space is reserved per track when it starts, and
  only workers+1 tracks are handed to the pool at a time (queueing the whole
  batch reserved space for all of it: 432 queued FLACs tripped the cache
  limit at 4.7 GB on 2026-10-17). No CPU
  pinning or systemd CPUQuota: the worker count leaves a core's worth free,
  and nice 19 lets a sync preempt.
- `dock_record.py`: the SQLite record (`/var/lib/musicdock/dock.db`; players,
  tracks, pending_plays, playlists). `dock_plays.py`: upload queued plays
  (batches of 25; leave the queue only on server confirmation). Both the sync
  and the hourly prepare job send queued plays.
- `ipod_handler.py`: stock-iPod handler (pygpod). Snapshot + queue plays,
  SysInfo recovery, backup/restore (on the Pi under `backups/<GUID>/`, last 3),
  reconcile, apply the staged plan, playlists, `sync_player` session. A copy of
  the record is saved on the player at `iPod_Control/musicdock/dock.db`.
- `subsonic.py`: token-auth client. Request URLs contain a working token: never
  log them. `dock_config.py`: loads config.toml (`MUSICDOCK_CONFIG` overrides).
- **`wifi.txt` on an opted-in iPod** (`dock_wifi.py`; write mode only, after
  the music): `SSID=` / `Password=` / optional `Hidden=yes`. The password goes
  into a root-only NetworkManager keyfile (`/etc/NetworkManager/system-
  connections/musicdock-<ssid>.nmconnection`, priority 10), never a command
  line or the log; existing networks stay saved. Joined + address = success:
  wifi.txt deleted, `wifi` screen ("Now on ...", Navidrome reachable or not).
  Failure: previous network brought back, the new profile deleted, wifi.txt
  kept, `wififail` screen with the reason. Read-only mode just reports it.
- **DOCK READY shows the next sync** (prepare job, `pending_args`): "Next sync:
  +19 -3 tracks" or "iPod is up to date" for the most recently synced player
  (record vs manifest via `ipod_handler.pending_changes`), plus "12/19 tracks
  prepared" while converting and "Last sync ... · updated ...". Redrawn only
  when those arguments differ from what's on screen.
- **Ogg/Opus tags (2026-10-18):** Vorbis comments live on the audio stream,
  so `-map_metadata 0` dropped them; Leashes' 3 Ogg/Opus tracks reached the
  Nano titled by their Navidrome ID (pygpod falls back to the file name) with
  no artist. Now: `-map_metadata 0:s:a:0` for ogg/oga/opus sources;
  `fill_missing_tags` fills title/artist/album/track/disc/year from Navidrome
  after every conversion (only empty fields, ID3v2.3); cached Ogg-sourced
  files are checked each run and repaired in place; `ipod_handler.
  fill_missing_names` fixes such tracks already on the player in the
  iTunesDB (no re-copy) and on add. Opus covers carried over already; ffmpeg
  7.1 doesn't carry Vorbis covers.
- **Removals only for songs that left every target:** `targeted_ids` = ready
  tracks + manifest `skipped` (targeted but not cached right now), so a failed
  download or a full cache never removes a song from the player.
- **Dark mode:** `[display] invert = true` (user's Pi: on, for a black PETG
  case) inverts every rendered frame as the last step of `render()`. No extra
  e-ink wear (bistable; wear comes from refreshes, not colour). `dock_status`
  reads `[display]` straight from the config (`MUSICDOCK_CONFIG`, set in every
  unit), so screens work before server settings exist.
- **Removal grace period:** `[sync] removal_grace_days` (0-30, default 7). A
  track that leaves every target leaves the plan at once (so the next sync
  removes it from the player) but its cache file is kept that long
  (`retired.json`, `KeptTracks`); re-adding it reuses the file. Kept files are
  deleted oldest-first when the SD card is below 25% free or when they'd block
  new music (cache limit). An old format of a still-wanted song (e.g. .m4a
  after switching to MP3) is deleted at once.
- **Back to DOCK READY after a sync:** `dock_status.py --rest` swaps SAFE (and
  FAILED, on unplug only) for `idle` with "Last synced 3:02 PM (+79 -0, 5
  plays)" / "Last sync failed ...". Triggered by the udev `remove` rule for the
  USB disk (`musicdock-rest@unplug`, keeps the result up >= 60 s in case the
  player drops USB after eject) and a transient systemd timer 10 min after a
  successful sync (`musicdock-rest@timer`, SAFE only). Uses `--only-over`
  under the panel lock, so it never covers a new sync.
- **Wi-Fi indicator refresh:** NetworkManager dispatcher hook
  `pi/90-musicdock-wifi` (wlan0 up/down) starts `musicdock-wifi-refresh.service`
  (5s settle) → `dock_status.py --refresh`, which redraws a resting screen
  (`idle`, `safe`, `failed`) only if connectivity changed, keeping its original
  time (`--at`). Every draw saves its content args, `drawn_at`, and `wifi_on`
  in the state file for this.
- **pygpod on the Pi** lives in a venv: `~/musicdock/venv` (created with
  `--system-site-packages` to use apt's Pillow/gpiozero; `pip install
  pygpod==0.0.6 mutagen==1.47.0`). Raspberry Pi OS blocks system-wide pip.
- **pygpod benchmark on the Pi Zero W (2026-10-16):** 82 tracks (810 MB) added
  to a fake iPod folder (`~/musicdock/bench-ipod`, SD to SD) in 18.2 min,
  13.3 s/track avg; ~6-8 s for tracks with <=1200px covers. Save and reopen
  0.3 s each at 83 tracks (signature valid); RSS 20-24 MB, peak 153 MB while
  decoding a 4000x4000 cover; no throttling. Big embedded covers dominate:
  4000px 87 s, 3000px 32-52 s per track (resized again for every track). The
  Nano's largest thumbnail is 240x240. Covers are now shrunk to 600px in the
  prepare job (60 of 82 shrunk; cache 773 -> 624 MB). Re-run with shrunk
  covers: 82 tracks in 7.8 min (5.7 s/track, slowest 6.5 s), peak memory
  28 MB. USB write speed to the real iPod not yet measured.
- **Flash-free progress updates:** repeated draws of the same screen are
  partial refreshes (only changed pixels, no flash), even from a fresh
  process: `draw_on_panel` writes the saved last frame
  (`/run/musicdock/dock_status.frame.png`: RAM, created at boot by
  `/etc/tmpfiles.d/musicdock.conf` from install.sh; next to the state file
  off the Pi; a reboot loses it, so the first draw is full) into the panel's old-image
  RAM (0x26), then `displayPartial`. Full refresh on a screen change, every
  `FULL_REFRESH_EVERY` (20) draws against ghosting, or when the frame is
  unknown (deleted before each draw, saved after, so a failed draw forces a
  full one). Progress redraws stay rate-limited to one per 30 s. Verified
  on the panel 2026-10-18 (no flash between progress updates).
- Panel access is serialized by `dock_status.panel_lock()` (flock next to the
  state file); the screen-state check and draw happen under it.
- `tests/`: `python3 -m unittest discover -s tests -v`.

## Development workflow

- Develop and test on the Mac. Use a folder or USB stick as a fake device, a
  sample `.scrobbler.log`, and `--preview` for screens.
- Deploy to the Pi with `rsync` or `scp` over SSH (key-based login).
- Test only hardware-specific behavior on the Pi: USB detection, mounting, the
  panel.
- Python only on the Pi. It's ARMv6, so avoid dependencies without armhf/ARMv6
  builds. Prefer `apt` packages: `python3-pil`, `python3-spidev`,
  `python3-gpiozero`, `ffmpeg`.
- udev only *triggers* a systemd service. Never do long work inside a udev rule,
  because udev kills it.

## Phase 4 plan: stock iPod sync (agreed 2026-10-16)

The user's priorities: **plays are always logged, and the dock always knows
what is and isn't on the iPod.**

- **How plays map to songs:** `Play Counts` entry #N → track #N in the
  *current* `iTunesDB` → its `dbid` (64-bit persistent ID, set when the dock
  adds the track and kept by pygpod across rewrites; `ipod_path` as a
  cross-check) → the Pi's table → Navidrome song ID. `track_id` and position
  are not stable; titles aren't unique; a file hash can't match the server's
  FLAC. The iPod logs a count plus the *last* play time only, so repeat plays
  between syncs get approximate times.
- **Pi database: SQLite** (`sqlite3` is in the standard library; transactions
  survive power cuts), in `/var/lib/musicdock/`. Tables:
  - `tracks`: iPod FireWire GUID + `dbid` + `ipod_path` → Navidrome ID, plus
    MusicBrainz recording ID (when tagged) and artist/album/title/track/
    duration, encoding, date added, and present/removed state. The extra
    fields let the dock re-find a song if Navidrome's ID changes (the server
    owner may move files). Keyed by GUID so each player has its own map.
  - `pending_plays`: Navidrome ID, play time, sent state (the queue above).
- **iPod-side backup of the mapping:** also store the Navidrome ID in a
  non-displayed field of each track record (the comment field is a
  candidate - test that the Nano doesn't show it), so a lost SD card can be
  rebuilt by reading the iPod.
- **Reconcile at every sync**, before changing anything: compare the iPod's
  actual `iTunesDB` and `Music/` files with the `tracks` table. Missing tracks
  are marked removed; tracks the dock didn't add are left alone and logged;
  files with no DB entry (orphans) are reported and removed.
- **Order of a sync:** reconcile → copy and parse play data → queue plays
  (fsync) → upload what can be sent → back up `iPod_Control/iTunes/` → open
  with pygpod (deletes `Play Counts`, already queued) → remove tracks that left
  the targets (`delete_file=True`) → add new tracks → save → unmount.
- **Resumable long syncs:** add tracks in batches (e.g. 25), saving the DB
  (with a fresh backup) after each batch, so an interrupted first sync loses
  at most one batch and the next sync resumes. Progress on the `syncing`
  screen: `Copying 12/72: Artist - Title`.
- **Before every save:** confirm `requires_hash` / HASH58 and the GUID (else
  pygpod writes an unsigned DB the iPod rejects); start artwork IDs at 100.

## Requirement: resilience without a mirror (2026-10-16)

The player's storage may exceed the Pi's (e.g. 200 GB of music on a 512 GB
iPod vs a 64 GB SD card). The dock must still be able to **rebuild a wiped
or reset player completely**. Wipes are rare, so a multi-day rebuild is
acceptable; a normal sync over ~50 GB is unlikely but must work.

- **Source of truth is Navidrome**, not the Pi's files: what the player
  should hold = its targets (starred + tagged/configured playlists). The
  cache is a speed-up; the Pi's SQLite record adds mappings and pending
  plays. A rebuild needs only Navidrome + the targets.
- **Hybrid cache:** mirror every targeted track while it fits under
  `max_cache_gb` / 80% of the card. When over, evict only tracks the record
  shows **confirmed on the player** (DB saved, reconciled), largest/oldest
  first. Never evict a track the player still needs.
- **Rolling batches for big jobs:** the prepare job fills the cache with the
  next batch of missing tracks in **priority order** (tagged playlists and
  Favorites first, then the rest); the sync copies a batch, saves the DB,
  marks those tracks evictable; repeat. While the player stays docked the
  sync should keep going as batches arrive (no re-plugging per batch).
  Progress on screen, e.g. `Restoring 12,340 / 40,000`.
- **Reset detection:** if every known track is missing at once, log "player
  appears to have been reset" and rebuild, instead of 1,000s of removals.
- **SysInfo recovery:** a reset wipes `SysInfo`, and pygpod then writes an
  unsigned DB. Store each player's model number in the record (keyed by
  FireWire GUID) once known, and rewrite `SysInfo` when it's missing.
- **Back up the record:** copy the SQLite DB (small) onto the player at each
  sync, and keep the Navidrome ID in each track record (see Phase 4 plan), so
  either side can rebuild the other if one is lost.
- **Throughput:** Wi-Fi ~2-4 MB/s (estimate; 200 GB ~15-30 h); **USB to the
  Nano measured ~1.3-1.5 MB/s effective** (first full sync; the Nano reports
  its write cache disabled, and the vfat mount uses `flush` - try without
  `flush` on a later sync and compare), so 200 GB of copying ~40+ h; FLAC->MP3 conversion is the bottleneck
  (~song length per track on the Zero W, so 200 GB of FLAC ~20 days; run
  conversions in parallel on a Zero 2 W, ~2 days). Plays recorded since the
  last sync are lost in a wipe; server-purged discovery tracks can't return.
- **Optional accelerators (not required):** "Mac mode" - run the dock's own
  sync engine on the Mac against the mounted player and hand the resulting
  record to the Pi (never Finder); or a larger microSD card so the cache can
  mirror again.

## Phase plan

1. ✅ Display working on hardware
2. ✅ Dock core: detect → mount → unmount → `safe` (Nano on the real dock,
   2026-10-16: udev → `musicdock-sync@sda1` in ~1 s, whole run 23 s, eject
   works, no throttling while charging; the Nano reports its write cache
   disabled)
3. ✅ Server side: play upload, starred/playlist fetch, prepare job with
   conversion (real play upload verified 2026-10-16)
4. ✅ Stock iPod handler via pygpod. Read-only dock on the real Nano
   (2026-10-16): adopted the 3 hand-copied MP3s, sent 4 plays, ~30 s. **First
   full sync (write = true), 2026-10-16:** 79 tracks + Favorites + Mel Test
   in 15.4 min (copy step 14.8 min, ~11 s/track vs 5.7 s SD-to-SD), played
   back fine. The user's Pi config now has `write = true`.
5. Large stock-firmware players: hybrid cache, rolling batches, rebuild
   (see "Resilience without a mirror"). Rockbox handler deprioritized.
6. 3D-printed dock enclosure
7. Documentation, setup guide, and packaging for friends. **Built
   2026-10-16:** README.md, INSTALL.md, `make_release.sh` (builds
   `dist/music-dock/` + `dist/music-dock-<date>.tar.gz`: only Pi code, pi/,
   vendor/, docs; refuses to build on failing tests or personal data; no macOS
   tar metadata). Verified by unpacking on the Pi, importing every module and
   running `install.sh --check`. A fresh-Pi install is untested until the
   friend (iPod classic) does it. Original plan:
   README (hardware list, OS image, SSH key setup, one-command install);
   `install.sh` installs apt deps (ffmpeg, rsync, python3-pil/-gpiozero/
   -spidev/-lgpio, fonts-dejavu-core), enables SPI (`raspi-config nonint
   do_spi 0`), fetches a pinned Waveshare e-Paper lib, creates the venv and a
   mode-600 config from the template; clear errors for unsupported setups
   (no NetworkManager, other display); placeholder server URL in
   `config.example.toml`; display model/rotation as config if needed.
   The guide must warn: a docked iPod with `MediaDock.txt` gets the dock
   owner's library and its plays go to the owner's Navidrome account; how to
   add/remove `MediaDock.txt` (disk mode on a computer).

## Notes

- The user wants album art on the player. Prepared tracks already embed the
  cover (1200x1200 JPEG). The stock-iPod handler must also write the iPod's
  artwork database; libgpod may need `iPod_Control/Device/SysInfoExtended` to
  know the Nano's thumbnail sizes (unverified on the Nano 4G).
- The user's Nano (inspected on the Mac 2026-10-15, after a factory restore on
  Windows): USB product ID 0x1263 (Nano 4G), **FAT32** on an MBR partition
  (`disk6s1`, label `MY IPOD`, 16.2 GB), so Linux can write it. `iTunesDB`
  is version 0x75 and **signed (hash58)** - any DB writer must re-sign it, or
  the iPod shows no music. hash58 needs the FireWire GUID, which is the USB
  serial number (`000A2700000000AB`; read it at dock time from udev
  `ID_SERIAL_SHORT` rather than hard-coding). `iPod_Control/Device/SysInfo` is
  empty and there's no `SysInfoExtended` or `Artwork/` yet; libgpod
  conventionally expects `FirewireGuid` in SysInfo.
- **pygpod manual test (2026-10-16, on the Mac): passed.** pygpod 0.0.6
  (PyPI, pure Python) identified the Nano as Nano 4G / hash58 once `SysInfo`
  had `ModelNumStr: xB903` and `FirewireGuid: 0x000A2700000000AB`, reproduced
  iTunes' own signature exactly, added a track with artwork (6 thumbnail
  sizes), and the iPod played it and recorded 1 play in `Play Counts`. Without
  a model in SysInfo, pygpod assumes no signature and writes an unsigned
  (rejected) DB - always check `requires_hash` before saving.
- **pygpod deletes `Play Counts` (and `iTunesStats`, `PlayCounts.plist`)
  every time `Database()` opens**, after merging them into memory, even if
  nothing is saved. That violates "never clear play data until the server
  confirms". The handler must copy and parse those files itself
  (`pygpod.db.playcounts.parse_play_counts`) and upload them before letting
  pygpod open the DB. Play times are Mac-epoch seconds in the iPod's local
  time, with no time zone.
- **Pending plays:** the queue lives on the Pi (e.g. a JSON-lines file in
  `/var/lib/musicdock/`, one entry per play with the Navidrome song ID and
  time). The sync uploads it when it can; the hourly prepare job also flushes
  it whenever Navidrome is reachable, so plays don't wait for the next dock.
  The final screen says how many plays are still queued. Mapping an iPod
  track to its Navidrome ID needs a record kept when the dock adds the track
  (design in Phase 4).
- **Play upload verified end to end (2026-10-16):** the real BLOOM play from
  the Nano's Play Counts (00:14:46, read via `ipod_handler`) went through the
  record's queue and `dock_plays.upload_plays`; Navidrome's playCount went 1
  -> 2 and `played` = 2026-10-16T00:14:46-07:00. The Pi's timezone
  (America/Los_Angeles) must match the iPod's clock for times to be right.
- **Handler rehearsal on the Pi (2026-10-16):** the real staged plan applied
  to a fake iPod built from the Nano backup (throwaway record via
  `MUSICDOCK_RECORD`): 82 tracks + Favorites (72) + Mel Test (22) in 7.7 min;
  second run a no-op. The backup's old AAC BLOOM was adopted by metadata
  match and replaced by the MP3 (different format = replace).
- **Navidrome album names can differ from file tags** ("Inertia (deluxe
  digital)" vs a TALB of "Inertia"); the handler passes Navidrome's album
  name to pygpod so the player matches Navidrome and editions don't merge.
- **The real Nano's 3 hand-copied MP3s** (BLOOM, Don't Care Crown, Make
  Love) will be adopted on the first dock sync (title + artist + length
  match, same format), not duplicated.
- **pygpod's `remove_track()` leaves the audio file on the iPod by default.**
  Pass `delete_file=True`, or removed tracks pile up as orphans (seen
  2026-10-16). Artwork is removed either way.
- **Album art replaced the built-in games' icons** (Klondike etc.) after the
  first pygpod write (user, 2026-10-16; cosmetic). pygpod's `ArtworkManager`
  numbers images from 1 (`_next_image_id = 1`); the games seem to use low
  image IDs too. iTunes/libgpod reportedly start at 100 (unverified). New
  artwork now starts at 100; the first 3 covers keep low IDs, so the icons stay
  replaced. **User doesn't use the games and accepts this (2026-10-16): no
  re-numbering needed.** Purely cosmetic; nothing else looks art up by ID.
- **Confirmed in real use (2026-10-16):** prepare picked up 19 new tracks on
  its own; the dock synced them + 12 plays and created MediaDock.txt on the
  Nano; a prepare run that started mid-sync waited for it; unplug -> DOCK
  READY with the summary; 5-min checks don't rewrite the manifest.
- **Removals confirmed on the real Nano (2026-10-16):** unstarring Daft Punk
  "Human After All" + a Renard album -> prepare pruned 29 from the cache within
  5 min, DOCK READY showed the pending removals, the next dock removed all 29
  (each logged with its reason, record history kept, Favorites updated) in 44 s.
- **Real-use checks still pending** (tested on fake iPods / fake servers):
  plays queued while Navidrome is down, docking during boot, wifi.txt on a real
  network.
- **Manual test with 3 tracks passed on the Nano (2026-10-16):** MP3s
  (2 converted V0, 1 original 320k with a 4000x4000 cover) played cleanly,
  showed art, and sorted correctly. Not yet tested on the Pi itself.
- **ffmpeg's built-in AAC encoder produces audible high-frequency artifacts**
  (user heard them on the Nano; measured 8-15 noise bursts per 28s vs 1 for
  Apple's encoder, and decoded peaks up to +3 dBFS on loud masters). The Pi
  has no fdkaac. LAME MP3 V0 via ffmpeg `libmp3lame` measured clean (0 bursts,
  no overs) and ~4x faster on the Pi Zero; ALAC is lossless and ~9x faster but
  ~3.3x larger. Test material and the pre-test iPod backup are in `tmp/`
  (git-ignored).
- Don't draw on the panel inside a long-running process: the Waveshare driver
  holds the GPIO pins (and a busy-looping thread) until the process exits,
  which blocks other jobs ("GPIO busy") and slows conversion. `Screen` and
  `dock_boot` draw via short-lived `dock_status.py` child processes.
