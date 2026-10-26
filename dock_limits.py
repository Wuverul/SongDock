"""
dock_limits.py - notes about a player's size, for the log, MediaDock.txt and
musicdock-status (user's thresholds, 2026-10-26).

Song counts are the larger of what's on the player and what its plan wants
there, so a note shows up before the songs are copied:

  20,000+   large library: syncs may be slower
  30,000+   large library: slowdowns or crashes may occur
  45,000+   (stock iPods) the limit the dock supports for them

These are warnings, not limits the dock enforces. The iPod database itself
was measured fine at 40,000 songs on the Pi (ipod_db: 114 MB peak); what
hasn't been measured at that size is album art and the prepare job.

A stock iPod with more than 250 GB of room that still gets MP3 is told
about ALAC (convert_to = "alac"): lossless, about 3x the size, and on a
player that big the space is there.
"""

LARGE, VERY_LARGE, STOCK_LIMIT = 20_000, 30_000, 45_000
ALAC_FROM = 250e9             # bytes of player storage


def library_note(songs, stock=True):
    """One line about the library's size, or None."""
    if stock and songs >= STOCK_LIMIT:
        return (f"{songs:,} songs: past the {STOCK_LIMIT:,} the dock supports on a "
                "stock iPod")
    if songs >= VERY_LARGE:
        return f"Large library ({songs:,} songs): slowdowns or crashes may occur"
    if songs >= LARGE:
        return f"Large library ({songs:,} songs): syncs may be slower"
    return None


def alac_note(capacity, cfg, stock=True):
    """Suggest lossless ALAC for a big stock iPod still getting MP3, or None."""
    device = (cfg or {}).get("device", {})
    if (not stock or not capacity or capacity <= ALAC_FROM
            or device.get("type", "stock_ipod") != "stock_ipod"
            or device.get("convert_to", "mp3") != "mp3"):
        return None
    return (f"This iPod holds {capacity / 1e9:,.0f} GB: lossless ALAC would fit "
            '(convert_to = "alac" in config.toml, about 3x the size of MP3)')


def player_notes(on_player, planned, capacity, cfg, kind="stock"):
    """The notes for one player: [line, ...]."""
    stock = kind in (None, "stock")
    notes = [library_note(max(on_player or 0, planned or 0), stock),
             alac_note(capacity, cfg, stock)]
    return [n for n in notes if n]
