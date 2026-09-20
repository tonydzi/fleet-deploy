# fleet-deploy

You fixed something. It has to land on eleven machines - laptops, a couple of servers, the
box in the other office, the two Macs. You copy it around, you post "please apply this", and
a week later you find out that four of them never did, one applied a half-broken version, and
nobody can tell you which four.

The usual answer is a config management tool, and if you can run one, run one. This is for
the other case: a small heterogeneous fleet, no agent to install, no inventory server, machines
that are asleep half the day - and a change that has to be **provably** on each of them.

The tool is one stdlib-only Python file. The idea it enforces is one sentence:

> **"Sent" is not "done", and silence is not consent.**

A change counts as rolled out on a machine only when a command ran *on that machine* and read
back a **fact** - a hash, a value, a marker. Everything else gets counted in a separate
column, out loud.

Built and running daily at [Palo Alto AI Research Lab](https://github.com/tonydzi/tonydzi),
where a fleet of autonomous agents rolls its own fixes to itself.

```console
$ python fleetdeploy.py board
FLEET ROLLOUT BOARD   2026-08-10T18:13:39Z
rolled out (PROVEN by a machine reading the fact): 1 of 2 node-parcels
still pending:                                     1

!! note-v2                        heartbeat note v2
     wave 0: HUB-1=applied
     wave 1: NODE-2=PENDING

NODES
  HUB-1            last seen 0.0h ago
  NODE-2           last seen 0.0h ago
       behind on: note-v2(pending)

NOT DONE. `done` means every target is applied or explicitly not-for-me.
```

Exit code 1. It stays 1 until the last machine reads the fact back.

## See it work in 30 seconds

```bash
git clone https://github.com/tonydzi/fleet-deploy
cd fleet-deploy
python examples/demo/run_demo.py
```

Two simulated machines in a temp directory, a real (harmless) artifact, and every interesting
moment printed: the second node refusing to jump the queue, the canary proving the change, a
double-apply doing nothing, the board going green. Nothing is installed outside the temp dir.

## The part that matters

The plumbing - copy a file, run a command - is the easy half. Here is the half that cost us
months.

### 1. A verify that reads a flag is not a verify

Our first version had a done-marker that looked **byte-for-byte identical** whether a machine
had verified the fact, a human had said "yes I did it", or someone had passed `--force` and
skipped the check. Four code paths wrote that marker, each with its own wording.

So when we said "rolled out to every machine", we could not defend the number — the split that fixed it is [docs/ROLLOUT-MODEL.md](docs/ROLLOUT-MODEL.md). When we finally
split it, a large share of our historical "applied" turned out to be nothing of the kind.

Now there are two words and they never get added together:

| `applied` | a machine command read a fact on that node and exited 0 |
| `claimed` | somebody says so: prose steps, a hand-confirmation, a no-op check |

`claimed` is legal - work that needs human hands exists, and hiding it would be worse, which [docs/ROLLOUT-MODEL.md](docs/ROLLOUT-MODEL.md) argues at length. It just
does not get to inflate the number, and it never unblocks the next wave.

### 2. An apply that does not deliver the artifact cannot be registered

This is the specific bug that produced **128 stuck parcels** in our fleet, written up in [docs/GOTCHAS.md](docs/GOTCHAS.md). A parcel said the
file would arrive over the sync share. For one node, that share did not exist. The parcel was undeliverable by construction - green at registration, impossible at the far end - and it sat there, along with the next hundred like it, until a node refused to fake a verify; [docs/GOTCHAS.md](docs/GOTCHAS.md) has the whole post-mortem.

Registration now blocks:

* a step that **promises** delivery ("will arrive via sync") while shipping nothing;
* a step that calls a file existing neither locally nor in the payload - the parcel that
  quietly *assumes*;
* a step hardcoding `/home/OPERATOR/...` or `C:\Users\...` while targeting other machines;
* PowerShell aimed at a Linux node, `chmod` aimed at a Windows one - judged by the **OS mix**
  of the targets, because one non-Windows target breaks a PowerShell parcel exactly as badly
  as ten;
* a node name the registry does not know. A typo used to create a queue file that no machine
  ever reads: invisible by construction, and nobody owns the gap.

Every gate in [fleetdeploy.py](fleetdeploy.py) has a named escape hatch, and every escape prints a loud line. A bypass that leaves
no trace brings back the whole class.

### 3. Canary first - "all machines at once" is itself the failure

We spent months building watchdogs against common-mode failures while creating one by hand every time we pushed a change to five machines simultaneously; [docs/ROLLOUT-MODEL.md](docs/ROLLOUT-MODEL.md) is what we replaced that habit with. When it broke, it broke
everywhere at once, and there was no healthy machine left to repair from.

```
wave 0   one canary, SAME class as the real consumers
wave 1   a node of a DIFFERENT class or OS      <- mandatory, not a nicety
wave 2   everybody else
```

A later wave cannot start until the earlier ones are `applied`, and [fleetdeploy.py](fleetdeploy.py) is what refuses to start it. Three details we paid for:

* the canary must match the consumers' class - a headless box is a bad canary for a bug that
  only bites in a desktop session, and the reverse;
* **hence** wave 1 is deliberately a different class: the canary shares the canary's blind
  spots, including its warm cache, so the cold-start surprise lives in wave 1;
* if the change touches a lock, a lease, a shared counter or one database, a single run is
  blind to races - `--concurrent` puts two nodes in wave 0.

And the rollback is named **before** the rollout, at registration, or it is not a rollback - it is an improvisation you will attempt on a broken fleet at 2am, as [docs/ROLLOUT-MODEL.md](docs/ROLLOUT-MODEL.md) puts it.

### 4. A node has the right to say "not mine"

```bash
python fleetdeploy.py not-for-me gpu-driver-bump --reason "no GPU on this box"
```

Without that, unapplicable parcels pile up and the board is permanently red - and a
permanently red board trains its readers to stop looking, which is worse than having no board.
The reason is mandatory in [fleetdeploy.py](fleetdeploy.py): an unexplained refusal is indistinguishable from a machine that quietly stopped applying things.

### 5. "That node is dead" expires

One refused connection once became a line in our notes, and a perfectly healthy machine was
excluded from every rollout for weeks - because the note was the reason nobody retried. When
somebody finally did, it worked on the first attempt in ten seconds.

```bash
python fleetdeploy.py node-down NODE-5 --reason "ssh refused" --recheck "ssh node-5 true"
```

The recheck command is **required** by [fleetdeploy.py](fleetdeploy.py), and the verdict expires in 30 days. After that the board
stops honouring it and tells you to re-measure. A negative result is a measurement taken on
one day, not a property of the world.

## Usage

```bash
python fleetdeploy.py init                       # create the bus + a starter fleet.json
```

Then describe your machines in `fleet-bus/fleet.json` - `class` and `os` are what the canary
picker reasons about:

```json
{
  "nodes": {
    "HUB-1":  {"os": "windows", "class": "desktop",  "aliases": ["hub", "hub-1.local"]},
    "NODE-2": {"os": "linux",   "class": "headless", "aliases": ["vps"]},
    "NODE-3": {"os": "darwin",  "class": "desktop",  "aliases": []}
  }
}
```

Register a parcel on the hub. `install` and `check-file` are built in so you do not have to
guess whether the receiver speaks `cp` or `copy`:

```bash
python fleetdeploy.py register watchdog-v3 \
  --title  "watchdog v3 (fixes the restart loop)" \
  --payload ./dist \
  --apply  '"$PYTHON" "$FLEETDEPLOY" install payload/watchdog.py "$FLEET_HOME/bin/watchdog.py"' \
  --verify '"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/bin/watchdog.py" --md5 4f2b...' \
  --targets all \
  --rollback 'reinstall watchdog v2 from parcels/watchdog-v2/payload'
```

On each machine:

```bash
python fleetdeploy.py status            # what is waiting for me, and what I am waiting on
python fleetdeploy.py apply watchdog-v3
```

And from anywhere:

```bash
python fleetdeploy.py board --html board.html   # who is behind, by name
python fleetdeploy.py audit                     # re-read the fact for things already applied
```

The `audit` subcommand of [fleetdeploy.py](fleetdeploy.py) is not optional in the long run. A marker short-circuits every check forever, so a parcel that was applied and then quietly reverted stays green until something asks again - the trap is named in [docs/GOTCHAS.md](docs/GOTCHAS.md). We
found one live: the marker existed, the file it installed did not.

**`$VAR` and `%VAR%` both work, everywhere.** The engine expands its own placeholders
(`$PARCEL`, `$PAYLOAD`, `$FLEET_HOME`, `$FLEET_NODE`, `$FLEET_BUS`, `$PYTHON`, `$FLEETDEPLOY`)
before handing the line to a shell - because `$X` is not a variable to `cmd.exe` and `%X%` is
not one to `sh`, and a parcel is one string that has to run on both.

## Transport

The engine never opens a socket. The whole system is one directory - the **bus** - and making
that directory look the same everywhere is your existing plumbing's job. Three drivers are
included, all stdlib, all short enough to read:

| [`transports/fileshare.py`](transports/fileshare.py) | you already sync a folder (Syncthing, Dropbox, NFS, SMB) - adds the delivery receipt a share does not give you |
| [`transports/ssh_push.py`](transports/ssh_push.py) | the hub pushes, applies remotely, and **pulls the proof back** |
| [`transports/git_bus.py`](transports/git_bus.py) | the bus is a git repo: history and an audit trail for free |

See [`transports/README.md`](transports/README.md) for the contract if you want to write your
own. One rule is not optional: never `--delete` another node's state directory - you would
erase the evidence that it was already done.

## Install

Copy `fleetdeploy.py` to your machines. That is the install. Python 3.8+, no dependencies, no
daemon, no database, nothing to keep running.

## Docs

* [`docs/ROLLOUT-MODEL.md`](docs/ROLLOUT-MODEL.md) - the four states, what makes a parcel
  provable, the wave rules, exit codes
* [`docs/GOTCHAS.md`](docs/GOTCHAS.md) - 16 dated field notes, each one a bug we shipped first
* [`docs/SECURITY.md`](docs/SECURITY.md) - **read before pointing this at real machines**: a
  parcel is remote code execution, so whoever can write to the bus owns your fleet
* [`PROMPT.md`](PROMPT.md) - paste into Claude Code or Codex and it wires this into your fleet

## Tests

```bash
python tests/test_fleetdeploy.py     # 35 checks, stdlib unittest, no network
```

The header of that file lists two one-line mutations that must turn the grid red. Run them
before you trust it: a suite that cannot fail on broken code proves nothing.

## License

MIT.

---

Part of the kit series by Palo Alto AI Research Lab. Closest neighbours:
[`verified-ops-starter`](https://github.com/tonydzi/verified-ops-starter) - same discipline,
different half of the job: its `rollout` check *asks* whether a fix is present on each box
(hub-side, read-only, no state), while this kit *runs* the rollout - gates, payload, canary
order, per-node markers, refusals. Use that one to audit a fleet you deploy to some other way;
use this one when you want the deploy itself to refuse to lie. Also
[`claude-consensus`](https://github.com/tonydzi/claw-consensus)
(how the machines agree on *what* to roll out before anyone rolls it), and
[`agent-approval-gate`](https://github.com/tonydzi/agent-approval-gate) (when a parcel needs a
human's `+` and nobody is at the terminal). Also
[`telegram-mcp-kit`](https://github.com/tonydzi/telegram-mcp-kit),
[`whatsapp-mcp-kit`](https://github.com/tonydzi/whatsapp-mcp-kit) and
[`mcp-daemon-diet`](https://github.com/tonydzi/mcp-daemon-diet). Questions, or a step that
does not work? Open an issue - we answer within 24h.

---

> **Publishing your own internals?** This repo was sanitized for release with
> [`oss-publish`](https://github.com/tonydzi/oss-publish) - our substitution pipeline:
> personal data is replaced by plausible fakes of the same shape (never `<REDACTED>`),
> and a fail-closed gate re-scans the whole tree before the push. Free, MIT.

---

<!--kits-series:start-->

## 🧰 Connector & Ops Kits

Eight kits, all published 2026-08-10, each lifted out of the same live fleet after it
survived production rather than written as a demo. They are independent: take one, ignore
the rest. All stdlib-only Python, all free.

| kit | what it solves |
|---|---|
| [`telegram-mcp-kit`](https://github.com/tonydzi/telegram-mcp-kit) | Connect your agent to your own Telegram account in ~15 minutes, with the production patches and every gotcha |
| [`whatsapp-mcp-kit`](https://github.com/tonydzi/whatsapp-mcp-kit) | Link WhatsApp, using a live self-refreshing QR page that makes pairing actually work |
| [`mcp-daemon-diet`](https://github.com/tonydzi/mcp-daemon-diet) | One shared MCP daemon per machine instead of a stdio copy in every session, with a watchdog that will not blind your live sessions |
| [`agent-approval-gate`](https://github.com/tonydzi/agent-approval-gate) | Your agent needs a human's OK and nobody is at the terminal: the ask goes to a messenger, the answer comes back into the run |
| [`fleet-deploy`](https://github.com/tonydzi/fleet-deploy) | Roll a fix to N machines and prove it landed on each one: canary waves and a verify that must read a fact back |
| [`secondop-panel`](https://github.com/tonydzi/secondop-panel) | Nobody reviews themselves, and one reviewer model is one blind spot: fan a change out to several model families with quorum and honest skips |
| [`oss-publish`](https://github.com/tonydzi/oss-publish) | Open up internal work without leaking it: plausible substitutions of the same shape, then a fail-closed gate over the whole tree |
| [`llm-spend-audit`](https://github.com/tonydzi/llm-spend-audit) | What your own wiring charges on every session, and which paid subscriptions are going undrawn |

<!--kits-series:end-->

<!--ecosystem-map:start-->

## 🧩 One piece of a working system

This repository is one piece lifted out of a live operation: one non-technical founder, an AI
cofounder, and a fleet of machines that reach consensus with each other and wake the human only
for money or the irreversible. It was extracted after it survived production, not written as a
demo — and it runs on its own: nothing here phones home to the rest.

**See how the whole thing fits together → [SYSTEM.md](https://github.com/tonydzi/tonydzi/blob/main/SYSTEM.md)**

Its closest neighbours in the **fleet** layer: [`claw-consensus`](https://github.com/tonydzi/claw-consensus) · [`claude-mac-patrol`](https://github.com/tonydzi/claude-mac-patrol)

<!--ecosystem-map:end-->

## AI contributors

This project is built by a human + AI team, and the git log says so under the rules in [AI-CONTRIBUTORS.md](https://github.com/tonydzi/.github/blob/main/AI-CONTRIBUTORS.md): Claude writes most of the code, Codex and Grok review it, Gemini feeds the research. Each is credited on a commit
**only if its output changed that commit's content** — no decorative credits. Lab-wide
policy, one source for every repo: [AI-CONTRIBUTORS.md](https://github.com/tonydzi/.github/blob/main/AI-CONTRIBUTORS.md).

<!-- READ-WITH-AI:START (generated by read_with_ai.py - do not hand-edit) -->

### READ THIS WITH AI

One click and an agent reads the repo, pulls out the patterns and helps you apply them to your own work.

<a href="https://chatgpt.com/codex?prompt=Read%20this%20repo%3A%20https%3A%2F%2Fgithub.com%2Ftonydzi%2Ffleet-deploy%20%28%E2%80%9Cfleet-deploy%E2%80%9D%20-%20Roll%20a%20fix%20to%20N%20machines%20and%20prove%20it%20landed%20on%20each%20one%3A%20applied-vs-claimed%20accounting%2C%20canary%20waves%2C%20a%20verify%20that%20must%20read%20a%20fact%2C%20and%20a%20board%20that%20names%20who%20is%20behind.%20Stdlib-only%2C%20MIT%29.%20Work%20out%20what%20problem%20it%20actually%20solves%2C%20pull%20out%20the%20reusable%20patterns%20and%20help%20me%20apply%20them%20to%20my%20own%20setup.%20Start%20by%20asking%20what%20I%20am%20working%20on."><img alt="Codex - open" src="https://img.shields.io/badge/Codex-open-000000?style=for-the-badge&logo=openai&logoColor=white"></a> <a href="https://chatgpt.com/?q=Read%20this%20repo%3A%20https%3A%2F%2Fgithub.com%2Ftonydzi%2Ffleet-deploy%20%28%E2%80%9Cfleet-deploy%E2%80%9D%20-%20Roll%20a%20fix%20to%20N%20machines%20and%20prove%20it%20landed%20on%20each%20one%3A%20applied-vs-claimed%20accounting%2C%20canary%20waves%2C%20a%20verify%20that%20must%20read%20a%20fact%2C%20and%20a%20board%20that%20names%20who%20is%20behind.%20Stdlib-only%2C%20MIT%29.%20Work%20out%20what%20problem%20it%20actually%20solves%2C%20pull%20out%20the%20reusable%20patterns%20and%20help%20me%20apply%20them%20to%20my%20own%20setup.%20Start%20by%20asking%20what%20I%20am%20working%20on."><img alt="ChatGPT - open" src="https://img.shields.io/badge/ChatGPT-open-10a37f?style=for-the-badge&logo=openai&logoColor=white"></a> <a href="https://claude.ai/new?q=Read%20this%20repo%3A%20https%3A%2F%2Fgithub.com%2Ftonydzi%2Ffleet-deploy%20%28%E2%80%9Cfleet-deploy%E2%80%9D%20-%20Roll%20a%20fix%20to%20N%20machines%20and%20prove%20it%20landed%20on%20each%20one%3A%20applied-vs-claimed%20accounting%2C%20canary%20waves%2C%20a%20verify%20that%20must%20read%20a%20fact%2C%20and%20a%20board%20that%20names%20who%20is%20behind.%20Stdlib-only%2C%20MIT%29.%20Work%20out%20what%20problem%20it%20actually%20solves%2C%20pull%20out%20the%20reusable%20patterns%20and%20help%20me%20apply%20them%20to%20my%20own%20setup.%20Start%20by%20asking%20what%20I%20am%20working%20on."><img alt="Claude - open" src="https://img.shields.io/badge/Claude-open-d97757?style=for-the-badge&logo=anthropic&logoColor=white"></a>

<details>
<summary>Copy the prompt (works in any agent: Gemini, Grok, a local model, your own CLI)</summary>

```text
Read this repo: https://github.com/tonydzi/fleet-deploy (“fleet-deploy” - Roll a fix to N machines and prove it landed on each one: applied-vs-claimed accounting, canary waves, a verify that must read a fact, and a board that names who is behind. Stdlib-only, MIT). Work out what problem it actually solves, pull out the reusable patterns and help me apply them to my own setup. Start by asking what I am working on.
```

</details>

<sub>— TonyDzi, Palo Alto AI Research Lab · second brain, agent coordination, persistent memory: github.com/tonydzi</sub>

<!-- READ-WITH-AI:END -->
