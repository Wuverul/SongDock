# SongDock

> **Name (2026-10-23):** the project is **SongDock** (formerly "Music Dock";
> user's choice after checking SimpleSong/SimpleSync/StrumSong - taken or
> off-message - and avoiding Apple marks like "Tunes"/"iPod"). Only the
> display name changed. Internal names stay as they were, on purpose:
> `MediaDock.txt` (the opt-in: renaming it would un-opt existing iPods),
> `MediaDock README.txt`, `MediaDock/` on Rockbox, `musicdock-*` units and
> commands, `~/musicdock`, `/var/lib/musicdock`. Two comments in
> `pi/install.sh` (`JOURNAL_WANT`, `WIFI_WANT`) still say "Music Dock": they
> are compared with the files already installed, and changing them would
> rewrite those files (and ask for a reboot) on every existing dock.

> The developer's own hosts, addresses and device IDs are in
> `CLAUDE.local.md` (git-ignored, never published).

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
  Wi-Fi only. Runs **Raspberry Pi OS Lite (32-bit)** (the only kind ARMv6
  runs). **The user's Pi Zero 2 W (since 2026-10-23) runs 64-bit** (Debian 13
  trixie, arm64; 64-bit Python and ffmpeg): its glibc per-thread malloc
  arenas (up to 64 MB each, vs 1 MB on 32-bit) caused the OOM that
  `limit_malloc_arenas()` fixes. A 64-bit card won't boot a Zero W; a 32-bit
  one works in both. Claude Code can't run on
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

| | Stock iPod (now) | Rockbox (`rockbox_handler.py`, built 2026-10-23) |
|---|---|---|
| Library | `iTunesDB` (signed), via **pygpod** | Plain files in `MediaDock/<album artist>/<album>/NN title.ext` + `Playlists/*.m3u8` (absolute paths) |
| Play data | Play-count changes since last sync | `.scrobbler.log`, timestamped (`#TZ/UNKNOWN` = local clock); `S` (skipped) ignored; matched by artist+title(+album, length) to the record |
| Formats | FLAC → MP3 LAME V0 (`convert_to`, `mp3_quality`: `v0`–`v9` or constant `128k`–`320k`; `alac`/`aac` also supported). The manifest records each track's `encoding`; changing settings re-converts on the next run | Same MP3 cache as stock (user's choice, 2026-10-23: one dock serves both). `type = "rockbox"` keeps FLAC instead and then refuses stock iPods |
| Keep signal | Unskipped play count (user, 2026-10-18): played a lot = keep. Plays already reach Navidrome; how keepers are marked there is TBD | `Keep.m3u8` playlist |
| Album art | Thumbnails in `iPod_Control/Artwork/` (ArtworkDB), made from the embedded cover when each track is added. Ignores art inside the file. | `cover.jpg` (baseline JPEG, <= 600 px, made from the embedded cover) in the album folder |

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
- `dock_players.py`: `musicdock-players` (installed like musicdock-status)
  lists the players in the record; `forget` / `keep <name>` set/clear
  `players.forgotten_at`, so the player stops holding songs in the cache
  (`songs_on_every_player`) and isn't DOCK READY's / the report's player.
  History and queued plays stay; `mark_synced` clears it (docking it again
  brings it back, logged). Use when replacing an iPod (the user plans a
  Nano -> Classic move). Players are named as people know them: the iPod's
  own name (`players.name`, from the iTunesDB master playlist, saved each
  sync; the user's Nano is the name in its backups) + an optional model
  hint `(nano)` / ID end `(AB12)`, or `all others`; curly/straight
  apostrophes and case don't matter. `Record` adds new columns to older
  records (`_add_column`).
- `dock_setup.py`: **`DockSetup.txt` on an opted-in iPod** (write mode
  only; user's choice of name, 2026-10-23), like wifi.txt: `Forget=` /
  `Keep=` lines, all or nothing (a bad line changes nothing and the file
  stays), deleted on success; can't forget the iPod being synced.
  **Settings lines** (written to config.toml by `dock_config.update`:
  edited in place, comments and column kept, loaded and checked before an
  atomic swap, mode 600, owner kept when run as root): `PlaylistTag=`
  (`none` = off; one word), `SyncStarred=` / `SyncFavorites=` yes|no
  (adds/removes "starred" in `targets`; `targets = []` is now valid when a
  tag is set = playlists only; both off is refused), `Server=` +
  `Username=` + `Password=` (Server always needs both: the current
  sign-in is never sent to an address someone typed; user/password alone
  are fine), `NewLibrary=yes`. A new sign-in is tried first (`ping`;
  code 40/41 = refused); a new address must have one of up to 5 of this
  iPod's songs (`getSong`), else it's another library: needs
  NewLibrary=yes and no unsent plays (they'd be lost). Passwords are
  never logged or shown. Runs after wifi.txt, so a new server is checked
  on the final network. Anyone who can dock an opted-in iPod can use it,
  like wifi.txt (physical access = control). Screens
  `setup` (DOCK UPDATED) / `setupfail` (SETUP NOT USED); a wifi.txt
  result takes precedence on screen. Every write sync then rewrites
  **MediaDock.txt** (existing spelling kept) with the known iPods (name,
  model, ID end, tracks, last sync / forgotten), a one-line DockSetup.txt
  example pointing to the README, and the last DockSetup.txt outcome
  (carried over between syncs). Runs after the music and wifi.txt; never
  fails the sync.
- `dock_guide.py`: **`MediaDock README.txt`**, a user manual on every
  write-synced iPod (user request, 2026-10-23): the files a person can add
  or edit there (MediaDock.txt, wifi.txt, DockSetup.txt) with every
  setting, how music is chosen in Navidrome (built from the config: tag,
  Favorites name, grace days, configured playlists), what the screens
  mean, and not to use Finder/iTunes. Rewritten only when its text
  changes. **Keep it in step** when a file's settings or a screen change
  (`tests/test_dock_guide.py` checks the main settings appear).
  MediaDock.txt points to it rather than repeating the details.
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
  `cover`/`cover_max`, so cached tracks are checked once. **No embedded
  cover** (art only in the album folder, which Navidrome shows but a
  converted file never carries; seen 2026-10-19 on Purity Ring's 2025 album,
  Virtual Self, a Chillhop compilation): `add_server_cover` embeds
  Navidrome's `getCoverArt` (song's `coverArt` ID, 600px, fetched once per
  ID) → `cover: navidrome`; the server having none → `missing` (not asked
  again). `none` from older manifests is rechecked once; a network error
  leaves it `none` for the next run. `apply_plan` adds the art in place to
  tracks already on the player (`cover: navidrome`, no artwork; pygpod's
  `_add_artwork_for_track`, no re-copy). Runs under the venv
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
  and nice 19 lets a sync preempt. **Memory:** `limit_malloc_arenas()` caps glibc
  malloc arenas at 2 (`mallopt(M_ARENA_MAX)`) at start: per-thread arenas
  kept freed memory and a run on the Zero 2 W (415 MB) was OOM-killed at
  ~230 MB after 116 tracks (2026-10-23).
- `dock_record.py`: the SQLite record (`/var/lib/musicdock/dock.db`; players,
  tracks, pending_plays, playlists). `dock_plays.py`: upload queued plays
  (batches of 25; leave the queue only on server confirmation). Both the sync
  and the hourly prepare job send queued plays.
- `ipod_handler.py`: stock-iPod handler (pygpod). Snapshot + queue plays,
  SysInfo recovery, backup/restore (on the Pi under `backups/<GUID>/`, last 3),
  reconcile, apply the staged plan, playlists, `sync_player` session. A copy of
  the record is saved on the player at `iPod_Control/musicdock/dock.db`.
- `rockbox_handler.py`: **Rockbox players** (2026-10-23; built against fake
  players, **untested on hardware**: the USB identity under Rockbox - serial
  used as the GUID, whether udev's trigger fires - is the main unknown).
  Chosen per player by a `.rockbox` folder (`opt_in_status` accepts
  `.rockbox` or `iPod_Control`; MediaDock.txt still required). Same plan,
  cache and record; the record's dbid is the file's path. Only
  `MediaDock/` is managed: files there carry the plan's title/artist/album/
  genre + `MUSICDOCK_NAVIDROME_ID` (mutagen easy tags), so `reconcile`
  re-adopts them if the record is lost; untagged files there are left
  alone; `.musicdock-tmp-*` leftovers deleted. Copies go to a temp name,
  get tagged, fsync, then rename, then the record. Removals delete the file
  and emptied album/artist folders. Name/genre changes re-tag in place
  (record `genre` column, added 2026-10-23). Playlists only written when
  their text changes; ones the dock didn't make are never touched. Plays:
  log snapshotted, queued (committed), and only then deleted, in write
  mode only; unmatched plays queue without a song. Record copy at
  `MediaDock/.musicdock/dock.db`. The iPod README gets a Rockbox paragraph
  (Last.fm Log setting, Database Auto Update). Keep.m3u8: not built.
- **Stock-iPod compatibility switch (2026-10-23):** `DockSetup.txt`
  `StockCompatible=no|yes` sets `[device] type` to `rockbox` / `stock_ipod`.
  **Failsafe (user requirement):** `=no` is refused while
  `Record.active_stock_players()` isn't empty - players not forgotten whose
  `players.kind` (set by each handler at every sync) is `stock` or NULL
  (not seen since kinds were recorded: assumed stock) - including the
  docked one; `Forget=`/`Keep=` lines in the same file are counted
  (`dock_setup.stock_blocker`). **A stock iPod on a Rockbox-only dock** is no
  longer refused up front: `dock_sync` runs `ipod_handler.sync_player` with
  write=False (plays read, music untouched), then wifi.txt and DockSetup.txt
  as usual, so `StockCompatible=yes` from that iPod switches the dock back
  (result `stock_compatible: True`, DOCK UPDATED; the music follows after
  the next prepare run via auto re-sync). Without that it fails with "Stock
  iPod: dock is set up for Rockbox" (or SETUP NOT USED with the reason).
- `subsonic.py`: token-auth client. Request URLs contain a working token: never
  log them. **Fallback addresses (2026-10-23):** `[server] url` may be a
  list in priority order (e.g. home LAN address, then a public one; user's
  choice over combining several servers' libraries, which was judged too
  risky). Before the first request each address gets a 5 s TCP check
  (`reachable`), so an unreachable home address doesn't cost a 60 s
  timeout per run; a network error mid-run moves to the next address that
  answers (a server *answering* with an error never switches). Logs
  addresses only. `DockSetup.txt` `Server=` takes them comma-separated. `dock_config.py`: loads config.toml (`MUSICDOCK_CONFIG` overrides).
- **`wifi.txt` verified on a real network change (2026-10-18,** between two
  of the user's SSIDs). WI-FI CHANGED now shows the new IP at the bottom (no
  "Safe to remove" line), so the dock can be found again.
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
- **One album, one name (2026-10-18):** Navidrome groups albums by ID, but
  a song's `album` field comes from its own tags and can differ ("Hold Your
  Colour" vs "Hold Your Colour (2007 re-release)"; "Inertia" vs "Inertia
  (deluxe digital)"). The iPod groups by name, so such a song split off.
  `unify_album_names` gives every song of an album ID the album's own name
  (from starred-album reads, else one `getAlbum` per disagreeing album).
  `fill_missing_names` also moves tracks already on the player to the
  plan's album name (iTunesDB only, no re-copy).
- **Auto re-sync of a docked iPod (2026-10-18, user: "right away"):** after
  eject the Nano stays on USB (charging; block device size 0). When a
  prepare run changes the plan and the last-synced player has pending
  changes, it writes `/var/lib/musicdock/resync.request`;
  `musicdock-resync.path` runs `dock_resync.py` as root, which deletes the
  request, skips if a sync runs, and for Apple USB devices whose serial is a
  synced player's GUID and whose disk is ejected writes `authorized` 0 then
  1 (2 s apart). It first draws `connecting` ("CONNECTING... New music
  ready", --only-over resting screens), which also stops the unplug rule
  (fired by the drop) from redrawing DOCK READY - that redraw delayed
  SYNCING by ~12 s in the first test. The sync's forced SYNCING replaces it;
  if none does within 60 s, the previous screen is restored (`--at`).
  udev then starts the normal sync. `[device] auto_resync`
  (default true) turns it off. Interrupts playback while docked. Verified on
  the real Nano 2026-10-18 (re-plug -> sync in 6 s).
- **One copy per recording (2026-10-19, user: "favor the album listing",
  verify first):** `merge_same_recordings` treats songs as one recording
  only with the same MusicBrainz recording ID (else ISRC) and lengths within
  2 s; no IDs, no merge. Keeps the copy whose release ranks best (album,
  untyped, EP, single, live, compilation; then more tracks, earlier year);
  the kept copy takes the other's targets and playlist places (once).
  Verified on 9 Kitsune² songs on two albums: spectrograms matched 0.999+
  (one was 1.9 dB louder), while studio vs live scored 0.2; 13 same-title
  Porter Robinson pairs (live/studio, different IDs) are left alone.
- **Simplified genres (2026-10-19, `dock_genres.py`):** Navidrome songs carry
  several genre tags, and the iPod showed each combination as a genre (56
  for 78 albums). `[genres]` in the config is an ordered list of groups
  (name + keywords; whole-word match, `release:<type>` matches the album's
  release type, `album:<words>` the album name, e.g. `album:chillhop`). A
  song gets the first group matching any tag (order = priority, so adding
  albums never reshuffles; user's choice over counting). Only songs no group
  matches go by count: their tag carried by the most synced albums
  (`unmatched = ""`, default; no tags = "Other"). Then each album takes its
  songs' majority group. The default list has the user's groups first, then
  general categories from Discogs/iTunes top-level genres (Classical,
  Country, Jazz, Metal, ...) for libraries this one doesn't cover. The prepare job writes it
  as each track's `genre` in the manifest (`album_types` caches release
  types); `fill_missing_names` applies it in the iTunesDB only (no re-copy;
  Navidrome and cached files untouched). Preview: `dock_prepare.py
  --genres`. Default list on the real library: 9 genres. Navidrome
  sometimes sends `releaseTypes` as one string ("album;compilation");
  `dock_genres.release_types` handles both (it was read letter by letter
  in `merge_same_recordings`).
- **Album track order on the iPod (2026-10-19):** the stock firmware browses
  by sort lists stored in the iTunesDB (MHOD 52/53). pygpod 0.0.6 sorts each
  by one field with a stable sort, so an album's tracks kept their add order
  ("Hold Your Colour", track 10, added first, showed as track 1).
  `install_sort_order()` (called by `checked_save`) pre-orders the tracks
  like iTunes/libgpod - albums by disc/track, artists by album/disc/track,
  genres by artist/album/disc/track - before pygpod's sort. A marker
  (`iPod_Control/musicdock/sort-order`) makes the next write sync re-save
  once so existing players get corrected lists.
- **Supported players (2026-10-19, user: Classic, Video and Nano only; no
  touch/shuffle):** `ipod_handler.SIGNED_GENERATIONS` (Classic 1-3, Nano
  3G/4G: hash58 required) and `UNSIGNED_GENERATIONS` (Video 5G/5.5G, Nano
  1G/2G: none required, following libgpod; pygpod 0.0.6 signs them with
  hash58 anyway, which the firmware ignores). `check_supported` runs in
  `ensure_sysinfo` before SysInfo is touched; anything else (Nano 5G hash72,
  Nano 6G+ hashAB, shuffles, older iPods) fails with "<model> isn't
  supported". `REPRESENTATIVE_MODELS` covers each supported USB product ID.
  Only the Nano 4G is tested on hardware. **HFS+ (Mac-formatted) iPods:**
  `dock_sync` reads their plays read-only (the Pi kernel has `hfsplus`),
  then fails with "Mac-formatted iPod (see guide)": a restore to FAT32
  would wipe unread plays.
- **No plan = no music changes (2026-10-23):** a fresh install has no
  `manifest.json` until its first prepare run *finishes* (hours for a big
  library). A sync before that used to apply `{}` as the plan and removed
  1238 of 1240 tracks from the Nano (plays were sent first; nothing lost on
  the server). Now `sync_player` only reads and sends plays when the manifest
  has no `tracks` key, and `apply_plan` raises on one.
- **Playlists re-adopted after a lost record (2026-10-23):** after the move
  to the Zero 2 W (fresh record), tracks were re-adopted by Navidrome ID but
  the 7 dock playlists were treated as "the player's own" and never updated
  (111 new songs missing from them). Worse, they were nearly *empty*: the
  morning's mass removal (1238 tracks) had emptied them, and nothing
  refilled them since, so the Nano had no working playlists all day.
  `sync_playlists` now adopts a playlist whose name matches a planned one
  and whose tracks are all the dock's - known to the record by dbid, or
  carrying a Navidrome ID (old hand-copied adoptions have no comment) - or
  that is empty (stock iPods can't make named playlists themselves);
  Rockbox: every line under `/MediaDock/`, or none. A playlist with any
  other track stays hands-off. The record copy on the player couldn't help:
  the new dock's first sync had already overwritten it. **Verified on the
  Nano 2026-10-23:** all 7 adopted; Favorites 701, the four Chillhop mixes
  188/184/184/164, DnB 95, Nostalgia 79 = the plan exactly.
- **Removals only for songs that left every target:** `targeted_ids` = ready
  tracks + manifest `skipped` (targeted but not cached right now), so a failed
  download or a full cache never removes a song from the player.
- **Dark mode:** `[display] theme = "dark"` / `"light"` (2026-10-23, user
  request); `invert = true` is the older spelling and still counts when no
  theme is set (`dock_status.dark_mode()`; the user's Pi config still uses
  `invert = true`, for a black PETG case). Inverts every rendered frame as
  the last step of `render()`. No extra e-ink wear (bistable; wear comes
  from refreshes, not colour). `dock_status` reads `[display]` straight
  from the config (`MUSICDOCK_CONFIG`, set in every unit), so screens work
  before server settings exist.
- **Check interval:** `[sync] check_interval_minutes` (1-1440, default 5;
  2026-10-23, user request) is how often the prepare timer runs. Only root
  can change the timer, so `musicdock-settings.path` (PathChanged on
  config.toml) starts `musicdock-settings.service` (root), which runs
  `dock_timer.py`: reads just that setting (invalid/missing = 5), writes
  `/etc/systemd/system/musicdock-prepare.timer.d/50-check-interval.conf`
  (empty `OnUnitInactiveSec=` first, or the values add up), daemon-reload,
  restart the timer - only when the value changed. install.sh runs it once.
  **Bug (found 2026-10-25):** an empty `OnUnitInactiveSec=` clears *every*
  trigger of the timer, OnBootSec too; after the next reboot no first run
  happened, so OnUnitInactiveSec (counted from a run's end) never fired and
  the dock stopped checking for ~2 days (`TimersMonotonic` showed only
  OnUnitInactiveUSec, timer "elapsed", NEXT "-"). The drop-in now sets
  `OnBootSec=1min` again; the text changed, so it's rewritten on the next
  run. Without sudo, a no-op config save (`: >> config.toml`) triggers
  musicdock-settings.path, which applies it.
  PathChanged firing on DockSetup.txt's atomic replace (rename) is
  unverified on hardware.
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
- **Hybrid cache (built 2026-10-23):** mirror every targeted track while it
  fits under `max_cache_gb` / 80% of the card. When over, evict only songs
  `Record.songs_on_every_player()` shows **present on every player** the
  record knows (DB saved), in the encoding the plan wants (an older one
  needs the new file to be replaced), largest first. Eviction happens only
  to make room for a song a player lacks, never for another held one (no
  churn). Held songs without a cache file are listed in the manifest's
  `on_player` (full entries, no `file`): `targeted_ids` keeps them on the
  player and `apply_plan` still applies name/genre fixes to them. A reset
  or newly added player holds nothing, so nothing is evicted until it's
  rebuilt. An unreadable record = nothing held (the cache just stops
  filling). Eviction is about efficiency, not safety: Navidrome has every
  song, so ignoring a player only costs re-downloads if it docks again.
  A player still in use that is *full* (e.g. a 16 GB Nano next to a big
  Classic) never gets the rest, so it blocks eviction too; forget it with
  `DockSetup.txt` or `musicdock-players forget` (not automatic yet).
- **Rolling batches for big jobs (built 2026-10-23):** missing songs are
  fetched in priority order (songs the player lacks first; among them
  playlist songs before starred-only ones; held songs last, while room
  lasts). The prepare job fills the cache and writes the plan; the auto
  re-sync syncs the docked player; the next prepare run (5 min later)
  evicts what was just copied and fetches the next batch. A prepare run
  waits for a running sync before it *starts*.
- **Concurrent batches (2026-10-23, user request):** within a run, every
  `[sync] sync_batch_size` (default 25; 0 = off) newly prepared tracks,
  `hand_over()` writes an interim plan (`build_manifest(waiting=True)`:
  ready tracks + every not-yet-prepared song in `skipped` as "waiting to be
  prepared", so the sync removes nothing) and `on_batch` asks for a re-sync
  of a docked iPod; conversion carries on, so USB copying overlaps
  conversion (today's 903-track run: ~3 h conversion then ~2.75 h copying,
  one after the other). Only with 2+ CPU cores (`os.cpu_count()`; on a
  Zero W a sync would just slow the one core). A batch that finds a sync
  still running skips the request; the end of the run waits for the
  running sync, then asks, so the last tracks aren't stranded. The final
  plan always replaces an interim one. Skipped batches combine by
  themselves: every plan lists everything ready, and a sync copies all of
  it that's missing. **No idle gap:** as a sync ends, `dock_sync.
  ask_for_followup` asks for the next one if the plan on disk is newer
  (`generated` differs) than the one it applied and has changes for this
  player - only then, so a full player or missing file never loops - and
  `dock_resync` now waits up to `SYNC_END_WAIT` (120 s) for that sync to
  end instead of skipping at once (unit timeout raised to 5 min).
  **Verified on hardware 2026-10-23:** 47 tracks; batch 1 (25) handed over
  at 13:25:52, synced 13:25:55-13:29:17 while the other 22 converted
  (done 13:29:58), final sync of 22 done 13:31:54, removed 0 both times.
  The follow-up path wasn't needed (batch sync ended before the run).
- **PREPARE & SYNC screen (`prepsync`, 2026-10-23, user design):** while a
  batch sync copies during a prepare run: header PREPARE & SYNC (inverted),
  "Converting done/total", "Synced n/to-copy" (to-copy also counts songs reused from the cache; the batch count is kept but not shown, user 2026-10-23), "Do not remove ·
  time", bar = (converted + synced) / (2 x total). `dock_progress.py` keeps
  one JSON per writer in /run/musicdock (prepare: run id, started/total,
  batches, pid; sync: copied so far this run, syncs), and a run only counts
  while its prepare pid is alive (cleared in a `finally`). The sync draws
  it from its copy step instead of SYNCING; the prepare job redraws it
  (only over prepsync, 30 s rate limit) to keep the conversion count
  current. Treated like SYNCING everywhere: in SYNC_SCREENS (prepare
  screens never cover it), dock_boot turns a leftover one into "Sync
  interrupted by restart", not in CONNECTING_OVER.
  Tested end to end (`test_player_bigger_than_cache_fills_over_several_syncs`);
  not yet on hardware with a full cache. Not built: a `Restoring 12,340 /
  40,000` screen (the sync shows `Copying n/N` per batch).
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
  `flush` on a later sync and compare), so 200 GB of copying ~40+ h.
  FLAC->MP3 conversion was the bottleneck on the Zero W (~song length per
  track, ~60 s per audio minute: 200 GB of FLAC ~20 days).
  **Measured on the Zero 2 W (2026-10-23, 903-track run, 3 workers):**
  - Per ffmpeg: **9.6 s per minute of audio** (6.2x real time).
  - Whole job (3 workers, downloads/covers overlapping): **3.7 s per minute
    of audio**, i.e. 16 audio min per wall min = 87% of 3x ideal; ~16x the
    Zero W. Short tracks make track counts misleading: measure audio
    minutes (start = journal `Converting <label>`, end = cache file mtime,
    length from the MP3; script kept on the Pi as `~/convrate.py`).
  - Rate fell ~18% (17.4 -> 14.2 audio min/min) on large FLACs (Pendulum,
    40-60 MB each: slower downloads and decodes).
  - 200 GB of FLAC (~35,000 audio min) ~1.5 days of conversion.
  - **USB copy to the Nano on the Zero 2 W (2026-10-23, auto re-sync after
    the run):** 902 tracks / 5.41 GB in 41 min (11:31-12:12) = **2.75
    s/track, 2.2 MB/s** (USB measured 1.7 MB/s instantaneous) vs ~11
    s/track, 1.3-1.5 MB/s on the Zero W: most of the Zero W's time was CPU
    (pygpod DB + artwork), not USB. So on the Zero 2 W conversion is the
    bottleneck again (200 GB: ~26 h copying vs ~36 h conversion), and with
    concurrent batches copying adds almost nothing.
  - ~50 C with a heatsink, never throttled; downloads ~2.5 MB/s on a good
    Wi-Fi network.
  - Memory with `limit_malloc_arenas()`: the job (RSS + swap) held ~75-100
    MB for 300+ tracks (vs ~230 MB RSS and OOM at 116 before), rose to
    ~120-150 MB during the large FLACs (~track 330) and then **stayed flat
    at ~135-150 MB to the end** (903 tracks, 2 h 52 min, finished 11:30;
    1292/1292 ready, 0 skipped, no OOM). Confirmed fixed for a full run;
    profile with `tracemalloc` if a run ever passes ~200 MB.
  Plays recorded since the last sync are lost in a wipe; server-purged
  discovery tracks can't return.
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
   (see "Resilience without a mirror"). Reset detection, SysInfo recovery,
   record backup and the cache side are built; hardware test with a cache
   smaller than the library pending. Rockbox handler built 2026-10-23
   (fake players only; see `rockbox_handler.py`).
6. 3D-printed dock enclosure
7. Documentation, setup guide, and packaging for friends. **Built
   2026-10-16:** README.md, INSTALL.md, `make_release.sh` (builds
   `dist/music-dock/` + `dist/music-dock-<date>.tar.gz`: only Pi code, pi/,
   vendor/, docs; refuses to build on failing tests or personal data; no macOS
   tar metadata). Verified by unpacking on the Pi, importing every module and
   running `install.sh --check`. **Fresh install, 2026-10-23:** the Pi Zero 2
   W was set up from scratch; the only problem was Wi-Fi (WPA2/WPA3 + the
   Raspberry Pi OS brcmfmac bug, now fixed by install.sh); once connected,
   everything worked (user). Not yet done from the release tarball itself.
   The friend (iPod classic) is still the first outside install. Original plan:
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
  (16.2 GB), so Linux can write it. `iTunesDB`
  is version 0x75 and **signed (hash58)** - any DB writer must re-sign it, or
  the iPod shows no music. hash58 needs the FireWire GUID, which is the USB
  serial number (the user's is in CLAUDE.local.md; read it at dock time from udev
  `ID_SERIAL_SHORT` rather than hard-coding). `iPod_Control/Device/SysInfo` is
  empty and there's no `SysInfoExtended` or `Artwork/` yet; libgpod
  conventionally expects `FirewireGuid` in SysInfo.
- **pygpod manual test (2026-10-16, on the Mac): passed.** pygpod 0.0.6
  (PyPI, pure Python) identified the Nano as Nano 4G / hash58 once `SysInfo`
  had `ModelNumStr: xB903` and `FirewireGuid: 0x<the GUID>`, reproduced
  iTunes' own signature exactly, added a track with artwork (6 thumbnail
  sizes), and the iPod played it and recorded 1 play in `Play Counts`. Without
  a model in SysInfo, pygpod assumes no signature and writes an unsigned
  (rejected) DB - always check `requires_hash` before saving.
- **Sync memory (investigated 2026-10-23, after a batch sync ran the Zero 2
  W out of memory):** (1) **Removals:** pygpod's `remove_artwork` flags a
  rebuild, and `_rebuild_ithmb` rewrites each `.ithmb` in place - opening
  it `"wb"` truncates it, so it first reads *every* surviving image of that
  size into a list. The Nano's art is 460 MB in 6 sizes (two of 169 MB,
  one ~115 KB image per track per size: per-track covers, not per-album
  like iTunes), so a removal sync held 169+ MB (likely more: 115 KB
  blocks sit under glibc's 128 KB mmap threshold, so freed memory may not
  return before the next size) and rewrote 460 MB over USB (7.5 min).
  Replaced by `install_artwork_compaction()`: stream one image at a time to
  `.compact` + fsync + rename, offsets updated only after; and only when a
  file is >= `COMPACT_WASTE` (25%) unused. Measured: 60 covers, half
  removed: 3.52 -> 0.15 MB peak, same files; real Nano, 30 removed: save
  2 s, no rewrite. (2) **Baseline:** opening the DB = 21 MB (python +
  pygpod) + 37 MB for 1443 tracks (~26 KB/track from a 3 MB file) + 24 MB
  parsing a 1.6 MB ArtworkDB = 83 MB; a save adds ~14 MB (peak 102). The
  sync also kept the play snapshot (a second full DB) alive all session
  and `plays_from_snapshot` loaded it a third time: now reused and `del`'d
  before the live DB opens (scratch test 139 -> 102 MB). Real sync peak
  148 -> 96 MB. **Concurrent batches back on by default (sync_batch_size
  25, 2026-10-23)** after shared album art: a 73-track run (+14 reused
  from the grace-period cache) on the Zero 2 W did 3 batch syncs (39, 26,
  22 tracks, 0 removed) beside 3 conversions; sync ~80-90 MB, prepare ~95
  MB, free RAM never below ~150 MB with 270+ MB swap free; iPod complete
  1.25 min after conversion ended. A test watchdog (stop prepare if RAM +
  swap free < 80 MB during a sync) never fired. Not built (optional
  hardening): pausing conversions during a sync, deferring removals to a
  run's last sync.
- **Shared album art (2026-10-23):** one image per album instead of per
  track on SIGNED_GENERATIONS (Classic 1-3, Nano 3G/4G): tracks with the
  same album name and source cover size share an image via mhii_link
  (`install_shared_artwork` wraps pygpod's `_add_artwork_for_track` and
  `remove_track`: an image another track still shows is never deleted, and
  its owner (`mhii` song_id) moves to a remaining track). Existing art is
  converted once (`share_existing_artwork`, marker `iPod_Control/musicdock/
  shared-art`). On the real Nano: 1413 images -> 118 (15 of 95 albums keep
  several: genuinely different covers), .ithmb 451 -> 39 MB, conversion
  sync ~1 min, peak 103 MB. **Right after the conversion (iPod docked
  through it) the album list showed no art and playing a song crashed the
  iPod; after a restart (Menu + center) everything was correct** - covers
  in the list, Now Playing, per-track variants (verified by the user).
  Cause unknown (likely stale state in the iPod across a big art change);
  README tells users to restart once. Video / nano 1G-2G keep per-track
  art (their firmware finds art per track, unverified). Also fixed:
  pygpod's ArtworkDB writer wrote an emptied list record back with its old
  entries (`if not rec.children: return rec.raw_bytes`), resurrecting the
  last image removed; compaction now swaps files in together with their
  ArtworkDB and keeps `.before-compact` until the sync succeeds
  (`undo_compaction` on restore, `recover_compaction` after a crash).
- **pygpod's USB fallback (found 2026-10-23):** if SysInfo has no model,
  pygpod scans `/sys/bus/usb` (no pyusb needed) and takes the model of the
  *first Apple USB device* it finds. Harmless on the dock (one port, and
  `ensure_sysinfo` writes the model before any write; `checked_save` still
  checks the GUID), but it made `test_refuses_to_save_unsigned` pass the
  save on the Pi while the Nano was plugged in. Tests that need "no model"
  must patch `pygpod.device.usb.detect_ipod_usb`.
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
