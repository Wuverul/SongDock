"""
dock_artists.py - one listing per artist in the player's Artists menu.

Navidrome's artist field holds the whole credit, so the iPod showed every
collaboration as its own artist (2026-10-30: 557 for 1455 songs). The dock
files a song under one artist instead, and moves anyone else to the title.
Only the player's names change; Navidrome and the cached files don't.

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
dock_prepare.unify_album_names.
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


def same_artist(name):
    """What two spellings of one artist share: no case, no punctuation."""
    return re.sub(r"\W", "", name).casefold() or name.casefold()


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
    return song.get("displayAlbumArtist") or song.get("albumArtist") or ""


def tidy(songs, extra_names=(), unproven=None):
    """Give each song its one artist and credit the others in its title.
    Changes the song records in place. Returns how many changed. Pass a set
    as `unproven` to get the credits left whole for lack of evidence."""
    keep = (*KEEP_NAMES, *extra_names)
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
    best = {}
    for name, count in sorted(spellings.items(), key=lambda kv: (-kv[1], kv[0])):
        best.setdefault(same_artist(name), name)
    changed = 0
    for song in songs:
        if song["id"] not in split:
            continue
        main, guests = split[song["id"]]
        artist = best[same_artist(main)]
        title = title_with_guests(song.get("title"), guests)
        if artist != song["artist"] or title != song.get("title"):
            song["artist"], song["title"] = artist, title
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
