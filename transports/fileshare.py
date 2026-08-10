#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""File-share transport: the bus lives in a folder that some sync tool already replicates.

    python transports/fileshare.py beat  --bus ./fleet-bus            # I am alive, and my clock
    python transports/fileshare.py check --bus ./fleet-bus --max-age-min 30

`pull` and `push` are no-ops here - the sync tool does them. That is the appeal, and also the
trap: **a folder that stopped syncing looks exactly like a fleet where nobody applied
anything yet.** Both show old files and no news. We could not tell those apart for six days
once, and spent them re-sending fixes that were never going to arrive.

So this driver adds the one thing a shared folder does not give you: a delivery receipt.
Each node drops a token with its own clock reading; `check` reads everybody else's token and
tells you which direction the silence is coming from.

Requires nothing but Python and the folder itself.
"""

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone


def iso(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def node_name(bus):
    """Same identity rule as the engine: FLEET_NODE, else hostname, else refuse to guess."""
    raw = os.environ.get("FLEET_NODE") or socket.gethostname()
    try:
        reg = json.load(open(os.path.join(bus, "fleet.json"), encoding="utf-8"))["nodes"]
    except Exception:
        return raw
    low = raw.lower()
    for key, meta in reg.items():
        if key.lower() == low or low in [str(a).lower() for a in (meta or {}).get("aliases", [])]:
            return key
    sys.exit("[fileshare] %r is not in fleet.json - set FLEET_NODE" % raw)


def beat(args):
    bus = os.path.abspath(args.bus)
    node = node_name(bus)
    d = os.path.join(bus, "state", node)
    os.makedirs(d, exist_ok=True)
    tok = {"node": node, "at": iso(), "host": socket.gethostname()}
    with open(os.path.join(d, "_share_token.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(tok, f, indent=2)
        f.write("\n")
    print("[fileshare] %s token %s" % (node, tok["at"]))
    return 0


def check(args):
    """Exit 1 when some node's token is older than --max-age-min.

    Read the failure carefully before blaming the node: a stale token means the LAST HOP you
    can see is stale. If every token but yours is old, the share is not reaching you; if only
    yours is fresh on the other machines, your writes are not leaving. Same symptom, opposite
    repair."""
    bus = os.path.abspath(args.bus)
    me = node_name(bus)
    reg = json.load(open(os.path.join(bus, "fleet.json"), encoding="utf-8"))["nodes"]
    now = datetime.now(timezone.utc)
    bad = 0
    for n in sorted(reg):
        p = os.path.join(bus, "state", n, "_share_token.json")
        if not os.path.exists(p):
            print("  %-16s no token yet%s" % (n, "  <- this node has never run `beat`"))
            if n != me:
                bad += 1
            continue
        try:
            at = datetime.fromisoformat(json.load(open(p, encoding="utf-8"))["at"].replace("Z", "+00:00"))
        except Exception as ex:
            print("  %-16s unreadable token: %s" % (n, ex))
            bad += 1
            continue
        age = (now - at.astimezone(timezone.utc)).total_seconds() / 60.0
        flag = "STALE" if age > args.max_age_min else "ok   "
        print("  %-16s %s %.0f min old" % (n, flag, age))
        if age > args.max_age_min:
            bad += 1
    print("[fileshare] %d node(s) not reaching this machine" % bad)
    return 1 if bad else 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["beat", "check", "pull", "push"])
    p.add_argument("--bus", default=os.environ.get("FLEET_DEPLOY_BUS", "fleet-bus"))
    p.add_argument("--max-age-min", type=float, default=30.0)
    a = p.parse_args()
    if a.action in ("pull", "push"):
        print("[fileshare] no-op: the sync tool owns delivery. Run `check` to prove it is working.")
        return 0
    return {"beat": beat, "check": check}[a.action](a)


if __name__ == "__main__":
    sys.exit(main())
