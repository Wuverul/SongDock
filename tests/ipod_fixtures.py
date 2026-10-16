"""Builds fake iPods for tests: a real (pygpod) iTunesDB, silent MP3s, and
Play Counts files. Needs pygpod + mutagen (the Pi venv, or tmp/venv on the Mac).
"""

import os
import struct

FAKE_GUID = "000A270000000001"           # not a real device
NANO_4G_SYSINFO = f"ModelNumStr: xB903\nFirewireGuid: 0x{FAKE_GUID}\n"

# One silent MPEG-1 Layer III frame: 128 kbps, 44.1 kHz, no padding = 417 bytes.
FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 413
FRAMES_PER_SECOND = 44100 / 1152


def have_ipod_tools():
    try:
        import mutagen  # noqa: F401
        import pygpod   # noqa: F401
        return True
    except ImportError:
        return False


def make_mp3(path, title, artist="Artist", album="Album", track=1, seconds=5, cover=False):
    from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TRCK
    with open(path, "wb") as f:
        f.write(FRAME * int(seconds * FRAMES_PER_SECOND))
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text=album))
    tags.add(TRCK(encoding=3, text=str(track)))
    if cover:
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (300, 300), (40, 120, 200)).save(buf, "JPEG")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=buf.getvalue()))
    tags.save(path, v2_version=3)
    return path


def make_ipod(root, songs, src_dir):
    """Create an iPod folder at `root` holding `songs` (dicts with title,
    artist, album, track, seconds). Returns the tracks in database order."""
    import pygpod
    from pygpod.device.mountpoint import init_ipod
    os.makedirs(root, exist_ok=True)
    init_ipod(root, model="nano_4g", music_dirs=10)
    os.makedirs(os.path.join(root, "iPod_Control", "Device"), exist_ok=True)
    with open(os.path.join(root, "iPod_Control", "Device", "SysInfo"), "w") as f:
        f.write(NANO_4G_SYSINFO)
    db = pygpod.Database(root)
    for i, s in enumerate(songs):
        db.add_track(make_mp3(os.path.join(src_dir, f"src{i}.mp3"), **s))
    db.save()
    return pygpod.Database(root).tracks


def write_play_counts(root, entries):
    """entries: list of (play_count, mac_time) per track position."""
    header = b"mhdp" + struct.pack("<III", 0x60, 0x1C, len(entries))
    header += b"\x00" * (0x60 - len(header))
    body = b"".join(struct.pack("<IIIIIII", n, t, 0, 0, 0, 0, 0) for n, t in entries)
    with open(os.path.join(root, "iPod_Control", "iTunes", "Play Counts"), "wb") as f:
        f.write(header + body)
