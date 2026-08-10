# The rollout model

Four states, one rule about proof, and an order that is not negotiable when the change is
risky. That is the entire model. It is short on purpose - a rollout discipline that needs a
manual is a rollout discipline people route around.

## 1. Four states, and only one of them counts

| state | what it means | counts as rolled out |
|---|---|---|
| `applied` | a machine command read a **fact** on that node and exited 0 | **yes** |
| `claimed` | somebody says it is done: prose steps, a hand-confirmation, a no-op check | no |
| `pending` | no marker at all - including "the node never answered" | no |
| `not-for-me` | the node explicitly declined, **with a reason on the record** | not counted as behind |

The board prints `applied` and `claimed` on separate lines and never adds them up. When we
introduced that split in our own fleet, a number we had been quoting for weeks - "rolled out
to every machine" - turned out to be mostly `claimed`. Nothing had broken that day; the
number had just never been measured.

**Why the status is computed, not stored.** `applied` is derived every time from the parcel's
own shape plus the marker on disk. Two reasons, both learned:

* a stored flag and a computed one drift apart silently, and then you have two truths;
* our parcels are signed after all fields are set, so writing a new field into an old parcel
  breaks its signature. A pure function applied to the existing records migrated ~1000 of them
  for free, without editing a single file.

## 2. What makes a parcel provable

Checked at registration, from the shape of the steps alone - before anything runs:

```
apply    must be a machine command that DELIVERS the change
verify   must be a machine command whose exit 0 READS THE FACT BACK
```

A step fails this if it is empty, if it is prose (a human instruction - non-ASCII text is
treated as prose, which catches most of them for free), or if it is a no-op with the shape of
a command: `exit 0`, `true`, `:`, `echo ok`, `python -c "pass"`.

That last one matters more than it looks. A gate that asks "is this a command?" is asking
about **form**, and `exit 0` is a perfectly well-formed command that always succeeds. It
passes the gate, stamps the marker, and proves nothing.

Prose parcels are still legal to register - work that needs human hands exists, and pushing
it into an unattended queue would just make it invisible. What changes is not the right to
register: it is the honesty of the total. A prose parcel can only ever reach `claimed`.

**A verify must read a fact, not an intention.** Concretely:

```
bad   verify: "the file should be in place now"        prose
bad   verify: exit 0                                    always green
bad   verify: test -d ~/bin                             the directory existed before, too
good  verify: fleetdeploy.py check-file "$FLEET_HOME/bin/w.py" --md5 4f2b...
good  verify: systemctl --user is-active my-agent
good  verify: python -c "import cfg,sys; sys.exit(0 if cfg.TIMEOUT==30 else 1)"
```

## 3. Registration gates

Each of these exists because we shipped the bug first. They all have a named escape hatch,
and every escape prints a loud line - a silent bypass reintroduces the whole class.

| gate | blocks | escape |
|---|---|---|
| unknown target | a node name `fleet.json` does not know | fix the name / add an alias |
| promised delivery | apply says it "will arrive via sync" but the parcel ships nothing | `--allow-no-payload` |
| unresolvable file | a step calls a file that is neither local nor in the payload | `--allow-missing-files` |
| node-local path | a step hardcodes `/home/OPERATOR/...` or `C:\Users\...` while targeting other machines | `--allow-local-paths` |
| portability | PowerShell for a Linux target, `chmod` for a Windows one | `--allow-nonportable` |
| rollback | a risky parcel with no rollback named | `--risk safe` |

The portability gate fires on the **OS mix of the targets**, not on their number. One
non-Windows target breaks a PowerShell parcel exactly as thoroughly as ten do.

## 4. Waves: the canary is not optional for risky changes

```
wave 0   one canary, SAME class as the real consumers
wave 1   a node of a DIFFERENT class or OS      <- mandatory
wave 2   everybody else
```

Editing every machine at once is, by itself, a common-mode failure: the same change breaks
the whole fleet at the same moment and leaves you no healthy machine to repair from. The
wave gate refuses to let a later wave start until every node in the earlier ones is `applied`
or `not-for-me`. **`claimed` does not open a wave** - the entire point of a canary is that
somebody read the fact.

Three refinements that cost us something to learn:

* **The canary must be the same class as the consumers.** A headless server is a bad canary
  for a change that only bites in a desktop session, and the reverse. The tool picks the
  majority class by default; `--canary NODE` overrides.
* **Hence wave 1 is a different class on purpose.** The canary shares the canary's blind
  spots. So does its warm cache: if the fix depends on state, the second step is where you
  find out what a cold machine does.
* **Concurrency needs two.** If the change touches a lock, a lease, a shared counter or one
  database, a single run is blind to races. `--concurrent` puts two nodes in wave 0.

`--risk safe` collapses the waves into one. Harmless changes should not pay the canary tax -
but "safe" is a claim the author makes on the record, in the manifest.

**A rollback is named before the rollout, or it is not a rollback** - it is an improvisation
you will attempt on a broken fleet at 2am.

## 5. Idempotency, and what happens when it fails

`apply` runs `verify` **first**. If the fact is already true, the apply step is skipped
entirely and the marker is written. That makes a double apply safe without requiring every
author to write an idempotent command, which most will not.

Then:

| outcome | what is recorded |
|---|---|
| apply exits non-zero | **nothing.** A failed rollout must look failed. The rollback is printed. |
| apply ok, verify non-zero | **nothing** - plus the interesting message. Either the command did not do what it says, or the check reads the wrong place. |
| apply ok, verify ok | `applied` (or `claimed` if the parcel was never provable), with the first 400 chars of what verify printed |

## 6. Silence, absence and death

* A node that never reported has **no** heartbeat file. The board prints "never reported" and
  treats it as behind. Absence of a footprint is a signal, not an empty cell.
* A node can decline with `not-for-me --reason "..."`. Without that, unapplicable parcels pile
  up and the board is permanently red - and a permanently red board trains its readers to stop
  looking, which is worse than having no board.
* A node that is genuinely unreachable gets `node-down`, which **requires** a recheck command
  and expires after 30 days. An undated verdict is not a fact, it is a rumour that outlives
  its evidence: one refused connection once got a healthy machine excluded from every rollout
  for weeks, because nothing in the system ever asked again.

## 7. Exit codes

| command | 0 | 1 | 2 |
|---|---|---|---|
| `register` | registered | - | a gate blocked it |
| `apply` | applied / already in place / claimed | apply or verify failed | refused (wave, identity, payload, signature) |
| `board` | every target applied or not-for-me | somebody is pending or claimed | - |
| `audit` | no drift | drift found | - |

`board` returning 1 is the normal state of an honest fleet mid-rollout. Wire it into CI or a
watchdog and let it be red until the last node reads the fact back.
