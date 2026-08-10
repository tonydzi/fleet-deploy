#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git transport: the bus is a git repository every node can push to.

    python transports/git_bus.py sync --bus ./fleet-bus

One command per node, ideally on a timer: pull, commit whatever this node changed, push, and
retry once on a race. You get history for free - who applied what, when, and what the verify
output was at that moment - which is the cheapest audit trail we have found.

Two things to know before you choose this transport:

* **Nodes need write access to the repo.** A parcel is a command that runs on your machines,
  so repo write access is fleet-wide code execution. Use a dedicated repo, protect it, and
  read `docs/SECURITY.md`.
* **Conflicts are real but rare.** Nodes only ever write `state/<their own name>/`, so two
  nodes never touch the same file. The only real conflict is hub-vs-hub on `parcels/`, and
  the retry below handles the ordinary case; anything else should fail loudly rather than be
  auto-merged, because a silently merged parcel is a parcel nobody wrote.
"""

import argparse
import os
import subprocess
import sys


def git(bus, *args, **kw):
    p = subprocess.run(["git", "-C", bus] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace").strip()
    if out and not kw.get("quiet"):
        print("  " + out.replace("\n", "\n  "))
    return p.returncode, out


def sync(bus, message):
    if not os.path.isdir(os.path.join(bus, ".git")):
        sys.exit("[git_bus] %s is not a git repo. `git init`, add a remote, push once." % bus)
    print("[git_bus] pull --rebase")
    git(bus, "pull", "--rebase", "--autostash")
    _, st = git(bus, "status", "--porcelain", quiet=True)
    if not st.strip():
        print("[git_bus] nothing of mine changed")
        return 0
    print("[git_bus] commit")
    git(bus, "add", "-A")
    git(bus, "commit", "-m", message)
    print("[git_bus] push")
    rc, _ = git(bus, "push")
    if rc:
        # Someone else pushed between our pull and our push. Rebase once and try again; if it
        # still fails, stop - an automatic force-push here would delete another node's proof.
        print("[git_bus] push rejected, rebasing once")
        git(bus, "pull", "--rebase", "--autostash")
        rc, _ = git(bus, "push")
        if rc:
            print("[git_bus] still rejected. Resolve by hand; do NOT force-push a bus -")
            print("          you would erase another machine's evidence that it was done.")
    return rc


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", choices=["sync", "pull", "push"])
    p.add_argument("--bus", default=os.environ.get("FLEET_DEPLOY_BUS", "fleet-bus"))
    p.add_argument("--message", default=None)
    a = p.parse_args()
    bus = os.path.abspath(a.bus)
    import socket
    node = os.environ.get("FLEET_NODE") or socket.gethostname()
    if a.action == "pull":
        return git(bus, "pull", "--rebase", "--autostash")[0]
    if a.action == "push":
        return git(bus, "push")[0]
    return sync(bus, a.message or ("fleet-deploy: state from %s" % node))


if __name__ == "__main__":
    sys.exit(main())
