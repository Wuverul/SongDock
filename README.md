# Music Dock

A Raspberry Pi dock that keeps a classic iPod in sync with a
[Navidrome](https://www.navidrome.org/) music server. Plug the iPod in, and
the dock:

1. **Logs what you played** on the iPod to your Navidrome account (play counts
   and "last played"). If the server is offline, plays wait on the dock and are
   sent later. Nothing is lost.
2. **Copies new music** from your Navidrome **Favorites** and from any playlist
   you tag with `#ipod`, converted for the iPod (MP3), with album art.
3. **Removes music** that has left those lists.
4. **Keeps your playlists** on the iPod (Favorites plus each tagged playlist).
5. **Charges the iPod**, and shows on its e-ink screen when it's safe to unplug.

Between docks it quietly checks Navidrome every 5 minutes and prepares new
music in the background, so syncs are quick.

**To set one up, follow [INSTALL.md](INSTALL.md).**

---

## Before you start: read these

- **Which iPods work.** The dock writes the iPod's own music database, which
  newer iPods sign in different ways.

  | iPod | Status |
  |---|---|
  | iPod nano 4th generation | **Tested** |
  | iPod classic (6th/7th generation, 2007-2009) | Should work (same database signature); not yet tested |
  | iPod nano 3rd generation | Should work; not tested |
  | iPod nano 5th generation and later, iPod touch | **Not supported** (different signature) |
  | Older iPods (video, mini, photo, original) | Not supported yet |

  Stock Apple firmware only (not Rockbox).

- **The iPod must be formatted FAT32 ("Windows format").** An iPod restored
  with Finder/iTunes on a **Mac** is usually formatted for Mac (HFS+), which the
  dock can't write to. Restore it once with iTunes on a **Windows** PC. See
  INSTALL.md, step 1.

- **The dock fills the iPod with *your* music and logs its plays to *your*
  account.** It only touches iPods that opted in with a `MediaDock.txt` file
  (see INSTALL.md). Don't add that file to someone else's iPod unless that's
  what you both want.

- **Music added any other way may be ignored.** Songs the dock didn't add are
  left alone, but don't sync the iPod with Finder/iTunes after setting up the
  dock: that replaces the whole iPod database, including the dock's music.

- **Your selection has to fit on the dock's SD card.** The dock keeps a copy
  of every song it syncs (about 2 MB per minute of music). A 64 GB card holds
  roughly 35-40 GB of music. Rebuilding an iPod that holds more than that is
  planned but not built yet.

## What the screen means

| Screen | Meaning |
|---|---|
| **STARTING UP...** | The dock is booting: waiting for Wi-Fi and the clock |
| **DOCK READY** | Idle. Shows what the next sync will do ("Next sync: +12 -3 tracks" or "iPod is up to date") |
| **PREPARING 3/12** | Downloading and converting new music. The iPod is safe to unplug |
| **SYNCING 5/6** | **Don't unplug.** Reading plays, copying music. The bar shows progress within the current step |
| **SAFE TO REMOVE** | Done: tracks added/removed and plays sent. Unplug whenever you like |
| **SYNC FAILED** | Something went wrong; the iPod was put back the way it was. Safe to unplug. See the log |
| **NOT SYNCED** | The iPod has no `MediaDock.txt`, or it isn't an iPod. Nothing was read or changed |
| **WI-FI CHANGED / UNCHANGED** | Result of a `wifi.txt` on the iPod (see INSTALL.md) |
| **NO WI-FI / CLOCK NOT SET** | The dock can't reach the network after starting up |

After a sync, the screen goes back to DOCK READY when you unplug the iPod, or
after 10 minutes.

## Settings

All settings are in `~/.config/musicdock/config.toml` on the Pi. See
[`config.example.toml`](config.example.toml) for every option with
explanations: which lists to sync, MP3 quality, how long removed songs stay
cached, dark mode, and more.

## Credits

- iPod database handling: [pygpod](https://github.com/Bionded/pygpod) (LGPL)
- E-ink display driver: Waveshare (MIT), bundled in `vendor/`
- Conversion: ffmpeg with LAME
