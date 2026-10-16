"""
dock_config.py - loads the dock's config.toml (see config.example.toml).

The real file lives on the Pi at ~/.config/musicdock/config.toml, mode 600.
MUSICDOCK_CONFIG overrides the path (e.g. for a root-run service).
"""

import logging
import os
import re
import tomllib

log = logging.getLogger("dock_config")

DEFAULT_PATH = os.environ.get("MUSICDOCK_CONFIG", "~/.config/musicdock/config.toml")
PLACEHOLDER_PASSWORD = "your-navidrome-password"

DEFAULTS = {
    "sync": {"targets": ["starred"], "cache_dir": "~/musicdock/cache", "max_cache_gb": 20},
    "device": {"type": "stock_ipod", "convert_to": "mp3", "mp3_quality": "v0",
               "aac_bitrate": "256k"},
}


class ConfigError(Exception):
    pass


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
    cfg["server"] = server

    cfg["sync"]["cache_dir"] = os.path.expanduser(cfg["sync"]["cache_dir"])

    # Older configs had a single `target = "starred"`.
    sync_raw = raw.get("sync", {})
    if "target" in sync_raw and "targets" not in sync_raw:
        cfg["sync"]["targets"] = [sync_raw["target"]]
    cfg["sync"].pop("target", None)
    targets = cfg["sync"]["targets"]
    if (not isinstance(targets, list) or not targets
            or not all(t == "starred" or (isinstance(t, str) and t.startswith("playlist:")
                                          and len(t) > len("playlist:"))
                       for t in targets)):
        raise ConfigError(f'{path}: targets must be a list like '
                          f'["starred", "playlist:Road Trip"]')
    if cfg["device"]["type"] not in ("stock_ipod", "rockbox"):
        raise ConfigError(f"{path}: device type must be stock_ipod or rockbox")
    if cfg["device"]["convert_to"] not in ("mp3", "alac", "aac"):
        raise ConfigError(f"{path}: convert_to must be mp3, alac, or aac")
    if not re.fullmatch(r"v[0-9]|(128|160|192|224|256|320)k",
                        str(cfg["device"]["mp3_quality"]).lower()):
        raise ConfigError(f"{path}: mp3_quality must be v0-v9 (variable) or a "
                          "constant rate: 128k, 160k, 192k, 224k, 256k, 320k")
    if cfg["device"]["convert_to"] == "aac":
        log.warning("convert_to = \"aac\" uses ffmpeg's built-in AAC encoder, which "
                    "adds audible artifacts; \"mp3\" is recommended")
    return cfg
