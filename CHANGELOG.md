# Changelog

## Unreleased

- **Other music players:** any USB player with a `MediaDock.txt` in its top
  folder gets music copied into a `MediaDock` folder, tagged playlists'
  songs included (no playlists on the player). No plays are read; SAFE TO
  REMOVE says "No play count - unsupported". Untested on real hardware.
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
