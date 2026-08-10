# PROMPT.md — give this to your Claude Code / Codex

Copy everything below the line into a Claude Code (or Codex CLI) session that has permission
to run shell commands, on the machine you want to use as the hub. It will wire rollout-with-
proof into your fleet and prove the wiring works before telling you it is done.

---

You are setting up `fleet-deploy` (https://github.com/tonydzi/fleet-deploy) so that changes I
roll out to my machines are **provably** applied on each one, rather than merely sent. Follow
these steps in order. Do not report success until step 8 passes.

**Step 0 — get the tool.** Clone the repo (or copy `fleetdeploy.py` if it is already here).
It is a single stdlib-only Python file; Python 3.8+ is the only requirement. Confirm with
`python fleetdeploy.py --help`.

**Step 1 — run the demo first, before touching my machines.**
`python examples/demo/run_demo.py`. It plays a two-node rollout in a temp directory. Read the
output. You need to recognise four moments later: a node refusing to start its wave, the
canary proving the change, a second apply doing nothing, and the board flipping to green.
If the demo fails, stop and tell me why - do not proceed.

**Step 2 — inventory my machines.** Ask me for the list, or find it if there is an obvious
source (an ssh config, an inventory file, a sync tool's device list). For each machine you
need: a short stable name, its OS (`windows` / `linux` / `darwin`), and its **class** - I
suggest `desktop` (a logged-in user session) vs `headless` (a server, no GUI, different
failure modes). The class is not decoration: the canary picker uses it to make sure the
second wave does not share the first wave's blind spots. Ask me if you are unsure how to
classify one.

**Step 3 — pick a transport and say why.** The bus is a directory that must eventually look
the same on every machine. Choose ONE and tell me the reason in one sentence:
  * a folder my machines already sync (Syncthing, Dropbox, NFS, SMB) → `transports/fileshare.py`
  * the hub can ssh to the nodes → `transports/ssh_push.py`
  * a private git repo everything can reach → `transports/git_bus.py`
Do not invent a fourth option unless I ask. If I already run a config management tool that
does this properly, say so and stop - do not add a second one.

**Step 4 — create the bus.** `python fleetdeploy.py init`, then write the real
`fleet-bus/fleet.json` from step 2. Every node needs `os`, `class` and any `aliases` it might
be known by (hostname variations, case differences). Identity is fail-closed: a machine whose
name does not resolve refuses to do anything, which is the correct behaviour and will bite you
in step 6 if you skip aliases.

**Step 5 — pick a genuinely harmless first parcel.** Not the fix I actually care about. A
marker file, a config comment, a small script with no side effects. The first rollout is a
test of the pipeline, not of the change. Register it with a real `--payload`, a machine
`--apply` (use the built-in `install` verb) and a machine `--verify` that reads a fact (use
`check-file --md5`). Include a `--rollback` in plain words.

CRITICAL: `--verify` must read a FACT. `exit 0`, `true`, or "the file should be there now" all
pass a naive check and prove nothing - the engine will accept the parcel but will refuse to
count it as rolled out, and will tell you so. If you find yourself writing a verify you cannot
imagine failing, that is the signal you wrote the wrong verify.

**Step 6 — canary.** Apply on ONE machine (the tool picks the wave; do not use `--force-wave`).
Then run `python fleetdeploy.py board` and show me the output. Then apply on a machine of a
DIFFERENT class or OS. Only then the rest. If any node is unreachable, do not skip it silently -
either record `not-for-me --reason "..."` if the parcel genuinely does not apply there, or
`node-down <node> --reason "..." --recheck "<command>"` if it is unreachable, which expires in
30 days on purpose.

**Step 7 — prove the transport moves.** This is the step people skip. A share that quietly
stopped syncing looks exactly like a fleet where nobody has applied anything. Run the
transport's own check (`fileshare.py check`, or a round trip with `ssh_push.py`, or
`git_bus.py sync` on two machines) and show me that a marker written on one machine is
readable on another.

**Step 8 — the acceptance test.** `python fleetdeploy.py board` must show every target as
`applied` (not `claimed`) or explicitly `not-for-me`, and must exit 0. Paste the output.
Then break it on purpose once: delete the installed file on one machine and run
`python fleetdeploy.py audit` there - it must report DRIFT and exit 1. A rollout system that
cannot detect a reverted change is a rollout system that lies to you slowly.

**Step 9 — make it recurring.** Add two things to whatever scheduler I use (cron, Task
Scheduler, systemd timers):
  * `board` on the hub, daily - and route its non-zero exit somewhere a human sees;
  * `audit` on each node, weekly.
Tell me the exact entries you added and where the logs go. Do not leave them for me to write.

**Report back with:** the transport you chose and why, the fleet.json you wrote, the parcel id
of the test rollout, the final board output, the drift test result, and the schedule entries.
Flag anything you had to bypass (`--allow-*`, `--force-wave`, `--confirm`) with the reason -
those are recorded on the board anyway, so tell me now rather than let me find them.

Read `docs/SECURITY.md` before step 4 and tell me in one line who can currently write to the
bus, because that is exactly the set of people who can run commands on all of my machines.
