"""
dock_artists.py - one listing per artist in the player's Artists menu.

Navidrome's artist field holds the whole credit, so the iPod showed every
collaboration as its own artist (2026-10-30: 557 for 1455 songs). The dock
files a song under its main artist instead, and moves any guests to the
title. Only the player's names change; Navidrome and the cached files don't.

  "Daft Punk feat. Panda Bear"  -> Daft Punk, "Title (feat. Panda Bear)"
  "usedcvnt w/ Akiba"           -> usedcvnt, "Title (feat. Akiba)"
  "No Mana & Chipzel" on an album by No Mana
                                -> No Mana,  "Title (feat. Chipzel)"
  "Porter Robinson & Madeon", also a release's album artist
                                -> as it is: equal partners
  "Afroham, Pleij"              -> Afroham,  "Title (feat. Pleij)"

A comma may be part of a name, and nothing from Navidrome says so: its
`artists` list often holds a whole credit as one name ("Anbuu, Needraw"), as
it does for a real one ("chromo, the dreamer"). So a comma counts as a list
only with evidence: `artists` names a part separately, or a part appears on
its own in the library. Without it the credit stays whole and is reported
for review. Names in KEEP_NAMES or [sync] artist_names are never split.

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
LIST_SEPARATOR = re.compile(r"\s*[,;]\s*|\s+•\s+")
PARTNERS = re.compile(r"\s*[,;]\s*|\s+•\s+|\s+[&/]\s+")
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


def split_credit(credit, keep=KEEP_NAMES, album_artist=None, duo=False, is_list=True):
    """(main artist, guests or None) for an artist credit.

    album_artist: the song's album artist; a credit naming it files under it.
    duo: the credit is itself some release's album artist, so its names are
    equal partners. is_list: a comma here separates names (see the top).
    """
    name, guests = without_guests(credit)
    folded = name.casefold()
    for kept in keep:
        kept = kept.casefold()
        if folded == kept:
            return name, ", ".join(guests) or None
        if folded.startswith(kept) and LIST_SEPARATOR.match(name[len(kept):]):
            rest = LIST_SEPARATOR.sub("", name[len(kept):], count=1)
            return name[:len(kept)], ", ".join([rest] + guests)
    if duo:
        return name, ", ".join(guests) or None
    if album_artist and PARTNERS.search(name):
        names = PARTNERS.split(name)
        for i, part in enumerate(names):
            if part and same_artist(part) == same_artist(album_artist):
                others = [n for j, n in enumerate(names) if j != i and n]
                return part, ", ".join(others + guests) or None
    parts = LIST_SEPARATOR.split(name, maxsplit=1)
    if is_list and len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], ", ".join([parts[1]] + guests)
    return (name or credit.strip()), ", ".join(guests) or None


def title_with_guests(title, guests):
    """The title, crediting guests unless it already names someone."""
    if not guests or not title or TITLE_CREDIT.search(title):
        return title
    return f"{title} (feat. {guests})"


def _album_artist(song):
    return song.get("displayAlbumArtist") or song.get("albumArtist") or ""


def tidy(songs, extra_names=(), unproven=None):
    """Give each song its main artist and credit guests in its title.
    Changes the song records in place. Returns how many changed. Pass a set
    as `unproven` to get the comma credits left whole for lack of evidence."""
    keep = (*KEEP_NAMES, *extra_names)
    credits = {s["id"]: without_guests(s["artist"])[0] for s in songs if s.get("artist")}
    alone = {same_artist(c) for c in credits.values() if not PARTNERS.search(c)}
    alone |= {same_artist(a) for a in map(_album_artist, songs) if a and not PARTNERS.search(a)}
    album_artists = {same_artist(a) for a in map(_album_artist, songs) if a}
    kept = [k.casefold() for k in keep]

    def is_list(song, credit):
        parts = [p for p in LIST_SEPARATOR.split(credit) if p]
        listed = [a.get("name", "") for a in song.get("artists") or []]
        named = {same_artist(entry) for entry in listed}
        return any(same_artist(p) in alone | named for p in parts) or \
            any(";" in entry for entry in listed)

    spellings, split = Counter(), {}
    for song in songs:
        credit = credits.get(song["id"])
        if credit is None:
            continue
        listing = is_list(song, credit)
        split[song["id"]] = split_credit(song["artist"], keep, _album_artist(song),
                                         same_artist(credit) in album_artists, listing)
        if not listing and unproven is not None and LIST_SEPARATOR.search(credit) \
                and credit.casefold() not in kept \
                and split[song["id"]][0] == credit:
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
