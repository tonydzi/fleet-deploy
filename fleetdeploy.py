#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fleetdeploy.py - roll a change out to N machines and prove it landed on each one.

    python fleetdeploy.py register <id> --title T --apply CMD --verify CMD --targets a,b
    python fleetdeploy.py status                     # what is waiting for THIS node
    python fleetdeploy.py apply <id>                 # apply + verify + write the marker
    python fleetdeploy.py not-for-me <id> --reason R # explicit refusal, with a reason
    python fleetdeploy.py board [--html out.html]    # who is behind, by name
    python fleetdeploy.py audit                      # re-read the fact for parcels marked applied
    python fleetdeploy.py node-down <node> --reason R --recheck CMD   # dated, expiring verdict

Stdlib only. No daemon, no database, no network calls of its own: the state lives in one
directory (the "bus"), and moving that directory between machines is the transport's job
(see transports/). The engine is deliberately ignorant of how the bytes travel.

The whole point of this file is one sentence: **"sent" is not "done", and silence is not
consent.** A parcel counts as rolled out on a node only when a machine command read a FACT
on that node - a hash, a value, a marker - and came back zero. Everything else is called
`claimed` and is counted separately, out loud.
"""

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone

VERSION = "1.0.0"
VERDICT_TTL_DAYS = 30          # a "node is dead" verdict expires; see node-down / board
VERIFY_TIMEOUT = 180
HEARTBEAT_STALE_HOURS = 24


# --------------------------------------------------------------------------------------
# time, paths, identity
# --------------------------------------------------------------------------------------

def now():
    """UTC now, overridable with FLEET_DEPLOY_NOW (ISO 8601) so tests can time-travel
    instead of sleeping. Tests that sleep are tests people delete."""
    raw = os.environ.get("FLEET_DEPLOY_NOW")
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def bus_root(args=None):
    """The one directory that is the whole system. --bus > FLEET_DEPLOY_BUS > ./fleet-bus."""
    if args is not None and getattr(args, "bus", None):
        return os.path.abspath(args.bus)
    return os.path.abspath(os.environ.get("FLEET_DEPLOY_BUS") or "fleet-bus")


def parcel_dir(bus, pid):
    return os.path.join(bus, "parcels", pid)


def manifest_path(bus, pid):
    return os.path.join(parcel_dir(bus, pid), "manifest.json")


def state_dir(bus, node):
    return os.path.join(bus, "state", node)


def marker_path(bus, node, pid):
    return os.path.join(state_dir(bus, node), pid + ".json")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)          # atomic: a half-written marker is a lie about state
    return path


def registry(bus):
    """fleet.json: {"nodes": {"HUB-1": {"os": "windows", "class": "desktop",
    "aliases": ["hub"]}}}. Absent registry is fatal for anything that names a node - see
    resolve_node()."""
    return read_json(os.path.join(bus, "fleet.json"), {"nodes": {}}) or {"nodes": {}}


def resolve_node(bus, name):
    """Canonical node key for any spelling in the registry, else None.

    FAIL CLOSED ON IDENTITY. Our earlier design kept one queue file per node name, so a
    misspelled target silently created a queue file that no node ever reads: the parcel was
    invisible by construction and nobody owned the gap. Targets now live inside the parcel
    and every name is resolved here or refused."""
    if not name:
        return None
    nodes = registry(bus)["nodes"]
    low = name.strip().lower()
    for key, meta in nodes.items():
        if key.lower() == low:
            return key
        for alias in (meta or {}).get("aliases", []):
            if str(alias).lower() == low:
                return key
    return None


def me(bus):
    """This node's canonical key. FLEET_NODE wins, otherwise the hostname is looked up in the
    registry. Unresolvable identity is a hard stop, never a guess: a node that guesses its own
    name writes its proof into somebody else's column."""
    raw = os.environ.get("FLEET_NODE") or socket.gethostname()
    node = resolve_node(bus, raw)
    if not node:
        die("this machine (%r) is not in %s.\n"
            "    Add it to \"nodes\" (or list this spelling under \"aliases\"), or set FLEET_NODE\n"
            "    to a name that is already there. Guessing an identity is how proof lands in the\n"
            "    wrong column." % (raw, os.path.join(bus, "fleet.json")))
    return node


def die(msg, code=2):
    sys.stderr.write("[fleetdeploy] " + msg + "\n")
    sys.exit(code)


# --------------------------------------------------------------------------------------
# what counts as proof
# --------------------------------------------------------------------------------------

APPLIED, CLAIMED, PENDING, NOT_FOR_ME = "applied", "claimed", "pending", "not-for-me"

CMD_HEADS = {
    "python", "python3", "py", "sh", "bash", "zsh", "cmd", "powershell", "pwsh", "node",
    "npm", "npx", "git", "cp", "copy", "mv", "move", "rsync", "scp", "curl", "wget", "test",
    "grep", "findstr", "md5sum", "shasum", "certutil", "systemctl", "launchctl", "schtasks",
    "chmod", "mkdir", "ln", "tar", "unzip", "pip", "uv", "cat", "type", "ssh", "docker",
}
CMD_EXTS = {".py", ".sh", ".ps1", ".cmd", ".bat", ".js", ".rb", ".pl", ".exe"}

# Steps that have the SHAPE of a command and the CONTENT of nothing: they always exit 0.
# Left unguarded, they turn the rollout board green without touching a single machine.
TRIVIAL = {
    "exit 0", "true", ":", "rem", "cd .", "echo ok", "echo done", "echo",
    'python -c "pass"', "python -c 'pass'", 'python3 -c "pass"', "python3 -c 'pass'",
    "nop", "noop",
}


def step_kind(s):
    """'' | 'cmd' (a machine can run it) | 'note' (prose for a human, never executed)."""
    s = (s or "").strip()
    if not s:
        return ""
    if any(ord(c) > 127 for c in s):
        # Non-ASCII in a step is prose in someone's language, not a command line. This one
        # heuristic caught most of our human-instruction parcels for free.
        return "note"
    head = s.split()[0].strip("\"'")
    base = os.path.basename(head).lower()
    if base in CMD_HEADS or os.path.splitext(base)[1] in CMD_EXTS:
        return "cmd"
    if head.startswith("$") or head.startswith("./") or "/" in head or "\\" in head:
        return "cmd"
    return "note"


def trivial_step(s):
    """A step that always exits 0 and checks nothing.

    The literal list is not enough, and an outside reviewer found the hole: `echo ok` was on
    it, `echo hello` was not, and both prove exactly as much. So any bare `echo`/`printf` is
    trivial - unless it is part of a real command line (a pipe, a redirect, a chain), where
    the echo is feeding something that might actually do work."""
    v = (s or "").strip().strip(";").lower()
    if v in TRIVIAL or v.replace(" ", "") in {t.replace(" ", "") for t in TRIVIAL}:
        return True
    head = v.split()[0] if v.split() else ""
    if head in ("echo", "printf") and not any(c in v for c in "|>&;`$("):
        return True
    return False


def provable(man):
    """Can this parcel EVER reach `applied` -> (bool, reason_if_not).

    Judged from the shape of the steps alone, before anything runs. `apply` must be a machine
    command that delivers the thing; `verify` must be a machine command that reads a fact
    back. A parcel that fails here is still legal to register - human-hands work exists - it
    just can never be counted as rolled out."""
    ap, ve = (man.get("apply") or "").strip(), (man.get("verify") or "").strip()
    if not ap:
        return False, "apply is empty - nothing delivers the change"
    if step_kind(ap) != "cmd":
        return False, "apply is prose - a human has to do it"
    if trivial_step(ap):
        return False, "apply is a no-op that always exits 0"
    if not ve:
        return False, "verify is empty - nothing reads the fact back"
    if step_kind(ve) != "cmd":
        return False, "verify is prose - it reads intent, not fact"
    if trivial_step(ve):
        return False, "verify is a no-op that always exits 0"
    return True, ""


def node_status(bus, node, man):
    """Honest status of one parcel on one node.

    `applied` needs BOTH: the parcel is provable by construction AND the marker on disk did
    not admit it was hand-waved. A marker alone is not proof - that is exactly how "rolled
    out to five machines" becomes a number nobody can defend."""
    m = read_json(marker_path(bus, node, man["id"]))
    if not m:
        return PENDING
    st = m.get("status")
    if st == NOT_FOR_ME:
        return NOT_FOR_ME
    if st == CLAIMED:
        return CLAIMED
    ok, _ = provable(man)
    return APPLIED if ok else CLAIMED


# --------------------------------------------------------------------------------------
# registration gates - every one of these is a bug we shipped first
# --------------------------------------------------------------------------------------

_DELIVERY_CLAIM = re.compile(
    r"will\s+arrive|arrives\s+via|will\s+be\s+synced|syncs?\s+over|"
    r"(?:syncthing|dropbox|onedrive|icloud|rsync|nfs)\s+(?:will\s+)?(?:deliver|bring|copy)|"
    r"already\s+on\s+(?:the|every)\s+(?:node|machine)", re.I)

_NODE_LOCAL = re.compile(
    r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"']+"
    r"|/Users/[^\s\"'/]+/[^\s\"']*"
    r"|/home/[^\s\"'/]+/[^\s\"']*"
    # A DRIVE letter is exactly one character. Without the look-behind this alternative also
    # matched the tail of any URL - `https://host/p` contains `s://host/p` - and a parcel that
    # downloaded something was blocked as if it carried a personal path.
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"']+")

_WINDOWS_ONLY = re.compile(
    r"(?:^|[\s|&;(])(?:powershell|pwsh|cmd\s*/c|certutil|schtasks|robocopy|reg\s+add|del\s+/)"
    r"|%[A-Za-z_][A-Za-z0-9_]*%"
    r"|\.(?:ps1|cmd|bat)(?:\s|$|\")")
_POSIX_ONLY = re.compile(
    r"(?:^|[\s|&;(])(?:chmod|chown|sudo|launchctl|systemctl|ln\s+-s|/bin/sh|/usr/bin/)"
    r"|\.sh(?:\s|$|\")")

# Variables the runner fills in on every node, in either spelling. See expand_vars().
PLACEHOLDERS = ("PARCEL", "PAYLOAD", "FLEET_BUS", "FLEET_NODE", "FLEET_HOME", "FLEETDEPLOY", "PYTHON")

# Only EXECUTABLE references are gated. Data files were in this list at first, and the gate
# then blocked the most normal parcel there is: a verify that names the file the apply is about
# to create. A gate that fires on correct input teaches people to pass --allow-missing-files,
# and then it protects nothing. The trailing boundary keeps `data.json` from matching `.js`.
_SCRIPT_REF = re.compile(
    r"""(?:^|[\s"'=])((?:\$\{?\w+\}?|~|\.{1,2})?[-\w./\\$@{}]*\.(?:py|sh|ps1|cmd|bat|js))(?![A-Za-z0-9])""")

_REDIRECT = re.compile(r"\d?>>?\s*\S+")
_CREATING = {"cp", "copy", "mv", "move", "install", "tee", "mkdir", "curl", "wget"}


def node_local_paths(text):
    """Absolute paths that only exist on the author's machine.

    Live failure this came from: a parcel authored on one laptop carried that laptop's data
    root in both steps. On every other machine `apply` died with FileNotFoundError, `verify`
    died right after it, and the parcel sat "not applied" forever while the work itself was
    fine. The sender saw a green registration; the receivers saw a red command; nobody owned
    the gap. Use the environment placeholders instead - each node fills in its own."""
    hits = []
    for m in _NODE_LOCAL.finditer(text or ""):
        hit = m.group(0)
        if hit.startswith("$") or "$" in hit.split(os.sep)[0]:
            continue
        hits.append(hit)
    return sorted(set(hits))


def portability_problems(steps, target_oses):
    """Commands that cannot run on some target's OS.

    Sender-side on purpose: fixing it here costs one line, fixing it after the fact costs one
    divergence per receiver. And the divergence is the expensive kind - the change gets applied
    by hand, `verify` still cannot return 0, and the node hangs on the board as a FALSE
    laggard. The gate fires on the OS MIX of the targets, not on their count: one non-Windows
    target breaks a PowerShell parcel exactly as badly as ten."""
    # Our own placeholders are expanded by expand_vars() before any shell sees them, so
    # `%FLEET_HOME%` is NOT Windows-only syntax here - it is portable by construction. Strip
    # them first, or the gate blocks the very form it recommends. Any OTHER `%VAR%` really is
    # a cmd.exe-ism and stays flagged.
    txt = "\n".join(s for s in steps if s)
    for name in PLACEHOLDERS:
        txt = txt.replace("%" + name + "%", "$" + name)
    problems = []
    if {"linux", "darwin", "macos", "posix"} & set(target_oses):
        m = _WINDOWS_ONLY.search(txt)
        if m:
            problems.append("Windows-only syntax %r, but a POSIX node is a target" % m.group(0).strip())
    if "windows" in target_oses:
        m = _POSIX_ONLY.search(txt)
        if m:
            problems.append("POSIX-only syntax %r, but a Windows node is a target" % m.group(0).strip())
    return problems


def _drop_created(step):
    """Remove the paths a step CREATES rather than depends on, before hunting for missing
    files. `python build.py > out.py` does not depend on out.py, and `cp SRC DST` does not
    depend on DST. A false block teaches people to set the bypass variable, which costs more
    than the class it prevents."""
    s = _REDIRECT.sub(" ", step or "")
    toks = s.split()
    if toks and os.path.basename(toks[0].strip("\"'")).lower() in _CREATING:
        paths = [t for t in toks[1:] if not t.startswith("-")]
        if len(paths) > 1:
            last = paths[-1]
            i = s.rfind(last)
            s = s[:i] + " " + s[i + len(last):]
    return s


def unresolvable_files(steps, payload):
    """Files a step invokes that exist NOWHERE we can look: not on this machine, not in the
    parcel payload.

    This is the parcel that merely ASSUMES the file is already there. It passes every other
    gate, ships, and then fails on each receiver forever - and on a node with unattended apply
    it raises the same alarm every day until the alarm means nothing."""
    missing = []
    payload = payload or os.devnull
    for step in steps:
        for raw in _SCRIPT_REF.findall(_drop_created(step or "")):
            ref = raw.strip("\"'")
            if not ref or ref.startswith("-"):
                continue
            if "$" in ref or "%" in ref or "~" in ref:
                continue           # resolves per node; unresolvable here is not missing
            local = os.path.expanduser(ref)
            if os.path.exists(local):
                continue
            tail = ref.replace("\\", "/").lstrip("./")
            hit = False
            for dirpath, _d, files in os.walk(payload):
                for f in files:
                    p = os.path.join(dirpath, f).replace("\\", "/")
                    if p.endswith("/" + tail) or f == os.path.basename(tail):
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                missing.append(ref)
    return sorted(set(missing))


# --------------------------------------------------------------------------------------
# canary waves
# --------------------------------------------------------------------------------------

def plan_waves(bus, targets, canary=None, concurrent=False, risk="risky"):
    """Split the target list into rollout waves.

    Editing the whole fleet in one shot is, by itself, a common-mode failure: the same change
    breaks every machine at the same moment and there is no healthy one left to repair from.
    So the order is fixed:

        wave 0  one canary OF THE SAME CLASS as the real consumers
        wave 1  a node of a DIFFERENT class or OS  (mandatory - the canary shares the
                canary's blind spots, including its warm state)
        wave 2  everybody else

    Two extra rules learned the hard way:
      * if the change touches a CONCURRENT place - a lock, a lease, a shared counter, one
        database - wave 0 takes TWO nodes, because a single run is blind to races;
      * `--risk safe` collapses this into one wave. Harmless changes should not pay the
        canary tax, but "safe" is a claim the author makes on the record."""
    targets = list(targets)
    if risk == "safe" or len(targets) <= 1:
        return [targets]
    nodes = registry(bus)["nodes"]

    def klass(n):
        meta = nodes.get(n) or {}
        return (meta.get("class") or "unknown", meta.get("os") or "unknown")

    if canary:
        first = [canary]
    else:
        # the majority class is where the consumers actually live
        counts = {}
        for n in targets:
            counts[klass(n)] = counts.get(klass(n), 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
        first = [n for n in targets if klass(n) == top][:1]
    rest = [n for n in targets if n not in first]
    if concurrent and rest:
        same = [n for n in rest if klass(n) == klass(first[0])]
        if same:
            first.append(same[0])
            rest = [n for n in rest if n != same[0]]
    if not rest:
        return [first]
    other = [n for n in rest if klass(n) != klass(first[0])]
    second = [other[0]] if other else [rest[0]]
    tail = [n for n in rest if n not in second]
    waves = [first, second]
    if tail:
        waves.append(tail)
    return waves


def wave_of(man, node):
    for i, w in enumerate(man.get("waves") or [man.get("targets", [])]):
        if node in w:
            return i
    return None


def wave_blockers(bus, man, node):
    """Nodes in earlier waves that have not PROVEN the parcel yet. `claimed` does not unblock
    a wave: the whole reason the canary exists is that somebody read the fact."""
    idx = wave_of(man, node)
    if idx is None:
        return []
    blockers = []
    for w in (man.get("waves") or [])[:idx]:
        for n in w:
            st = node_status(bus, n, man)
            if st not in (APPLIED, NOT_FOR_ME):
                blockers.append((n, st))
    return blockers


# --------------------------------------------------------------------------------------
# signing (optional, stdlib HMAC)
# --------------------------------------------------------------------------------------

def _sig_key():
    return (os.environ.get("FLEET_DEPLOY_HMAC_KEY") or "").encode("utf-8")


def canonical(man):
    core = {k: man.get(k) for k in ("id", "title", "apply", "verify", "targets", "waves",
                                    "payload", "rollback", "risk")}
    return json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign(man):
    key = _sig_key()
    if not key:
        return None
    return "hmac-sha256:" + hmac.new(key, canonical(man), hashlib.sha256).hexdigest()


def signature_ok(man):
    """(ok, reason). No key configured -> unsigned parcels are accepted and SECURITY.md tells
    you what you are trusting instead. Key configured -> unsigned or mismatched is refused,
    because a parcel is remote code execution with extra steps."""
    key = _sig_key()
    if not key:
        return True, "no FLEET_DEPLOY_HMAC_KEY set - bus is trusted as-is"
    got = man.get("sig")
    if not got:
        return False, "parcel is unsigned and this node requires signatures"
    want = sign(man)
    if not hmac.compare_digest(str(got), str(want)):
        return False, "signature does not match the parcel contents (tampered or re-edited after signing)"
    return True, "signature ok"


# --------------------------------------------------------------------------------------
# running steps
# --------------------------------------------------------------------------------------

def expand_vars(cmd, env):
    """Expand $VAR / ${VAR} / %VAR% OURSELVES, before handing the string to a shell.

    Not a nicety: `$PARCEL` is not a variable to cmd.exe and `%PARCEL%` is not one to sh, so a
    parcel that has to run on Windows and Linux cannot delegate expansion to the shell. One
    string, expanded here, runs on both."""
    def repl(m):
        name = m.group(1) or m.group(2) or m.group(3)
        return env.get(name, m.group(0))
    return re.sub(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%", repl, cmd or "")


def step_env(bus, pid, node):
    env = dict(os.environ)
    env.update({
        "PARCEL": parcel_dir(bus, pid),
        "PAYLOAD": os.path.join(parcel_dir(bus, pid), "payload"),
        "FLEET_BUS": bus,
        "FLEET_NODE": node,
        # FLEET_HOME is the per-node install root. It defaults to the home directory, but an
        # exported FLEET_HOME wins - that is how a node with a non-standard layout, or a test
        # harness running two nodes on one machine, stays honest without editing parcels.
        "FLEET_HOME": os.environ.get("FLEET_HOME") or os.path.expanduser("~"),
        "FLEETDEPLOY": os.path.abspath(__file__),
        "PYTHON": sys.executable,
    })
    return env


def run_step(cmd, env, timeout=VERIFY_TIMEOUT):
    """-> (exit_code, output). Shell on purpose: parcels are command lines, and pretending
    otherwise would just move the quoting problem into the manifest."""
    line = expand_vars(cmd, env)
    try:
        p = subprocess.run(line, shell=True, env=env, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss" % timeout
    except Exception as ex:                                  # noqa: BLE001 - report, never crash
        return 125, "could not run: %s" % ex


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------

def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_init(args):
    bus = bus_root(args)
    os.makedirs(bus, exist_ok=True)
    fj = os.path.join(bus, "fleet.json")
    if os.path.exists(fj) and not args.force:
        die("%s already exists (use --force to overwrite)" % fj)
    host = socket.gethostname()
    osname = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system(), "linux")
    write_json(fj, {"nodes": {host: {"os": osname, "class": "desktop", "aliases": []}}})
    print("[fleetdeploy] bus at %s" % bus)
    print("  wrote fleet.json with this machine (%s, %s). Add the other nodes - each one needs" % (host, osname))
    print("  \"os\" and \"class\"; the canary picker uses them to choose a SECOND wave that does")
    print("  not share the first one's blind spots.")
    return 0


def cmd_register(args):
    bus = bus_root(args)
    pid = args.id
    if not re.match(r"^[A-Za-z0-9][-_.A-Za-z0-9]{1,63}$", pid):
        die("parcel id must be 2-64 chars of [A-Za-z0-9._-] (it becomes a directory name)")
    if os.path.exists(manifest_path(bus, pid)) and not args.force:
        die("parcel %r already registered (%s). Registering is idempotent by id on purpose - "
            "re-sending the same fix under the same id must not fork the record." % (pid, manifest_path(bus, pid)))
    if args.force:
        # Rewriting a parcel does NOT re-open it: existing markers stay, so nodes that already
        # applied the OLD steps will keep showing green against the NEW ones. Almost always you
        # want a new id. (With signing on, an edit by anyone else is refused outright.)
        done = [n for n in registry(bus)["nodes"] if os.path.exists(marker_path(bus, n, pid))]
        if done:
            sys.stderr.write("[fleetdeploy] WARNING: %s already reported on this id: %s\n"
                             "    Their markers survive this rewrite and will look green against the\n"
                             "    new steps. Use a new parcel id unless you know why you want this.\n"
                             % (", ".join(done), pid))

    nodes = registry(bus)["nodes"]
    if not nodes:
        die("no fleet.json in %s - run `fleetdeploy.py init` first" % bus)
    raw_targets = ["*"] if args.targets.strip() == "all" else [t.strip() for t in args.targets.split(",") if t.strip()]
    if raw_targets == ["*"]:
        targets = sorted(nodes)
    else:
        targets, unknown = [], []
        for t in raw_targets:
            r = resolve_node(bus, t)
            (targets.append(r) if r else unknown.append(t))
        if unknown:
            die("fleet.json does not know %s.\n"
                "    A parcel addressed to a name nobody answers to is invisible by construction:\n"
                "    it can never be applied and never shows as missing. Fix the spelling, or add\n"
                "    it under \"aliases\" of the right node." % ", ".join(repr(u) for u in unknown))
    targets = sorted(set(targets))

    # NOTHING is written to the bus until every gate has passed. The first cut copied the
    # payload first, so a blocked registration left an orphan payload directory behind - and
    # the next attempt at the same id then found its own leftovers and considered the missing
    # file "shipped". A gate that can be satisfied by the debris of a previous failure is not
    # a gate.
    pdir = parcel_dir(bus, pid)
    src = os.path.abspath(args.payload) if args.payload else None
    if src and not os.path.isdir(src):
        die("--payload %s is not a directory" % src)
    if src and not any(files for _d, _s, files in os.walk(src)):
        die("--payload %s is empty - a parcel that ships nothing should not claim to" % src)

    steps = [args.apply, args.verify]
    text = "%s %s %s" % (args.title, args.apply, args.verify)

    # --- gate 1: an apply that promises delivery must actually carry the thing -------------
    claim = _DELIVERY_CLAIM.search(text)
    if claim and not src and not args.allow_no_payload:
        die("the parcel PROMISES delivery (%r) but ships nothing.\n"
            "    This is the exact shape that produced our 128 stuck parcels: registration is\n"
            "    green, the rail does not exist, and the fix never arrives. Put the file in\n"
            "    --payload DIR, or write an apply that fetches it from somewhere real.\n"
            "    Deliberate exception: --allow-no-payload." % claim.group(0))

    # --- gate 2: a step that calls a file nobody can find ---------------------------------
    missing = unresolvable_files(steps, src)
    if missing and not args.allow_missing_files:
        die("apply/verify call files that exist neither here nor in the payload:\n"
            "      %s\n"
            "    Ship them in --payload, or deliver them first and register afterwards.\n"
            "    Deliberate exception (the step creates them): --allow-missing-files"
            % "\n      ".join(missing))

    # --- gate 3: paths that only exist on the author's machine ----------------------------
    author = resolve_node(bus, os.environ.get("FLEET_NODE") or socket.gethostname())
    local = node_local_paths("%s\n%s" % (args.apply, args.verify))
    if local and [t for t in targets if t != author] and not args.allow_local_paths:
        die("apply/verify hardcode a path that exists only on your machine:\n"
            "      %s\n"
            "    Use the placeholders the runner fills in per node: $FLEET_HOME, $PARCEL,\n"
            "    $PAYLOAD, $FLEET_BUS, $PYTHON, $FLEETDEPLOY.\n"
            "    Deliberate exception: --allow-local-paths" % "\n      ".join(local))

    # --- gate 4: a command half the fleet cannot execute ----------------------------------
    oses = {(nodes.get(t) or {}).get("os", "unknown") for t in targets}
    probs = portability_problems(steps, oses)
    if probs and not args.allow_nonportable:
        die("this command cannot run on every target:\n      %s\n"
            "    The receivers will apply it by hand, verify will still not return 0, and they\n"
            "    will sit on the board as FALSE laggards forever.\n"
            "    Deliberate exception (parcel really is OS-specific - then narrow --targets):\n"
            "      --allow-nonportable" % "\n      ".join(probs))
    if probs and args.allow_nonportable:
        sys.stderr.write("[fleetdeploy] WARNING: portability gate bypassed on purpose: %s\n" % "; ".join(probs))

    # --- gate 5: risky rollout without a named rollback -----------------------------------
    risk = "safe" if args.risk == "safe" else "risky"
    if risk == "risky" and not args.rollback:
        die("--rollback is required unless --risk safe.\n"
            "    The rollback is named BEFORE the rollout or it is not a rollback, it is an\n"
            "    improvisation you will attempt on a broken fleet at 2am.\n"
            "    Anything touching autostart, a watchdog, sync, auth or agent config is risky.\n"
            "    In doubt: risky.")

    canary = resolve_node(bus, args.canary) if args.canary else None
    if args.canary and not canary:
        die("--canary %r is not in fleet.json" % args.canary)
    if canary and canary not in targets:
        die("--canary %s is not among the targets" % canary)
    waves = plan_waves(bus, targets, canary=canary, concurrent=args.concurrent, risk=risk)

    # Gates are done: now, and only now, the bus gets written.
    payload = {}
    if src:
        dst = os.path.join(pdir, "payload")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
        for dirpath, _d, files in os.walk(dst):
            for f in files:
                p = os.path.join(dirpath, f)
                payload[os.path.relpath(p, dst).replace("\\", "/")] = md5(p)
    os.makedirs(pdir, exist_ok=True)

    man = {
        "id": pid, "title": args.title, "apply": args.apply, "verify": args.verify,
        "targets": targets, "waves": waves, "risk": risk, "rollback": args.rollback or "",
        "concurrent": bool(args.concurrent), "payload": payload,
        "registered": iso(), "from": os.environ.get("FLEET_NODE") or socket.gethostname(),
        "tool": VERSION,
    }
    s = sign(man)
    if s:
        man["sig"] = s
    write_json(manifest_path(bus, pid), man)

    # Soft warning, not a gate: an unquoted placeholder is fine until a node's home is
    # `C:\Users\<First Last>`, and then the shell splits it into two arguments and the parcel
    # fails on exactly the machines whose owner has a space in their name.
    for m in re.finditer(r"(?<![\"'\w])[$%](?:\{)?(" + "|".join(PLACEHOLDERS) + r")\b", text):
        sys.stderr.write("[fleetdeploy] note: %s is used unquoted. Wrap placeholders in double "
                         "quotes - a node whose path contains a space will otherwise split it.\n"
                         % m.group(0))
        break

    ok, why = provable(man)
    print("[fleetdeploy] registered %s -> %s" % (pid, manifest_path(bus, pid)))
    print("  targets : %s" % ", ".join(targets))
    for i, w in enumerate(waves):
        label = "canary" if i == 0 and len(waves) > 1 else ("second class" if i == 1 else "rest")
        print("  wave %-2d : %-22s %s" % (i, ", ".join(w), "(" + label + ")"))
    print("  rollback: %s" % (man["rollback"] or "(none - declared safe)"))
    if payload:
        print("  payload : %d file(s), md5 recorded" % len(payload))
    if not ok:
        print("  NOTE: this parcel can never be counted as rolled out - %s." % why)
        print("        It will show as `claimed` on the board. That is allowed (human-hands work")
        print("        exists), it just does not get to inflate the number.")
    return 0


def cmd_status(args):
    bus = bus_root(args)
    node = me(bus)
    heartbeat(bus, node)
    rows = []
    for pid in sorted(os.listdir(os.path.join(bus, "parcels"))) if os.path.isdir(os.path.join(bus, "parcels")) else []:
        man = read_json(manifest_path(bus, pid))
        if not man or node not in man.get("targets", []):
            continue
        st = node_status(bus, node, man)
        rows.append((pid, st, man))
    todo = [r for r in rows if r[1] == PENDING]
    print("[fleetdeploy] %s - %d parcel(s) addressed to me, %d waiting" % (node, len(rows), len(todo)))
    for pid, st, man in rows:
        mark = {APPLIED: "OK  ", CLAIMED: "CLM ", PENDING: "TODO", NOT_FOR_ME: "SKIP"}[st]
        line = "  %s %-28s %s" % (mark, pid, (man.get("title") or "")[:60])
        blockers = wave_blockers(bus, man, node) if st == PENDING else []
        if blockers:
            line += "\n       waiting on wave %s: %s" % (
                wave_of(man, node), ", ".join("%s(%s)" % (n, s) for n, s in blockers))
        print(line)
    if todo:
        print("\n  apply one:  python %s apply <id>" % os.path.basename(__file__))
        print("  not mine :  python %s not-for-me <id> --reason \"...\"" % os.path.basename(__file__))
    return 0


def heartbeat(bus, node):
    """Every command a node runs leaves a dated footprint. Absence of a footprint is a
    SIGNAL, not an empty cell: a node that never reports is never green."""
    write_json(os.path.join(state_dir(bus, node), "heartbeat.json"),
               {"node": node, "at": iso(), "tool": VERSION,
                "os": platform.system().lower(), "host": socket.gethostname()})


def cmd_apply(args):
    bus = bus_root(args)
    node = me(bus)
    heartbeat(bus, node)
    man = read_json(manifest_path(bus, args.id))
    if not man:
        die("no parcel %r in %s" % (args.id, bus))
    if node not in man.get("targets", []):
        die("%s is not a target of %s (targets: %s).\n"
            "    Not being addressed is not the same as being behind - the board will not count\n"
            "    you as a laggard." % (node, args.id, ", ".join(man.get("targets", []))))

    ok_sig, why_sig = signature_ok(man)
    if not ok_sig:
        die("refusing %s: %s" % (args.id, why_sig))

    blockers = wave_blockers(bus, man, node)
    if blockers and not args.force_wave:
        die("wave %s cannot start yet - these have not PROVEN it:\n      %s\n"
            "    A canary that nobody verified is just the first casualty. `claimed` does not\n"
            "    unblock a wave; only a machine-read fact does.\n"
            "    Deliberate exception: --force-wave (recorded in the marker and on the board)."
            % (wave_of(man, node), "\n      ".join("%s - %s" % (n, s) for n, s in blockers)))

    env = step_env(bus, args.id, node)

    if man.get("payload"):
        bad = [f for f, want in man["payload"].items()
               if not os.path.exists(os.path.join(env["PAYLOAD"], f))
               or md5(os.path.join(env["PAYLOAD"], f)) != want]
        if bad:
            die("payload has not arrived intact on this node: %s\n"
                "    This is the transport's job, not the parcel's. Do not apply half a payload."
                % ", ".join(bad[:6]))

    if args.confirm:
        # A human says they did it. Recorded, never counted as proof.
        write_json(marker_path(bus, node, args.id),
                   {"id": args.id, "node": node, "status": CLAIMED, "at": iso(),
                    "note": args.confirm, "verify_exit": None, "evidence": "",
                    "wave_forced": bool(args.force_wave)})
        print("[fleetdeploy] %s marked CLAIMED on %s (human confirmation, not proof)." % (args.id, node))
        return 0

    # Idempotency by construction: if the fact is ALREADY true, do not run apply again.
    # This is what makes a double apply safe without asking every author to write an
    # idempotent command - most of them will not.
    pre_code, pre_out = run_step(man["verify"], env) if (man.get("verify") or "").strip() else (1, "")
    if pre_code == 0:
        st = APPLIED if provable(man)[0] else CLAIMED
        write_json(marker_path(bus, node, args.id),
                   {"id": args.id, "node": node, "status": st, "at": iso(),
                    "verify_exit": 0, "verify_cmd": man["verify"], "evidence": pre_out[:400],
                    "note": "verify already passed before apply (nothing to do)",
                    "wave_forced": bool(args.force_wave)})
        print("[fleetdeploy] %s: already in place on %s (verify passed before apply) -> %s"
              % (args.id, node, st.upper()))
        return 0

    if args.dry_run:
        print("[fleetdeploy] DRY RUN on %s" % node)
        print("  apply : %s" % expand_vars(man["apply"], env))
        print("  verify: %s" % expand_vars(man["verify"], env))
        return 0

    code, out = run_step(man["apply"], env)
    print("[fleetdeploy] apply exit %s" % code)
    if out:
        print(indent(out))
    if code != 0:
        print("  apply failed - no marker written. A failed rollout must LOOK failed:")
        print("  rollback for this parcel: %s" % (man.get("rollback") or "(none declared)"))
        return 1

    vcode, vout = run_step(man["verify"], env)
    if vcode != 0:
        print("[fleetdeploy] verify exit %s - NOT applied" % vcode)
        if vout:
            print(indent(vout))
        print("  apply ran and verify still cannot read the fact. That is the interesting case:")
        print("  either the command did not do what it says, or the check reads the wrong place.")
        print("  Nothing is recorded. Silence is not consent, and neither is a green apply.")
        return 1

    st = APPLIED if provable(man)[0] else CLAIMED
    write_json(marker_path(bus, node, args.id),
               {"id": args.id, "node": node, "status": st, "at": iso(),
                "verify_exit": 0, "verify_cmd": man["verify"], "evidence": vout[:400],
                "note": "" if st == APPLIED else provable(man)[1],
                "wave_forced": bool(args.force_wave)})
    print("[fleetdeploy] %s -> %s on %s" % (args.id, st.upper(), node))
    if vout:
        print("  fact read back: %s" % vout.splitlines()[0][:120])
    return 0


def indent(text, pad="    "):
    return "\n".join(pad + l for l in (text or "").splitlines()[:20])


def cmd_not_for_me(args):
    bus = bus_root(args)
    node = me(bus)
    heartbeat(bus, node)
    man = read_json(manifest_path(bus, args.id))
    if not man:
        die("no parcel %r" % args.id)
    if not args.reason:
        die("--reason is required. An unexplained refusal is indistinguishable from a node that "
            "quietly stopped applying things.")
    write_json(marker_path(bus, node, args.id),
               {"id": args.id, "node": node, "status": NOT_FOR_ME, "at": iso(),
                "note": args.reason, "verify_exit": None, "evidence": ""})
    print("[fleetdeploy] %s marked NOT-FOR-ME on %s: %s" % (args.id, node, args.reason))
    print("  It stops counting as a laggard and stays visible on the board with your reason.")
    return 0


def cmd_audit(args):
    """Re-run verify for parcels this node already reported as applied.

    A marker short-circuits everything downstream forever, so a parcel that was marked applied
    and then quietly reverted (a reinstall, a config reset, a colleague's cleanup) stays green
    until somebody re-reads the fact. Found live once: the marker existed, the guarded file did
    not."""
    bus = bus_root(args)
    node = me(bus)
    heartbeat(bus, node)
    drift = 0
    checked = 0
    pdirs = sorted(os.listdir(os.path.join(bus, "parcels"))) if os.path.isdir(os.path.join(bus, "parcels")) else []
    for pid in pdirs:
        man = read_json(manifest_path(bus, pid))
        if not man or node not in man.get("targets", []):
            continue
        if node_status(bus, node, man) != APPLIED:
            continue
        checked += 1
        code, out = run_step(man["verify"], step_env(bus, pid, node))
        if code == 0:
            print("  OK    %s" % pid)
        else:
            drift += 1
            print("  DRIFT %s - marked applied, verify now exits %s" % (pid, code))
            if out:
                print(indent(out, "        "))
    print("[fleetdeploy] audit on %s: %d checked, %d drifted" % (node, checked, drift))
    return 1 if drift else 0


def cmd_node_down(args):
    """Record that a node is unreachable - with a date, a recheck command and an expiry.

    An undated verdict is not a fact, it is a rumour that outlives its evidence. We lost a
    live node to this: one failed connection became "that machine is dead" in somebody's
    notes, and it was excluded from every rollout for weeks while being perfectly fine."""
    bus = bus_root(args)
    node = resolve_node(bus, args.node) or die("unknown node %r" % args.node)
    if not args.recheck:
        die("--recheck is required: the one command that would prove this verdict wrong.\n"
            "    Without it, 'dead' is a rumour with no way back.")
    # One file per subject node, not one shared dictionary. A shared file means a
    # read-modify-write, and two operators marking two different machines down in the same
    # minute would silently drop one of the verdicts - a whole class of bug removed by
    # choosing a filename. (An outside reviewer flagged exactly this race.)
    rec = {"status": "unreachable", "at": iso(), "reason": args.reason or "",
           "recheck": args.recheck, "by": os.environ.get("FLEET_NODE") or socket.gethostname(),
           "expires": iso(now() + timedelta(days=args.ttl))}
    write_json(os.path.join(bus, "verdicts", node + ".json"), rec)
    print("[fleetdeploy] %s marked unreachable until %s (%d days)." % (node, rec["expires"], args.ttl))
    print("  recheck: %s" % args.recheck)
    print("  After that date the board stops honouring this verdict and asks you to re-measure.")
    return 0


def read_verdicts(bus):
    """All "this node is unreachable" verdicts, one file per subject node."""
    out, d = {}, os.path.join(bus, "verdicts")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".json"):
            rec = read_json(os.path.join(d, f))
            if rec:
                out[f[:-5]] = rec
    return out


def board_data(bus):
    parcels = []
    proot = os.path.join(bus, "parcels")
    for pid in sorted(os.listdir(proot)) if os.path.isdir(proot) else []:
        man = read_json(manifest_path(bus, pid))
        if man:
            parcels.append(man)
    nodes = sorted(registry(bus)["nodes"])
    verdicts = read_verdicts(bus)
    rows = []
    for man in parcels:
        cells = {}
        for n in man.get("targets", []):
            cells[n] = node_status(bus, n, man)
        rows.append((man, cells))
    beats = {}
    for n in nodes:
        hb = read_json(os.path.join(state_dir(bus, n), "heartbeat.json"))
        at = parse_iso((hb or {}).get("at"))
        beats[n] = None if not at else round((now() - at).total_seconds() / 3600.0, 1)
    return rows, nodes, beats, verdicts


def cmd_board(args):
    bus = bus_root(args)
    rows, nodes, beats, verdicts = board_data(bus)
    total = applied = claimed = pending = skipped = 0
    behind = {}
    for man, cells in rows:
        for n, st in cells.items():
            total += 1
            if st == APPLIED:
                applied += 1
            elif st == CLAIMED:
                claimed += 1
                behind.setdefault(n, []).append((man["id"], "claimed"))
            elif st == NOT_FOR_ME:
                skipped += 1
            else:
                pending += 1
                behind.setdefault(n, []).append((man["id"], "pending"))

    if args.json:
        print(json.dumps({"parcels": [{"id": m["id"], "title": m.get("title"), "cells": c,
                                       "waves": m.get("waves"), "risk": m.get("risk")}
                                      for m, c in rows],
                          "heartbeat_hours": beats, "verdicts": verdicts,
                          "totals": {"targets": total, "applied": applied, "claimed": claimed,
                                     "pending": pending, "not_for_me": skipped}},
                         ensure_ascii=False, indent=2))
        return 0 if (pending == 0 and claimed == 0) else 1

    print("FLEET ROLLOUT BOARD   %s" % iso())
    print("rolled out (PROVEN by a machine reading the fact): %d of %d node-parcels" % (applied, total))
    if claimed:
        print("claimed but not proven:                            %d   <- not counted as rolled out" % claimed)
    print("still pending:                                     %d" % pending)
    if skipped:
        print("explicitly not-for-me:                             %d" % skipped)
    print("")
    for man, cells in rows:
        flag = "OK " if all(s in (APPLIED, NOT_FOR_ME) for s in cells.values()) else "!! "
        print("%s%-30s %s" % (flag, man["id"], (man.get("title") or "")[:44]))
        for i, w in enumerate(man.get("waves") or []):
            marks = []
            for n in w:
                st = cells.get(n, PENDING)
                marks.append("%s=%s" % (n, {APPLIED: "applied", CLAIMED: "CLAIMED",
                                            PENDING: "PENDING", NOT_FOR_ME: "skip"}[st]))
            print("     wave %d: %s" % (i, "  ".join(marks)))
    print("")
    print("NODES")
    for n in nodes:
        hb = beats.get(n)
        if hb is None:
            note = "never reported  <- absence of a footprint is a signal, not an empty cell"
        elif hb > HEARTBEAT_STALE_HOURS:
            note = "last seen %.0fh ago  <- stale" % hb
        else:
            note = "last seen %.1fh ago" % hb
        v = verdicts.get(n)
        if v:
            exp = parse_iso(v.get("expires"))
            if exp and exp < now():
                note += "  | VERDICT EXPIRED (%s) - re-measure: %s" % (v.get("at", "")[:10], v.get("recheck"))
            else:
                note += "  | unreachable since %s, recheck by %s" % (v.get("at", "")[:10], (v.get("expires") or "")[:10])
        mine = behind.get(n)
        print("  %-16s %s" % (n, note))
        if mine:
            print("       behind on: %s" % ", ".join("%s(%s)" % (i, s) for i, s in mine))
    if args.html:
        write_html(args.html, rows, nodes, beats, verdicts,
                   {"applied": applied, "claimed": claimed, "pending": pending,
                    "not_for_me": skipped, "total": total})
        print("\nhtml board -> %s" % args.html)
    if pending or claimed:
        print("\nNOT DONE. `done` means every target is applied or explicitly not-for-me.")
        return 1
    return 0


def write_html(path, rows, nodes, beats, verdicts, tot):
    css = ("body{font:14px system-ui,sans-serif;margin:24px;color:#111;background:#fff}"
           "table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #ddd;padding:6px 10px;text-align:left}"
           ".ok{background:#e6f4ea}.clm{background:#fff4ce}.pend{background:#fde7e9}.skip{background:#eee;color:#666}"
           "code{background:#f5f5f5;padding:1px 4px}"
           "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}td,th{border-color:#444}"
           ".ok{background:#14351f}.clm{background:#3a3213}.pend{background:#3a1416}.skip{background:#222;color:#999}"
           "code{background:#222}}")
    cls = {APPLIED: "ok", CLAIMED: "clm", PENDING: "pend", NOT_FOR_ME: "skip"}
    out = ["<!doctype html><meta charset=utf-8><title>Fleet rollout board</title><style>%s</style>" % css,
           "<h1>Fleet rollout board</h1>",
           "<p>%s &middot; proven <b>%d</b> / %d &middot; claimed %d &middot; pending %d</p>"
           % (iso(), tot["applied"], tot["total"], tot["claimed"], tot["pending"]),
           "<table><tr><th>parcel</th>" + "".join("<th>%s</th>" % n for n in nodes) + "</tr>"]
    for man, cells in rows:
        out.append("<tr><td><b>%s</b><br><small>%s</small></td>" % (man["id"], (man.get("title") or "")[:60]))
        for n in nodes:
            st = cells.get(n)
            if st is None:
                out.append("<td class=skip>-</td>")
            else:
                out.append("<td class=%s>%s</td>" % (cls[st], st))
        out.append("</tr>")
    out.append("</table><h2>Nodes</h2><table><tr><th>node</th><th>last seen</th><th>verdict</th></tr>")
    for n in nodes:
        hb = beats.get(n)
        seen = "never" if hb is None else "%.1f h ago" % hb
        v = verdicts.get(n)
        vtxt = "-"
        if v:
            exp = parse_iso(v.get("expires"))
            vtxt = ("EXPIRED, re-measure: <code>%s</code>" % v.get("recheck")) if (exp and exp < now()) \
                else "unreachable since %s" % (v.get("at") or "")[:10]
        out.append("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (n, seen, vtxt))
    out.append("</table>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def cmd_install(args):
    """Portable copy, so a parcel does not have to guess whether the receiver speaks `cp` or
    `copy`. Creates parent directories, verifies the copy by hash, prints the hash."""
    src = args.src
    if not os.path.isabs(src):
        src = os.path.join(os.environ.get("PARCEL", "."), src)
    if not os.path.exists(src):
        die("install: no source %s" % src)
    dst = os.path.expanduser(expand_vars(args.dst, dict(os.environ)))
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    shutil.copy2(src, dst)
    h = md5(dst)
    if h != md5(src):
        die("install: copy does not match source hash")
    print("installed %s -> %s  md5=%s" % (os.path.basename(src), dst, h))
    return 0


def cmd_check_file(args):
    """A verify that reads a FACT, portable across the fleet. Exit 0 only if the file is there
    AND satisfies every check you asked for."""
    path = os.path.expanduser(expand_vars(args.path, dict(os.environ)))
    if not os.path.exists(path):
        print("MISSING %s" % path)
        return 1
    if args.md5:
        h = md5(path)
        if h != args.md5:
            print("HASH MISMATCH %s: %s != %s" % (path, h, args.md5))
            return 1
        print("md5 %s %s" % (h, path))
    if args.contains:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                body = f.read()
        except Exception as ex:
            print("UNREADABLE %s: %s" % (path, ex))
            return 1
        if args.contains not in body:
            print("MISSING TEXT %r in %s" % (args.contains, path))
            return 1
        print("contains %r: yes" % args.contains)
    if args.newer_than is not None:
        age_h = (now().timestamp() - os.path.getmtime(path)) / 3600.0
        if age_h > args.newer_than:
            print("STALE %s: %.1fh old, wanted < %sh" % (path, age_h, args.newer_than))
            return 1
        print("fresh %.1fh" % age_h)
    if not (args.md5 or args.contains or args.newer_than is not None):
        print("exists %s" % path)
    return 0


# --------------------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="fleetdeploy.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bus", help="bus directory (default: $FLEET_DEPLOY_BUS or ./fleet-bus)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the bus and a starter fleet.json")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("register", help="register a parcel (runs the gates)")
    s.add_argument("id")
    s.add_argument("--title", required=True)
    s.add_argument("--apply", required=True, help="machine command that DELIVERS the change")
    s.add_argument("--verify", required=True, help="machine command whose exit 0 READS THE FACT")
    s.add_argument("--targets", required=True, help="comma-separated node names, or 'all'")
    s.add_argument("--payload", help="directory of files shipped with the parcel")
    s.add_argument("--rollback", help="how to undo this, named BEFORE the rollout")
    s.add_argument("--risk", choices=["risky", "safe"], default="risky")
    s.add_argument("--canary", help="node to put in wave 0 (default: picked by class)")
    s.add_argument("--concurrent", action="store_true",
                   help="the change touches a lock/lease/shared counter: canary wave takes TWO nodes")
    s.add_argument("--force", action="store_true", help="overwrite an existing parcel id")
    s.add_argument("--allow-no-payload", action="store_true")
    s.add_argument("--allow-missing-files", action="store_true")
    s.add_argument("--allow-local-paths", action="store_true")
    s.add_argument("--allow-nonportable", action="store_true")
    s.set_defaults(func=cmd_register)

    s = sub.add_parser("status", help="what is waiting for this node")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("apply", help="apply + verify + record")
    s.add_argument("id")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force-wave", action="store_true", help="ignore the canary order (recorded)")
    s.add_argument("--confirm", metavar="NOTE",
                   help="a human did it by hand: records CLAIMED, never counts as proof")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("not-for-me", help="explicit refusal with a reason")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_not_for_me)

    s = sub.add_parser("audit", help="re-read the fact for parcels already marked applied")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("board", help="who is behind, by name")
    s.add_argument("--html", metavar="PATH")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("node-down", help="dated, expiring 'unreachable' verdict")
    s.add_argument("node")
    s.add_argument("--reason")
    s.add_argument("--recheck", help="the one command that would prove this wrong")
    s.add_argument("--ttl", type=int, default=VERDICT_TTL_DAYS)
    s.set_defaults(func=cmd_node_down)

    s = sub.add_parser("install", help="portable copy for use inside an apply step")
    s.add_argument("src", help="path, relative to $PARCEL if not absolute")
    s.add_argument("dst")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("check-file", help="portable fact-reading verify")
    s.add_argument("path")
    s.add_argument("--md5")
    s.add_argument("--contains")
    s.add_argument("--newer-than", type=float, metavar="HOURS")
    s.set_defaults(func=cmd_check_file)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
