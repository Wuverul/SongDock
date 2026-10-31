"""Tests for the Subsonic client's fallback addresses. No network needed.

Run: python3 -m unittest discover -s tests -v
"""

import io
import json
import logging
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "songdock"))
import dock_paths  # noqa: E402,F401
import subsonic  # noqa: E402
from subsonic import Client, SubsonicError  # noqa: E402

HOME, AWAY = "http://192.168.1.50:4533", "https://music.example.com"
OK = {"subsonic-response": {"status": "ok"}}


class FakeNet:
    """urlopen stand-in: addresses in `down` can't be reached."""

    def __init__(self, down=()):
        self.down, self.opened = set(down), []

    def urlopen(self, url, timeout=None):
        address = url.split("/rest/")[0]
        self.opened.append(address)
        if address in self.down:
            raise urllib.error.URLError("No route to host")
        r = io.BytesIO(json.dumps(OK).encode())
        r.headers = {"Content-Type": "application/json"}
        return r


class FallbackTests(unittest.TestCase):
    def client(self, net, url=(HOME, AWAY), probe=None):
        patcher = mock.patch.object(subsonic.urllib.request, "urlopen", net.urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        probe = probe or (lambda address: address not in net.down)
        return Client(list(url) if not isinstance(url, str) else url, "jo", "pw", probe=probe)

    def test_one_address_is_never_probed(self):
        probe = mock.Mock()
        c = self.client(FakeNet(), url=HOME, probe=probe)
        c.call("ping")
        probe.assert_not_called()
        self.assertEqual(c.address, HOME)

    def test_first_reachable_address_is_used(self):
        net = FakeNet(down={HOME})                      # away from home
        c = self.client(net)
        c.call("ping")
        c.call("getStarred2")
        self.assertEqual(net.opened, [AWAY, AWAY])     # no slow tries at HOME
        self.assertEqual(c.address, AWAY)

    def test_switches_when_the_address_in_use_stops_answering(self):
        net = FakeNet()
        c = self.client(net)
        c.call("ping")
        net.down.add(HOME)
        hidden = logging.root.manager.disable          # other test files mute logging
        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, hidden)
        with self.assertLogs("subsonic", logging.INFO) as logs:
            c.call("getStarred2")
        self.assertEqual(c.address, AWAY)
        self.assertEqual(net.opened, [HOME, HOME, AWAY])
        self.assertNotIn("t=", " ".join(logs.output))   # addresses only, no tokens

    def test_all_down_raises_a_network_error(self):
        net = FakeNet(down={HOME, AWAY})
        c = self.client(net)
        with self.assertRaises(SubsonicError) as e:
            c.call("ping")
        self.assertTrue(e.exception.network)
        self.assertEqual(c.address, HOME)               # tried again first next time

    def test_a_server_error_answer_is_not_a_reason_to_switch(self):
        net = FakeNet()
        c = self.client(net)
        refused = io.BytesIO(json.dumps({"subsonic-response": {
            "status": "failed", "error": {"code": 40, "message": "Wrong password"}}}).encode())
        refused.headers = {}
        with mock.patch.object(subsonic.urllib.request, "urlopen", return_value=refused):
            with self.assertRaises(SubsonicError) as e:
                c.call("ping")
        self.assertEqual((e.exception.code, c.address), (40, HOME))

    def test_repr_hides_password(self):
        self.assertNotIn("pw", repr(Client([HOME, AWAY], "jo", "pw")))


if __name__ == "__main__":
    unittest.main()
