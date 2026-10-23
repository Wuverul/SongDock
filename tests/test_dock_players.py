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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
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


if __name__ == "__main__":
    unittest.main()
