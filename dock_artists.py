"""
dock_artists.py - one listing per artist in the player's Artists menu.

Navidrome's artist field holds the whole credit, so the iPod showed every
collaboration as its own artist (2026-10-30: 557 for 1455 songs). The dock
files a song under its main artist instead, and moves any guests to the
title. Only the player's names change; Navidrome and the cached files don't.

  "Daft Punk feat. Panda Bear"  -> Daft Punk, "Title (feat. Panda Bear)"
  "usedcvnt w/ Akiba"           -> usedcvnt, "Title (feat. Akiba)"
  "Afroham, Pleij"              -> Afroham,  "Title (feat. Pleij)"
  "Porter Robinson & Madeon"    -> as it is: "&" and "/" are equal partners

Commas count as main artist first (user, 2026-10-30), so a real name with a
comma has to be listed: KEEP_NAMES, plus [sync] artist_names in the config.
Navidrome's `artists` list can't decide this: it often holds a whole credit
as one name ("Anbuu, Needraw"). Spellings that differ only in case or
punctuation ("leavv"/"Leavv", "El. Train"/"El Train") become the most common
one, like the album names in dock_prepare.unify_album_names.
"""

import re
from collections import Counter

KEEP_NAMES = ("chromo, the dreamer", "Tyler, the Creator", "Earth, Wind & Fire",
              "Crosby, Stills & Nash", "Emerson, Lake & Palmer", "Peter, Paul and Mary",
              "Now, Now")
GUESTS = re.compile(r"\s+[(\[]?(?:feat\.?|ft\.?|featuring|w/)\s+(.+?)[)\]]?\s*$", re.I)
LIST_SEPARATOR = re.compile(r"\s*[,;]\s*|\s+•\s+")
TITLE_CREDIT = re.compile(r"\b(?:feat|ft|featuring)\b", re.I)


def split_credit(credit, keep=KEEP_NAMES):
    """(main artist, guests or None) for an artist credit."""
    name, guests = credit.strip(), []
    found = GUESTS.search(name)
    if found:
        name, guests = name[:found.start()].strip(), [found.group(1).strip()]
    folded = name.casefold()
    for kept in keep:
        kept = kept.casefold()
        if folded == kept:
            return name, ", ".join(guests) or None
        if folded.startswith(kept) and LIST_SEPARATOR.match(name[len(kept):]):
            rest = LIST_SEPARATOR.sub("", name[len(kept):], count=1)
            return name[:len(kept)], ", ".join([rest] + guests)
    parts = LIST_SEPARATOR.split(name, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], ", ".join([parts[1]] + guests)
    return (name or credit.strip()), ", ".join(guests) or None


def same_artist(name):
    """What two spellings of one artist share: no case, no punctuation."""
    return re.sub(r"\W", "", name).casefold() or name.casefold()


def title_with_guests(title, guests):
    """The title, crediting guests unless it already names someone."""
    if not guests or not title or TITLE_CREDIT.search(title):
        return title
    return f"{title} (feat. {guests})"


def tidy(songs, extra_names=()):
    """Give each song its main artist and credit guests in its title.
    Changes the song records in place. Returns how many changed."""
    keep = (*KEEP_NAMES, *extra_names)
    spellings, split = Counter(), {}
    for song in songs:
        if song.get("artist"):
            split[song["id"]] = split_credit(song["artist"], keep)
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
