"""
subsonic.py - minimal Subsonic API client for Navidrome.

Uses token auth: each request sends md5(password + salt) with a fresh random
salt, never the password itself. Request URLs carry a working token, so they
are never logged or put in error messages.
"""

import hashlib
import json
import secrets
import shutil
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = "1.16.1"
CLIENT_NAME = "musicdock"


class SubsonicError(Exception):
    """A failed request. `network` is True when the server couldn't be
    reached (try again later); `code` is the Subsonic error code when the
    server answered with an error (e.g. 70 = not found)."""

    def __init__(self, message, code=None, network=False):
        super().__init__(message)
        self.code = code
        self.network = network


class Client:
    def __init__(self, url, username, password, timeout=60):
        self.base = url.rstrip("/") + "/rest/"
        self.username = username
        self._password = password
        self.timeout = timeout

    def __repr__(self):
        return f"Client({self.base!r}, user={self.username!r})"   # no password

    def _url(self, endpoint, **params):
        salt = secrets.token_hex(8)
        query = {
            "u": self.username, "s": salt,
            "t": hashlib.md5((self._password + salt).encode()).hexdigest(),
            "v": API_VERSION, "c": CLIENT_NAME, "f": "json", **params,
        }
        # doseq: list values repeat the parameter (e.g. several id= for scrobble).
        return f"{self.base}{endpoint}.view?{urllib.parse.urlencode(query, doseq=True)}"

    def _open(self, endpoint, **params):
        try:
            return urllib.request.urlopen(self._url(endpoint, **params),
                                          timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise SubsonicError(f"{endpoint}: HTTP {e.code} {e.reason}",
                                network=e.code >= 500) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise SubsonicError(f"{endpoint}: can't reach server ({reason})",
                                network=True) from None

    @staticmethod
    def _check(endpoint, body):
        resp = body.get("subsonic-response", {})
        if resp.get("status") != "ok":
            err = resp.get("error", {})
            raise SubsonicError(f"{endpoint}: {err.get('message', 'request failed')} "
                                f"(code {err.get('code')})", code=err.get("code"))
        return resp

    def call(self, endpoint, **params):
        """Call an API endpoint and return the parsed subsonic-response."""
        with self._open(endpoint, **params) as r:
            try:
                body = json.load(r)
            except ValueError:
                raise SubsonicError(f"{endpoint}: server sent invalid JSON") from None
        return self._check(endpoint, body)

    def download(self, song_id, dest):
        """Save the original (untranscoded) file for song_id to dest."""
        with self._open("download", id=song_id) as r:
            if r.headers.get("Content-Type", "").startswith(("application/json", "text/")):
                self._check("download", json.load(r))   # an error, not audio
                raise SubsonicError("download: server sent text instead of audio")
            with open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 16)
