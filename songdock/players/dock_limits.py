"""
dock_limits.py - notes about a player's size, for the log, MediaDock.txt and
musicdock-status (user's thresholds, 2026-10-26).

Song counts are the larger of what's on the player and what its plan wants
there, so a note shows up before the songs are copied:

  20,000+   large library: syncs may be slower
  30,000+   large library: slowdowns or crashes may occur
  45,000+   (stock iPods) close to the limit
  47,000    (stock iPods) the limit: ipod_handler adds no more songs

Stock iPods are reported to fail at about 50,000 songs; 47,000 leaves a
buffer (user's choice). The 45,000/47,000 are for a ~50,000-song iPod: each model
gets its own (user, 2026-11-09), scaled the same way from MODEL_SONGS, so a
~20,000-song iPod warns at 18,000 and stops at 18,800. Models not listed (the
nanos, whose storage runs out long before) keep 45,000/47,000. The iPod database itself was measured fine at
40,000 songs on the Pi (ipod_db: 114 MB peak); what hasn't been measured
at that size is album art and the prepare job.

A stock iPod with more than 250 GB of room that still gets MP3 is told
about ALAC (convert_to = "alac") when the planned songs would still fit:
ALAC converts ~9x faster than MP3 V0 on a Pi but is ~3.3x the size
(measured 2026-10-16), so copying and the cache take ~3x as long, and a
512 GB iPod holds ~15,000-17,000 songs of it rather than 40,000+.
"""

LARGE, VERY_LARGE, STOCK_WARN, STOCK_LIMIT = 20_000, 30_000, 45_000, 47_000

# About how many songs each model's firmware handles, by model number (MC297 = C297), from
# iFlash's compatibility table (https://www.iflash.xyz/store/iflash-compatibility/, read
# 2026-11-09; the user's source of truth). "Only approximate": it depends on the tags and
# the iPod's memory; users report 10,000-30,000 for the 30 GB models and 30,000-60,000 for
# the bigger ones. 3rd/4th gen (~20,000) have no model numbers there and aren't supported.
MODEL_SONGS = {
    **dict.fromkeys(("A002", "A146"), 20_000),                    # Video 5th gen 30 GB
    **dict.fromkeys(("A003", "A147"), 50_000),                    # Video 5th gen 60 GB
    **dict.fromkeys(("A444", "A446", "A664"), 20_000),            # Video 5.5th gen 30 GB
    **dict.fromkeys(("A448", "A450"), 50_000),                    # Video 5.5th gen 80 GB
    **dict.fromkeys(("B029", "B147"), 50_000),                    # Classic 6th gen 80 GB
    **dict.fromkeys(("B145", "B150"), 50_000),                    # Classic 6th gen 160 GB
    **dict.fromkeys(("B565", "B562"), 50_000),                    # Classic 6.5th gen 120 GB
    **dict.fromkeys(("C297", "C293"), 50_000),                    # Classic 7th gen 160 GB
}
ALAC_FROM = 250e9             # bytes of player storage
ALAC_VS_MP3 = 3.3             # ALAC is about this many times the size of MP3 V0
ALAC_FILL = 0.8               # suggest ALAC only if it would fill at most this much
SPACE_KEPT_FREE = 200e6       # the handlers' FREE_SPACE_MARGIN
LOSSLESS_SUFFIXES = {"flac", "wav", "aif", "aiff", "ape", "wv"}   # sources that would become ALAC


def model_number(model):
    """'C297' for 'xC297', 'MC297', 'PC297' or 'C297'; None for none."""
    model = (model or "").strip().upper()
    return model[1:] if len(model) == 5 and model[0] in "XMP" else model or None


def song_limits(model=None):
    """(warn at, stop adding at) for a stock iPod of this model: 90% and 94% of
    what MODEL_SONGS says it handles (45,000 and 47,000 for ~50,000)."""
    songs = MODEL_SONGS.get(model_number(model))
    if not songs:
        return STOCK_WARN, STOCK_LIMIT
    return round(songs * STOCK_WARN / 50_000), round(songs * STOCK_LIMIT / 50_000)


def library_note(songs, stock=True, model=None):
    """One line about the library's size, or None."""
    warn, limit = song_limits(model)
    if stock and songs >= limit:
        return (f"{songs:,} songs: limit reached - the dock adds no more than "
                f"{limit:,} to this iPod")
    if stock and songs >= warn:
        return (f"{songs:,} songs: close to the limit - past {limit:,}, no more "
                "songs are added to this iPod")
    if songs >= VERY_LARGE:
        return f"Large library ({songs:,} songs): slowdowns or crashes may occur"
    if songs >= LARGE:
        return f"Large library ({songs:,} songs): syncs may be slower"
    return None


def alac_note(capacity, cfg, stock=True, alac_size=None):
    """Suggest lossless ALAC for a big stock iPod still getting MP3, or None.
    `alac_size`: the planned music's size if lossless sources were ALAC
    (unknown = no suggestion): it must still fit."""
    device = (cfg or {}).get("device", {})
    if (not stock or not capacity or capacity <= ALAC_FROM or not alac_size
            or device.get("type", "stock_ipod") != "stock_ipod"
            or device.get("convert_to", "mp3") != "mp3"
            or alac_size > capacity * ALAC_FILL):
        return None
    return (f"This iPod holds {capacity / 1e9:,.0f} GB: lossless ALAC would fit "
            '(convert_to = "alac" in config.toml, about 3x the size of MP3)')


def planned_songs(manifest):
    """[(entry, bytes or None)] for every song in the plan: the real size if
    it's known, else the prepare job's estimate. None = no idea (older plans)."""
    return [(e, e.get("size") or e.get("estimate") or None)
            for key in ("tracks", "on_player", "skipped") for e in manifest.get(key, [])]


def plan_bytes(manifest, scale=lambda entry: 1):
    """The planned music's size. Songs of unknown size count at the average
    of the others. None if the plan has no sizes."""
    songs = planned_songs(manifest)
    known = [size for _, size in songs if size]
    if not known:
        return None
    average = sum(known) / len(known)
    return sum((size or average) * scale(entry) for entry, size in songs)


def alac_bytes(manifest):
    """The plan's size if lossless sources were ALAC: only those MP3s grow.
    Songs already lossless, and lossy sources, stay as they are."""
    return plan_bytes(manifest, lambda e: ALAC_VS_MP3 if (
        (e.get("encoding") or "").startswith("mp3")
        and (e.get("source_suffix") or "").lower() in LOSSLESS_SUFFIXES) else 1)


def fit_note(capacity, music_bytes):
    """A warning if the planned music is more than the player holds, or None."""
    if not capacity or not music_bytes or music_bytes <= capacity - SPACE_KEPT_FREE:
        return None
    return (f"The planned music is about {music_bytes / 1e9:,.1f} GB and this player holds "
            f"{capacity / 1e9:,.1f} GB: some songs or lossless versions won't fit")


def player_notes(on_player, planned, capacity, cfg, kind="stock", music_bytes=None,
                 alac_size=None, model=None):
    """The notes for one player: [line, ...]. model: its model number, for its limit."""
    stock = kind in (None, "stock")
    notes = [library_note(max(on_player or 0, planned or 0), stock, model),
             fit_note(capacity, music_bytes),
             alac_note(capacity, cfg, stock, alac_size)]
    return [n for n in notes if n]
