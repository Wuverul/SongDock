# Changelog

## 0.5.0 - first release

The first packaged version. Used daily with an iPod nano 4th generation on a
Pi Zero W and a Pi Zero 2 W; see "Known limitations" in the README for what
hasn't been tried on real hardware yet.

**Syncing**
- Logs plays from the iPod to Navidrome; plays wait on the dock if the
  server is offline and are never lost.
- Copies starred songs and albums and `#ipod`-tagged playlists, converted
  to MP3 (LAME V0) with album art, and removes songs that leave them.
- Recreates Favorites and each tagged playlist on the iPod, with albums in
  disc/track order, one copy per recording, and a short list of genres.
- An iPod left docked syncs again by itself when new music is ready.
- iPods bigger than the dock's storage fill over several syncs.
- Supports the iPod classic, video and nano 1st-4th generation (only the
  nano 4G tested), and Rockbox players (untested on hardware); Rockbox can
  get FLAC as-is on a Rockbox-only dock.

**Setting up and changing the dock from the iPod**
- `MediaDock.txt` opts an iPod in; after each sync it lists the iPods the
  dock knows.
- `MediaDock README.txt` on the iPod explains every file and setting.
- `wifi.txt` moves the dock to another Wi-Fi network.
- `DockSetup.txt` retires old iPods, changes the playlist tag, starred
  songs on/off, the server and sign-in, and stock-iPod compatibility.

**The dock**
- E-ink screen with sync status, progress and what the next sync will do.
- `musicdock-status` report; `musicdock-players` to list or retire iPods.
- Several server addresses (e.g. home and away), tried in order.
- Fixes Wi-Fi on WPA2/WPA3 networks with Raspberry Pi OS's 6.18 kernels.
