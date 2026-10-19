"""
dock_genres.py - a short list of genres for the player's Genres menu.

Navidrome gives each song several genre tags ("dance", "electronic",
"rave"). The iPod showed each combination as its own genre, so nearly
every genre held one album (2026-10-19: 56 combinations across 78 albums).
The dock instead files every song under one group from a short list the
user controls ([genres] in config.toml). Only the player's database
changes; Navidrome and the cached files keep their tags.

How a song's group is chosen:
  * The groups are checked in order; the first group with a keyword that
    matches any of the song's tags wins. The order is the priority, so the
    result never shifts as the library grows (user, 2026-10-19).
  * A keyword matches whole words anywhere in a tag, ignoring case and
    punctuation: "rock" matches "alternative rock", "drum and bass" matches
    "dancefloor drum & bass", but "pop" doesn't match "electropop".
  * "release:<type>" matches the album's release type in Navidrome
    (e.g. "release:live"), for things genre tags don't say. "album:<words>"
    matches words in the album's name (e.g. "album:chillhop" files every
    "Chillhop Essentials" volume under Lofi / Chill, whatever its tags).
  * No match: the song's tag that the most synced albums carry (ties: the
    song's first tag), or "Other" if it has no tags. The list always comes
    first; counting only names what the list doesn't cover (user,
    2026-10-19). `unmatched = "<name>"` puts all of these under one name.
Then every song of an album gets the group most of its songs got (ties go
to the group listed first; a matched group beats `unmatched`), so an album
never splits across genres.
"""

import re
from collections import Counter

# The default list (config.example.toml carries the same; a test keeps them
# equal). Order is priority, so specific groups come before broad ones:
# "indie rock" before "rock", "lo-fi hip hop" before "hip hop". The user's
# own groups (Chiptune ... Indie Pop) lead; the general categories after
# them (from Discogs' and iTunes' top-level genres, 2026-10-19) catch music
# this library doesn't have yet, e.g. classical or country.
DEFAULT_GROUPS = [
    # What the record is, whatever its style.
    {"name": "Live Recordings", "match": ["release:live", "live"]},
    {"name": "Soundtracks", "match": ["release:soundtrack", "soundtrack", "score", "ost",
                                      "video game music", "musical", "show tunes"]},
    {"name": "Holiday", "match": ["christmas", "holiday", "xmas"]},
    {"name": "Children's", "match": ["children", "kids", "nursery", "lullaby"]},
    {"name": "Spoken Word", "match": ["spoken word", "comedy", "audiobook", "podcast",
                                      "poetry", "speech", "non music"]},
    # The user's groups.
    {"name": "Chiptune", "match": ["chiptune", "chipstep", "bitpop", "8 bit"]},
    {"name": "Breakcore", "match": ["breakcore", "dariacore", "digital hardcore",
                                    "speedcore", "mashcore"]},
    {"name": "DnB", "match": ["drum and bass", "drum n bass", "dnb", "jungle",
                              "liquid funk", "neurofunk"]},
    {"name": "Lofi / Chill", "match": ["album:chillhop", "album:lofi", "album:lo fi",
                                       "lo fi", "lofi", "chill", "chillout", "chillhop",
                                       "trip hop", "new age"]},
    {"name": "Hip-Hop", "match": ["hip hop", "hiphop", "boom bap", "rap", "trap", "grime",
                                  "drill"]},
    {"name": "Indie Rock", "match": ["indie rock"]},
    {"name": "Indie Pop", "match": ["indie pop", "dream pop", "bedroom pop",
                                    "indietronica", "art pop", "alternative pop"]},
    # General categories.
    {"name": "Metal", "match": ["metal", "metalcore", "deathcore", "grindcore", "djent"]},
    {"name": "Country", "match": ["country", "bluegrass", "americana", "honky tonk"]},
    {"name": "Folk", "match": ["folk", "singer songwriter", "acoustic"]},
    {"name": "Rock", "match": ["rock", "punk", "grunge", "emo", "post hardcore",
                               "screamo", "shoegaze"]},
    {"name": "Electronic", "match": ["electronic", "electronica", "edm", "house", "techno",
                                     "trance", "dubstep", "brostep", "riddim", "electro",
                                     "complextro", "breaks", "dance", "rave", "eurodance",
                                     "hardcore", "footwork", "synthwave", "future bass",
                                     "idm"]},
    {"name": "Pop", "match": ["pop", "electropop", "hyperpop"]},
    {"name": "R&B / Soul", "match": ["r&b", "rnb", "rhythm and blues", "soul", "funk",
                                     "motown", "disco"]},
    {"name": "Jazz", "match": ["jazz", "bebop", "swing", "big band", "bossa nova"]},
    {"name": "Blues", "match": ["blues"]},
    {"name": "Classical", "match": ["classical", "orchestral", "symphony", "opera",
                                    "baroque", "chamber music", "choral", "neoclassical"]},
    {"name": "Reggae", "match": ["reggae", "ska", "dub", "dancehall", "rocksteady"]},
    {"name": "Latin", "match": ["latin", "reggaeton", "salsa", "bachata", "cumbia",
                                "samba", "tango"]},
    {"name": "Gospel & Christian", "match": ["gospel", "christian", "worship"]},
    {"name": "World", "match": ["world", "afrobeat", "afrobeats", "celtic", "flamenco"]},
]
# Songs no group matches take their tag most common across the library (by
# albums); "Other" only if they have no tags. A fixed name can be set instead.
DEFAULT_UNMATCHED = ""
NO_TAGS = "Other"
RELEASE = "release:"
ALBUM = "album:"
SMALL_WORDS = {"and", "n", "of", "the", "a"}


def normalize(text):
    """Lower case, "&" as "and", punctuation as spaces: "Drum & Bass" ->
    " drum and bass " (padded, so whole words can be found with `in`)."""
    text = (text or "").lower().replace("&", " and ")
    return " " + " ".join(re.findall(r"[a-z0-9]+", text)) + " "


def song_tags(song):
    """A song's genre tags, in Navidrome's order (OpenSubsonic `genres`,
    else the single `genre` field, which may hold "a;b")."""
    names = [g.get("name") for g in song.get("genres") or [] if isinstance(g, dict)]
    if not names and song.get("genre"):
        names = song["genre"].split(";")
    return [n.strip() for n in names if n and n.strip()]


def release_types(album):
    """An album's release types, lower case. Navidrome sends a list, or for
    some albums one string such as "album;compilation"."""
    types = album.get("releaseTypes") or []
    if isinstance(types, str):
        types = types.split(";")
    return [t.strip().lower() for t in types if t and t.strip()]


def tidy(tag):
    """'drum and bass' -> 'Drum and Bass' (Navidrome's tags are often lower case)."""
    words = tag.split()
    return " ".join(w if w != w.lower() or (i and w in SMALL_WORDS) else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))


def compile_groups(groups):
    """[(name, [normalized tag keywords], {release types}, [normalized
    album-name keywords])], in order."""
    out = []
    for g in groups:
        words, types, album_words = [], set(), []
        for kw in g["match"]:
            if kw.lower().startswith(RELEASE):
                types.add(kw[len(RELEASE):].strip().lower())
            elif kw.lower().startswith(ALBUM):
                album_words.append(normalize(kw[len(ALBUM):]))
            else:
                words.append(normalize(kw))
        out.append((g["name"], words, types, album_words))
    return out


def uses_release_types(groups):
    return any(kw.lower().startswith(RELEASE) for g in groups for kw in g["match"])


def song_group(tags, types, compiled, album=""):
    """(rank, group name) for one song; rank = len(compiled) if no match."""
    norm = [normalize(t) for t in tags]
    album = normalize(album)
    for rank, (name, words, rtypes, album_words) in enumerate(compiled):
        if rtypes & set(types) or any(w in album for w in album_words) \
                or any(w in t for w in words for t in norm):
            return rank, name
    return len(compiled), None


def tag_album_counts(songs):
    """{lower-case tag: how many albums carry it} across the synced songs."""
    albums = {}
    for s in songs:
        for t in song_tags(s):
            albums.setdefault(t.lower(), set()).add(album_key(s))
    return {t: len(a) for t, a in albums.items()}


def most_common_tag(tags, albums_with):
    """The tag the most albums carry (ties: the earlier tag), tidied; None
    for no tags."""
    if not tags:
        return None
    best = max(enumerate(tags), key=lambda it: (albums_with.get(it[1].lower(), 0), -it[0]))
    return tidy(best[1])


def album_key(song):
    return song.get("albumId") or (song.get("album"), song.get("albumArtist") or song.get("artist"))


def assign(songs, groups, unmatched=DEFAULT_UNMATCHED, album_types=None):
    """{song id: genre} for the player. album_types: {album id: [release
    types]} (only needed if a group uses "release:")."""
    compiled = compile_groups(groups)
    album_types = album_types or {}
    albums_with = tag_album_counts(songs)
    picks = {}                     # song id -> (rank, genre)
    for s in songs:
        tags = song_tags(s)
        rank, name = song_group(tags, album_types.get(s.get("albumId"), []), compiled,
                                s.get("album"))
        if name is None:
            name = unmatched or most_common_tag(tags, albums_with) or NO_TAGS
        picks[s["id"]] = (rank, name)
    by_album = {}
    for s in songs:
        by_album.setdefault(album_key(s), []).append(picks[s["id"]])
    winner = {}
    for key, votes in by_album.items():
        counts = Counter(votes)
        # A matched group beats `unmatched`; then most songs; then list order.
        winner[key] = min(counts, key=lambda v: (v[0] == len(compiled), -counts[v], v))[1]
    return {s["id"]: winner[album_key(s)] for s in songs}


def summary(songs, genres):
    """Lines for the --genres preview: each genre with its albums, and the
    tags that fed it."""
    albums, tags = {}, {}
    for s in songs:
        g = genres[s["id"]]
        albums.setdefault(g, {})[album_key(s)] = f"{s.get('artist')} - {s.get('album')}"
        for t in song_tags(s) or ["(no tags)"]:
            tags.setdefault(g, Counter())[t.lower()] += 1
    lines = [f"{len(albums)} genres for {len({album_key(s) for s in songs})} albums "
             f"({len(songs)} songs):"]
    for g in sorted(albums, key=lambda g: (-len(albums[g]), g.lower())):
        lines.append(f"\n{g}: {len(albums[g])} album(s)")
        lines.append("  tags: " + ", ".join(t for t, _ in tags[g].most_common()))
        lines += [f"  - {a}" for a in sorted(albums[g].values(), key=str.lower)]
    return lines
