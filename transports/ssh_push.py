#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ssh/rsync transport: the hub pushes the bus to a node, optionally applies there, pulls the
proof back.

    python transports/ssh_push.py --node NODE-2 --host ops@10.0.0.12 \
        --bus ./fleet-bus --remote-bus '~/fleet-bus' --parcel note-v2

What it does, in order:

    1. push  fleet.json + parcels/  + fleetdeploy.py   ->  the node
    2. run   `python fleetdeploy.py apply <parcel>`     on the node   (only with --parcel)
    3. pull  state/<node>/  back                        <-  the node

Step 3 is the whole point. Step 2 printing "APPLIED" on the remote terminal is not proof you
have; the marker file is. If the connection dies between 2 and 3, the board stays red - which
is correct, because from here you genuinely do not know.

Needs `ssh` and `rsync` on PATH (rsync also on the node). No Python libraries. Keys and host
verification are yours to manage; this script deliberately does not disable them.
"""

import argparse
import os
import shlex
import subprocess
import sys


def sh(cmd, dry=False):
    print("  $ " + " ".join(shlex.quote(c) for c in cmd))
    if dry:
        return 0
    return subprocess.call(cmd)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--node", required=True, help="canonical node name, as in fleet.json")
    p.add_argument("--host", required=True, help="user@host for ssh")
    p.add_argument("--bus", default=os.environ.get("FLEET_DEPLOY_BUS", "fleet-bus"))
    p.add_argument("--remote-bus", default="~/fleet-bus")
    p.add_argument("--remote-python", default="python3")
    p.add_argument("--parcel", help="apply this parcel on the node after pushing")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    bus = os.path.abspath(a.bus)
    engine = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleetdeploy.py")
    rb = a.remote_bus.rstrip("/")

    print("[ssh_push] 1/3 push parcels + engine -> %s" % a.host)
    rc = sh(["ssh", a.host, "mkdir -p %s/parcels %s/state" % (rb, rb)], a.dry_run)
    if rc:
        return rc
    # parcels/ may use --delete (the hub owns them); state/ NEVER may - deleting another
    # node's marker erases the proof that it was already done and turns the board red again.
    for src, dst, extra in ((os.path.join(bus, "fleet.json"), rb + "/", []),
                            (os.path.join(bus, "parcels") + "/", rb + "/parcels/", ["--delete"]),
                            (engine, rb + "/", [])):
        rc = sh(["rsync", "-az"] + extra + [src, "%s:%s" % (a.host, dst)], a.dry_run)
        if rc:
            return rc

    if a.parcel:
        print("[ssh_push] 2/3 apply %s on %s" % (a.parcel, a.node))
        remote = ("cd %s && FLEET_NODE=%s FLEET_DEPLOY_BUS=%s %s fleetdeploy.py apply %s"
                  % (rb, shlex.quote(a.node), rb, a.remote_python, shlex.quote(a.parcel)))
        rc_apply = sh(["ssh", a.host, remote], a.dry_run)
    else:
        rc_apply = 0

    print("[ssh_push] 3/3 pull the proof back")
    os.makedirs(os.path.join(bus, "state", a.node), exist_ok=True)
    rc = sh(["rsync", "-az", "%s:%s/state/%s/" % (a.host, rb, a.node),
             os.path.join(bus, "state", a.node) + os.sep], a.dry_run)
    if rc:
        print("[ssh_push] the apply may have succeeded, but the proof did not come back.")
        print("           The board will keep showing this node as pending. That is honest:")
        print("           from here, you do not know. Re-run this script.")
        return rc
    if rc_apply:
        print("[ssh_push] remote apply exited %s - see the output above" % rc_apply)
    return rc_apply


if __name__ == "__main__":
    sys.exit(main())
