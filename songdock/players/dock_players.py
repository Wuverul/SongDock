#!/usr/bin/env python3
"""
dock_players.py - the iPods the dock knows: list them, forget old ones.

    musicdock-players                           list them
    musicdock-players forget "Jo's iPod (nano)"
    musicdock-players forget AB12               (or the end of its ID)
    musicdock-players keep "Jo's iPod (nano)"

The same can be done without SSH from a DockSetup.txt on a docked iPod
(dock_setup.py); every write sync lists the known iPods in MediaDock.txt.

Forgetting retires a player you no longer use (e.g. a nano replaced by a
classic). While the dock remembers it, songs count as "on the player" only
if *every* player has them, so an old, smaller iPod stops the cache from
making room and a bigger one never fills. A forgotten player:
  * no longer holds songs in the cache, and isn't the iPod DOCK READY and
    musicdock-status report on;
  * keeps its history and any queued plays (still sent to Navidrome);
  * comes back by itself if it's docked again (or with keep).
Nothing on any iPod changes.
"""

import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dock_paths  # noqa: E402

import argparse
import re
import sys
from datetime import datetime

from dock_record import Record


def model_name(model):
    """'iPod Nano 4th Gen' for 'xB903' (pygpod's table), else the number."""
    if not model:
        return "model unknown"
    try:
        import warnings
        with warnings.catch_warnings():      # pygpod warns about optional pyusb
            warnings.simplefilter("ignore")
            from pygpod.device.models import get_generation_name, lookup_model
        gen = lookup_model(model).generation
        if gen.name != "UNKNOWN":
            return get_generation_name(gen)
    except Exception:
        pass
    return model


def label(p):
    """How people see a player: "Jo's iPod (iPod Nano 4th Gen)"."""
    return f"{p.get('name') or 'Unnamed iPod'} ({model_name(p.get('model'))})"


def when(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def describe(p):
    state = f"forgotten {when(p['forgotten_at'])}" if p.get("forgotten_at") else "active"
    return (f"{label(p)}, ID {p['guid']}: {p['present']} tracks, "
            f"last synced {when(p['last_sync'])}, {state}")


def _plain(text):
    """Names compare without case or curly-apostrophe differences."""
    return re.sub(r"[‘’`]", "'", (text or "").strip()).casefold()


ALL_OTHERS = {"all others", "all other ipods", "others"}


def match(players, ref, this_guid=None):
    """The players `ref` names, as typed by a person: a name with an
    optional model hint ("Jo's iPod (nano)"), the end of an ID ("AB12"),
    or "all others" (every player except `this_guid`).
    Returns (players, None) or ([], why not)."""
    ref = (ref or "").strip()
    if not ref:
        return [], "No iPod named"
    if _plain(ref) in ALL_OTHERS:
        others = [p for p in players if p["guid"] != this_guid]
        return (others, None) if others else ([], "The dock knows no other iPod")
    name, hint = ref, ""
    m = re.fullmatch(r"(.*?)\s*\(([^()]*)\)", ref)
    if m and m.group(1):
        name, hint = m.group(1), m.group(2)
    found = [p for p in players if _plain(p.get("name")) == _plain(name)]
    if hint:
        found = [p for p in found if _plain(hint) in _plain(model_name(p.get("model")))
                 or p["guid"].upper().endswith(hint.strip().upper())]
    if not found and re.fullmatch(r"(0x)?[0-9A-Fa-f]{4,16}", ref):
        tail = ref.upper().removeprefix("0X")
        found = [p for p in players if p["guid"].upper().endswith(tail)]
    if not found:
        return [], f"No iPod called {ref!r}"
    if len(found) > 1:
        return [], f"{len(found)} called {name!r}: add (model)"
    return found, None


def forget(record, players, ref, this_guid=None):
    """Forget the players `ref` names. Returns (messages, error)."""
    found, why = match(players, ref, this_guid)
    if why:
        return [], why
    if any(p["guid"] == this_guid for p in found):
        return [], "Can't forget the iPod being synced"
    done = []
    for p in found:
        if p.get("forgotten_at"):
            done.append(f"Already forgotten: {label(p)}")
        else:
            record.forget_player(p["guid"])
            done.append(f"Forgot {label(p)}")
    return done, None


def keep(record, players, ref, this_guid=None):
    """Bring forgotten players back. Returns (messages, error)."""
    found, why = match(players, ref, this_guid)
    if why:
        return [], why
    done = []
    for p in found:
        if p.get("forgotten_at"):
            record.remember_player(p["guid"])
            done.append(f"Brought back {label(p)}")
        else:
            done.append(f"Already active: {label(p)}")
    return done, None


def main(argv=None, record=None):
    p = argparse.ArgumentParser(description="List the iPods the dock knows; forget old ones.")
    sub = p.add_subparsers(dest="command")
    for name, what in (("forget", "retire an iPod you no longer use"),
                       ("keep", "bring a forgotten iPod back")):
        s = sub.add_parser(name, help=what)
        s.add_argument("player", help='its name, e.g. "Jo\'s iPod (nano)", '
                                      'or the end of its ID')
    args = p.parse_args(argv)
    record = record or Record()
    try:
        players = record.players()
        if args.command:
            act = forget if args.command == "forget" else keep
            done, why = act(record, players, args.player)
            if why:
                raise SystemExit(f"{why}. Run musicdock-players to see the iPods.")
            print("\n".join(done))
            if args.command == "forget":
                print("Its songs no longer hold files in the cache; the next prepare run "
                      "(at the next check) can make room for the other iPods. Docking it "
                      "again brings it back.")
            return 0
        if not players:
            print("The dock hasn't synced any iPod yet.")
        for player in players:
            print(describe(player))
        return 0
    finally:
        record.close()


if __name__ == "__main__":
    sys.exit(main())
