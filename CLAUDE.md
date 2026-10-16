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
- **Player later:** modded iPod Video running **Rockbox**, plays FLAC natively.
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
   3. Read play data and upload it to Navidrome.
   4. Read the on-device keep list and mark those tracks on the server.
   5. Copy new tracks from the cache, remove purged ones, update playlists.
   6. Flush, unmount, show `safe` (or `failed`).

**Device handlers.** One sync engine, with one handler per player type. Only
the "write tracks / read plays" step differs:

| | Stock iPod (now) | Rockbox (later) |
|---|---|---|
| Library | `iTunesDB` (signed), via **pygpod** | Plain files + `.m3u8` playlists |
| Play data | Play-count changes since last sync | `.scrobbler.log`, timestamped |
| Formats | FLAC → MP3 LAME V0 (`convert_to`; `alac` and `aac` also supported) | FLAC copied as-is |
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
core library is loaded once from the Mac. Keep the SD card (64GB; 57G root filesystem) under ~80% full.

**Initial target:** the user's **starred** items (`getStarred2`). Starred albums
expand to their tracks. Discovery playlists come later.

## Safety rules (do not simplify these away)

- **Never clear play data until the server confirms it received it.** A failed
  sync must lose nothing.
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
  queue). Built in `cache/tmp/`, renamed when complete. `--dry-run` shows the
  plan.
- `subsonic.py`: token-auth client. Request URLs contain a working token: never
  log them. `dock_config.py`: loads config.toml (`MUSICDOCK_CONFIG` overrides).
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

## Phase plan

1. ✅ Display working on hardware
2. Dock core: detect → mount → unmount → `safe`, using the Nano or a USB stick
3. Server side: play upload, starred-track fetch, prepare job with conversion
4. Stock iPod handler via pygpod (after the manual test passes)
5. Modded iPod Video + Rockbox handler
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
