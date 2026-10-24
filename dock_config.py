"""
dock_config.py - loads the dock's config.toml (see config.example.toml).

The real file lives on the Pi at ~/.config/musicdock/config.toml, mode 600.
MUSICDOCK_CONFIG overrides the path (e.g. for a root-run service).
"""

import json
import logging
import os
import re
import tomllib

import dock_genres

log = logging.getLogger("dock_config")

DEFAULT_PATH = os.environ.get("MUSICDOCK_CONFIG", "~/.config/musicdock/config.toml")
PLACEHOLDER_PASSWORD = "your-navidrome-password"

DEFAULTS = {
    "sync": {"targets": ["starred"], "favorites_playlist": "Favorites",
             "playlist_tag": "#ipod", "workers": "auto", "removal_grace_days": 7,
             "cache_dir": "~/musicdock/cache", "max_cache_gb": 20,
             "check_interval_minutes": 5,
             # Concurrent batches: on (2026-10-23) after the memory fixes -
             # verified on a Zero 2 W; single-core Pis skip it by themselves.
             "sync_batch_size": 25},
    "device": {"type": "stock_ipod", "convert_to": "mp3", "mp3_quality": "v0",
               "aac_bitrate": "256k", "write": False, "auto_resync": True},
    "display": {"theme": "", "invert": False},     # theme "" = follow invert (older configs)
    "genres": {"unmatched": dock_genres.DEFAULT_UNMATCHED,
               "groups": dock_genres.DEFAULT_GROUPS},
}


class ConfigError(Exception):
    pass


def expand_home(value, config_path):
    """Expand a leading ~ to the home of the config file's owner, not the
    current user's: the sync job runs as root, whose ~ is /root."""
    if not value.startswith("~"):
        return value
    try:
        import pwd
        home = pwd.getpwuid(os.stat(config_path).st_uid).pw_dir
    except (KeyError, OSError, ImportError):
        return os.path.expanduser(value)
    return home + value[1:] if value in ("~",) or value.startswith("~/") \
        else os.path.expanduser(value)


def load(path=None):
    path = os.path.expanduser(path or DEFAULT_PATH)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"No config at {path}. Copy config.example.toml there "
                          f"and run: chmod 600 {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None

    if os.stat(path).st_mode & 0o077:
        log.warning("%s is readable by other users; run: chmod 600 %s", path, path)

    cfg = {section: {**defaults, **raw.get(section, {})}
           for section, defaults in DEFAULTS.items()}

    server = raw.get("server", {})
    missing = [k for k in ("url", "username", "password") if not server.get(k)]
    if missing:
        raise ConfigError(f"{path}: [server] is missing {', '.join(missing)}")
    if server["password"] == PLACEHOLDER_PASSWORD:
        raise ConfigError(f"{path}: still has the placeholder password")
    urls = [server["url"]] if isinstance(server["url"], str) else server["url"]
    if not (isinstance(urls, list) and urls and all(
            isinstance(u, str) and re.fullmatch(r"https?://\S+", u) for u in urls)):
        raise ConfigError(f'{path}: [server] url must be an address like '
                          f'"https://music.example.com", or a list of them in the '
                          f'order to try, e.g. ["http://192.168.1.50:4533", '
                          f'"https://music.example.com"]')
    cfg["server"] = server

    cfg["sync"]["cache_dir"] = expand_home(cfg["sync"]["cache_dir"], path)

    # Older configs had a single `target = "starred"`.
    sync_raw = raw.get("sync", {})
    if "target" in sync_raw and "targets" not in sync_raw:
        cfg["sync"]["targets"] = [sync_raw["target"]]
    cfg["sync"].pop("target", None)
    targets = cfg["sync"]["targets"]
    if (not isinstance(targets, list)
            or not all(t == "starred" or (isinstance(t, str) and t.startswith("playlist:")
                                          and len(t) > len("playlist:"))
                       for t in targets)):
        raise ConfigError(f'{path}: targets must be a list like '
                          f'["starred", "playlist:Road Trip"]')
    if not isinstance(cfg["sync"]["playlist_tag"], str) or \
            any(c.isspace() for c in cfg["sync"]["playlist_tag"]):
        raise ConfigError(f'{path}: playlist_tag must be one word, e.g. "#ipod" ("" = off)')
    if not targets and not cfg["sync"]["playlist_tag"]:
        # targets = [] is fine: playlists found by their tag only.
        raise ConfigError(f'{path}: nothing to sync: targets is empty and playlist_tag '
                          f'is "" (set one, e.g. targets = ["starred"])')
    if cfg["device"]["type"] not in ("stock_ipod", "rockbox"):
        raise ConfigError(f"{path}: device type must be stock_ipod or rockbox")
    if not isinstance(cfg["display"]["invert"], bool):
        raise ConfigError(f"{path}: [display] invert must be true or false")
    if cfg["display"]["theme"] not in ("", "light", "dark"):
        raise ConfigError(f'{path}: [display] theme must be "light" or "dark"')
    b = cfg["sync"]["sync_batch_size"]
    if not (isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 1000):
        raise ConfigError(f"{path}: sync_batch_size must be a whole number of tracks "
                          f"from 0 to 1000 (default 25; 0 = only when all are prepared)")
    m = cfg["sync"]["check_interval_minutes"]
    if not (isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 1440):
        raise ConfigError(f"{path}: check_interval_minutes must be a whole number of "
                          f"minutes from 1 to 1440 (default 5)")
    if not isinstance(cfg["device"]["write"], bool):
        raise ConfigError(f"{path}: [device] write must be true or false")
    if not isinstance(cfg["device"]["auto_resync"], bool):
        raise ConfigError(f"{path}: [device] auto_resync must be true or false")
    if cfg["device"]["convert_to"] not in ("mp3", "alac", "aac"):
        raise ConfigError(f"{path}: convert_to must be mp3, alac, or aac")
    if not re.fullmatch(r"v[0-9]|(128|160|192|224|256|320)k",
                        str(cfg["device"]["mp3_quality"]).lower()):
        raise ConfigError(f"{path}: mp3_quality must be v0-v9 (variable) or a "
                          "constant rate: 128k, 160k, 192k, 224k, 256k, 320k")
    g = cfg["sync"]["removal_grace_days"]
    if not (isinstance(g, int) and not isinstance(g, bool) and 0 <= g <= 30):
        raise ConfigError(f"{path}: removal_grace_days must be a whole number from 0 to 30")
    w = cfg["sync"]["workers"]
    if not (w == "auto" or (isinstance(w, int) and not isinstance(w, bool) and w >= 1)):
        raise ConfigError(f'{path}: workers must be "auto" or a whole number >= 1')
    check_genres(cfg["genres"], path)
    if cfg["device"]["convert_to"] == "aac":
        log.warning("convert_to = \"aac\" uses ffmpeg's built-in AAC encoder, which "
                    "adds audible artifacts; \"mp3\" is recommended")
    return cfg


def check_genres(genres, path):
    """[genres]: unmatched is text; groups is a list of {name, match}."""
    if not isinstance(genres["unmatched"], str):
        raise ConfigError(f'{path}: [genres] unmatched must be text, e.g. "Other" '
                          '("" = keep each song\'s first tag)')
    groups = genres["groups"]
    ok = isinstance(groups, list) and all(
        isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"].strip()
        and isinstance(g.get("match"), list) and g["match"]
        and all(isinstance(m, str) and m.strip() for m in g["match"])
        for g in groups)
    if not ok:
        raise ConfigError(f"{path}: each [[genres.groups]] needs a name and a match "
                          f'list, e.g. name = "Rock" / match = ["rock", "punk"]')


# ------------------------------------------------------------------ edits ----
# The dock changes a few settings itself (DockSetup.txt on an iPod). The file
# is edited in place - comments and layout kept - checked by loading it, and
# swapped in whole with its owner and mode 600.

def toml_literal(value):
    """A TOML value for a string, bool, int or list of strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)   # a valid TOML basic string


def _value_end(text, start):
    """Index just past the TOML value starting at text[start] (a string,
    array - possibly over several lines - or bare word)."""
    i, depth, quote = start, 0, None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
                if depth == 0:
                    return i + 1
        elif c in "\"'":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        elif c == "#" and depth:                        # comment inside an array
            i = text.find("\n", i)
            if i < 0:
                return len(text)
            continue
        elif depth == 0 and (c in "#\n" or c.isspace()):
            return i
        i += 1
    return i


def set_value(text, section, key, value):
    """`text` (a config.toml) with [section] key set to `value`."""
    literal = toml_literal(value)
    head = re.search(rf"^\[{re.escape(section)}\][ \t]*(#.*)?$", text, re.M)
    if not head:
        return text.rstrip("\n") + f"\n\n[{section}]\n{key} = {literal}\n"
    nxt = re.search(r"^\[", text[head.end():], re.M)
    end = head.end() + nxt.start() if nxt else len(text)
    line = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=[ \t]*", re.M).search(text, head.end(), end)
    if line:
        stop = _value_end(text, line.end())
        rest = text[stop:]
        pad = (stop - line.end()) - len(literal)       # keep a trailing comment's column
        if pad > 0 and re.match(r"[ \t]+#", rest):
            literal += " " * pad
        return text[:line.end()] + literal + rest
    return text[:head.end()] + f"\n{key} = {literal}" + text[head.end():]


def update(changes, path=None):
    """Apply {(section, key): value} to config.toml. Raises ConfigError (and
    changes nothing) if the result wouldn't load or doesn't say what was
    asked. Returns the loaded new config."""
    path = os.path.expanduser(path or DEFAULT_PATH)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    for (section, key), value in changes.items():
        text = set_value(text, section, key, value)
    st = os.stat(path)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)        # stays the login user's file
        try:
            cfg = load(tmp)
            with open(tmp, "rb") as f:
                raw = tomllib.load(f)
        except ConfigError as e:
            raise ConfigError(str(e).replace(tmp, path)) from None
        wrong = [f"{s}.{k}" for (s, k), v in changes.items() if raw.get(s, {}).get(k) != v]
        if wrong:
            raise ConfigError(f"{path}: couldn't set {', '.join(wrong)}")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return cfg
