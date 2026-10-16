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
- `dock_sync.py`: sync job. Detects/mounts (read-only for now) the player,
  lists it, unmounts, shows `safe`/`failed`. `--fake-device DIR` uses a folder
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
- `deploy.sh [--install] user@host`: runs tests, rsyncs to `~/musicdock/app`
  on the Pi (cache stays in `~/musicdock/cache`).
- `dock_prepare.py`: prepare job (`musicdock-prepare.timer`: 10 min after boot,
  then hourly; runs as the login user at nice 19). Starred songs + starred
  albums' tracks → `cache/tracks/<id>.<ext>` (stock iPod: FLAC etc. → AAC;
  MP3/AAC/ALAC kept), prunes unstarred, writes `cache/manifest.json` (the sync
  plan the sync applies): each track's `sources` (targets it came from) and
  `encoding`, plus `playlists` (id, name, `changed`, ordered `song_ids`) for
  the sync to recreate on the player. Built in `cache/tmp/`, renamed when
  complete. `--dry-run` shows what a run would do; `--show-plan` prints the
  staged plan (no network). **Covers:** embedded covers over 600px / 300 KB /
  non-JPEG are replaced with a 600px baseline JPEG via mutagen (tags only;
  audio bytes untouched), one decode per album; recorded per track as
  `cover`/`cover_max`, so cached tracks are checked once. Runs under the venv
  (`musicdock-prepare.service` uses `~/musicdock/venv/bin/python`; install.sh
  creates the venv from `pi/requirements.txt`). Without mutagen it logs a
  warning and leaves covers as they are.
- `subsonic.py`: token-auth client. Request URLs contain a working token: never
  log them. `dock_config.py`: loads config.toml (`MUSICDOCK_CONFIG` overrides).
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
- **Throughput (estimates; measure on hardware):** Wi-Fi ~2-4 MB/s (200 GB
  ~15-30 h); USB to player ~10 MB/s; FLAC->MP3 conversion is the bottleneck
  (~song length per track on the Zero W, so 200 GB of FLAC ~20 days; run
  conversions in parallel on a Zero 2 W, ~2 days). Plays recorded since the
  last sync are lost in a wipe; server-purged discovery tracks can't return.
- **Optional accelerators (not required):** "Mac mode" - run the dock's own
  sync engine on the Mac against the mounted player and hand the resulting
  record to the Pi (never Finder); or a larger microSD card so the cache can
  mirror again.

## Phase plan

1. ✅ Display working on hardware
2. Dock core: detect → mount → unmount → `safe`, using the Nano or a USB stick
3. Server side: play upload, starred-track fetch, prepare job with conversion
4. Stock iPod handler via pygpod (after the manual test passes)
5. Large stock-firmware players: hybrid cache, rolling batches, rebuild
   (see "Resilience without a mirror"). Rockbox handler deprioritized.
6. 3D-printed dock enclosure

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
- **pygpod's `remove_track()` leaves the audio file on the iPod by default.**
  Pass `delete_file=True`, or removed tracks pile up as orphans (seen
  2026-10-16). Artwork is removed either way.
- **Album art replaced the built-in games' icons** (Klondike etc.) after the
  first pygpod write (user, 2026-10-16; cosmetic). pygpod's `ArtworkManager`
  numbers images from 1 (`_next_image_id = 1`); the games seem to use low
  image IDs too. iTunes/libgpod reportedly start at 100 (unverified). Phase 4:
  start IDs at 100 and re-number existing images once, then check the icons.
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
