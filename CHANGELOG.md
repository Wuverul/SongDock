# Changelog

## 0.7.1

Lossless songs by playlist, and one artist listing per artist.

**To update:** copy the new package over and run the installer again, as in INSTALL.md ("Updating"). Your settings, cache and the record of what's on your players are kept.
- **Your Artists menu will change at the next sync.** This is on by default and only changes names on the player, with nothing re-copied. To keep the old names, set `primary_artist = false` in `[sync]`.
- **Lossless does nothing until you tag a playlist** with `#lossless`.

**Lossless for chosen songs**
- Add `#lossless` to a playlist's comment in Navidrome and its songs sync as ALAC (FLAC on a Rockbox-only dock), if the server's copy is lossless. The new file replaces the MP3, so a song is never on the player twice. Remove the tag and the MP3 comes back. Set `lossless_tag`, or `LosslessTag=` in `DockSetup.txt`, to change the tag or turn it off.
- Lossy sources are never "upgraded": MP3 and AAC stay as they are, and Ogg/Opus becomes MP3, including on a dock set to `convert_to = "alac"`.
- ALAC is always 16-bit, 48 kHz at most, because stock iPods are believed not to play more. 16-bit sources stay bit-exact. Existing ALAC files are converted again once.
- Plays are read and queued before a file is swapped, and the iPod keeps the song's play count, skips, rating and last played.
- The tag needs an iPod in the dock's list (a Rockbox player on a Rockbox-only dock with `flac = false`), so a dock serving only basic MP3 players gets plain MP3.
- DOCK READY shows "Lossless pending: 109 songs" or "Format changes: 4 songs", and a docked iPod re-syncs for them.

**One listing per artist**
- A song lists under its album's artist, or on a Various Artists album under the first name in its credit. Anyone else moves to the title: "Doin' It Right (feat. Panda Bear)". On a real library of 1,455 songs, 557 listings became 217.
- A comma, `&` or `/` separates names only with evidence, so "Above & Beyond" stays whole: Navidrome lists the names separately, or one appears alone in your library. Credits left whole are listed in `MediaDock.txt` and `musicdock-status`, and `artist_names` protects a name.
- Spellings differing only in case or punctuation merge ("leavv" and "Leavv").
- **`compilation_albums`** lists chosen compilations as one artist, e.g. `"Now That's What I Call Music"`, or `"Monthly Mix = My Mixes"` to name it. Also `CompilationAlbums=` in `DockSetup.txt`.

**Known limits**
- A dock serving both an iPod and a basic MP3 player has one cache, so the basic player gets the ALAC files too and may not play them.
- A hi-res ALAC file on the server is copied as it is, so it could reach an iPod that can't play it.
- Songs in a listed compilation can't be browsed by their real artists on the player.
- Verified on an iPod nano 4G: ALAC playback including a 24-bit/96 kHz source, album art, playlists, and 187 plays sent during a swap. Not yet tried on hardware: ALAC on an iPod Classic, `CompilationAlbums=` in `DockSetup.txt`, and a docked iPod re-syncing by itself for format swaps.

## 0.7.0

Getting ready for big iPods (e.g. a 512 GB Classic with 40,000 songs), plus
safe hours and a few new screens.

**If you're updating** (INSTALL.md, "Updating"): run the installer again; it
adds the Wi-Fi check. Safe hours are on from 7 to 10 AM unless you set
`safe_hours` (e.g. `"off"`). A config from 0.6.0 says `sync_batch_size = 25`;
change it to `"auto"` for the new batch sizes. Not yet tried on hardware:
plays read through the new database code, safe hours on a real dock, the
RESTORING screen (needs a new or wiped iPod) and an actual Wi-Fi switch-back.

- **The iPod's database is no longer loaded whole.** The dock keeps an index
  of it and saves by streaming the old file into a new one, so memory stays
  low however many songs the iPod has (40,000 songs: under 120 MB, where
  the old way needed about 1.3 GB). Saves go to a temporary file on the
  iPod and are then swapped in, so a cut-off save can't leave a broken
  database.
- **One save per sync** (or one an hour during a long one), instead of one
  every 25 songs. Songs copied before a crash or power cut are picked up by
  the next sync instead of being copied again.
- **One sync per batch of new music.** While new music converts, the docked
  iPod stays connected and takes each part as it's ready, instead of being
  reconnected and synced again for every 25 songs.
- **`sync_batch_size = "auto"`** (new default): a twentieth of the run per
  hand-over, 25 to 500 songs. A number still sets it; 0 still turns it off.
- **Back to the preferred Wi-Fi by itself.** When the network set with
  `wifi.txt` drops out for a moment, the dock joins the old one as a
  fallback - and used to stay there. It now checks every 5 minutes and goes
  back once the preferred network is in range (never during a sync; if that
  fails, it returns to the fallback and waits 30 minutes). Run the installer
  again to add it.
- **Safe hours, on by default from 7 to 10 AM:** no syncs in that daily
  window, so you can grab the iPod in the morning. A sync running when it
  opens finishes early; a docked iPod syncs once it's over. Change it with
  `safe_hours = "06:30-08:00"` (or `SafeHours=` in `DockSetup.txt`), or turn
  it off with `"off"`.
- **RESTORING screen** for a new or wiped iPod, which fills over several
  syncs: one count across all of them and the time left, instead of each
  round counting on its own. Not yet tried on a real new iPod.
- **Player icon on SYNCING and PREPARE & SYNC:** a click-wheel player
  (stock iPod), music notes (Rockbox) or a USB plug (other players) in the
  top bar's corner shows which kind of player is syncing (from the second
  screen on, once the player is mounted).
- **SYNCING no longer shows a stage number** ("5/6"): stages went past
  between redraws, so the numbers skipped. The line below says what it's
  doing, and the bar still shows progress (e.g. songs copied).
- **Size notes** in `musicdock-status`, the sync log and the iPod's
  `MediaDock.txt`: a large library from 20,000 songs (syncs may be slower)
  and 30,000 (slowdowns or crashes may occur); close to the limit from
  45,000.
- **At most 47,000 songs on a stock iPod.** They're reported to fail at
  about 50,000, so the dock stops adding songs at 47,000 (removals and
  plays carry on as usual).
- **ALAC suggestion** for a stock iPod with more than 250 GB that still gets
  MP3, when the music would still fit as ALAC (lossless, about 3x the size;
  it converts much faster but takes about 3x as long to copy).
- **A song too big for the dock's cache is skipped** (more than half the room
  it can ever have: `max_cache_gb`, or what keeping the SD card under 80%
  full leaves). Before, such a song - on a very small SD card, or a long mix
  in FLAC - stopped every run, so nothing after it ever arrived.
- **An album cover too big to decode in the Pi's memory** (e.g. a huge PNG)
  is removed from the song instead, and Navidrome's 600 px cover is used.
  Songs themselves are streamed through the dock, so their size never
  matters for memory.

## 0.6.0

More kinds of players, and a dock that tells you when its USB port has
stopped working. Run the installer again to update (INSTALL.md,
"Updating"): it adds the new USB check.

- **Other music players:** any USB player with a `MediaDock.txt` in its top
  folder gets music copied into a `MediaDock` folder, tagged playlists'
  songs included (no playlists on the player). No plays are read; SAFE TO
  REMOVE says "No play count - unsupported". Tried with a USB flash drive
  standing in for a player.
- **Rockbox players** ran on real hardware for the first time, a USB flash
  drive with a `.rockbox` folder: 1,454 songs, covers and 7 playlists, all
  correct. Reading plays from a real Rockbox player is still untested.
- Messages say "player" rather than "iPod" for Rockbox and other players.
- **USB NOT WORKING screen.** When a device failed to connect (e.g. a
  loose plug), the dock's USB port could stay dead - nothing plugged in
  afterwards was noticed until a restart - while the screen still said
  DOCK READY. The dock now spots this in the kernel log and asks for a
  restart. It doesn't try to revive the port itself: resetting the USB
  controller crashed the Pi's kernel when tried.
- **`flac = false`** (`[device]`, or `FLAC=no` in `DockSetup.txt`): with
  stock-iPod compatibility off, FLAC is still converted (to `convert_to`)
  for players that can't play it; other formats are copied as they are.

## 0.5.1

- **Fix: the dock stopped checking for new music after a restart.** The
  check-interval setting's timer override also removed the "1 minute
  after boot" check, so after a reboot the first check never ran and
  nothing started the next ones. Running the installer again (as when
  updating) repairs an existing dock.

## 0.5.0 - first release

The first packaged version of SongDock. Used daily with an iPod nano 4th
generation on a Pi Zero W and a Pi Zero 2 W; see "Known limitations" in the
README for what hasn't been tried on real hardware yet.

**Syncing**
- Logs plays from the iPod to Navidrome; plays wait on the dock if the
  server is offline and are never lost.
- Copies starred songs and albums and `#ipod`-tagged playlists, converted
  to MP3 (LAME V0) with album art, and removes songs that leave them.
- Recreates Favorites and each tagged playlist on the iPod, with albums in
  disc/track order, one copy per recording, and a short list of genres.
- An iPod left docked syncs again by itself when new music is ready.
- On a multi-core Pi (e.g. Zero 2 W), new music is copied to a docked iPod
  in batches of 25 while the rest is still converting, so a big batch of
  new music is on the iPod minutes after the last song converts.
- Album art is stored once per album on the iPod instead of once per song
  (on the Nano: 451 MB -> 39 MB). Restart the iPod once after the first
  sync converts its existing art.
- iPods bigger than the dock's storage fill over several syncs.
- Supports the iPod classic, video and nano 1st-4th generation (only the
  nano 4G tested), and Rockbox players, including non-iPod ones in theory
  (untested on hardware); a Rockbox-only dock can copy FLAC as-is.

**Setting up and changing the dock from the iPod**
- `MediaDock.txt` opts an iPod in; after each sync it lists the iPods the
  dock knows.
- `MediaDock README.txt` on the iPod explains every file and setting.
- `wifi.txt` moves the dock to another Wi-Fi network.
- `DockSetup.txt` retires old iPods, changes the playlist tag, turns
  starred songs on or off, changes the server and sign-in, and switches
  stock-iPod compatibility (with a failsafe while a stock iPod still syncs).

**The dock**
- E-ink screen with sync status, progress and what the next sync will do;
  PREPARE & SYNC shows conversion and copying together for a whole run.
  Light or dark (`[display] theme`).
- How often it checks Navidrome is adjustable (`check_interval_minutes`,
  default 5); saving the setting is enough.
- `musicdock-status` report; `musicdock-players` to list or retire iPods.
- Several server addresses (e.g. home and away), tried in order.
- Fixes Wi-Fi on WPA2/WPA3 networks with Raspberry Pi OS's 6.18 kernels.

**Reliability**
- A sync's memory peaks around 100 MB on a 1,700-track iPod (it used to
  run a 512 MB Pi out of memory): album-art clean-up after removals now
  works one image at a time, and only when worthwhile.
- Album-art files and their index are swapped in together and kept
  recoverable until a sync succeeds, so a failed sync or a power cut can't
  leave covers mismatched.
- After a lost record or a move to a new Pi, the dock re-adopts its own
  tracks and playlists from the iPod instead of treating them as yours.
- A fresh install never empties the iPod before the first music plan
  exists.
