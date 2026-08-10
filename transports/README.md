# Transports

`fleetdeploy.py` never opens a socket. The whole system is one directory - the **bus** - and a
transport's only job is to make that directory eventually look the same on every machine.

That split is deliberate. The interesting part of a rollout is proof, not plumbing, and every
fleet already has plumbing: a synced folder, ssh, a git remote, an S3 bucket, a config
management tool. Pick one, keep it dumb.

```
bus/
  fleet.json                     the node registry (who exists, what OS, what class)
  parcels/<id>/manifest.json     the parcel: steps, targets, waves, rollback, hashes
  parcels/<id>/payload/...       the artifact actually being shipped
  state/<node>/<id>.json         that node's marker: applied / claimed / not-for-me
  state/<node>/heartbeat.json    that node was alive at this timestamp
  verdicts/<node>.json           dated, expiring "this node is unreachable" verdicts
```

**Who writes what.** The hub writes `parcels/**` and `fleet.json`. A node writes only
`state/<its own name>/**`. Nothing else is shared state, so two nodes applying at the same
moment cannot collide - and a transport that only ever moves whole files (rsync, Syncthing,
git) is enough. No locking, no database.

## The three drivers here

| driver | when it fits | what it costs |
|---|---|---|
| [`fileshare.py`](fileshare.py) | you already run Syncthing / Dropbox / NFS / SMB across the fleet | the share has no delivery receipt - the driver adds one |
| [`ssh_push.py`](ssh_push.py) | the hub can reach the nodes directly | hub must hold keys; nodes need nothing installed but Python |
| [`git_bus.py`](git_bus.py) | you want history and an audit trail for free | every node needs repo access; conflicts are real |

All three are ~100 lines of stdlib wrapping tools you already have. Read them before running
them; they are examples as much as they are code.

## Writing your own

A transport needs two verbs:

```
pull()   bring the current bus here  (parcels + everybody's state)
push()   publish what changed here   (the hub's parcels, or this node's state dir)
```

Two rules that are not optional:

1. **Never delete another node's state.** `rsync --delete` on `state/` erases the proof that
   some machine was already done, and the board silently reverts to red. Sync `parcels/` with
   `--delete` if you like; sync `state/` additively.
2. **Prove the transport moved.** A share that quietly stopped syncing looks exactly like a
   fleet where nobody has applied anything yet. `fileshare.py check` exists precisely because
   we could not tell those two apart for six days.
