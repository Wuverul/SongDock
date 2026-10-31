"""Tests for uploading queued plays. Run: python3 -m unittest discover -s tests -v"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dock_plays  # noqa: E402
from dock_record import Record  # noqa: E402
from subsonic import SubsonicError  # noqa: E402

GUID = "000A270000000001"


class FakeScrobbler:
    def __init__(self, reject=(), down_after=None):
        self.reject = set(reject)          # navidrome ids the server rejects
        self.down_after = down_after       # requests before the network "drops"
        self.requests = []
        self.recorded = []                 # (id, time ms) the server accepted

    def call(self, endpoint, **params):
        assert endpoint == "scrobble" and params["submission"] == "true"
        if self.down_after is not None and len(self.requests) >= self.down_after:
            raise SubsonicError("scrobble: can't reach server (timed out)", network=True)
        self.requests.append(params)
        if self.reject & set(params["id"]):
            raise SubsonicError("scrobble: Song not found (code 70)", code=70)
        self.recorded += list(zip(params["id"], params["time"]))
        return {"status": "ok"}


class UploadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rec = Record(os.path.join(tmp.name, "dock.db"))
        self.addCleanup(self.rec.close)

    def queue(self, n, nid=lambda i: f"song{i}"):
        self.rec.queue_plays(GUID, [{"dbid": i, "played_at": 1792100000 + i,
                                     "navidrome_id": nid(i)} for i in range(n)])

    def test_sends_in_batches_with_times_in_ms(self):
        self.queue(30)
        server = FakeScrobbler()
        self.assertEqual(dock_plays.upload_plays(server, self.rec), (30, 0, False))
        self.assertEqual([len(r["id"]) for r in server.requests], [25, 5])
        self.assertEqual(server.recorded[0], ("song0", 1792100000000))
        self.assertEqual(self.rec.unsent_plays(), [])

    def test_network_down_keeps_everything_unsent(self):
        self.queue(30)
        server = FakeScrobbler(down_after=1)       # first batch OK, then offline
        sent, failed, stopped = dock_plays.upload_plays(server, self.rec)
        self.assertEqual((sent, stopped), (25, True))
        left = self.rec.unsent_plays()
        self.assertEqual(len(left), 5)
        self.assertIn("can't reach", left[0]["last_error"])

    def test_one_rejected_play_doesnt_block_the_rest(self):
        self.queue(4)
        server = FakeScrobbler(reject={"song2"})
        sent, failed, stopped = dock_plays.upload_plays(server, self.rec)
        self.assertEqual((sent, failed, stopped), (3, 1, False))
        left = self.rec.unsent_plays()
        self.assertEqual([p["navidrome_id"] for p in left], ["song2"])
        self.assertIn("not found", left[0]["last_error"])

    def test_unmatched_plays_are_not_sent(self):
        self.queue(2, nid=lambda i: None if i == 0 else "song1")
        server = FakeScrobbler()
        self.assertEqual(dock_plays.upload_plays(server, self.rec)[0], 1)
        self.assertEqual(self.rec.unsent_count(), (0, 1))

    def test_nothing_queued_sends_nothing(self):
        server = FakeScrobbler()
        self.assertEqual(dock_plays.upload_plays(server, self.rec), (0, 0, False))
        self.assertEqual(server.requests, [])


class FavoriteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.rec = Record(os.path.join(tmp.name, "dock.db"))
        self.addCleanup(self.rec.close)
        self.rec.note_ratings("G", ["a", "b", "c"], [])
        self.stars = []

    def client(self, fail=None):
        def call(method, id):
            if fail and id in fail:
                raise fail[id]
            self.stars.append((method, id))
        return mock.Mock(call=call)

    def test_sent_once(self):
        self.assertEqual(dock_plays.upload_favorites(self.client(), self.rec), 3)
        self.assertEqual(self.stars[0], ("star", "a"))
        self.assertEqual(dock_plays.upload_favorites(self.client(), self.rec), 0)

    def test_server_down_keeps_the_rest_queued(self):
        c = self.client({"b": SubsonicError("down", network=True)})
        self.assertEqual(dock_plays.upload_favorites(c, self.rec), 1)
        self.assertEqual(self.rec.unsent_favorites(), ["b", "c"])

    def test_song_gone_from_the_server_is_dropped_other_errors_retry(self):
        c = self.client({"a": SubsonicError("gone", code=70), "b": SubsonicError("no", code=50)})
        self.assertEqual(dock_plays.upload_favorites(c, self.rec), 1)
        self.assertEqual(self.rec.unsent_favorites(), ["b"])

    def test_two_players_rating_one_song_favorite_it_once(self):
        self.rec.note_ratings("H", ["a"], [])
        self.assertEqual(dock_plays.upload_favorites(self.client(), self.rec), 3)


class ClientParamTests(unittest.TestCase):
    def test_list_params_repeat(self):
        from subsonic import Client
        url = Client("https://x", "u", "p")._url("scrobble", id=["a", "b"], time=[1, 2])
        self.assertIn("id=a&id=b", url)
        self.assertIn("time=1&time=2", url)


if __name__ == "__main__":
    unittest.main()
