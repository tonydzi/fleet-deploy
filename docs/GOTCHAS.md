# Gotchas

Field notes from running this across a small fleet of machines - Windows desktops, laptops, a
Linux VPS, two Macs - every day. Dates are when we hit the thing, not when we understood it;
the gap between those two is usually the expensive part. Names and paths are placeholders.

---

### 1. "It will arrive via sync" is not a delivery mechanism (2026-07-20)

A parcel's instructions said the file would arrive over our sync share. That share did not
reach one of the nodes at all. The parcel was **undeliverable by construction**: registration
was green, the rail did not exist, and the only reason we found out was one node refusing to
fake a verify.

**Fix:** if a parcel promises delivery, it must carry the payload. The gate reads the apply
text for delivery claims and blocks when nothing is shipped.

---

### 2. Eight of thirteen stuck parcels were prose (2026-07-30)

A queue audit on one laptop: 13 parcels not applied, some 8 days old. Only 5 could ever have
applied themselves. The other 8 had apply steps like "read APPLY-2026-07-26.md" or "copy file
X" - instructions for a human. They were waiting for a live session that nobody was going to
open, and they aged silently while the automation ran daily and found nothing to do.

**Fix:** prose is legal, but it can never be counted as rolled out, and unattended apply
never touches it. The dishonest number was the bug, not the prose.

---

### 3. `exit 0` passes every gate that checks the shape of a command (2026-07-30)

Our first gate asked "is this a machine command?" - `exit 0`, `true`, `python -c "pass"` all
answer yes. They stamp the marker and prove nothing. You cannot check semantics, but you can
close the observed lazy pattern with a list.

**Fix:** an explicit no-op list, and one copy of it. We had the list in two files for a week;
they had already diverged when we noticed.

---

### 4. A parcel carrying the author's own paths (2026-07-29)

A fix authored on one laptop hardcoded that laptop's data root in both steps. On every other
machine `apply` died with FileNotFoundError and `verify` died right after it. The sender saw
a green registration, the receivers saw a red command, and the parcel would have sat "not
applied" forever while the work itself was perfectly correct.

**Fix:** the node-local path gate, plus placeholders (`$FLEET_HOME`, `$PARCEL`, `$PAYLOAD`)
that each node fills in with its own values.

---

### 5. A Windows command sent to macOS nodes hangs the board forever (2026-08-03)

Three parcels in one day, all with valid signatures and correct intent, all carrying
PowerShell. The receivers did the work by hand, but `verify` could never return 0, so no
marker was written and those nodes showed as laggards indefinitely. **A false laggard costs
more than a real one**: you chase a machine that is already fine.

The first version of the gate only fired when a parcel went to more than one node. Wrong: one
non-Windows target breaks the parcel exactly as badly as ten. Fire on the OS mix.

---

### 6. Identity decided the filename, and a typo made a parcel invisible (2026-07-30)

The early design kept one queue file per node name. A misspelled target created
`PENDING-<misspelling>.jsonl` - a file that no node ever reads, because every node reads its
own canonical name. The parcel was invisible by construction. Third occurrence of the same
class that month: the first was letter case, the second was a sync tool splitting one file
into two conflict copies.

**Fix:** targets live inside the parcel; node identity resolves against a registry with
aliases; an unresolvable identity is a hard stop, never a guess. A node that guesses its own
name writes its proof into somebody else's column.

---

### 7. The parcel that assumes the file is already there (2026-08-04)

Two parcels sat in a queue calling scripts that existed on **no** rail: not locally, not in
any payload. They passed every "did you promise delivery?" check, because they promised
nothing - they just assumed. Both were marked safe for unattended apply, so on an armed node
they raised the same alarm every single day.

That is the real damage: an alarm that is always red teaches its reader that red means
nothing. The next real failure arrives into a channel nobody reads.

**Fix:** if a step calls a file, prove it exists somewhere - locally or in the payload.

---

### 8. Every marker looked identical, so "rolled out to N machines" was not a fact (2026-08-06)

The done-marker was byte-for-byte the same whether a machine verified the fact, a human said
"I did it", or someone passed `--force` and skipped verification entirely. Four different
code paths wrote it, each with its own wording. We could not reconstruct, after the fact,
which of our rollouts had ever been proven.

**Fix:** one writer, one machine-readable first line, and the `applied` / `claimed` split.
When we ran it retroactively, a large share of our historical "applied" turned out to be
`claimed`.

---

### 9. A marker short-circuits the check forever (2026-07-20)

A parcel was marked applied on a Mac. The file it installed did not exist. Something had
reverted it - a reinstall, most likely - and nothing ever asked again, because the marker
answered the question first.

**Fix:** `audit` re-runs verify for everything already marked applied. Run it weekly. Drift
is not rare.

---

### 10. Rolling the whole fleet at once is itself the failure mode (2026-08-06)

We were watching for common-mode failures in our watchdogs while creating one by hand every
time we pushed a change to five machines simultaneously. There was no healthy machine left to
repair from.

Three refinements, each from a different reviewer, each right:
the canary must be the **same class** as the consumers (a headless box is a bad canary for a
desktop-session bug); therefore wave 1 must be a **different** class, because the canary
shares the canary's blind spots; and if the change touches a **concurrent** place - a lock, a
lease, a shared counter - one run is blind to races, so wave 0 takes two nodes.

Also: the canary has a warm cache. The second step is where you learn what a cold machine
does.

---

### 11. "That node is dead" outlived its evidence (2026-08-06)

One refused connection became a line in our notes saying a machine was unavailable. Nobody
retried for weeks - the note was the reason not to. When somebody finally did, it worked on
the first attempt, in ten seconds.

**Fix:** a verdict needs a date, a one-command recheck, and a 30-day expiry. After that the
board stops honouring it and asks you to re-measure. A negative result is a measurement taken
on one day, not a property of the world.

---

### 12. Generating parcel files on Windows with PowerShell (2026-08-10)

`Out-File` and `>` in PowerShell 5.1 write a BOM and CRLF line endings. Patch files break
(`git apply` refuses them), shell scripts break, and `.ps1` files without a BOM get read as
ANSI so any non-ASCII character turns to mojibake - or, inside code, into a parse error.

**Fix:** write files from Python with `newline="\n"`, or from a POSIX shell redirect. If a
repo ships patches, add `.gitattributes` with `*.patch -text`, or a Windows clone will
checkout CRLF patches that will not apply on any machine.

---

### 13. `$VAR` and `%VAR%` are each wrong on half the fleet

`$PARCEL` is not a variable to `cmd.exe`; `%PARCEL%` is not one to `sh`. A parcel is a single
string that has to run on both, so the engine expands its own placeholders **before** handing
the line to a shell. Consequence worth knowing: inside a parcel step, those names are
substituted by us, not by your shell - and a `%SOMETHING%` we do not recognise is still
flagged as Windows-only syntax, on purpose.

---

### 14. A space in someone's home directory (Windows, always)

`$FLEET_HOME` on a machine owned by "First Last" is `C:\Users\<First Last>`. Unquoted, the shell
splits it in two and the parcel fails on exactly the machines whose owner has a space in their
name - which is most of them, on Windows. Always write `"$FLEET_HOME/bin/x.py"`, with the
quotes. Registration prints a note when it sees an unquoted placeholder; it does not block,
because sometimes you mean it.

---

### 15. `echo ok` was on the no-op list; `echo hello` was not (2026-08-10)

Found by an outside review panel on the day we published this. Both always exit 0 and prove
exactly the same amount - nothing - but only one of them was blocked from counting as proof.
A hand-written list of no-ops is a list of the ones you thought of.

**Fix:** a bare `echo`/`printf` with no pipe, redirect or chain is trivial regardless of what
it prints. Same review also pointed out that two operators marking two *different* machines
unreachable in the same minute could drop one verdict, because verdicts shared one JSON file
and that is a read-modify-write. Verdicts are now one file per node - a whole class removed by
choosing a filename.

---

### 16. The counter that measures the wrong thing

Our first parity board counted parcels *sent*. It looked healthy for weeks. Counting nodes
that had *read the fact back* moved the same number from "fine" to "one node has been behind
for six days". Pick the metric that can embarrass you.
