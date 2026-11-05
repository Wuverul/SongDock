"""config.example.toml (the installer's copy for a new dock) and genres.example.toml
say what the code does: every setting is there, and what it shows is the default."""

import os
import re
import sys
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import dock_config  # noqa: E402
import dock_genres  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
# Kept out of the example on purpose: invert is the older spelling of theme.
NOT_SHOWN = {("display", "invert")}
# Shown with a value other than the code's: theme "" means "follow invert", i.e. light.
SHOWN_AS = {("display", "theme"): "light"}


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class ExampleConfigTests(unittest.TestCase):
    def setUp(self):
        self.text = read("config.example.toml")
        self.active = tomllib.loads(self.text)
        # Every "# group.setting = value" line switched on.
        self.everything = tomllib.loads(re.sub(r"^# ([a-z0-9_]+\.[a-z0-9_]+ = )", r"\1",
                                               self.text, flags=re.M))

    def test_every_setting_is_there_and_shows_its_default(self):
        for section in ("sync", "device", "display"):
            for key, default in dock_config.DEFAULTS[section].items():
                if (section, key) in NOT_SHOWN:
                    continue
                with self.subTest(setting=f"{section}.{key}"):
                    self.assertIn(key, self.everything[section])
                    self.assertEqual(self.everything[section][key],
                                     SHOWN_AS.get((section, key), default))

    def test_nothing_shown_that_the_code_doesnt_know(self):
        for section in ("sync", "device", "display"):
            unknown = set(self.everything[section]) - set(dock_config.DEFAULTS[section])
            self.assertEqual(unknown, set(), section)

    def test_required_settings_come_first_and_the_rest_are_off(self):
        # The installer's copy sets only what a person must look at; everything
        # else follows the code's defaults, so later releases can improve them.
        self.assertEqual(set(self.active), {"server", "sync", "device", "display"})
        self.assertEqual(set(self.active["sync"]), {"safe_hours", "targets", "playlist_tag",
                                                    "favorites_playlist", "max_cache_gb"})
        self.assertEqual(self.active["device"], {"write": False})
        required = self.text.index("1. REQUIRED: set these")
        for line in ("server.url =", "sync.safe_hours =", "device.write ="):
            self.assertLess(required, self.text.index(line))
            self.assertLess(self.text.index(line), self.text.index("2. CHECK THESE:"))

    def test_the_genre_list_isnt_copied_into_new_configs(self):
        self.assertNotIn("genres", self.active)       # so docks follow the built-in list

    def test_loads_as_a_dock_config_once_filled_in(self):
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"songdock-example-{os.getpid()}.toml")
        with open(path, "w") as f:
            f.write(self.text.replace("your-navidrome-password", "secret"))
        os.chmod(path, 0o600)
        try:
            cfg = dock_config.load(path)
        finally:
            os.remove(path)
        self.assertEqual(cfg["sync"]["safe_hours"], "07:00-10:00")
        self.assertIs(cfg["device"]["write"], False)
        self.assertEqual(cfg["genres"]["groups"], dock_genres.DEFAULT_GROUPS)

    def test_lines_fit_a_terminal(self):
        self.assertEqual([n for n, l in enumerate(self.text.splitlines(), 1) if len(l) > 88],
                         [])


class ExampleGenresTests(unittest.TestCase):
    def test_matches_the_built_in_list(self):
        example = tomllib.loads(read("genres.example.toml"))["genres"]
        self.assertEqual(example["groups"], dock_genres.DEFAULT_GROUPS)
        self.assertEqual(example["unmatched"], dock_genres.DEFAULT_UNMATCHED)

    def test_pasted_at_the_end_of_the_example_it_still_loads(self):
        both = tomllib.loads(read("config.example.toml") + "\n" + read("genres.example.toml"))
        self.assertEqual(len(both["genres"]["groups"]), len(dock_genres.DEFAULT_GROUPS))
        self.assertEqual(both["sync"]["safe_hours"], "07:00-10:00")


if __name__ == "__main__":
    unittest.main()
