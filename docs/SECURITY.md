# Security

Read this before you point this at machines you care about.

## What a parcel actually is

A parcel is **a command line that runs on every targeted machine**, usually as the user that
owns the agent sessions, the browser profiles and the ssh keys. Everything else in this repo
is bookkeeping around that fact.

So the real access-control question is not "who can run `fleetdeploy.py`". It is:

> **Who can write to the bus?**

Whoever that is can execute code on every node in `fleet.json`. A shared folder that a
colleague can write to, a git repo with loose permissions, an S3 bucket with a wide policy -
each of those is fleet-wide code execution wearing a different hat.

## Signing (optional, and what it does not do)

Set `FLEET_DEPLOY_HMAC_KEY` to the same secret on the hub and on every node:

```bash
export FLEET_DEPLOY_HMAC_KEY='...'      # same value everywhere
```

With a key set, `register` signs the parcel (HMAC-SHA256 over id, title, steps, targets,
waves, payload hashes, rollback, risk) and `apply` **refuses** anything unsigned or altered.
Without a key, unsigned parcels are accepted and the bus is trusted as-is.

What this buys you: a parcel edited in transit, or planted by someone who can write to the
share but does not have the key, will not run.

What it does **not** buy you:

* **Confidentiality.** The bus is plaintext. Anyone who can read it sees every command.
* **Replay protection.** A previously valid parcel re-added to the bus is still valid. Keep
  the bus in git (history), or check ids you did not create.
* **Protection from the key holder.** Symmetric key: every node can sign as the hub. If your
  nodes are not equally trusted, use asymmetric signatures instead - the hook is one function
  (`sign` / `signature_ok`), and swapping in Ed25519 via `cryptography` is a small change we
  deliberately left out to keep this stdlib-only.
* **Anything about payload authenticity without a key.** The md5 list in the manifest catches
  corruption and partial sync. It is not a security control: whoever can rewrite the payload
  can rewrite the hashes.

## Markers are self-reported, and that is a deliberate limit

Each node writes its own proof into `state/<node>/`. Nothing signs that marker, so a node -
or anyone who can write the bus - can put `status: applied` there without having applied
anything.

This model defends against **accident**, not against a hostile node: mis-declared steps, a
transport that silently stopped, a human who thinks they did it, a check that reads a flag
instead of a fact. Those are the failures that actually happen in a fleet you own, and they
are invisible without it.

If you need proof against a node that lies, you need something the node cannot produce alone:
have the verify write a value the hub can independently re-read (a hash the hub also knows, a
value it can query over ssh), or run `audit` from a machine that is not the one being
audited. Do not read the board as an attestation.

## Secrets never go in a parcel

`apply` and `verify` strings are stored in the manifest, printed by `status`, and echoed in
error messages. The first 400 characters of what `verify` prints are stored in the marker as
evidence, and that marker travels back over the transport to every node.

So:

* no tokens, passwords or connection strings in the steps;
* a verify that prints a secret puts that secret in the bus - have it print a hash or a
  boolean instead;
* if a step must read a credential, have it read it from the node's own secret store.

## Unattended apply

Applying without a human is exactly as safe as the parcel is boring. Before arming a node:

* keep human-hands work (2FA, a UAC prompt, a physical device) as prose parcels - they can
  never reach `applied`, which is the point;
* make every unattended parcel reversible, and name the rollback at registration;
* remember that an unattended node with an impossible parcel raises the same alarm every day
  until the alarm stops meaning anything (GOTCHAS #7). Decline with `not-for-me --reason`
  instead of letting it rot.

## Escape hatches leave a trace, on purpose

`--force-wave`, `--allow-nonportable`, `--allow-no-payload`, `--allow-missing-files`,
`--allow-local-paths` and `--confirm` all exist, because a gate with no way around it gets
disabled wholesale. Each one prints a loud line, and `--force-wave` and `--confirm` are
recorded in the marker and shown on the board. A bypass that leaves no trace re-introduces
the entire class it was guarding.

## Transport notes

* **File share.** Anyone who can write the folder owns every node. If the share is also used
  for documents, do not put the bus in it.
* **ssh.** The hub holds keys to every node; treat the hub as the crown jewels. This kit does
  not disable host-key checking anywhere, and you should not either.
* **git.** Repo write access = fleet-wide code execution. Use a dedicated private repo,
  protect the default branch, and never force-push a bus - you would erase another machine's
  evidence that it was already done.

## Reporting

Found something? Open an issue, or write to the address in the org profile. We answer within
24 hours.
