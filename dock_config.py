"""
dock_config.py - loads the dock's config.toml (see config.example.toml).

The real file lives on the Pi at ~/.config/musicdock/config.toml, mode 600.
MUSICDOCK_CONFIG overrides the path (e.g. for a root-run service).
"""

import logging
import os
import tomllib

log = logging.getLogger("dock_config")

DEFAULT_PATH = os.environ.get("MUSICDOCK_CONFIG", "~/.config/musicdock/config.toml")
PLACEHOLDER_PASSWORD = "your-navidrome-password"

DEFAULTS = {
    "sync": {"target": "starred", "cache_dir": "~/musicdock/cache", "max_cache_gb": 20},
    "device": {"type": "stock_ipod", "convert_to": "aac", "aac_bitrate": "256k"},
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
    if cfg["device"]["type"] not in ("stock_ipod", "rockbox"):
        raise ConfigError(f"{path}: device type must be stock_ipod or rockbox")
    return cfg
