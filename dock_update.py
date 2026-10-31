#!/usr/bin/env python3
"""
dock_update.py - install new SongDock releases by itself (user, 2026-10-30).

musicdock-update.timer runs this as root every 15 minutes. It asks GitHub for
the latest release once a day. A higher version is announced for
NOTICE_HOURS on DOCK READY and in musicdock-status, then installed once no
player is connected and no sync or prepare run is going. With a player left
docked it waits: the screen says to unplug it.

A release installs only if its signature checks out against the keys in
pi/release_signers, which arrive with the code already on the dock: a stolen
GitHub account alone can't reach a dock. The version inside the package must
match the release and be higher than this one, so an old signed package can't
be passed off as new. Without a signers file, or with [sync] auto_update =
false, it only says an update is available.

The new code replaces the old folder only after all that, with syncs masked
and the prepare timer stopped. If the installer fails, the old folder and its
units go back. A power cut during the installer is picked up by the next run.

Standard library only: this runs under the system python3.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("dock_update")

LATEST = "https://api.github.com/repos/Wuverul/SongDock/releases/latest"
STATE = os.environ.get("MUSICDOCK_UPDATE_STATE", "/var/lib/musicdock/update.json")
NOTICE_HOURS = 4
CHECK_EVERY = 86400
SIGNER = "songdock-release"             # also the signature's namespace
MAX_DOWNLOAD = 200_000_000
INSTALL_TIMEOUT = 30 * 60
SYNC_UNITS = "musicdock-sync@.service"
PREPARE_TIMER = "musicdock-prepare.timer"


class UpdateError(Exception):
    pass


class BadRelease(UpdateError):
    """This release must not be installed. Other errors are tried again."""


def version_of(text):
    """(0, 7, 1) from "0.7.1", "v0.7.1" or "SongDock 0.7.1 (abc, date)", or None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(n) for n in m.groups()) if m else None


def version_text(version):
    return ".".join(str(n) for n in version)


def current_version(app=HERE):
    try:
        with open(os.path.join(app, "VERSION")) as f:
            return version_of(f.read())
    except OSError:
        return None


def load_state(path=None):
    try:
        with open(path or STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    path = path or STATE
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)              # the prepare job and the report read it
    os.replace(tmp, path)


def note(state, now=None):
    """The line for DOCK READY, or None."""
    version = state.get("version")
    if not version:
        return None
    after = state.get("install_after")
    if not after:
        return f"Update {version} available"
    now = time.time() if now is None else now
    if now >= after:
        return f"Update {version}: when unplugged"
    return "Update {} at {}".format(version, time.strftime("%-I:%M %p", time.localtime(after)))


def auto_update_on(config=None):
    """[sync] auto_update, on unless it says otherwise. A config that can't be
    read says nothing about what its owner wanted, so that's off."""
    config = config or os.environ.get("MUSICDOCK_CONFIG") or os.path.expanduser(
        "~/.config/musicdock/config.toml")
    try:
        with open(config, "rb") as f:
            return tomllib.load(f).get("sync", {}).get("auto_update", True) is True
    except (OSError, ValueError):
        return False


def fetch_latest(url=LATEST, timeout=30):
    """(version, package url, signature url or None) of the latest release."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "SongDock"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    version = version_of(data.get("tag_name"))
    if not version:
        raise UpdateError(f"Release has no version: {data.get('tag_name')!r}")
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", [])}
    name = f"songdock-{version_text(version)}.tar.gz"
    return version, assets.get(name), assets.get(name + ".sig")


def download(url, path, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r, open(path, "wb") as f:
        total = 0
        while chunk := r.read(65536):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise BadRelease(f"Download over {MAX_DOWNLOAD} bytes")
            f.write(chunk)


def signature_ok(package, signature, signers):
    with open(package, "rb") as f:
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers, "-I", SIGNER,
                            "-n", SIGNER, "-s", signature], stdin=f, capture_output=True)
    return r.returncode == 0


def player_connected(usb="/sys/bus/usb/devices"):
    """Any USB storage device, or anything from Apple: an ejected iPod has no
    disk, but it's still plugged in."""
    try:
        names = os.listdir(usb)
    except OSError:
        return False

    def read(name, attr):
        try:
            with open(os.path.join(usb, name, attr)) as f:
                return f.read().strip().lower()
        except OSError:
            return ""
    return any(read(n, "bInterfaceClass") == "08" or read(n, "idVendor") == "05ac"
               for n in names)


def systemctl(*args):
    return subprocess.run(["systemctl", *args], capture_output=True, text=True)


def sync_running():
    r = systemctl("list-units", "--type=service", "--no-legend", "--state=active,activating",
                  "musicdock-sync@*")
    return bool(r.stdout.strip())


def prepare_running():
    return systemctl("is-active", "musicdock-prepare.service").stdout.strip() in (
        "active", "activating")


def plan_unfinished(app=HERE):
    """A prepare run left an interim plan: more of it is on its way."""
    config = os.environ.get("MUSICDOCK_CONFIG") or ""
    try:
        with open(config, "rb") as f:
            cache = tomllib.load(f).get("sync", {}).get("cache_dir", "~/musicdock/cache")
        home = os.path.dirname(os.path.dirname(os.path.dirname(config)))
        cache = cache.replace("~", home, 1)
        with open(os.path.join(cache, "manifest.json")) as f:
            return bool(json.load(f).get("interim"))
    except (OSError, ValueError):
        return False


def blocker():
    """Why now isn't the moment, or None."""
    if player_connected():
        return "a player is connected"
    if sync_running():
        return "a sync is running"
    if prepare_running() or plan_unfinished():
        return "a prepare run isn't finished"
    return None


def draw(*args):
    """Screens are best effort: a broken panel mustn't stop an update."""
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "dock_status.py"), *args],
                       capture_output=True, timeout=120)
    except Exception:
        log.exception("Couldn't update the screen")


def run_installer(app):
    """The installer as if its owner ran it with sudo (it sets up that user)."""
    import pwd
    owner = pwd.getpwuid(os.stat(app).st_uid).pw_name
    r = subprocess.run([os.path.join(app, "pi", "install.sh")],
                       env={**os.environ, "SUDO_USER": owner}, capture_output=True,
                       text=True, timeout=INSTALL_TIMEOUT)
    if r.returncode:
        raise UpdateError(f"Installer failed: {r.stdout[-400:]} {r.stderr[-400:]}")


def unpack(package, folder, want, owner_of):
    """Unpack into `folder`, which ends up holding the release's own files."""
    staging = folder + ".unpack"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    with tarfile.open(package) as tar:
        tar.extractall(staging, filter="data")
    inner = os.path.join(staging, "songdock")
    if not os.path.isfile(os.path.join(inner, "pi", "install.sh")):
        shutil.rmtree(staging)
        raise BadRelease("The package isn't a SongDock release")
    if current_version(inner) != want:
        shutil.rmtree(staging)
        raise BadRelease(f"The package's version isn't {version_text(want)}")
    shutil.rmtree(folder, ignore_errors=True)
    os.rename(inner, folder)
    shutil.rmtree(staging)
    if os.path.exists(os.path.join(folder, "pi", "install.sh")):
        os.chmod(os.path.join(folder, "pi", "install.sh"), 0o755)
    st = os.stat(owner_of)            # the login user, like the folder it replaces
    for root, dirs, files in os.walk(folder):
        for name in [".", *dirs, *files]:
            os.lchown(os.path.join(root, name), st.st_uid, st.st_gid)


def install(state, app=HERE, path=None, get=download, check=signature_ok):
    """Download, verify and swap in the release in `state`. Raises UpdateError."""
    version = version_of(state["version"])
    signers = os.path.join(app, "pi", "release_signers")
    new, old = app + ".new", app + ".old"
    work = app + ".download"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        package, signature = os.path.join(work, "p.tar.gz"), os.path.join(work, "p.sig")
        get(state["package"], package)
        get(state["signature"], signature)
        if not check(package, signature, signers):
            raise BadRelease("Its signature doesn't match this dock's release key")
        unpack(package, new, version, app)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if systemctl("mask", "--runtime", SYNC_UNITS).returncode:
        raise UpdateError("Couldn't hold syncs back for the update")
    systemctl("stop", PREPARE_TIMER)
    try:
        why = blocker()               # things may have started while downloading
        if why:
            raise UpdateError(f"Not now: {why}")
        draw("updating", "--detail", f"To version {state['version']}", "--force")
        shutil.rmtree(old, ignore_errors=True)
        state["installing"] = True
        save_state(state, path)
        os.rename(app, old)
        os.rename(new, app)
        try:
            run_installer(app)
        except Exception:
            os.rename(app, new)
            os.rename(old, app)
            run_installer(app)        # the old code's units again
            raise
    finally:
        systemctl("unmask", "--runtime", SYNC_UNITS)
        systemctl("daemon-reload")
        systemctl("start", PREPARE_TIMER)
        state.pop("installing", None)
        save_state(state, path)


def run(now=None, path=None, app=HERE, fetch=fetch_latest, get=download, check=signature_ok,
        auto=None, blocked=blocker, on_notice=None, installer=None):
    """One timer tick. Returns what it did, for the log and tests."""
    now = time.time() if now is None else now
    state = load_state(path)
    current = current_version(app)
    if current is None:
        return "no version here"      # a development copy: nothing to compare

    if state.get("installing"):       # a power cut during the installer
        log.warning("Finishing the update a restart interrupted")
        run_installer(app)
        systemctl("unmask", "--runtime", SYNC_UNITS)
        systemctl("start", PREPARE_TIMER)
        state.pop("installing")
        save_state(state, path)

    if version_of(state.get("version")) and version_of(state["version"]) <= current:
        for key in ("version", "package", "signature", "install_after", "found_at"):
            state.pop(key, None)      # done, or someone installed it by hand
        save_state(state, path)

    auto = auto_update_on() if auto is None else auto
    signed = os.path.isfile(os.path.join(app, "pi", "release_signers"))
    if now - state.get("checked", 0) >= CHECK_EVERY:
        try:
            version, package, signature = fetch()
        except UpdateError:
            raise
        except Exception as e:
            log.info("Couldn't check for updates (%s); trying again later", e)
            return "offline"
        state["checked"] = now
        if version > current and version_text(version) != state.get("failed"):
            usable = package and signature
            if state.get("version") != version_text(version):
                log.warning("SongDock %s is available (this is %s)", version_text(version),
                            version_text(current))
                state["found_at"] = now
                state.pop("install_after", None)
            state.update(version=version_text(version), package=package, signature=signature)
            if auto and signed and usable and not state.get("install_after"):
                state["install_after"] = now + NOTICE_HOURS * 3600
            save_state(state, path)
            if on_notice:
                on_notice()
        else:
            save_state(state, path)

    # Turned off since it was announced, say: no install.
    if state.get("install_after") and not (auto and signed):
        state.pop("install_after")
        save_state(state, path)
        if on_notice:
            on_notice()
    after = state.get("install_after")
    if not after or now < after:
        return "waiting" if after else "nothing to install"
    why = blocked()
    if why:
        log.info("Update %s waits: %s", state["version"], why)
        return "blocked"
    try:
        (installer or install)(state, app=app, path=path, get=get, check=check)
    except BadRelease as e:
        log.error("Update %s refused: %s", state["version"], e)
        state["failed"] = state["version"]
        state["failed_why"] = str(e)
    except UpdateError as e:
        if str(e).startswith("Not now"):
            return "blocked"
        log.error("Update %s failed, keeping %s: %s", state["version"],
                  version_text(current), e)
        state["failed"] = state["version"]
        state["failed_why"] = str(e)
    except OSError as e:              # the network, usually: the next tick tries again
        log.warning("Update %s couldn't be fetched (%s); trying again", state["version"], e)
        return "retry"
    else:
        log.warning("Updated to SongDock %s", state["version"])
        state.pop("failed", None)
        state.pop("failed_why", None)
        result = "installed"
    if "failed" in state:
        result = "failed"
    for key in ("version", "package", "signature", "install_after", "found_at"):
        state.pop(key, None)
    save_state(state, path)
    return result


def after_update():
    """The screen, and a player docked while syncs were held back."""
    draw("idle", "--force")
    subprocess.run([sys.executable, os.path.join(HERE, "dock_resync.py"), "--after-safe-hours"],
                   capture_output=True)


def main(argv=None):
    p = argparse.ArgumentParser(description="Install new SongDock releases")
    p.add_argument("--check", action="store_true", help="only print the latest release")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.check:
        version, package, signature = fetch_latest()
        print(f"This is {version_text(current_version() or (0, 0, 0))}; latest is "
              f"{version_text(version)}; package {'yes' if package else 'no'}, "
              f"signature {'yes' if signature else 'no'}")
        return 0
    result = run(on_notice=lambda: systemctl("start", "--no-block", "musicdock-prepare.service"))
    if result in ("installed", "failed"):
        after_update()
    return 0


if __name__ == "__main__":
    sys.exit(main())
