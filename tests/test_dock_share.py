"""dock_share: what a shared report holds, and above all what it doesn't."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dock_report as report  # noqa: E402
import dock_share as share  # noqa: E402
from dock_record import Record  # noqa: E402

GUID = "000A27001DA0FFFF"
CFG = {"server": {"url": ["https://music.secret.example/", "http://10.1.2.3:4533"],
                  "username": "secretuser", "password": "hunter2pass"},
       "device": {"type": "stock_ipod", "convert_to": "mp3", "mp3_quality": "v0"},
       "sync": {"max_cache_gb": 20, "safe_hours": "07:00-10:00", "targets": ["playlist:Secret"],
                "playlist_tag": "#secrettag", "artist_names": ["Secret Artist"]},
       "display": {"model": "v4", "theme": "dark"}, "genres": {}}
PLAN = {"tracks": [{"id": "a", "title": "Hidden Song", "artist": "Hidden Band",
                    "album": "Hidden Album", "encoding": "alac 16", "source_suffix": "flac",
                    "size": 30e6}],
        "on_player": [{"id": "b", "title": "Other Tune", "artist": "Hidden Band",
                       "encoding": "mp3 v0", "source_suffix": "flac", "size": 8e6}],
        "skipped": [{"id": "c", "title": "Hidden Band - Third", "reason": "cache limit",
                     "error": False}],
        "playlists": [{"name": "My Private List", "target": "playlist:x", "song_ids": ["a"]}],
        "lossless_playlists": ["id1"]}
LEAKS = ["secret.example", "10.1.2.3", "secretuser", "hunter2pass", "Hidden", "Other Tune", "Third",
         "My Private List", "Test Person's iPod", GUID, "HomeWifi", "secrettag", "Secret Artist",
         "aa:bb:cc:dd:ee:ff", "/home/someone", "myhost"]


class ShareTests(unittest.TestCase):
    def build(self, errors=()):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dock.db")
            rec = Record(path)
            rec.upsert_player(GUID, model="xB903")
            rec.set_player_name(GUID, "Test Person's iPod")
            rec.set_player_kind(GUID, "stock")
            rec.set_player_capacity(GUID, 16_000_000_000)
            rec.add_track(GUID, 1, navidrome_id="a", title="Hidden Song", encoding="alac 16")
            rec.close()

            def run(*cmd):
                if "connection" in cmd:
                    return "HomeWifi\nmusicdock-HomeWifi\n"
                if "-g" in cmd:
                    return ("Conversion: 10.0 s for 2.0 min of audio (mp3 v0)\n"
                            "Conversion: 30.0 s for 4.0 min of audio (mp3 v0)\n"
                            "Copied 10 tracks, 50.0 MB in 25.0 s\n")
                return ""
            activity = (["Prepare job: idle"], {"errors": list(errors), "failed_units": []})
            with mock.patch.object(report, "run", side_effect=run), \
                    mock.patch.object(report, "activity_section", return_value=activity), \
                    mock.patch.object(report, "system_section",
                                      return_value=([], {"temp": 50.0, "throttled": "0x0"})), \
                    mock.patch("platform.node", return_value="myhost"), \
                    mock.patch("dock_update.load_state", return_value={}):
                return share.build(CFG, PLAN, tmp, path, "SongDock 0.7.1", "7 days ago",
                                   now=1_800_000_000, screen="idle")

    def test_holds_what_helps(self):
        text = self.build()
        for wanted in ("SongDock 0.7.1", "model xB903", "stock", "16.0 GB", "1 songs",
                       "Songs planned: 3", "alac 16 1", "flac 2", "lossless playlists: 1",
                       "convert_to = mp3", "safe_hours = 07:00-10:00", "server addresses: 2",
                       "mp3 v0: 6.7 s per minute", "2.0 MB/s", "cache limit", "Errors"):
            self.assertIn(wanted.replace("mp3 v0:", "Conversion to mp3 v0:"), text)

    def test_leaves_out_what_is_private(self):
        text = self.build(errors=[
            (1_799_999_000, "Couldn't prepare Hidden Band - Hidden Song"),
            (1_799_999_100, "fail https://music.secret.example/rest?u=secretuser&p=hunter2pass"),
            (1_799_999_200, f"player {GUID} Test Person's iPod at 10.1.2.3 aa:bb:cc:dd:ee:ff"),
            (1_799_999_300, "open /home/someone/x failed on myhost, HomeWifi down")])
        for leak in LEAKS:
            self.assertNotIn(leak, text, leak)
        self.assertIn("Couldn't prepare", text)      # the error itself survives
        self.assertIn("4 in all", text)

    def test_only_listed_settings_appear(self):
        cfg = {"sync": {"max_cache_gb": 5, "new_secret_setting": "shh"}}
        self.assertNotIn("shh", "\n".join(share.settings_lines(cfg)))

    def test_short_or_common_names_dont_eat_the_report(self):
        words = share.private_words({"server": {"username": "a"}}, {}, [{"name": "Pi"}])
        self.assertEqual(share.scrub("Raspberry Pi Zero", ["Pix"]), "Raspberry Pi Zero")
        self.assertNotIn("a", words)

    def test_long_errors_are_cut(self):
        self.assertEqual(len(share.clean_error("x" * 900, [])), 200)

    def test_no_measurements_yet(self):
        self.assertEqual(share.speeds([]), ["No measurements yet"])

    def test_a_broken_part_doesnt_lose_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "dock.db")
            with mock.patch.object(report, "run", return_value=""), \
                    mock.patch.object(report, "activity_section", side_effect=ValueError("x")), \
                    mock.patch.object(report, "system_section",
                                      return_value=([], {})), \
                    mock.patch("dock_update.load_state", return_value={}):
                text = share.build({}, {}, tmp, bad, "v", "1 day ago", now=1)
        self.assertIn("couldn't read this part", text)
        self.assertIn("Problems", text)


if __name__ == "__main__":
    unittest.main()
