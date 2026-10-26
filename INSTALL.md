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
  `model` in the settings (step 6):

  ```toml
  [display]
  model = "v3"        # or "v2"; "v4" is the default
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
- The SongDock package: `songdock-<version>.tar.gz`

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

> The dock adds music from *your* Navidrome account to this iPod and logs its
> plays to *your* account. Delete `MediaDock.txt` to stop.

## 2. Put Raspberry Pi OS on the SD card

1. Install **Raspberry Pi Imager** on your computer
   (https://www.raspberrypi.com/software/).
2. Choose:
   - **Device:** your Pi (e.g. Raspberry Pi Zero 2 W)
   - **OS:** Raspberry Pi OS (other) > **Raspberry Pi OS Lite**:
     **32-bit** on a Pi Zero W (the only kind it can run); **64-bit** on a
     Pi Zero 2 W (what the dock is tested with there). Pick 32-bit on a
     Zero 2 W only if you may move the card to a Zero W later (see "Which
     Raspberry Pi" in README.md).
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

## 4. Copy the package to the Pi

On your computer, open a terminal (Mac: Terminal; Windows: PowerShell) in the
folder holding `songdock-<version>.tar.gz` and run (use your username, and your
hostname if you picked a different one):

```sh
scp songdock-*.tar.gz yourname@musicdock.local:
ssh yourname@musicdock.local
```

The second command logs you into the Pi (type `yes` the first time, then your
Pi password). Everything below is typed **on the Pi**:

```sh
mkdir -p ~/musicdock
tar -xzf songdock-*.tar.gz -C ~/musicdock
cd ~/musicdock/songdock
```

## 5. Install

```sh
sudo ./pi/install.sh
```

This installs what the dock needs (ffmpeg, Python packages, the display
driver), turns on the display interface, creates your settings file, sets up
the dock's background services, and keeps the logs across restarts (up to
50 MB) so problems can be traced afterwards. It takes a while on a Pi Zero. If it says
a reboot is needed:

```sh
sudo reboot
```

After the reboot the screen shows **STARTING UP...** and then **DOCK READY**.

## 6. Enter your Navidrome details

Log back in (`ssh yourname@musicdock.local`) and open the settings file:

```sh
nano ~/.config/musicdock/config.toml
```

Fill in the `[server]` section:

```toml
url = "http://192.168.1.50:4533"     # your Navidrome address
username = "your-navidrome-username"
password = "your-navidrome-password"
```

Leave `write = false` for now (step 9 turns it on). Save with **Ctrl-O**, then
**Enter**, and exit with **Ctrl-X**. The file is readable only by you; the
password is used to sign each request and never sent as is.

## 7. Choose your music

In Navidrome:
- **Favorites:** star songs or whole albums. Starred albums sync all their tracks.
- **Playlists:** open a playlist, click the edit (pencil) button, and put
  **`#ipod`** in its **Comment** field (the field is easy to miss). Remove the
  tag to stop syncing that playlist.

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

(Ctrl-C to stop watching.) You should see a line like
`Player ... read-only (plays only; config write = false)`.

## 9. Turn on syncing

```sh
nano ~/.config/musicdock/config.toml
```

Change `write = false` to `write = true` in the `[device]` section, save, and
dock the iPod again. This time it copies your music and playlists. Expect
**about 10 seconds per song** over USB, so a first sync of 500 songs takes about
an hour and a half. Later syncs only copy what changed.

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
  To turn that off: `auto_resync = false` in the `[device]` settings.
- Songs removed from your lists stay cached on the dock for 7 days, so
  re-adding one is instant (`removal_grace_days` in the settings).

## Optional extras

**Dark mode** (white on black, nice in a dark case): in the settings file set

```toml
[display]
theme = "dark"       # or "light" (the default)
```

**How often the dock checks for new music** (default every 5 minutes): in
the settings file set, for example,

```toml
[sync]
check_interval_minutes = 15
```

It takes effect a few seconds after you save the file (no restart needed).

**Copying while converting** (Pi Zero 2 W and other multi-core Pis): during
a big batch of new music, finished songs are handed to a docked iPod in
batches, which it copies while the rest convert - one sync for the whole run;
the screen shows **PREPARE & SYNC** until it's all done. By default a batch
is a twentieth of the run (25 to 500 songs). Set a fixed number, or 0 to
copy only once everything is converted:

```toml
[sync]
sync_batch_size = "auto"
```
Checking more often means new music reaches a docked iPod sooner; each check
that finds nothing new takes a few seconds and changes nothing.

**Safe hours** (no syncs in a daily window, e.g. so a sync doesn't hold the
iPod on the dock when you leave in the morning):

```toml
[sync]
safe_hours = "07:00-10:00"
```

Or put `SafeHours=07:00-10:00` in `DockSetup.txt` on the iPod. An iPod docked
in the window just charges and stays usable; a sync already running when it
opens stops copying and finishes (SAFE HOURS on screen). Either way, a
still-docked iPod syncs when the window ends.

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
  Database > Auto Update** (or use **Update Now** after a sync).

**Music format:** by default Rockbox gets the same files as stock iPods (MP3,
or ALAC with `convert_to = "alac"`), so one dock serves both. To send FLAC
as-is instead (lossless, no conversion, about 2-3 times larger), turn off
stock-iPod compatibility in the settings:

```toml
[device]
type = "rockbox"     # copy music as it is on the server (FLAC included)
```

The cache is then re-filled in the new format at the next check, and songs
already on the player are swapped for their FLAC versions at the next syncs.
A dock set this way doesn't sync music to stock iPods (they can't play
FLAC); set it back to `"stock_ipod"` to serve them again. If a player can't
play FLAC (many cheap MP3 players can't), keep the conversion for FLAC only:

```toml
[device]
type = "rockbox"
flac = false         # FLAC converted to convert_to; other formats as they are
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
| **NOT SYNCED · No MediaDock.txt** | Add `MediaDock.txt` to the iPod's top folder (step 1) |
| **NOT SYNCED · Not an iPod** | Only iPods are synced |
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

**On the iPod itself:** after each sync, `MediaDock README.txt` in the
iPod's top folder explains every file you can add there (`wifi.txt`,
`DockSetup.txt`, `MediaDock.txt`) and what the screens mean.

**Changing what syncs, or the server, from the iPod:** a `DockSetup.txt`
can also hold `PlaylistTag=#car`, `SyncStarred=no` (playlists only),
or `Server=` with `Username=` and `Password=`. `MediaDock README.txt` on
the iPod lists every option.

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
~/musicdock/venv/bin/python ~/musicdock/songdock/dock_prepare.py --show-plan
```

## Updating

Copy a newer package over and run the installer again. Your settings, cache
and the record of what's on your iPod are kept:

```sh
tar -xzf songdock-*.tar.gz -C ~/musicdock
cd ~/musicdock/songdock && sudo ./pi/install.sh
```

## Uninstalling

```sh
sudo systemctl disable --now musicdock-prepare.timer musicdock-boot.service \
        musicdock-resync.path musicdock-settings.path musicdock-usbport.timer \
        musicdock-wifiprefer.timer
sudo rm -rf /etc/systemd/system/musicdock-*
sudo rm /etc/udev/rules.d/99-musicdock.rules \
        /etc/systemd/journald.conf.d/50-musicdock-persistent.conf \
        /etc/tmpfiles.d/musicdock.conf \
        /etc/NetworkManager/dispatcher.d/90-musicdock-wifi \
        /usr/local/bin/musicdock-status /usr/local/bin/musicdock-players
sudo systemctl daemon-reload
rm -rf ~/musicdock ~/.config/musicdock
sudo rm -rf /var/lib/musicdock
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
