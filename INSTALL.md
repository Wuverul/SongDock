# Setting up a Music Dock

About an hour of hands-on time, plus waiting for software to install and
music to convert. You'll type a few commands into a terminal; each step shows
exactly what to type.

## What you need

**Hardware** (the tested setup):
- **Raspberry Pi Zero 2 W** (or Zero W) **with a soldered pin header**
- **Waveshare 2.13" e-Paper HAT, V4** (250x122, black and white)
- **microSD card**, 32 GB or larger (64 GB recommended: the dock caches your music)
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
- The Music Dock package: `music-dock-<date>.tar.gz`

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
   - **OS:** Raspberry Pi OS (other) > **Raspberry Pi OS Lite (32-bit)**
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
folder holding `music-dock-<date>.tar.gz` and run (use your username, and your
hostname if you picked a different one):

```sh
scp music-dock-*.tar.gz yourname@musicdock.local:
ssh yourname@musicdock.local
```

The second command logs you into the Pi (type `yes` the first time, then your
Pi password). Everything below is typed **on the Pi**:

```sh
mkdir -p ~/musicdock
tar -xzf music-dock-*.tar.gz -C ~/musicdock
cd ~/musicdock/music-dock
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

Within about 5 minutes the dock notices, then downloads and converts the music
(**PREPARING** on the screen). On a Pi Zero W, converting a FLAC song takes a
bit less time than the song lasts (a Zero 2 W is faster); songs that are
already MP3 just download. A big first batch can take hours. The iPod doesn't
need to be docked for this.

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

**Dark mode** (white on black, nice in a dark case): in the settings file add

```toml
[display]
invert = true
```

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
~/musicdock/venv/bin/python ~/musicdock/music-dock/dock_prepare.py --show-plan
```

## Updating

Copy a newer package over and run the installer again. Your settings, cache
and the record of what's on your iPod are kept:

```sh
tar -xzf music-dock-*.tar.gz -C ~/musicdock
cd ~/musicdock/music-dock && sudo ./pi/install.sh
```

## Uninstalling

```sh
sudo systemctl disable --now musicdock-prepare.timer musicdock-boot.service \
        musicdock-resync.path
sudo rm /etc/systemd/system/musicdock-* /etc/udev/rules.d/99-musicdock.rules \
        /etc/systemd/journald.conf.d/50-musicdock-persistent.conf \
        /etc/tmpfiles.d/musicdock.conf \
        /etc/NetworkManager/dispatcher.d/90-musicdock-wifi \
        /usr/local/bin/musicdock-status
sudo systemctl daemon-reload
rm -rf ~/musicdock ~/.config/musicdock
sudo rm -rf /var/lib/musicdock
```

The music already on your iPod stays there.
