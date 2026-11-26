"""
dock_lookup.py - genres from outside the library, for albums with none.

An album whose songs carry no genre tags, with no rule for it and no other
songs by its artist to borrow from, lands in "Other" (dock_genres). On the
user's library that was 61 albums, 119 songs (2026-11-14), mostly small-label
electronic singles. For those only, the dock asks two public services (user,
2026-11-14: both, on by default; `[sync] genre_lookup = false` turns it off):

  1. MusicBrainz, by the song's recording ID: the recording's genres, else its
     release group's (the album), else its first artist's. Specific ("electro
     house"), but few small releases have any: 10 of the 61 albums.
  2. Deezer, by the song's ISRC (from Navidrome, else MusicBrainz): the genres
     of the album Deezer holds it on. Broad ("Electro", "Dance", "Alternative"),
     but an exact match, not a search: 29 of the other 51. Its name search was
     no use ("Vulpey Sever" found Carmina Burana).

MusicBrainz user tags were tried too and not used: one held a song's lyrics,
two said "_to_do". Only IDs are sent (recording IDs, ISRCs), never the server's
address or anything about the dock's owner.

The answers are kept in the plan ({album id: {"genres": [...], "checked":
epoch}}), so each album is asked once; an album with no answer is asked again
after RECHECK_DAYS. A network failure keeps nothing and ends the run's lookups,
so they're tried again next run.
"""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import dock_paths

log = logging.getLogger("dock_lookup")

MUSICBRAINZ = "https://musicbrainz.org/ws/2/"
DEEZER = "https://api.deezer.com/"
MB_INTERVAL = 1.1             # s between MusicBrainz requests (its limit: 1 a second)
RETRY_WAITS = (2, 5, 15)      # MusicBrainz answers 503 when asked too fast
RECHECK_DAYS = 30             # an album with no answer is asked again after this
MAX_ALBUMS_PER_RUN = 50       # about 5 min at most; the rest wait for the next run
SONGS_PER_ALBUM = 3           # songs of an album tried before giving up on it
TIMEOUT = 20


class LookupFailed(Exception):
    """A service couldn't be reached: nothing is kept, the next run tries again."""


def user_agent():
    """MusicBrainz asks every program to name itself and a contact."""
    try:
        with open(os.path.join(dock_paths.ROOT, "VERSION")) as f:
            version = f.read().strip()
    except OSError:
        version = "dev"
    return f"SongDock/{version} ( https://github.com/Wuverul/SongDock )"


class Fetcher:
    """GET a JSON answer, keeping to MusicBrainz's rate. Tests pass a stand-in
    with the same call: fetch(url) -> dict, raising LookupFailed."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.last_mb = None
        self.agent = user_agent()

    def __call__(self, url):
        for wait in (0,) + RETRY_WAITS:
            if wait:
                self.sleep(wait)
            if url.startswith(MUSICBRAINZ):
                if self.last_mb is not None:
                    self.sleep(max(0.0, MB_INTERVAL - (self.clock() - self.last_mb)))
                self.last_mb = self.clock()
            request = urllib.request.Request(url, headers={"User-Agent": self.agent,
                                                           "Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {}
                if e.code not in (429, 502, 503):
                    raise LookupFailed(f"{url.split('?')[0]}: HTTP {e.code}") from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise LookupFailed(f"{url.split('?')[0]}: {e}") from None
        raise LookupFailed(f"{url.split('?')[0]}: busy (HTTP 503)")


RELEASE_GROUP_ID = re.compile(
    r"release-group[:/]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def release_group_types(album_id, release_mbid, fetch):
    """A release's types from MusicBrainz, for a server that sends none (Aurral,
    2026-11-25: no releaseTypes on any album, so no single was ever known to be
    one). Aurral's album IDs name the release group ("album:release-group:<id>");
    otherwise the album's own MusicBrainz ID is a release, whose group is asked
    for. Lower case, primary type first: ["single"], ["album", "live"]. [] when
    MusicBrainz knows no type or there's no ID to ask with. Raises LookupFailed."""
    found = RELEASE_GROUP_ID.search(album_id or "")
    if found:
        group = fetch(f"{MUSICBRAINZ}release-group/{found.group(1).lower()}?fmt=json")
    elif release_mbid:
        group = fetch(f"{MUSICBRAINZ}release/{urllib.parse.quote(release_mbid)}"
                      "?inc=release-groups&fmt=json").get("release-group") or {}
    else:
        return []
    types = [group.get("primary-type")] + list(group.get("secondary-types") or [])
    return [t.lower() for t in types if t]


def genre_names(entity):
    """Genre names, most votes first."""
    found = sorted(entity.get("genres") or [], key=lambda g: -(g.get("count") or 0))
    return [g["name"] for g in found if g.get("name")]


def musicbrainz(mbid, fetch, wider=True):
    """(genres, ISRCs) for a recording: its own genres, else (wider) its release
    group's, else its first artist's. Those two are the same for every song of an
    album, so only an album's first song asks for them: each request costs a
    second, and 69 albums with three songs each took over 6 minutes."""
    r = fetch(f"{MUSICBRAINZ}recording/{urllib.parse.quote(mbid)}"
              "?inc=genres+isrcs+artists+releases+release-groups&fmt=json")
    isrcs = list(r.get("isrcs") or [])
    genres = genre_names(r)
    if not genres and wider:
        groups = sorted({rel["release-group"]["id"] for rel in r.get("releases") or []
                         if (rel.get("release-group") or {}).get("id")})
        for rgid in groups[:1]:
            genres += [g for g in genre_names(fetch(f"{MUSICBRAINZ}release-group/{rgid}"
                                                    "?inc=genres&fmt=json"))
                       if g not in genres]
    if not genres and wider:
        for credit in (r.get("artist-credit") or [])[:1]:
            artist = (credit.get("artist") or {}).get("id")
            if artist:
                genres = genre_names(fetch(f"{MUSICBRAINZ}artist/{artist}?inc=genres&fmt=json"))
    return genres, isrcs


def deezer(isrcs, fetch):
    """Genres of the album Deezer has the first of `isrcs` it knows on."""
    for isrc in isrcs[:2]:
        track = fetch(f"{DEEZER}track/isrc:{urllib.parse.quote(isrc)}")
        album = (track.get("album") or {}).get("id")
        if album:
            info = fetch(f"{DEEZER}album/{album}")
            return [g["name"] for g in (info.get("genres") or {}).get("data") or []
                    if g.get("name")]
    return []


def song_isrcs(song):
    """A Navidrome song's ISRCs (OpenSubsonic sends a list; older servers none)."""
    value = song.get("isrc") or []
    return [value] if isinstance(value, str) else [i for i in value if i]


def album_genres(songs, fetch):
    """Genres for an album from up to SONGS_PER_ALBUM of its songs: MusicBrainz,
    then Deezer. [] if neither knows. Raises LookupFailed."""
    for i, s in enumerate(songs[:SONGS_PER_ALBUM]):
        genres, isrcs = [], song_isrcs(s)
        if s.get("musicBrainzId"):
            genres, more = musicbrainz(s["musicBrainzId"], fetch, wider=not i)
            isrcs += [i for i in more if i not in isrcs]
        if not genres and isrcs:
            genres = deezer(isrcs, fetch)
        if genres:
            return genres
    return []


def lookup(albums, known, fetch=None, now=None):
    """{album id: {"genres": [...], "checked": epoch}} for `albums` ({album id:
    [songs]}): what's in `known` (the last plan's), asking again for albums with
    no answer that's older than RECHECK_DAYS, at most MAX_ALBUMS_PER_RUN a run.
    Albums not asked keep their entry, or get none."""
    now = int(now or time.time())
    result = {a: known[a] for a in albums if a in known}
    due = [a for a in sorted(albums)
           if a not in known or (not known[a].get("genres")
                                 and now - known[a].get("checked", 0) > RECHECK_DAYS * 86400)]
    if not due:
        return result
    fetch = fetch or Fetcher()
    log.info("Looking up genres for %d album(s) with none (MusicBrainz, Deezer)%s",
             min(len(due), MAX_ALBUMS_PER_RUN),
             f"; {len(due) - MAX_ALBUMS_PER_RUN} wait for the next check"
             if len(due) > MAX_ALBUMS_PER_RUN else "")
    found = 0
    for album in due[:MAX_ALBUMS_PER_RUN]:
        songs = [s for s in albums[album] if s.get("musicBrainzId") or song_isrcs(s)]
        try:
            genres = album_genres(songs, fetch) if songs else []
        except LookupFailed as e:
            log.warning("Genre lookup stopped (%s); the next check carries on", e)
            break
        result[album] = {"genres": genres, "checked": now}
        found += bool(genres)
    log.info("Genre lookup: %d found", found)
    return result
