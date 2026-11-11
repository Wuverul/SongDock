"""
dock_artists.py - one listing per artist in the player's Artists menu.

Navidrome's artist field holds the whole credit, so the iPod showed every
collaboration as its own artist (2026-10-30: 557 for 1455 songs). The dock
files a song under one artist instead, and moves anyone else to the title.
Only the player's names change; Navidrome and the cached files don't.

  0. Compilations the user lists ([sync] compilation_albums) get one artist of
     their own, the song's artists going to the title: "Now That's What I Call
     Music", or "Chillhop Essentials = Chillhop Releases" to name it.
  1. A song is credited to its album's artist (user, 2026-10-30), or to the
     first of them: Caribou on a Porter Robinson album -> Porter Robinson.
     Compilations ("Various Artists") are the exception: step 2.
  2. Otherwise the first name in the credit:
       "Daft Punk feat. Panda Bear"  -> Daft Punk, "Title (feat. Panda Bear)"
       "usedcvnt w/ Akiba"           -> usedcvnt
       "Porter Robinson & Madeon"    -> Porter Robinson
       "Afroham, Pleij"              -> Afroham

A comma, "&" or "/" may be part of a name ("Above & Beyond"), and nothing from
Navidrome says so: its `artists` list often holds a whole credit as one name
("Anbuu, Needraw"), as it does for a real one ("chromo, the dreamer"). So they
separate names only with evidence: `artists` names a part separately, or a
part appears on its own in the library. Without it the credit stays whole and
is reported for review. Names in KEEP_NAMES or [sync] artist_names are never
split. feat./ft./w/ need no evidence.

Spellings that differ only in case or punctuation ("leavv"/"Leavv", "El.
Train"/"El Train") become the most common one, like the album names in
dock_prepare.unify_album_names. Others the user names in [sync] artist_aliases
(user, 2026-11-04: one artist tagged in two scripts listed twice), before any of
the above and even with primary_artist off.
"""

import re
from collections import Counter

KEEP_NAMES = ("chromo, the dreamer", "Tyler, the Creator", "Earth, Wind & Fire",
              "Crosby, Stills & Nash", "Emerson, Lake & Palmer", "Peter, Paul and Mary",
              "Now, Now")
GUESTS = re.compile(r"\s+[(\[.]?(?:feat\.?|ft\.?|featuring|w/)\s+(.+?)[)\]]?\s*$", re.I)
PARTNERS = re.compile(r"\s*[,;]\s*|\s+\u2022\s+|\s+[&/]\s+")
COMPILATION = re.compile(r"\bvarious(?: artists)?\b", re.I)
TITLE_CREDIT = re.compile(r"\b(?:feat|ft|featuring)\b", re.I)


LABEL = "label:"


def compilations(entries, labels=False):
    """{words (lower case): artist} from compilation_albums entries: "words", listed
    under those words, or "words = artist". Words match the album's name, or with
    labels=True its record label: entries "label:words = artist" (label_artist)."""
    found = {}
    for entry in entries or ():
        words, _, artist = entry.partition("=")
        words = words.strip()
        if words.casefold().startswith(LABEL) != labels:
            continue
        words = words[len(LABEL):].strip() if labels else words
        found[words.casefold()] = artist.strip() or words
    return found


def _has_words(words, text):
    return bool(text) and re.search(r"(?<!\w)" + re.escape(words) + r"(?!\w)", text, re.I)


def label_artist(rules, labels, album_artist, compilation):
    """The artist for an album that a "label:" entry chooses (`rules` from
    compilations(labels=True)), or None (user, 2026-11-10: Chillhop releases that
    aren't "Chillhop Essentials"). The words must be in its record label or its
    album artist (Chillhop's albums carry the label as album artist when the label
    tag is missing), and the album must be a compilation: flagged as one
    (`compilation`), by "Various Artists", or by the label itself, so a label's
    single-artist albums keep their artist."""
    for words, artist in rules.items():
        by_artist = _has_words(words, album_artist)
        if not (by_artist or any(_has_words(words, label) for label in labels)):
            continue
        if compilation or by_artist or COMPILATION.search(album_artist or ""):
            return artist
    return None


THE = re.compile(r"^\s*the\s+(?=\S)", re.I)


def same_artist(name):
    """What two spellings of one artist share: no case, no punctuation, no leading
    "The" (user, 2026-11-10: "Prodigy" and "The Prodigy" were two listings)."""
    return re.sub(r"\W", "", THE.sub("", name)).casefold() or name.casefold()


def aliases(entries):
    """{spelling as same_artist() sees it: name} from artist_aliases entries
    "other spelling = name"."""
    found = {}
    for entry in entries or ():
        other, _, name = entry.partition("=")
        if other.strip() and name.strip():
            found[same_artist(other.strip())] = name.strip()
    return found


# Between the names of a credit: PARTNERS, or where the guests start.
SEPARATORS = re.compile(r"(\s*[,;]\s*|\s+•\s+|\s+[&/]\s+"
                        r"|\s+[(\[]?(?:feat\.?|ft\.?|featuring|w/)\s+)", re.I)


def renamed(credit, names):
    """`credit` with each name `names` (aliases()) lists replaced: the whole
    credit, or one name in it ("X & Y", "Song feat. X"). Never part of a name."""
    if not names or not credit:
        return credit
    if same_artist(credit) in names:
        return names[same_artist(credit)]
    parts = SEPARATORS.split(credit)
    for i in range(0, len(parts), 2):               # names; separators between them
        name, close = re.match(r"(.*?)([)\]]*)$", parts[i]).groups()
        if name and same_artist(name) in names:
            parts[i] = names[same_artist(name)] + close
    return "".join(parts)


def apply_aliases(songs, entries):
    """Rename the artists [sync] artist_aliases lists, in each song's artist and
    album artist (and Navidrome's lists of them, the evidence tidy() reads).
    Changes the song records in place. Returns how many changed."""
    names = aliases(entries)
    if not names:
        return 0
    changed = 0
    for song in songs:
        before = [song.get(f) for f in ("artist", "displayAlbumArtist", "albumArtist")]
        for field in ("artist", "displayAlbumArtist", "albumArtist"):
            if song.get(field):
                song[field] = renamed(song[field], names)
        for field in ("artists", "albumArtists"):
            if song.get(field):
                song[field] = [{**a, "name": renamed(a.get("name", ""), names)}
                               for a in song[field]]
        changed += before != [song.get(f) for f in ("artist", "displayAlbumArtist",
                                                     "albumArtist")]
    return changed


def without_guests(credit):
    """(credit up to any feat./ft./w/, [the guests named after it])."""
    name, found = credit.strip(), GUESTS.search(credit.strip())
    if not found:
        return name, []
    return name[:found.start()].strip(), [found.group(1).strip()]


def names_in(name, keep=KEEP_NAMES, is_list=True):
    """The names in a credit (no feat. part), main one first."""
    folded = name.casefold()
    for kept in keep:
        kept = kept.casefold()
        if folded == kept:
            return [name]
        if folded.startswith(kept) and PARTNERS.match(name[len(kept):]):
            rest = PARTNERS.sub("", name[len(kept):], count=1)
            return [name[:len(kept)]] + [p for p in PARTNERS.split(rest) if p]
    return ([p for p in PARTNERS.split(name) if p] or [name]) if is_list else [name]


def split_credit(credit, keep=KEEP_NAMES, album_main=None, is_list=True):
    """(main artist, the others or None) for an artist credit.

    album_main: the artist of the song's album, if it isn't a compilation.
    is_list: a comma, "&" or "/" here separates names (see the top).
    """
    name, guests = without_guests(credit)
    names = names_in(name, keep, is_list) if name else [credit.strip()]
    if album_main:
        main = same_artist(album_main)
        if same_artist(name) == main:           # the album's own credit, split or not
            names = [album_main]
        return album_main, ", ".join([n for n in names if same_artist(n) != main]
                                     + guests) or None
    return names[0], ", ".join(names[1:] + guests) or None


def title_with_guests(title, guests):
    """The title, crediting whoever it doesn't already name ("(X remix)")."""
    if not guests or not title or TITLE_CREDIT.search(title):
        return title
    named = same_artist(title)
    missing = [g for g in guests.split(", ") if same_artist(g) not in named]
    return f"{title} (feat. {', '.join(missing)})" if missing else title


def _album_artist(song):
    """The album's artist without any feat. part ("X feat. Y" is X's album)."""
    return without_guests(song.get("displayAlbumArtist") or song.get("albumArtist") or "")[0]


def tidy(songs, extra_names=(), unproven=None, compilation_albums=(), albums=None):
    """Give each song its one artist and credit the others in its title.
    Changes the song records in place. Returns how many changed. Pass a set
    as `unproven` to get the credits left whole for lack of evidence.
    compilation_albums: [sync] compilation_albums from the config. albums: {album id:
    artist} chosen by its "label:" entries (dock_prepare.compilation_labels)."""
    keep = (*KEEP_NAMES, *extra_names)
    chosen = compilations(compilation_albums)
    kept = {k.casefold() for k in keep}
    credits = {s["id"]: without_guests(s["artist"])[0] for s in songs if s.get("artist")}
    alone = {same_artist(c) for c in credits.values() if not PARTNERS.search(c)}
    alone |= {same_artist(a) for a in map(_album_artist, songs) if a and not PARTNERS.search(a)}

    def is_list(listed, credit):
        names = {same_artist(a.get("name", "")) for a in listed or []}
        parts = [p for p in PARTNERS.split(credit) if p]
        return any(same_artist(p) in alone | names for p in parts) or \
            any(";" in a.get("name", "") for a in listed or [])

    def album_main(song):
        if albums and song.get("albumId") in albums:
            return albums[song["albumId"]]
        name = (song.get("album") or "").casefold()
        for words, artist in chosen.items():
            if words in name:
                return artist
        album = _album_artist(song)
        if not album or COMPILATION.search(album):
            return None
        return names_in(album, keep, is_list(song.get("albumArtists"), album))[0]

    spellings, split = Counter(), {}
    for song in songs:
        credit = credits.get(song["id"])
        if credit is None:
            continue
        main, listing = album_main(song), is_list(song.get("artists"), credit)
        split[song["id"]] = split_credit(song["artist"], keep, main, listing)
        if not listing and not main and unproven is not None and PARTNERS.search(credit) \
                and credit.casefold() not in kept:
            unproven.add(credit)
        spellings[split[song["id"]][0]] += 1
    best = {}            # the most common spelling; one with "The" over one without
    for name, count in sorted(spellings.items(),
                              key=lambda kv: (not THE.match(kv[0]), -kv[1], kv[0])):
        best.setdefault(same_artist(name), name)
    changed = 0
    for song in songs:
        if song["id"] not in split:
            continue
        main, guests = split[song["id"]]
        artist = best[same_artist(main)]
        title = title_with_guests(song.get("title"), guests)
        album_artist = song.get("displayAlbumArtist") or song.get("albumArtist")
        new_album_artist = _album_artist(song) if album_artist else album_artist
        if (artist, title, new_album_artist) != (song["artist"], song.get("title"),
                                                 album_artist):
            song["artist"], song["title"] = artist, title
            if new_album_artist != album_artist:
                song["displayAlbumArtist"] = new_album_artist  # its guests are in titles
            changed += 1
    return changed


def note(names, shown=20):
    """A line for MediaDock.txt and musicdock-status, or None."""
    if not names:
        return None
    names = sorted(names, key=str.casefold)
    more = f" and {len(names) - shown} more" if len(names) > shown else ""
    return ("Listed as one artist, since nothing shows they're separate: "
            + "; ".join(names[:shown]) + more)


def group_singles(songs, song_counts, minimum=4):
    """An artist with `minimum` or more one-track albums gets them as one album,
    "<Artist> Singles" (user, 2026-10-31). song_counts: {album id: tracks the album
    really has}, so one song kept from a full album doesn't count. A song's real
    album stays in "folder_album", for players that keep art per folder. Returns
    how many songs moved. Only names change, so falling below `minimum` undoes it."""
    if not minimum:
        return 0
    in_plan = {}
    for s in songs:
        in_plan.setdefault(s.get("albumId"), []).append(s)
    by_artist = {}
    for album_id, tracks in in_plan.items():
        if album_id and len(tracks) == 1 and song_counts.get(album_id) == 1:
            by_artist.setdefault(tracks[0].get("artist"), []).append(tracks[0])
    moved = 0
    for artist, singles in by_artist.items():
        if artist and len(singles) >= minimum:
            for s in singles:
                s["folder_album"] = s.get("album")
                s["album"] = f"{artist} Singles"
                s["displayAlbumArtist"] = artist      # one album, not one per credit
                moved += 1
    return moved


def playlist_singles(songs, playlists):
    """Songs of a playlist tagged with [sync] singles_tag (e.g. "#singles") that are
    the only song of their album in the plan go into one album named after the
    playlist, "<Playlist> Singles", album artist "Various Artists" (user, 2026-11-10:
    loose songs from many albums, one album each, crowded the Albums menu). Once a
    second song of that album is in the plan, the song is back on its own album. A
    song in two such playlists goes under the first by name; this comes after (and
    wins over) "<Artist> Singles". Names only; "folder_album" keeps the real album.
    Returns how many songs moved."""
    lists = sorted((p for p in playlists if p.get("singles")), key=lambda p: p["name"].casefold())
    if not lists:
        return 0
    per_album = Counter(s.get("albumId") for s in songs if s.get("albumId"))
    by_id = {s["id"]: s for s in songs}
    moved = set()
    for pl in lists:
        for sid in pl.get("song_ids", ()):
            s = by_id.get(sid)
            if not s or sid in moved or per_album.get(s.get("albumId"), 0) != 1:
                continue
            s["folder_album"] = s.get("folder_album") or s.get("album")
            s["album"] = f"{pl['name']} Singles"
            s["displayAlbumArtist"] = "Various Artists"
            moved.add(sid)
    return len(moved)
