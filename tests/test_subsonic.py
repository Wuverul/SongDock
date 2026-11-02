"""Tests for the Subsonic client's fallback addresses. No network needed.

Run: python3 -m unittest discover -s tests -v
"""

import io
import json
import logging
import socket
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
    def setUp(self):
        patcher = mock.patch.object(subsonic.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_a_short_outage_is_tried_again(self):
        net = FakeNet(down=[HOME])
        client = self.client(net, HOME)

        def back_up(seconds):
            net.down = set()
        self.sleep.side_effect = back_up
        self.assertEqual(client.call("getAlbum", id="x")["status"], "ok")
        self.sleep.assert_called_once_with(subsonic.RETRY_WAITS[0])

    def test_gives_up_after_the_last_wait(self):
        client = self.client(FakeNet(down=[HOME]), HOME)
        with self.assertRaises(SubsonicError) as caught:
            client.call("getAlbum", id="x")
        self.assertTrue(caught.exception.network)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list],
                         list(subsonic.RETRY_WAITS))

    def test_sending_is_never_repeated(self):
        client = self.client(FakeNet(down=[HOME]), HOME)
        for endpoint in ("scrobble", "star"):
            with self.assertRaises(SubsonicError):
                client.call(endpoint, id="x")
        self.sleep.assert_not_called()

    def test_a_timeout_isnt_tried_again(self):
        client = self.client(FakeNet(down=[HOME]), HOME)
        with mock.patch.object(subsonic.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaises(SubsonicError):
                client.call("getAlbum", id="x")
        self.sleep.assert_not_called()

    def test_a_slow_failed_name_lookup_is_still_tried_again(self):
        lookups = iter([urllib.error.URLError(socket.gaierror(-3, "Temporary failure in "
                                                               "name resolution"))])

        def urlopen(url, timeout=None):
            for error in lookups:
                raise error
            return FakeNet().urlopen(url)
        with mock.patch.object(subsonic.urllib.request, "urlopen", urlopen), \
                mock.patch.object(subsonic.time, "monotonic", side_effect=[0, 11, 20, 20]):
            self.assertEqual(Client(HOME, "jo", "pw").call("getAlbum", id="x")["status"], "ok")
        self.sleep.assert_called_once_with(subsonic.RETRY_WAITS[0])

    def test_an_answer_with_an_error_isnt_tried_again(self):
        refused = io.BytesIO(json.dumps({"subsonic-response": {
            "status": "failed", "error": {"code": 70, "message": "not found"}}}).encode())
        refused.headers = {}
        with mock.patch.object(subsonic.urllib.request, "urlopen", return_value=refused):
            with self.assertRaises(SubsonicError):
                Client(HOME, "jo", "pw").call("getAlbum", id="x")
        self.sleep.assert_not_called()

    def test_repr_hides_password(self):
        self.assertNotIn("pw", repr(Client([HOME, AWAY], "jo", "pw")))


if __name__ == "__main__":
    unittest.main()
