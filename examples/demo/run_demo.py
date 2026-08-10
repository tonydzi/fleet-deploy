#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A complete two-node rollout, end to end, in a temp directory. No fleet required.

    python examples/demo/run_demo.py

It creates a bus, registers a real (harmless) parcel that installs a small script, then plays
two machines in turn - HUB-1 and NODE-2 - by setting FLEET_NODE and FLEET_HOME per step. You
will see:

  1. NODE-2 REFUSING to apply, because the canary has not proven it yet;
  2. the canary applying, the fact being read back, and the wave opening;
  3. a second apply on the same node doing nothing (idempotent by construction);
  4. the board going from red to green - and telling you who was behind, by name.

Everything happens under one temp dir, printed at the end so you can look inside.
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FD = os.path.join(ROOT, "fleetdeploy.py")


def run(args, node=None, home=None, bus=None, expect=None):
    env = dict(os.environ)
    env.pop("FLEET_DEPLOY_NOW", None)
    if node:
        env["FLEET_NODE"] = node
    if home:
        env["FLEET_HOME"] = home
    if bus:
        env["FLEET_DEPLOY_BUS"] = bus
    print("\n$ %s %s%s" % ("fleetdeploy.py", " ".join(args), ("   [as %s]" % node) if node else ""))
    p = subprocess.run([sys.executable, FD] + args, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "replace").rstrip()
    print(out)
    if expect is not None and p.returncode != expect:
        print("\n!! expected exit %s, got %s" % (expect, p.returncode))
        sys.exit(1)
    return p.returncode, out


def main():
    tmp = tempfile.mkdtemp(prefix="fleet-demo-")
    bus = os.path.join(tmp, "bus")
    home1, home2 = os.path.join(tmp, "hub1-home"), os.path.join(tmp, "node2-home")
    payload = os.path.join(tmp, "payload")
    os.makedirs(payload)
    os.makedirs(home1)
    os.makedirs(home2)

    # The benign artifact we are rolling out: a tiny script the nodes will run later.
    with open(os.path.join(payload, "heartbeat_note.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env python3\n"
                "# Harmless demo artifact, version 2.\n"
                "print('fleet note v2')\n")

    os.makedirs(bus, exist_ok=True)
    with open(os.path.join(bus, "fleet.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write('{\n  "nodes": {\n'
                '    "HUB-1":  {"os": "%s", "class": "desktop",  "aliases": ["hub"]},\n'
                '    "NODE-2": {"os": "%s", "class": "headless", "aliases": ["vps"]}\n'
                '  }\n}\n' % (_os(), _os()))

    print("=" * 78)
    print("DEMO 1/5  register the parcel on the hub (the gates run here)")
    print("=" * 78)
    run(["register", "note-v2",
         "--title", "heartbeat note v2",
         "--apply", '"$PYTHON" "$FLEETDEPLOY" install payload/heartbeat_note.py "$FLEET_HOME/bin/heartbeat_note.py"',
         "--verify", '"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/bin/heartbeat_note.py" --contains "v2"',
         "--targets", "HUB-1,NODE-2",
         "--payload", payload,
         "--rollback", 'delete $FLEET_HOME/bin/heartbeat_note.py and re-apply note-v1'],
        node="HUB-1", home=home1, bus=bus, expect=0)

    print("\n" + "=" * 78)
    print("DEMO 2/5  NODE-2 tries to jump the queue")
    print("=" * 78)
    run(["apply", "note-v2"], node="NODE-2", home=home2, bus=bus, expect=2)

    print("\n" + "=" * 78)
    print("DEMO 3/5  the canary applies, and the fact is read back")
    print("=" * 78)
    run(["apply", "note-v2"], node="HUB-1", home=home1, bus=bus, expect=0)
    run(["apply", "note-v2"], node="HUB-1", home=home1, bus=bus, expect=0)   # idempotent

    print("\n" + "=" * 78)
    print("DEMO 4/5  board while NODE-2 is still behind (exit 1 on purpose)")
    print("=" * 78)
    run(["board"], node="HUB-1", home=home1, bus=bus, expect=1)

    print("\n" + "=" * 78)
    print("DEMO 5/5  NODE-2 applies; the board goes green")
    print("=" * 78)
    run(["apply", "note-v2"], node="NODE-2", home=home2, bus=bus, expect=0)
    run(["board"], node="HUB-1", home=home1, bus=bus, expect=0)

    installed = os.path.join(home2, "bin", "heartbeat_note.py")
    print("\nthe artifact really is on node 2: %s -> %s"
          % (installed, open(installed, encoding="utf-8").read().strip().splitlines()[-1]))
    print("\nbus and both node homes are under: %s" % tmp)
    print("(nothing was installed outside that directory; delete it whenever you like)")
    if "--clean" in sys.argv:
        shutil.rmtree(tmp, ignore_errors=True)
        print("cleaned up.")
    return 0


def _os():
    import platform
    return {"Windows": "windows", "Darwin": "darwin"}.get(platform.system(), "linux")


if __name__ == "__main__":
    sys.exit(main())
