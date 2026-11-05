# Setting up a SongDock

About an hour of hands-on time, plus waiting for software to install and
music to convert. You'll type a few commands into a terminal; each step shows
exactly what to type.

## What you need

**Hardware** (the tested setup):
- **Raspberry Pi Zero 2 W** (or Zero W) **with a soldered pin header**
- **Waveshare 2.13" e-Paper HAT** (250x122, black and white): **V4** is the
  tested one; **V3** and **V2** are supported too (untested on hardware).
  The version is printed on the back of the screen; for V3 or V2, set
  it in the settings (step 6):

  ```toml
  display.model = "v3"        # or "v2"; "v4" is the default
  ```

  A V2 screen redraws with a full refresh every time (a brief flash on each
  update) instead of updating in place.
  - *Have a spare Pwnagotchi?* It's usually this hardware - a Pi Zero W or
    Zero 2 W with one of these screens - so it can become a SongDock.
- **microSD card**, 32 GB or larger (64 GB recommended: the dock caches your music).
  8 GB works but leaves only about 3 GB for music, so a big library takes many
  rounds; 4 GB is too small (the system alone needs about 3 GB)
- **micro-USB OTG adapter** + an iPod **USB-to-30-pin cable** (the iPod plugs
  into the Pi's "USB" port, not the "PWR" one)
- A good **5 V power supply, 2.5 A**, for the Pi's "PWR" port. The Pi also
  charges the iPod, so a weak supply causes random restarts.
- A supported **iPod** (see [README.md](README.md))

**Also:**
- A computer (Mac, Windows or Linux) on the same Wi-Fi
- A **Navidrome** server and an account on it, reachable from the dock's Wi-Fi
  - Use either `http://` on your local network, or `https://` with a properly
    signed certificate. A self-signed certificate will be refused.

## 1. Prepare the iPod

1. **Check it's FAT32.** Plug it into your computer and enable disk use
   (in iTunes/Finder: tick "Enable disk use"; it then appears as a drive).
   - **Mac:** select the iPod in Finder, choose File > Get Info: "Format" must
     say **MS-DOS (FAT32)**.
   - **Windows:** if it shows up as a drive letter in File Explorer, it's FAT32.
   - If it's Mac formatted ("Mac OS Extended"), restore it with **iTunes on a
     Windows PC**. That erases it and formats it FAT32.
2. **Opt it in:** in the iPod's top folder, create an empty text file named
   **`MediaDock.txt`**. Without it the dock won't touch the iPod.
3. Eject it.

> **Whose music and plays:** the dock fills this iPod from the Navidrome account in
> its settings, and sends the iPod's plays to that account. Only add `MediaDock.txt`
> to an iPod if that's what its owner wants. After you add the file, keep the iPod
> away from Finder and iTunes syncing: they replace its whole music database.
>
> **Once an iPod has synced, deleting `MediaDock.txt` doesn't stop it:** the dock
> remembers the iPod and makes the file again. To stop, don't dock it.

## 2. Put Raspberry Pi OS on the SD card

1. Install **Raspberry Pi Imager** on your computer
   (https://www.raspberrypi.com/software/).
2. Choose:
   - **Device:** your Pi (e.g. Raspberry Pi Zero 2 W)
   - **OS:** Raspberry Pi OS (other) > **Raspberry Pi OS Lite**:
     **32-bit** on a Pi Zero W (the only kind it can run); **64-bit** on a
     Pi Zero 2 W (what the dock is tested with there). Pick 32-bit on a
     Zero 2 W only if you may move the card to a Zero W later (see "Which
     Raspberry Pi" in README.md). It must be Bookworm or newer; the installer checks.
   - **Storage:** your SD card
3. When asked, **edit the settings**:
   - **Hostname:** e.g. `musicdock`
   - **Username and password:** pick your own and write them down
   - **Wi-Fi:** your network name and password
   - **Locale/time zone:** **set your real time zone**. Play times come from
     the iPod's clock, read in the dock's time zone.
   - **Services:** enable **SSH** (password authentication is fine)
4. Write the card.

## 3. Assemble and boot

1. Press the e-Paper HAT onto the Pi's pin header (the display faces up, away
   from the Pi).
2. Put the SD card in the Pi and connect power to the **PWR** port.
3. Wait about 5 minutes for the first boot.

## 4. Download SongDock onto the Pi

On your computer, open a terminal (Mac: Terminal; Windows: PowerShell) and log
into the Pi, using your username, and your hostname if you picked a different one:

```sh
ssh yourname@musicdock.local
```

Type `yes` the first time, then your Pi password. Everything below is typed
**on the Pi**. Find the newest version on the
[releases page](https://github.com/Wuverul/SongDock/releases) and put it in place
of `0.7.5`:

```sh
VERSION=0.7.5
curl -LO "https://github.com/Wuverul/SongDock/releases/download/v$VERSION/songdock-$VERSION.tar.gz"
mkdir -p ~/musicdock
tar -xzf "songdock-$VERSION.tar.gz" -C ~/musicdock
cd ~/musicdock/songdock
```

## 5. Install

```sh
sudo ./pi/install.sh
```

The dock's code goes to `/opt/songdock`, owned by root, since the dock's services
run as root. The unpacked folder is only the source: you can delete it afterwards.

This installs what the dock needs (ffmpeg, Python packages, the display
driver), turns on the display interface, creates your settings file, sets up
the dock's background services, including automatic updates (see "Updating"), and
keeps the logs across restarts (up to 50 MB) so problems can be traced afterwards. It
takes a while on a Pi Zero. If it says a reboot is needed:

```sh
sudo reboot
```

After the reboot the screen shows **STARTING UP...** and then **DOCK READY**.

## 6. Enter your Navidrome details

Log back in (`ssh yourname@musicdock.local`) and open the settings file:

```sh
nano ~/.config/musicdock/config.toml
```

The file is in four parts, from "must set" to "change only if you know you
need to". Fill in part **1. REQUIRED**:

```toml
server.url = "http://192.168.1.50:4533"     # your Navidrome address
server.username = "your-navidrome-username"
server.password = "your-navidrome-password"
sync.safe_hours = "07:00-10:00"             # no syncing in this daily window, or "off"
device.write = false                        # leave false for now: step 9 turns it on
```

Safe hours keep the dock from starting a sync when you're about to grab the
iPod, e.g. before a morning commute (more under "Safe hours" below). Set a
window that fits your day.

Then look over part **2. CHECK THESE** (what syncs, cache size, the screen's
version). Parts 3 and 4 can wait: their lines start with `#`, which means the
dock uses the default shown. To change one, delete the `# ` and edit the value.

Save with **Ctrl-O**, then **Enter**, and exit with **Ctrl-X**. The file is
readable only by you; the password is used to sign each request and never
sent as is.

*Settings files from SongDock 0.7 and earlier* group settings under headings
like `[sync]`. In those, write a setting from this guide without its group:
`make_room = true` under `[sync]` rather than `sync.make_room = true`.

## 7. Choose your music

In Navidrome:
- **Favorites:** star songs or whole albums. Starred albums sync all their tracks.
- **Playlists:** open a playlist, click the edit (pencil) button, and put
  **`#ipod`** in its **Comment** field (the field is easy to miss). Remove the
  tag to stop syncing that playlist.
- **Lossless (optional):** put **`#lossless`** in a playlist's comment too, and its songs
  sync as ALAC if the server's copy is lossless. It takes about 3 times the space, so
  leave this for later on a small iPod.

At its next check (every 5 minutes unless you change
`check_interval_minutes`) the dock notices, then downloads and converts the music
(**PREPARING** on the screen). On a Pi Zero W, converting a FLAC song takes a
bit less time than the song lasts; a Pi Zero 2 W converts about 16 minutes of
music per minute (see "Which Raspberry Pi" in README.md). Songs that are
already MP3 just download. A big first batch can take hours (Zero 2 W) or
days (Zero W). The iPod doesn't need to be docked for this.

## 8. First dock: a read-only check

Plug the iPod into the dock. The screen shows **SYNCING**, then **SAFE TO
REMOVE**. This first time the dock only reads: it logs any plays to Navidrome
but doesn't change the iPod. To watch what it's doing:

```sh
journalctl -u 'musicdock-*' -f
```

(Ctrl-C to stop watching.) It should show the iPod's plays being read and no music
being copied.

## 9. Turn on syncing

```sh
nano ~/.config/musicdock/config.toml
```

Change `device.write = false` to `device.write = true` (part 1), save, and
dock the iPod again. This time it copies your music and playlists. Expect about
**3 seconds per song on a Pi Zero 2 W** (500 songs in about 25 minutes) or **11 seconds
on a Pi Zero W** (about an hour and a half). Later syncs only copy what changed.

When it shows **SAFE TO REMOVE**, unplug it and check the music, playlists
and album art.

## Everyday use

- Dock the iPod whenever you like, e.g. overnight to charge. Each dock sends
  your plays and brings the iPod up to date.
- **DOCK READY** shows what the next sync will do. For more detail, run
  `musicdock-status` on the Pi (see Troubleshooting).
- If Navidrome is offline, music still syncs and plays are sent later.
- An iPod left on the dock syncs again by itself when new music has been
  prepared (it briefly reconnects; playback stops until the sync is done).
  To turn that off: `device.auto_resync = false` in the settings.
- From 7 to 10 AM the dock doesn't sync (see "Safe hours" below). It shows **SAFE
  HOURS** while a player is connected, and the iPod is free to take.
- Rate a song 4 or 5 stars on the iPod and the next sync favorites it in Navidrome, once.
  It never removes a favorite. To change the stars, set `favorite_rating`; 0 turns it off.
- If the iPod fills up, new songs wait and the screen shows **PLAYER FULL**. Nothing is
  removed unless you turn on `make_room` (below).
- Songs removed from your lists stay cached on the dock for 7 days, so
  re-adding one is instant (`removal_grace_days` in the settings).

## Optional extras

**Dark mode** (white on black, nice in a dark case): in the settings file set

```toml
display.theme = "dark"       # or "light" (the default)
```

**How often the dock checks for new music** (default every 5 minutes): in
the settings file set, for example,

```toml
sync.check_interval_minutes = 15
```

It takes effect a few seconds after you save the file (no restart needed).
Checking more often means new music reaches a docked iPod sooner; each check
that finds nothing new takes a few seconds and changes nothing.

**Copying while converting** (Pi Zero 2 W and other multi-core Pis): during
a big batch of new music, finished songs are handed to a docked iPod in
batches, which it copies while the rest convert - one sync for the whole run;
the screen shows **PREPARE & SYNC** until it's all done. By default a batch
is a twentieth of the run (25 to 500 songs). Set a fixed number, or 0 to
copy only once everything is converted:

```toml
sync.sync_batch_size = "auto"
```

**Safe hours** (no syncs in a daily window, so a sync doesn't hold the iPod
on the dock when you leave in the morning). On by default, 7 to 10 AM local
time; you set the window in step 6, or turn it off:

```toml
sync.safe_hours = "07:00-10:00"         # or e.g. "06:30-08:00", or "off"
```

Or put `SafeHours=06:30-08:00` (or `SafeHours=off`) in `DockSetup.txt` on the
iPod. An iPod docked
in the window just charges and stays usable; a sync already running when it
opens stops copying and finishes (SAFE HOURS on screen). Either way, a
still-docked iPod syncs when the window ends. Other players stop too, but need
plugging in again to get the rest.

**Making room on a full player** (off by default): with this on, new songs that don't
fit push off the oldest songs, only as many as needed. Songs played, added or in an
updated playlist within `recent_play_months` are safe, and removed songs come back when
there's room, or as soon as you play one in Navidrome's web or desktop app (a docked iPod
syncs to bring it back). `MediaDock.txt` on the iPod lists what was taken off. Whole albums go, dated by their newest song, so one recent play keeps an
album; `make_room_albums = false` removes songs one by one instead:

```toml
sync.make_room = true
sync.make_room_albums = true
sync.recent_play_months = 3
```

**Names on the player:** a song lists under its album's artist, with guests moved to the
title. These only change the player, not Navidrome:

```toml
sync.primary_artist = true                 # false: keep the credits as they are
sync.artist_names = ["Now, Now"]           # names with a comma, & or / to keep whole
sync.compilation_albums = ["Monthly Mix = My Mixes"]   # list these as one artist
sync.artist_aliases = ["Beatles = The Beatles"]        # another spelling of one artist
sync.singles_album_min = 4                 # one-song albums before "<Artist> Singles"
```

**Genres:** the iPod's Genres menu sorts every song into one of a built-in
list of groups (Electronic, Hip-Hop, Soundtracks...). To make your own, copy
the list from `genres.example.toml` (in `/opt/songdock/app/`) to the end of
your settings file and edit it; the file explains how.

**Moving the dock to another Wi-Fi network:** put a file named **`wifi.txt`**
in the iPod's top folder (next to `MediaDock.txt`):

```
SSID=Network Name
Password=network password
```

Dock the iPod (with `write = true`). After the sync the dock tries the new
network: **WI-FI CHANGED** means it worked and `wifi.txt` was deleted; **WI-FI
UNCHANGED** means it went back to its old network and left the file there,
with the reason on screen. Add `Hidden=yes` for a hidden network; leave out
the password for an open one.

The old network stays saved as a fallback. If the new one drops out for a
moment, the dock may join the fallback; it checks every 5 minutes and goes
back to the network you gave it in `wifi.txt` once that's in range again.

**Rockbox players** (tried with a flash drive standing in for one, not yet a real player): opt in the same way,
with `MediaDock.txt` in the player's top folder. The dock recognizes Rockbox
by its `.rockbox` folder and then:

- copies music into a **`MediaDock`** folder (`MediaDock/Artist/Album/01
  Title.mp3`, with `cover.jpg`), and playlists into **`Playlists`**. Music
  you keep anywhere else on the player is never touched;
- reads plays from Rockbox's **`.scrobbler.log`**: turn on **Settings >
  Playback Settings > Last.fm Log**. Skipped songs don't count. The log is
  emptied once its plays are saved on the dock;
- for browsing by artist/album, turn on **Settings > General Settings >
  Database > Auto Update** (or use **Update Now** after a sync);
- to favorite songs in Navidrome, add them to the **`Keep`** playlist the dock makes in
  `Playlists`. The next sync favorites each one, once.

**Music format:** by default Rockbox gets the same files as stock iPods (MP3,
or ALAC with `convert_to = "alac"`), so one dock serves both. To send FLAC
as-is instead (lossless, no conversion, about 2-3 times larger), turn off
stock-iPod compatibility in the settings:

```toml
device.type = "rockbox"     # copy music as it is on the server (FLAC included)
```

The cache is then re-filled in the new format at the next check, and songs
already on the player are swapped for their FLAC versions at the next syncs.
A dock set this way doesn't sync music to stock iPods (they can't play
FLAC); set it back to `"stock_ipod"` to serve them again. If a player can't
play FLAC (many cheap MP3 players can't), keep the conversion for FLAC only:

```toml
device.type = "rockbox"
device.flac = false         # FLAC converted to convert_to; other formats as they are
```

`FLAC=no` / `FLAC=yes` in a `DockSetup.txt` does the same from a player.

The same switch works without SSH, from a `DockSetup.txt` on a docked
player: `StockCompatible=no` (refused while any stock iPod still syncs,
including the one docked; retire them first with `Forget=`, which can go in
the same file) or `StockCompatible=yes`. A stock iPod docked on a
Rockbox-only dock still has its plays read, and a `DockSetup.txt` with
`StockCompatible=yes` on it switches the dock back; its music follows once
it has been converted.

**Other music players** (cheap MP3 players, SnowSky-style players: anything
that plays a folder of music, without Rockbox; tried with a USB flash drive
standing in for one): put an empty `MediaDock.txt` in the player's top folder (on the drive
you want synced, if it shows up as two) and dock it. The dock needs
`write = true` for these, since it has nothing to read from them. It then:

- copies music into a **`MediaDock`** folder, as for Rockbox, including the
  songs of tagged playlists. There are no playlists on the player;
- removes songs that left your lists;
- reads **no plays**: these players keep no history the dock can read, so
  SAFE TO REMOVE says "No play count - unsupported";
- writes a small ID file in `MediaDock/.musicdock/` so it can tell your
  players apart (cheap ones often share one USB serial number). Deleting it
  makes the dock treat the player as a new one.

`wifi.txt` and `DockSetup.txt` work on these players too. Unplug and replug
to sync new music. On a dock with stock-iPod compatibility off, set
`flac = false` (above) if the player can't play FLAC.

## Troubleshooting

| What you see | What to do |
|---|---|
| Screen stays blank after install | Reboot (`sudo reboot`); check the HAT is pressed fully onto the pins |
| **NOT SYNCED · No MediaDock.txt** | Add `MediaDock.txt` to the player's top folder (step 1) |
| **PLAYER FULL** | New songs didn't fit. Remove lossless tags or some music from your lists, or turn on `make_room`. The sync itself finished, so it's safe to unplug |
| **UPDATING** | A new version is installing. Leave the power connected. It shouldn't take long, and the previous version comes back if it fails |
| **SYNC FAILED · Unknown iPod model** | Look up the model on the iPod (Settings > About > Model, e.g. `MB029`). On the iPod (disk mode on your computer), open `iPod_Control/Device/SysInfo` in a text editor and add a line `ModelNumStr: xB029` (an `x` plus the model without its first letter). Dock again |
| **SYNC FAILED · iPod ... isn't supported** | That iPod's music database can't be written by the dock (see "Which iPods work" in the README). Nothing on it was changed |
| **SYNC FAILED · Mac-formatted iPod** | The iPod is formatted for Mac (HFS+), which the dock can only read. Its plays were still sent (or queued). Restore it once with iTunes on a Windows PC to make it FAT32 (this erases it), add `MediaDock.txt` again, and dock it |
| **SYNC FAILED** with another reason | Check the log (below). The iPod was restored to how it was before the sync |
| Nothing happens when docking | Check you're using the Pi's **USB** port (not PWR) with the OTG adapter; check the log |
| **NO WI-FI** after starting up | The dock can't join its network. Fix it with `wifi.txt`, or re-flash the SD card's Wi-Fi settings with Raspberry Pi Imager |
| The screen shows Wi-Fi and an IP address, but you can't reach the dock | The dock joined an access point that isn't passing its traffic. Restart that access point, or in your router's settings pin the dock to another one. The dock keeps working offline meanwhile: plays wait and music still syncs |
| The Pi's green light is on solid | That's normal: on a Pi Zero the light is on when idle and flickers when the SD card is busy. It alone doesn't mean the Pi has frozen |
| Music doesn't appear | Check `url`, `username` and `password` in the settings, and the log of the prepare job |

**Status report:** `musicdock-status` on the Pi (or
`ssh yourname@musicdock.local musicdock-status` from your computer) sums up
what the dock is doing and lists anything that needs attention.

**Asking for help:** `musicdock-status --share` prints a report to paste into a
[GitHub issue](https://github.com/Wuverul/SongDock/issues) or a message. It leaves out
your server, sign-in, Wi-Fi, player, playlist and song names, and sends nothing by itself.

**On the iPod itself:** after each sync, `MediaDock README.txt` in the
iPod's top folder explains every file you can add there (`wifi.txt`,
`DockSetup.txt`, `MediaDock.txt`) and what the screens mean.

**Changing settings from the iPod:** a `DockSetup.txt` can also hold, for example,
`PlaylistTag=#car`, `SyncStarred=no` (playlists only), `MakeRoom=yes`, `ArtistNames=`, `ArtistAliases=`,
`AutoUpdate=no`, or `Server=` with `Username=` and `Password=`. `MediaDock README.txt`
on the iPod lists every option.

**Switching to a new iPod:** the dock keeps songs cached until every iPod
it syncs has them, so an old, smaller iPod can stop a bigger new one from
filling up once the dock's storage runs short. Once you stop using the old
one, retire it:

1. Open `MediaDock.txt` on the new iPod (disk mode on a computer). After
   each sync it lists the iPods the dock knows, by the name you gave them.
2. Next to it, create `DockSetup.txt` containing, for example:
   ```
   Forget=Jo's iPod (nano)
   ```
   The model in brackets is only needed if two iPods share a name; use
   `Forget=all others` to retire every iPod except this one, and
   `Keep=<name>` to bring one back.
3. Dock the iPod. The screen shows **DOCK UPDATED** (or **SETUP NOT
   USED** and why), the dock deletes `DockSetup.txt`, and the outcome is
   noted at the end of `MediaDock.txt`.

Over SSH, `musicdock-players` lists the same iPods and
`musicdock-players forget "Jo's iPod (nano)"` does the same. Forgetting
changes nothing on either iPod, the old one's plays are still sent, and
docking it again brings it back.

**Logs:**

```sh
journalctl -u 'musicdock-*' --since today        # everything today
journalctl -b -1 -e                              # the end of the previous run (before the last restart)
journalctl -u musicdock-prepare -e               # downloads and conversions
journalctl -u 'musicdock-sync@*' -e              # docking sessions
/opt/songdock/venv/bin/python /opt/songdock/app/songdock/jobs/dock_prepare.py --show-plan
```

## Updating

From 0.7.2 on, the dock updates itself: DOCK READY shows the new version and its install
time, 4 hours ahead, and it installs once no player is connected and nothing is running.
Only releases signed by the project install. To only be told about them, set
`sync.auto_update = false` in the settings. Your settings, cache and the record of
what's on your iPod are kept. Automatic updates are new and haven't yet run for real, so
check `musicdock-status` after one.

To update by hand, unplug the iPod and run this on the Pi, with the newest version:

```sh
VERSION=0.7.5
curl -LO "https://github.com/Wuverul/SongDock/releases/download/v$VERSION/songdock-$VERSION.tar.gz"
tar -xzf "songdock-$VERSION.tar.gz" -C ~/musicdock
cd ~/musicdock/songdock && sudo ./pi/install.sh
```

## Uninstalling

```sh
sudo systemctl disable --now musicdock-prepare.timer musicdock-boot.service \
        musicdock-resync.path musicdock-settings.path musicdock-usbport.timer \
        musicdock-wifiprefer.timer musicdock-update.timer
sudo rm -rf /etc/systemd/system/musicdock-*
sudo rm /etc/udev/rules.d/99-musicdock.rules \
        /etc/systemd/journald.conf.d/50-musicdock-persistent.conf \
        /etc/tmpfiles.d/musicdock.conf \
        /etc/NetworkManager/dispatcher.d/90-musicdock-wifi \
        /usr/local/bin/musicdock-status /usr/local/bin/musicdock-players
sudo systemctl daemon-reload
rm -rf ~/musicdock ~/.config/musicdock
sudo rm -rf /var/lib/musicdock /opt/songdock
```

The music already on your iPod stays there.

Two things are left in place on purpose, because removing them could cut
the Pi off from Wi-Fi:

- Wi-Fi networks added with `wifi.txt`: list them with
  `nmcli connection show` (they're named `musicdock: <network>`) and delete
  one with `sudo nmcli connection delete "musicdock: <network>"`.
- The Wi-Fi driver fix for WPA2/WPA3 networks,
  `/etc/modprobe.d/rpi-brcmfmac.conf` (written by the installer on 6.18
  kernels). Delete it only if you know your network doesn't need it, then
  restart.
