"""Tests for musicdock-players and DockSetup.txt.

Run: python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import dock_players  # noqa: E402
import dock_setup  # noqa: E402
from dock_record import Record  # noqa: E402

NANO, CLASSIC, FRIEND = "000A27001DA0AAAA", "000A27002BB0BBBB", "000A27003CC0DDDD"
MODELS = {"xB903": "iPod Nano 4th Gen", "xB029": "iPod Classic 1st Gen (6G)"}


class PlayersBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(tmp.name, "dock.db")
        rec = Record(self.path)
        for guid, model, name in ((NANO, "xB903", "Jo’s iPod"),
                                  (CLASSIC, "xB029", "Jo's iPod"),
                                  (FRIEND, "xB903", "Sam's iPod")):
            rec.upsert_player(guid, model=model)
            rec.set_player_name(guid, name)
        rec.mark_synced(NANO)
        rec.add_track(NANO, 1, navidrome_id="n1")
        rec.close()
        # pygpod's model table isn't on every test machine.
        patcher = mock.patch.object(dock_players, "model_name",
                                    side_effect=lambda m: MODELS.get(m, m or "model unknown"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self):
        rec = Record(self.path)
        self.addCleanup(rec.close)
        return rec

    def forgotten(self):
        return {p["guid"] for p in self.record().players() if p["forgotten_at"]}


class MatchTests(PlayersBase):
    def names(self, ref, this=None):
        found, why = dock_players.match(self.record().players(), ref, this)
        return why or sorted(p["guid"] for p in found)

    def test_name_needs_the_model_when_two_share_it(self):
        self.assertIn("2 called", self.names("jo's ipod"))         # curly vs straight '
        self.assertEqual(self.names("Jo's iPod (nano)"), [NANO])
        self.assertEqual(self.names("JO'S IPOD (Classic)"), [CLASSIC])
        self.assertEqual(self.names("Jo's iPod (AAAA)"), [NANO])    # or the ID's end
        self.assertEqual(self.names("Sam's iPod"), [FRIEND])

    def test_id_end_and_all_others(self):
        self.assertEqual(self.names("bbbb"), [CLASSIC])
        self.assertEqual(self.names("all others", this=CLASSIC), sorted([NANO, FRIEND]))
        self.assertIn("No iPod called", self.names("Pat's iPod"))
        self.assertIn("No iPod", self.names(""))


class CommandTests(PlayersBase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dock_players.main(list(argv), record=Record(self.path))
        return code, out.getvalue()

    def test_lists_players(self):
        code, out = self.run_cli()
        lines = out.splitlines()
        self.assertEqual((code, len(lines)), (0, 3))
        self.assertTrue(lines[0].startswith("Jo’s iPod (iPod Nano 4th Gen)"))  # synced first
        self.assertIn(f"ID {NANO}: 1 tracks", lines[0])
        self.assertIn("last synced never", lines[1])

    def test_forget_and_keep(self):
        code, out = self.run_cli("forget", "Jo's iPod (nano)")
        self.assertIn("Forgot Jo’s iPod (iPod Nano 4th Gen)", out)
        self.assertEqual(self.forgotten(), {NANO})
        self.assertIn("Already forgotten", self.run_cli("forget", "aaaa")[1])
        self.assertIn("Brought back", self.run_cli("keep", "aaaa")[1])
        self.assertEqual(self.forgotten(), set())

    def test_unclear_name_changes_nothing(self):
        for ref in ("Jo's iPod", "ZZZZ", ""):
            with self.assertRaises(SystemExit):
                self.run_cli("forget", ref)
        self.assertEqual(self.forgotten(), set())


class SetupFileTests(PlayersBase):
    def write(self, text, name="DockSetup.txt"):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            f.write(text)

    def apply(self, this=CLASSIC):
        return dock_setup.apply_setup_file(self.dir, self.record(), this)

    def test_no_file_no_result(self):
        self.assertIsNone(self.apply())

    def test_forget_all_others_then_file_is_gone(self):
        self.write("# moving to the classic\nForget=all others\n", name="docksetup.TXT")
        result = self.apply()
        self.assertTrue(result["ok"])
        self.assertEqual(self.forgotten(), {NANO, FRIEND})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "docksetup.TXT")))
        self.assertEqual(dock_setup.summary_line(result), "2 iPods updated")

    def test_all_or_nothing(self):
        self.write("Forget: Sam's iPod\nForget=Jo's iPod\n")       # 2nd is ambiguous
        result = self.apply()
        self.assertFalse(result["ok"])
        self.assertIn("add (model)", result["reason"])
        self.assertEqual(self.forgotten(), set())
        self.assertTrue(os.path.exists(os.path.join(self.dir, "DockSetup.txt")))

    def test_cant_forget_the_ipod_being_synced(self):
        self.write("Forget=Jo's iPod (classic)\n")
        self.assertEqual(self.apply()["reason"], "Can't forget the iPod being synced")
        self.assertEqual(self.forgotten(), set())

    def test_bad_files(self):
        for text, why in (("Remove=Jo's iPod\n", "Unknown setting"),
                          ("Forget Jo's iPod\n", "isn't Setting=value"),
                          ("# nothing\n\n", "Nothing in it")):
            self.write(text)
            self.assertIn(why, self.apply()["reason"])

    def test_marker_lists_players_and_keeps_last_result(self):
        rec = self.record()
        rec.forget_player(FRIEND)
        result = {"ok": True, "done": ["Forgot Sam's iPod (iPod Nano 4th Gen)"], "reason": ""}
        first = dock_setup.marker_text(rec.players(), CLASSIC, "Intro\n", result)
        self.assertTrue(first.startswith("Intro\n"))
        self.assertIn("Jo's iPod (iPod Classic 1st Gen (6G)), ID ...BBBB - this iPod", first)
        self.assertIn("forgotten", first)
        self.assertIn("1 tracks, last synced", first)
        later = dock_setup.marker_text(rec.players(), CLASSIC, "Intro\n", None, first)
        self.assertIn("Last DockSetup.txt", later)
        self.assertIn("Forgot Sam's iPod", later)

    def test_write_marker_keeps_its_spelling(self):
        self.write("old\n", name="mediadock.txt")
        dock_setup.write_marker(self.dir, "MediaDock.txt", self.record().players(),
                                NANO, "Intro\n")
        self.assertIn("mediadock.txt", os.listdir(self.dir))
        with open(os.path.join(self.dir, "mediadock.txt")) as f:
            self.assertIn("Sam's iPod", f.read())


CONFIG = """[server]
url = "https://old.example.com"   # where Navidrome is
username = "jo"
password = "old-secret"

[sync]
targets = ["starred"]              # what to sync
playlist_tag = "#ipod"             # tagged playlists
"""


class FakeServer:
    """subsonic.Client stand-in: `songs` exist; `password` signs in."""

    def __init__(self, songs=("n1",), password="new-secret", down=False):
        self.songs, self.password, self.down = set(songs), password, down
        self.logins = []

    def __call__(self, url, username, password, timeout=None):
        self.logins.append((url, username))
        server = self

        class Client:
            def call(self, endpoint, **params):
                from subsonic import SubsonicError
                if server.down:
                    raise SubsonicError("ping: can't reach server", network=True)
                if password != server.password:
                    raise SubsonicError("Wrong username or password (code 40)", code=40)
                if endpoint == "getSong" and params["id"] not in server.songs:
                    raise SubsonicError("Song not found (code 70)", code=70)
                return {}
        return Client()


class SettingsTests(PlayersBase):
    def setUp(self):
        super().setUp()
        self.config = os.path.join(self.dir, "config.toml")
        with open(self.config, "w") as f:
            f.write(CONFIG)
        os.chmod(self.config, 0o600)
        self.server = FakeServer()

    def apply(self, text, this=NANO):
        import dock_config
        with open(os.path.join(self.dir, "DockSetup.txt"), "w") as f:
            f.write(text)
        return dock_setup.apply_setup_file(self.dir, self.record(), this,
                                           dock_config.load(self.config), self.config,
                                           connect=self.server)

    def settings(self):
        import tomllib
        with open(self.config, "rb") as f:
            return tomllib.load(f)

    def assert_unchanged(self, result, why):
        self.assertFalse(result["ok"])
        self.assertIn(why, result["reason"])
        with open(self.config) as f:
            self.assertEqual(f.read(), CONFIG)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "DockSetup.txt")))

    def test_playlists_only_with_a_new_tag(self):
        result = self.apply("PlaylistTag=#car\nSyncFavorites=no\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["done"], ["Playlists tagged #car sync",
                                          "Starred songs don't sync"])
        self.assertEqual(self.settings()["sync"], {"targets": [], "playlist_tag": "#car"})
        with open(self.config) as f:
            text = f.read()
        self.assertIn('playlist_tag = "#car"              # tagged playlists', text)
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "DockSetup.txt")))
        self.assertEqual(self.server.logins, [])                  # no sign-in to test

    def test_compilation_albums_from_the_ipod(self):
        result = self.apply("CompilationAlbums=Now That's What I Call Music;"
                            "Chillhop Essentials=Chillhop Releases\n")
        self.assertTrue(result["ok"])
        self.assertEqual(self.settings()["sync"]["compilation_albums"],
                         ["Now That's What I Call Music", "Chillhop Essentials = Chillhop Releases"])
        self.apply("CompilationAlbums=none\n")
        self.assertEqual(self.settings()["sync"]["compilation_albums"], [])
        self.assertIn("needs a name", self.apply("CompilationAlbums== X\n")["reason"])

    def test_artist_names_from_the_ipod(self):
        result = self.apply("ArtistNames=Now, Now; Simon & Garfunkel ;\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["done"], ["2 artist name(s) kept whole"])
        self.assertEqual(self.settings()["sync"]["artist_names"], ["Now, Now", "Simon & Garfunkel"])
        self.apply("artistnames=Tyler, the Creator\n")       # replaces the list; any case
        self.assertEqual(self.settings()["sync"]["artist_names"], ["Tyler, the Creator"])
        self.assertEqual(self.apply("ArtistNames=none\n")["done"],
                         ["No extra artist names kept whole"])
        self.assertEqual(self.settings()["sync"]["artist_names"], [])
        self.assertIn("needs artist names", self.apply("ArtistNames=\n")["reason"])
        self.assertIn("needs artist names", self.apply("ArtistNames=;\n")["reason"])

    def test_artist_names_quote_safely_in_the_config(self):
        self.apply('ArtistNames=The "Best" \\ Band, Inc.\n')
        self.assertEqual(self.settings()["sync"]["artist_names"], ['The "Best" \\ Band, Inc.'])

    def test_favorite_rating_from_the_ipod(self):
        self.assertEqual(self.apply("FavoriteRating=5\n")["done"], ["Songs rated 5+ stars get favorited"])
        self.assertEqual(self.settings()["sync"]["favorite_rating"], 5)
        self.assertEqual(self.apply("FavoriteRating=0\n")["done"], ["Ratings don't favorite songs"])
        self.assertIn("0 to 5", self.apply("FavoriteRating=9\n")["reason"])

    def test_lossless_tag_can_be_turned_off(self):
        result = self.apply("LosslessTag=none\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["done"], ["No playlists sync as lossless"])
        self.assertEqual(self.settings()["sync"]["lossless_tag"], "")

    def test_starred_back_on_goes_first(self):
        self.apply("SyncStarred=no\n")
        self.apply("SyncStarred=yes\n")
        self.assertEqual(self.settings()["sync"]["targets"], ["starred"])

    def test_refused_settings_change_nothing(self):
        for text, why in (("SyncStarred=no\nPlaylistTag=none\n", "Nothing would sync"),
                          ("PlaylistTag=my car\n", "one word"),
                          ("LosslessTag=my tag\n", "one word"),
                          ("LosslessTag=\n", "needs a tag"),
                          ("LosslessTag=#iPod\n", "syncs playlists"),
                          ("PlaylistTag=\n", "needs a tag"),
                          ("SyncStarred=maybe\n", "yes or no"),
                          ("PlaylistTag=#a\nPlaylistTag=#b\n", "given twice"),
                          ("Server=https://new.example.com\n", "needs Username"),
                          ("Server=ftp://x\nUsername=jo\nPassword=p\n", "http"),
                          ("Password=\n", "is empty")):
            self.assert_unchanged(self.apply(text), why)

    def test_new_address_for_the_same_server(self):
        result = self.apply("Server=https://new.example.com/\nUsername=jo\n"
                            "Password=new-secret\n")
        self.assertTrue(result["ok"])
        self.assertEqual(self.settings()["server"], {"url": "https://new.example.com",
                                                     "username": "jo",
                                                     "password": "new-secret"})
        self.assertNotIn("new-secret", " ".join(result["done"]))
        self.assertEqual(self.server.logins, [("https://new.example.com", "jo")])

    def test_home_and_away_addresses(self):
        self.assert_unchanged(self.apply("Server=https://a.example.com, ftp://b\n"
                                         "Username=jo\nPassword=new-secret\n"), "http")
        result = self.apply("Server=http://192.168.1.50:4533, https://new.example.com\n"
                            "Username=jo\nPassword=new-secret\n")
        self.assertTrue(result["ok"])
        self.assertEqual(self.settings()["server"]["url"],
                         ["http://192.168.1.50:4533", "https://new.example.com"])
        self.assertIn("Server is now http://192.168.1.50:4533 or https://new.example.com",
                      result["done"])
        self.assertEqual(self.server.logins[0][0],
                         ["http://192.168.1.50:4533", "https://new.example.com"])

    def test_sign_in_is_tried_first(self):
        self.assert_unchanged(self.apply("Password=typo\n"), "refused that username")
        self.server.down = True
        self.assert_unchanged(self.apply("Password=new-secret\n"), "Can't reach")
        self.server.down = False
        self.assertEqual(self.apply("Password=new-secret\n")["done"], ["Password changed"])
        self.assertEqual(self.settings()["server"]["password"], "new-secret")

    def test_other_library_needs_new_library_and_no_waiting_plays(self):
        self.server.songs = set()                              # none of this iPod's songs
        login = "Server=https://other.example.com\nUsername=sam\nPassword=new-secret\n"
        self.assert_unchanged(self.apply(login), "NewLibrary=yes")
        rec = self.record()
        rec.queue_plays(NANO, [{"dbid": 1, "seq": 0, "played_at": 1792100000,
                                "navidrome_id": "n1"}])
        self.assert_unchanged(self.apply(login + "NewLibrary=yes\n"),
                              "1 plays not yet sent")
        rec.mark_sent([p["id"] for p in rec.unsent_plays()])
        self.assertTrue(self.apply(login + "NewLibrary=yes\n")["ok"])
        self.assertEqual(self.settings()["server"]["url"], "https://other.example.com")

    def test_stock_compatibility_off_needs_no_active_stock_ipod(self):
        rec = self.record()
        # NANO and FRIEND have never synced since kinds were recorded: they
        # count as stock. CLASSIC is a Rockbox player.
        rec.set_player_kind(CLASSIC, "rockbox")
        self.assert_unchanged(self.apply("StockCompatible=no\n", this=CLASSIC),
                              "2 stock iPods still sync")
        rec.set_player_kind(FRIEND, "rockbox")
        self.assert_unchanged(self.apply("StockCompatible=no\n", this=CLASSIC),
                              "still syncs: Forget it first")
        self.assert_unchanged(self.apply("StockCompatible=no\n", this=NANO),
                              "This is a stock iPod")
        # Forgetting the last stock iPod in the same file is fine...
        result = self.apply("Forget=Jo's iPod (nano)\nStockCompatible=no\n", this=CLASSIC)
        self.assertTrue(result["ok"])
        self.assertIs(result["stock_compatible"], False)
        self.assertEqual(self.settings()["device"]["type"], "rockbox")
        # ...and bringing it back in the same file as switching off isn't.
        self.apply("StockCompatible=yes\n", this=CLASSIC)
        result = self.apply("Keep=Jo's iPod (nano)\nStockCompatible=no\n", this=CLASSIC)
        self.assertFalse(result["ok"])

    def test_stock_compatibility_back_on_from_a_stock_ipod(self):
        self.record().set_player_kind(NANO, "stock")
        with open(self.config, "a") as f:
            f.write('\n[device]\ntype = "rockbox"\n')
        result = self.apply("StockCompatible=yes\n", this=NANO)
        self.assertTrue(result["ok"])
        self.assertIs(result["stock_compatible"], True)
        self.assertEqual(result["done"], ["Stock iPods sync again (music converted)"])
        self.assertEqual(self.settings()["device"]["type"], "stock_ipod")
        self.assertEqual(self.apply("StockCompatible=yes\n", this=NANO)["done"],
                         ["Already compatible with stock iPods"])

    def test_safe_hours(self):
        self.assertEqual(self.apply("SafeHours=7-10\n")["done"],          # the default
                         ["Safe hours already 07:00-10:00"])
        result = self.apply("SafeHours=6:30-8\n")
        self.assertEqual(result["done"], ["Safe hours 06:30-08:00"])
        self.assertEqual(self.settings()["sync"]["safe_hours"], "06:30-08:00")
        self.assertEqual(self.apply("SafeHours=off\n")["done"], ["Safe hours off"])
        self.assertEqual(self.settings()["sync"]["safe_hours"], "off")
        self.assertEqual(self.apply("SafeHours=none\n")["done"], ["Safe hours already off"])
        result = self.apply("SafeHours=morning\n")
        self.assertFalse(result["ok"])
        self.assertIn("07:00-10:00", result["reason"])

    def test_flac_switch(self):
        with open(self.config, "a") as f:
            f.write('\n[device]\ntype = "rockbox"\n')
        result = self.apply("FLAC=no\n")
        self.assertEqual(result["done"], ["FLAC converted to MP3"])
        self.assertIs(self.settings()["device"]["flac"], False)
        self.assertEqual(self.apply("FLAC=no\n")["done"], ["FLAC already converted"])
        self.assertEqual(self.apply("FLAC=yes\n")["done"], ["FLAC copied as-is"])
        self.assertIs(self.settings()["device"]["flac"], True)
        result = self.apply("FLAC=maybe\n")
        self.assertFalse(result["ok"])
        self.assertIn("FLAC must be yes or no", result["reason"])

    def test_flac_switch_on_a_stock_compatible_dock_says_when_it_applies(self):
        result = self.apply("FLAC=no\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["done"],
                         ["FLAC converted to MP3 (used once the dock is Rockbox-only)"])

    def test_settings_and_players_together(self):
        result = self.apply("Forget=Sam's iPod\nPlaylistTag=#car\n")
        self.assertEqual(dock_setup.summary_line(result), "2 changes made")
        self.assertEqual(self.forgotten(), {FRIEND})


class ConfigEditTests(unittest.TestCase):
    def test_missing_key_section_and_multiline_array(self):
        import tomllib
        import dock_config
        text = ('[sync]\ntargets = [\n  "starred",   # mine\n  "playlist:Road ]",\n]\n'
                'cache_dir = "x"\n')
        out = dock_config.set_value(text, "sync", "targets", ["playlist:Road ]"])
        out = dock_config.set_value(out, "sync", "playlist_tag", "#car")
        out = dock_config.set_value(out, "server", "url", 'https://a"b')
        data = tomllib.loads(out)
        self.assertEqual(data["sync"], {"targets": ["playlist:Road ]"], "cache_dir": "x",
                                        "playlist_tag": "#car"})
        self.assertEqual(data["server"]["url"], 'https://a"b')


if __name__ == "__main__":
    unittest.main()
