"""
subsonic.py - minimal Subsonic API client for Navidrome.

Uses token auth: each request sends md5(password + salt) with a fresh random
salt, never the password itself. Request URLs carry a working token, so they
are never logged or put in error messages.

The server may have several addresses (e.g. a home-network one and a public
one), tried in priority order: before the first request each gets a quick
connection check (PROBE_TIMEOUT), and the first that answers is used; if it
stops answering mid-run, the others are tried. Only addresses are logged.
"""

import hashlib
import json
import logging
import secrets
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("subsonic")
PROBE_TIMEOUT = 5          # seconds: can this address be reached at all?

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


def addresses(url):
    """config [server] url (one address, or a list in priority order) -> list."""
    return [url] if isinstance(url, str) else list(url)


def reachable(address, timeout=PROBE_TIMEOUT):
    """True if a TCP connection to the address's host and port opens."""
    parts = urllib.parse.urlsplit(address)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        socket.create_connection((parts.hostname, port), timeout=timeout).close()
        return True
    except (OSError, ValueError):
        return False


class Client:
    def __init__(self, url, username, password, timeout=60, probe=reachable):
        self.addresses = [u.rstrip("/") for u in addresses(url)]
        self.address = self.addresses[0]      # the one in use
        self.username = username
        self._password = password
        self.timeout = timeout
        self._probe = probe
        self._picked = len(self.addresses) == 1

    @property
    def base(self):
        return self.address + "/rest/"

    def __repr__(self):
        return f"Client({self.base!r}, user={self.username!r})"   # no password

    def _pick(self):
        """Before the first request: the first address that can be reached."""
        self._picked = True
        for address in self.addresses:
            if self._probe(address):
                if address != self.addresses[0]:
                    log.info("Navidrome: %s can't be reached; using %s",
                             self.addresses[0], address)
                self.address = address
                return

    def _url(self, endpoint, base=None, **params):
        salt = secrets.token_hex(8)
        query = {
            "u": self.username, "s": salt,
            "t": hashlib.md5((self._password + salt).encode()).hexdigest(),
            "v": API_VERSION, "c": CLIENT_NAME, "f": "json", **params,
        }
        # doseq: list values repeat the parameter (e.g. several id= for scrobble).
        return (f"{base or self.base}{endpoint}.view?"
                f"{urllib.parse.urlencode(query, doseq=True)}")

    def _open(self, endpoint, **params):
        """Open a request at the address in use; if it can't be reached, at
        the next ones (switching to the first that answers)."""
        if not self._picked:
            self._pick()
        try:
            return self._open_at(self.address, endpoint, **params)
        except SubsonicError as e:
            if not e.network or len(self.addresses) == 1:
                raise
            failed, first_error = self.address, e
        for address in self.addresses:
            if address == failed:
                continue
            try:
                r = self._open_at(address, endpoint, **params)
            except SubsonicError as e:
                if e.network:
                    continue
                raise
            log.info("Navidrome: %s stopped answering; using %s", failed, address)
            self.address = address
            return r
        raise first_error

    def _open_at(self, address, endpoint, **params):
        try:
            return urllib.request.urlopen(self._url(endpoint, address + "/rest/", **params),
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

    def cover_art(self, cover_id, size=None):
        """Image bytes for a coverArt ID (a song's or an album's). Raises
        SubsonicError (code 70) when the server has no such image."""
        params = {"id": cover_id, **({"size": size} if size else {})}
        with self._open("getCoverArt", **params) as r:
            if r.headers.get("Content-Type", "").startswith(("application/json", "text/")):
                self._check("getCoverArt", json.load(r))   # an error, not an image
                raise SubsonicError("getCoverArt: server sent text instead of an image")
            return r.read()

    def download(self, song_id, dest):
        """Save the original (untranscoded) file for song_id to dest."""
        with self._open("download", id=song_id) as r:
            if r.headers.get("Content-Type", "").startswith(("application/json", "text/")):
                self._check("download", json.load(r))   # an error, not audio
                raise SubsonicError("download: server sent text instead of audio")
            with open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 16)
