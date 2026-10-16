"""
dock_plays.py - send queued plays from the Pi's record to Navidrome.

Plays reach the queue from a docked player (ipod_handler + dock_record).
They are sent with the Subsonic `scrobble` call (submission=true, with the
play time), in batches. A play leaves the queue only when the server has
confirmed it (CLAUDE.md: never clear play data until the server confirms).

Called by the sync job after reading a player, and by the hourly prepare job,
so plays queued while the server was down go out as soon as it's back.
"""

import logging

from subsonic import SubsonicError

log = logging.getLogger("dock_plays")

BATCH = 25


def upload_plays(client, record, limit=None):
    """Send unsent, matched plays. Returns (sent, failed, stopped_early).

    - A network failure (server unreachable / 5xx) stops at once; the rest
      stay queued for next time.
    - If the server rejects a batch, its plays are retried one at a time so
      one bad play (e.g. a song deleted on the server) can't block the rest;
      a rejected play stays queued with its error recorded.
    """
    plays = record.unsent_plays(limit)
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
