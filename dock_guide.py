"""
dock_guide.py - the user manual kept on every synced iPod.

`MediaDock README.txt` in the iPod's top folder explains the files a person
can add or edit there (MediaDock.txt, wifi.txt, DockSetup.txt), how music is
chosen in Navidrome, and what the screens mean. It's built from the dock's
own settings (playlist tag, Favorites name, ...), so it can't drift from
them, and rewritten on a write sync only when its text changes.
"""

import os

import dock_setup

GUIDE_FILE = dock_setup.GUIDE_FILE


def guide_text(cfg, marker="MediaDock.txt", wifi="wifi.txt",
               setup=dock_setup.SETUP_FILE, rockbox=False, kind=None):
    kind = kind or ("rockbox" if rockbox else "stock")
    sync = cfg.get("sync", {})
    tag = sync.get("playlist_tag", "")
    lossless = sync.get("lossless_tag", "#lossless")
    favorites = sync.get("favorites_playlist", "")
    grace = sync.get("removal_grace_days", 7)
    targets = sync.get("targets", [])
    lists = []
    if "starred" in targets:
        lists.append("songs and albums you star (heart) in Navidrome"
                     + (f",\n    gathered on the iPod in a playlist called \"{favorites}\""
                        if favorites else ""))
    if tag:
        lists.append(f"every Navidrome playlist whose comment contains {tag}")
    lists += [f"the playlist \"{t.split(':', 1)[1]}\"" for t in targets
              if t.startswith("playlist:")]
    chosen = "\n".join(f"  - {item}" for item in lists) or "  - (nothing is set up yet)"
    removal = (f"its copy on the dock is kept {grace} days in case you add it back."
               if grace else "its copy on the dock is deleted right away.")
    tag_how = (f"""
  To add a playlist, open it in Navidrome's web page, edit it, and put
  {tag} in its comment (the "Comment" field is easy to miss). Removing
  {tag} takes the playlist off the iPod at the next sync.
""" if tag else "")

    if kind == "generic":
        hands_off = """This is a simple music player that plays from folders. The dock's music
goes in the MediaDock folder (one folder per artist and album, with
cover.jpg). Music you put anywhere else is left alone; don't add or change
files inside MediaDock. This player keeps no play history the dock can
read, so plays aren't sent to Navidrome ("No play count - unsupported" on
the dock's screen), and no playlists are made: songs from your tagged
playlists are simply part of the music library. To get new music, unplug
the player and plug it back in (only iPods are synced again by themselves
while docked)."""
    elif kind == "rockbox":
        hands_off = """This player runs Rockbox. The dock's music goes in the MediaDock folder
(one folder per artist and album, with cover.jpg), its playlists in the
Playlists folder. Music you put anywhere else is left alone; don't add or
change files inside MediaDock. To browse the dock's music by artist and
album, turn on Settings > General Settings > Database > Auto Update (or
run Update Now after a sync). Plays are read from Rockbox's .scrobbler.log
(Settings > Playback Settings > Last.fm Log: on); skipped songs aren't
counted."""
    else:
        hands_off = """Don't add music with Finder, iTunes or the Apple Music app: they replace
the iPod's music list, and the dock loses track of everything it added.
Don't change the hidden iPod_Control folder."""
    lossless_how = (f"""Songs in a playlist whose Navidrome comment has {lossless} are lossless when
the server has a lossless copy (ALAC on stock iPods, about 3 times the
space; FLAC on Rockbox). The song replaces its MP3, so it's never on the
iPod twice. The playlist needn't be synced; only songs that are get
this. This needs an iPod the dock has synced (a Rockbox player on a
Rockbox-only dock); music players on the same dock get these files too,
and basic ones may not play them.""" if lossless
                    else "Songs are not made lossless by playlist on this dock.")
    return f"""SONGDOCK - HOW TO USE AND RECONFIGURE IT FROM THIS IPOD
==========================================================

The SongDock keeps this iPod in step with a Navidrome music server: when
you dock it, it sends what you played to Navidrome, copies new music onto
the iPod, removes music you took off your lists, and charges the iPod.
The dock rewrites this file after a sync; editing it changes nothing.

To edit the files below, connect the iPod to a computer in disk mode (on a
Mac it appears in Finder; on Windows in File Explorer) and use any plain
text editor (TextEdit: Format > Make Plain Text; Windows: Notepad). The
files go in the iPod's top folder, next to this one. Their names can be
any mix of upper and lower case.

{hands_off}


WHAT ENDS UP ON THE IPOD
------------------------
Chosen in Navidrome, not on the iPod:
{chosen}
{tag_how}
{lossless_how}
Each song lists under one artist: its album's artist, or on a Various Artists
album the first name in its credit. A song by "A feat. B", "A, B" or "A & B"
lists under A and is titled "Song (feat. B)". A comma, & or / stays part of
the name if nothing shows two people (MediaDock.txt lists these).
This only changes the names on the iPod, not in Navidrome.
Compilations can list as one artist instead: see CompilationAlbums below.
When a song leaves all of those, the next sync removes it from the iPod;
{removal}


{marker}  (already here: this iPod is synced)
-------------
Its presence means "sync this iPod". Without it, the dock reads nothing
from the iPod and changes nothing on it.
  - To stop syncing this iPod: delete {marker}.
  - To start again: create an empty file with that name.
After each sync the dock fills it with a list of the iPods it knows (by
the name you gave each iPod, with its model and the end of its ID). Use
those names in {setup}.


{wifi}  (move the dock to another Wi-Fi network)
--------
Create it with these lines, then dock the iPod:
  SSID=Name of the network
  Password=the network password
  Hidden=yes            (only if the network doesn't broadcast its name)
  - SSID is required (up to 32 characters). Leave out Password for an open
    network; otherwise it must be 8 to 63 characters.
  - "SSID: name" works as well as "SSID=name".
After the music is synced the dock tries the network:
  - Joined: the screen says WI-FI CHANGED and shows the dock's new IP
    address, and {wifi} is deleted. The old network stays saved as a
    fallback.
  - Not joined: the dock goes back to its previous network, the screen
    says WI-FI UNCHANGED with the reason, and {wifi} stays for you to fix.
The password is stored only on the dock, readable by its administrator,
and never written to its log.


{setup}  (change what syncs, the server, or retire an old iPod)
-------------
Create it with one or more of the lines below, then dock the iPod. Lines
starting with # are ignored.

Which music syncs:
  PlaylistTag=#car       sync the playlists whose Navidrome comment has
                         #car (instead of {tag or "no tag"}); PlaylistTag=none stops
                         syncing tagged playlists
  CompilationAlbums=Now That's What I Call Music; Monthly Mix = My Mixes
                         list albums whose name contains these words as one
                         artist, the songs' artists going to their titles.
                         "= name" chooses the artist's name. Separate them
                         with ;. This replaces the whole list; =none clears it
  SyncStarred=no         stop syncing starred songs, e.g. to keep a big
                         library starred in a desktop app while the iPod
                         gets playlists only; SyncStarred=yes turns it back
                         on (SyncFavorites= means the same)
  Songs that no longer belong to anything synced leave the iPod at the
  next sync. At least one of the two must stay on.

Music format (stock iPods and Rockbox players):
  LosslessTag=none       ignore {lossless or "the lossless tag"} on this dock, e.g. one serving a
                         small iPod; LosslessTag=#hifi uses another tag
  StockCompatible=no     for a dock that only serves Rockbox players: copy
                         music as it is on the server, FLAC included
                         (lossless, about 2-3 times the space). Refused
                         while any stock iPod still syncs - the one docked
                         included; retire them first (Forget=, which can go
                         in the same file)
  StockCompatible=yes    convert music for stock iPods again (the default).
                         Works from a stock iPod docked while the dock is
                         Rockbox-only: the dock reads its plays, switches
                         back, and fills it once the music is converted.
  FLAC=no                on a Rockbox-only dock, still convert FLAC (to
                         the same format as for stock iPods), for players
                         that can't play it; other formats stay as they
                         are. FLAC=yes copies it as-is again (the default)
  Either way, the dock's copy of the music is redone in the new format and
  songs already on players are swapped at the next syncs.

A daily window with no syncs, e.g. to grab the iPod for a morning commute:
  SafeHours=07:00-10:00  (local time; the default; SafeHours=off for none)
  An iPod docked then just charges; a sync running when the window opens
  finishes early. Either way it syncs afterwards if it's still docked.

The Navidrome server and the account the dock signs in with:
  Server=https://music.example.com
  Username=your-name
  Password=your-password
  - Several addresses for the same server (e.g. one at home and one for
    elsewhere) go on one line, comma-separated, in the order to try:
      Server=http://192.168.1.50:4533, https://music.example.com
    The dock uses the first one that answers.
  - A new Server= always needs Username= and Password= with it, so the
    dock never signs in to a new address with the old details.
  - Username= and Password= also work on their own (same server).
  - The dock signs in with them first; if that fails, nothing changes.
  - If the new server doesn't have this iPod's music (another library,
    not just a new address for the same one), add NewLibrary=yes. The
    iPod's music is then replaced at the next syncs, and the change waits
    until plays from this iPod have been sent to the old server.
  - The password is saved only on the dock (readable by its owner) and is
    deleted from the iPod with this file. If the change can't be made,
    the file stays - delete it yourself if you give up.

Retiring an iPod you've replaced:
  The dock keeps a copy of each song until every iPod it syncs has it,
  so an old, smaller iPod you no longer use can stop a bigger new one
  from filling up. Retiring the old one fixes that:
  Forget=Jo's iPod (nano)    retire that iPod
  Forget=all others          retire every iPod except this one
  Keep=Jo's iPod (nano)      bring a retired iPod back
  - Use the names listed in {marker}. The model in brackets, e.g.
    (nano) or (classic), is only needed when two iPods share a name; the
    last characters of the ID, e.g. (AB12), work too.
  - You can't retire the iPod you're docking.
  Retiring changes nothing on any iPod. A retired iPod's plays are still
  sent to Navidrome, and docking it again brings it back by itself.

After the music (and any {wifi}) is done:
  - Done: the screen says DOCK UPDATED, {setup} is deleted, and the
    result is noted at the end of {marker}. Music changes show up from
    the next sync (the dock re-syncs a docked iPod by itself).
  - Not done (an unknown setting, a sign-in the server refuses, no iPod
    by that name, ...): nothing at all changes, the screen says SETUP NOT
    USED with the reason, and {setup} stays for you to fix.


WHAT THE SCREEN MEANS
---------------------
  SYNCING            Do not unplug the iPod.
  PREPARE & SYNC     Do not unplug: new music is still being converted
                     while the first of it copies to the iPod.
  SAFE TO REMOVE     Done: the iPod can be unplugged (or left to charge).
  SYNC FAILED        Safe to unplug; nothing was lost. The reason is shown,
                     and the next dock tries again.
  NOT SYNCED         The iPod has no {marker} (or isn't an iPod).
  CONNECTING         New music is ready for a docked iPod; it syncs again
                     by itself. Do not unplug.
  DOCK READY         Idle. Shows what the next sync will add and remove.
                     "No room to convert 7": 7 songs keep their format
                     until the player has space.
  WI-FI CHANGED / WI-FI UNCHANGED, DOCK UPDATED / SETUP NOT USED
                     Results of {wifi} and {setup} (see above).
  USB NOT WORKING    A plug-in failed and the dock stopped noticing
                     players. Unplug the dock's power briefly to restart it.
  RESTORING          Filling a new or wiped iPod, which takes several
                     syncs: songs on the iPod so far, and the time left.
                     Safe to unplug between syncs; it carries on when
                     docked.
  SAFE HOURS         No syncs right now (SafeHours=, see above). Safe to
                     unplug; shows when syncing resumes.
Plays are never lost: if Navidrome can't be reached, the dock keeps them
and sends them later.


EVERYTHING ELSE
---------------
Other settings (the music format, the cache size, the dock's screen) live
in the dock's own configuration file, which its owner edits over the
network. They can't be changed from the iPod.
"""


def write_guide(root, cfg, rockbox=False, kind=None):
    """Write the guide to the player's top folder if its text changed.
    Returns True if it was (re)written."""
    path = os.path.join(root, GUIDE_FILE)
    text = guide_text(cfg, rockbox=rockbox, kind=kind)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    return True
