"""
dock_plays.py - send queued plays from the Pi's record to Navidrome.

Plays reach the queue from a docked player (ipod_handler + dock_record).
They are sent with the Subsonic `scrobble` call (submission=true, with the
play time), in batches. A play leaves the queue only when the server has
confirmed it (CLAUDE.md: never clear play data until the server confirms).

Called by the sync job after reading a player, and by every prepare check,
so plays queued while the server was down go out as soon as it's back.

Navidrome answers "ok" to a play or a favorite for a song it doesn't have, and drops
it (checked 2026-11-10). So each song is looked up first (`check_songs`): one the
server no longer has is searched for by artist, title and length (e.g. after a
re-index that changed its tags too, so the prepare job couldn't follow it), and its
plays, favorite and players' tracks move to the new ID; with no single match its
plays are kept back (`gone_at`) and reported instead of being sent into nothing.
"""

import logging
import re

from subsonic import SubsonicError

log = logging.getLogger("dock_plays")

BATCH = 25


SAME_LENGTH = 2                 # seconds: as ipod_handler.reconcile matches tracks
FEAT = re.compile(r"\s*[(\[](?:feat|ft|featuring)\b.*$", re.I)


def _plain_title(title):
    return re.sub(r"\s+", " ", FEAT.sub("", title or "")).strip().casefold()


def find_moved_song(client, artist, title, duration=None):
    """The ID the server now has for a song by `artist` called `title` (the names the
    dock gave it: one artist, guests in the title), or None unless exactly one
    song matches. Its length must agree within SAME_LENGTH when it's known."""
    import dock_artists
    if not title:
        return None
    try:
        found = client.call("search3", query=FEAT.sub("", title), songCount=100,
                            artistCount=0, albumCount=0)["searchResult3"].get("song", [])
    except SubsonicError as e:
        if e.network:
            raise
        return None
    want = dock_artists.same_artist(artist or "")
    matches = set()
    for song in found:
        names = [song.get("artist"), song.get("displayArtist"), song.get("displayAlbumArtist")]
        names += [a.get("name") for a in song.get("artists") or []]
        names += [dock_artists.without_guests(song.get("artist") or "")[0]]
        if _plain_title(song.get("title")) != _plain_title(title):
            continue
        if want not in {dock_artists.same_artist(n) for n in names if n}:
            continue
        if duration and abs((song.get("duration") or 0) - duration) > SAME_LENGTH:
            continue
        matches.add(song["id"])
    return matches.pop() if len(matches) == 1 else None


def check_songs(client, record, ids):
    """{id: id to use, or None for a song gone with no new copy} for song IDs about
    to be sent. A song found under a new ID moves there in the record (its plays,
    favorite and tracks on players: Record.rename_songs). A network failure raises."""
    result = {}
    for nid in dict.fromkeys(ids):
        try:
            client.call("getSong", id=nid)
            result[nid] = nid
            continue
        except SubsonicError as e:
            if e.network:
                raise
            if e.code != 70:                       # something else: send as before
                result[nid] = nid
                continue
        artist, title, duration = record.song_names(nid) or (None, None, None)
        new = find_moved_song(client, artist, title, duration)
        if new:
            record.rename_songs({nid: new})
            log.warning("%s - %s has a new ID on the server: its plays go there", artist, title)
        else:
            record.mark_gone(nid)
            log.warning("%s - %s is no longer on the server and no single song matches it: "
                        "nothing is sent for it (its plays are kept back)",
                        artist or "?", title or nid)
        result[nid] = new
    return result


def upload_plays(client, record, limit=None):
    """Send unsent, matched plays. Returns (sent, failed, stopped_early).

    - A network failure (server unreachable / 5xx) stops at once; the rest
      stay queued for next time.
    - If the server rejects a batch, its plays are retried one at a time so
      one bad play (e.g. a song deleted on the server) can't block the rest;
      a rejected play stays queued with its error recorded.
    """
    try:
        check_songs(client, record, [p["navidrome_id"] for p in record.unsent_plays(limit)])
    except SubsonicError as e:
        log.warning("Server unreachable; plays stay queued: %s", e)
        return 0, 0, True
    plays = record.unsent_plays(limit)           # after any moves or set-asides
    sent = failed = 0
    for i in range(0, len(plays), BATCH):
        batch = plays[i:i + BATCH]
        try:
            _scrobble(client, batch)
            record.mark_sent([p["id"] for p in batch])
            sent += len(batch)
            continue
        except SubsonicError as e:
            if e.network:
                record.mark_failed([p["id"] for p in plays[i:]], e)
                log.warning("Server unreachable; %d play(s) stay queued: %s",
                            len(plays) - i, e)
                return sent, failed, True
            log.warning("Server rejected a batch of %d plays (%s); retrying one by one",
                        len(batch), e)
        for p in batch:
            try:
                _scrobble(client, [p])
                record.mark_sent([p["id"]])
                sent += 1
            except SubsonicError as e:
                record.mark_failed([p["id"]], e)
                if e.network:
                    rest = plays[plays.index(p) + 1:]
                    record.mark_failed([q["id"] for q in rest], e)
                    log.warning("Server unreachable; plays stay queued: %s", e)
                    return sent, failed + 1, True
                failed += 1
                log.warning("Play of %s - %s rejected, kept in queue: %s",
                            p.get("artist"), p.get("title"), e)
    if sent:
        log.info("Sent %d play(s) to Navidrome", sent)
    return sent, failed, False


def _scrobble(client, plays):
    client.call("scrobble", id=[p["navidrome_id"] for p in plays],
                time=[int(p["played_at"]) * 1000 for p in plays],   # milliseconds
                submission="true")


def upload_favorites(client, record):
    """Favorite the songs ratings queued (Record.note_ratings). Returns how many
    went out. The server being down keeps them queued; a song it no longer has
    is dropped, since asking again can't help."""
    sent = 0
    try:
        found = check_songs(client, record, record.unsent_favorites())
    except SubsonicError as e:
        log.warning("Server unreachable; rated songs stay queued to favorite: %s", e)
        return 0
    for nid in record.unsent_favorites():
        if nid in found and found[nid] is None:          # gone: starring it would do nothing
            log.warning("Couldn't favorite song %s: it's no longer on the server", nid)
            record.mark_favorited(nid)
            continue
        try:
            client.call("star", id=nid)
        except SubsonicError as e:
            if e.network:
                log.warning("Server unreachable; rated songs stay queued to favorite: %s", e)
                break
            log.warning("Couldn't favorite song %s: %s", nid, e)
            if e.code != 70:
                continue
        else:
            sent += 1
        record.mark_favorited(nid)
    if sent:
        log.info("Favorited %d song(s) rated on a player", sent)
    return sent
