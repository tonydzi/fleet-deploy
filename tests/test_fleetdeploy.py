#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test grid for fleetdeploy.py. Stdlib unittest, no network, no fixtures on disk.

    python tests/test_fleetdeploy.py         # or: python -m unittest discover tests

Every test drives the CLI as a subprocess, exactly the way a node will, in a fresh temp bus
with two simulated machines (FLEET_NODE + FLEET_HOME per call). Time travel uses
FLEET_DEPLOY_NOW instead of sleeping.

CAN THESE TESTS FAIL? We checked, and you should too. Three one-line mutations, each verified
to turn the grid red on 2026-08-10:

  * `wave_blockers`: change `if st not in (APPLIED, NOT_FOR_ME)` to
    `if st not in (APPLIED, CLAIMED, NOT_FOR_ME)`
    -> test_claimed_does_not_unblock_wave fails.
  * `node_status`: delete the `if st == CLAIMED: return CLAIMED` branch
    -> test_claimed_does_not_unblock_wave and test_confirm_is_claimed_not_applied fail.
  * `cmd_apply`: change `if vcode != 0:` to `if False:` (record the marker even when verify
    could not read the fact)
    -> test_verify_reading_emptiness_leaves_it_pending fails.

One mutation that does NOT turn it red, and that is the design working: making `cmd_apply`
write `status: applied` unconditionally changes nothing, because `node_status` recomputes
provability from the parcel's own shape when it reads the marker back. Both the writer and
the reader have to be broken before a prose parcel can claim to be proven.

A suite that cannot go red on broken code proves nothing.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FD = os.path.join(os.path.dirname(HERE), "fleetdeploy.py")

FLEET = {
    "nodes": {
        "HUB-1": {"os": "linux", "class": "desktop", "aliases": ["hub"]},
        "NODE-2": {"os": "linux", "class": "headless", "aliases": ["vps"]},
        "NODE-3": {"os": "linux", "class": "headless", "aliases": []},
    }
}
IS_WIN = os.name == "nt"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-test-")
        self.bus = os.path.join(self.tmp, "bus")
        os.makedirs(self.bus)
        with open(os.path.join(self.bus, "fleet.json"), "w", encoding="utf-8") as f:
            json.dump(FLEET, f)
        self.homes = {}
        for n in FLEET["nodes"]:
            self.homes[n] = os.path.join(self.tmp, n.lower() + "-home")
            os.makedirs(self.homes[n])
        self.payload = os.path.join(self.tmp, "payload")
        os.makedirs(self.payload)
        with open(os.path.join(self.payload, "thing.txt"), "w", encoding="utf-8") as f:
            f.write("version 2\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------------------
    def fd(self, args, node="HUB-1", now=None, key=None):
        env = dict(os.environ)
        env["FLEET_DEPLOY_BUS"] = self.bus
        env["FLEET_NODE"] = node
        env["FLEET_HOME"] = self.homes.get(node, self.tmp)
        env.pop("FLEET_DEPLOY_HMAC_KEY", None)
        if now:
            env["FLEET_DEPLOY_NOW"] = now
        if key:
            env["FLEET_DEPLOY_HMAC_KEY"] = key
        p = subprocess.run([sys.executable, FD] + args, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def register(self, pid="p1", targets="HUB-1,NODE-2", extra=None, apply_=None, verify=None,
                 node="HUB-1", key=None):
        apply_ = apply_ or ('"$PYTHON" "$FLEETDEPLOY" install payload/thing.txt '
                            '"$FLEET_HOME/inst/thing.txt"')
        verify = verify or ('"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/inst/thing.txt" '
                            '--contains "version 2"')
        args = ["register", pid, "--title", "test parcel", "--apply", apply_, "--verify", verify,
                "--targets", targets, "--payload", self.payload,
                "--rollback", "remove the file"]
        return self.fd(args + (extra or []), node=node, key=key)

    def board_json(self):
        code, out = self.fd(["board", "--json"])
        return code, json.loads(out)

    # -- registration gates ---------------------------------------------------------------
    def test_unknown_target_fails_closed(self):
        code, out = self.register(targets="HUB-1,NODE-9")
        self.assertEqual(code, 2)
        self.assertIn("does not know", out)

    def test_double_registration_refused(self):
        self.assertEqual(self.register()[0], 0)
        code, out = self.register()
        self.assertEqual(code, 2)
        self.assertIn("already registered", out)

    def test_delivery_claim_without_payload_blocked(self):
        code, out = self.fd(["register", "p2", "--title", "t",
                              "--apply", "python -c \"print(1)\"  # syncthing will deliver it",
                              "--verify", '"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/x"',
                              "--targets", "HUB-1,NODE-2", "--rollback", "r"])
        self.assertEqual(code, 2)
        self.assertIn("PROMISES delivery", out)

    def test_missing_file_reference_blocked(self):
        code, out = self.fd(["register", "p3", "--title", "t",
                              "--apply", "python tools/not_here.py",
                              "--verify", '"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/x"',
                              "--targets", "HUB-1,NODE-2", "--rollback", "r"])
        self.assertEqual(code, 2)
        self.assertIn("not_here.py", out)

    def test_node_local_path_blocked(self):
        code, out = self.fd(["register", "p4", "--title", "t",
                              "--apply", "cp /home/OPERATOR/tool.py /opt/tool.py",
                              "--verify", "test -f /opt/tool.py",
                              "--targets", "HUB-1,NODE-2", "--rollback", "r",
                              "--allow-missing-files"])
        self.assertEqual(code, 2)
        self.assertIn("only on your machine", out)

    def test_portability_gate_blocks_windows_cmd_to_posix(self):
        code, out = self.fd(["register", "p5", "--title", "t",
                              "--apply", 'powershell -c "Copy-Item a b"',
                              "--verify", "test -f b",
                              "--targets", "HUB-1,NODE-2", "--rollback", "r",
                              "--allow-missing-files"])
        self.assertEqual(code, 2)
        self.assertIn("cannot run on every target", out)

    def test_risky_without_rollback_blocked(self):
        code, out = self.fd(["register", "p6", "--title", "t",
                              "--apply", '"$PYTHON" -c "print(1)"',
                              "--verify", '"$PYTHON" -c "print(1)"',
                              "--targets", "HUB-1,NODE-2"])
        self.assertEqual(code, 2)
        self.assertIn("--rollback is required", out)

    def test_safe_risk_is_one_wave(self):
        code, out = self.register(extra=["--risk", "safe"])
        self.assertEqual(code, 0)
        man = json.load(open(os.path.join(self.bus, "parcels", "p1", "manifest.json"), encoding="utf-8"))
        self.assertEqual(len(man["waves"]), 1)

    def test_waves_second_step_is_a_different_class(self):
        self.assertEqual(self.register(targets="HUB-1,NODE-2,NODE-3")[0], 0)
        man = json.load(open(os.path.join(self.bus, "parcels", "p1", "manifest.json"), encoding="utf-8"))
        # The canary belongs to the MAJORITY class - that is where the consumers are. Here
        # two of three targets are headless, so a headless node goes first and the desktop
        # (a different class, different blind spots) is the mandatory second step.
        self.assertIn(man["waves"][0][0], ("NODE-2", "NODE-3"))
        self.assertEqual(man["waves"][1], ["HUB-1"])
        self.assertEqual(len(man["waves"]), 3)

    def test_concurrent_takes_two_canaries(self):
        self.assertEqual(self.register(targets="NODE-2,NODE-3,HUB-1",
                                       extra=["--concurrent", "--canary", "NODE-2"])[0], 0)
        man = json.load(open(os.path.join(self.bus, "parcels", "p1", "manifest.json"), encoding="utf-8"))
        self.assertEqual(len(man["waves"][0]), 2)

    # -- identity -------------------------------------------------------------------------
    def test_unknown_identity_fails_closed(self):
        self.assertEqual(self.register()[0], 0)
        code, out = self.fd(["status"], node="LAPTOP-TYPO")
        self.assertEqual(code, 2)
        self.assertIn("not in", out)

    def test_alias_resolves_to_canonical_node(self):
        self.assertEqual(self.register()[0], 0)
        code, out = self.fd(["status"], node="hub")
        self.assertEqual(code, 0)
        self.assertIn("HUB-1", out)

    # -- the canary order -------------------------------------------------------------------
    def test_canary_order_enforced(self):
        self.assertEqual(self.register()[0], 0)
        code, out = self.fd(["apply", "p1"], node="NODE-2")
        self.assertEqual(code, 2)
        self.assertIn("cannot start yet", out)
        self.assertIn("HUB-1", out)

    def test_claimed_does_not_unblock_wave(self):
        """The sharp one. A human saying 'done' on the canary must NOT open the next wave."""
        self.assertEqual(self.register()[0], 0)
        self.assertEqual(self.fd(["apply", "p1", "--confirm", "did it by hand"], node="HUB-1")[0], 0)
        code, out = self.fd(["apply", "p1"], node="NODE-2")
        self.assertEqual(code, 2)
        self.assertIn("claimed", out.lower())

    def test_force_wave_is_recorded(self):
        self.assertEqual(self.register()[0], 0)
        code, _ = self.fd(["apply", "p1", "--force-wave"], node="NODE-2")
        self.assertEqual(code, 0)
        m = json.load(open(os.path.join(self.bus, "state", "NODE-2", "p1.json"), encoding="utf-8"))
        self.assertTrue(m["wave_forced"])

    def test_not_a_target_is_not_a_laggard(self):
        self.assertEqual(self.register(targets="HUB-1,NODE-2")[0], 0)
        code, out = self.fd(["apply", "p1"], node="NODE-3")
        self.assertEqual(code, 2)
        self.assertIn("not a target", out)
        _, data = self.board_json()
        self.assertNotIn("NODE-3", data["parcels"][0]["cells"])

    # -- apply / verify semantics -----------------------------------------------------------
    def test_apply_then_fact_is_read_back(self):
        self.assertEqual(self.register()[0], 0)
        code, out = self.fd(["apply", "p1"], node="HUB-1")
        self.assertEqual(code, 0)
        self.assertIn("APPLIED", out)
        self.assertTrue(os.path.exists(os.path.join(self.homes["HUB-1"], "inst", "thing.txt")))

    def test_apply_is_idempotent(self):
        self.assertEqual(self.register()[0], 0)
        self.assertEqual(self.fd(["apply", "p1"], node="HUB-1")[0], 0)
        code, out = self.fd(["apply", "p1"], node="HUB-1")
        self.assertEqual(code, 0)
        self.assertIn("already in place", out)

    def test_verify_reading_emptiness_leaves_it_pending(self):
        """apply 'succeeds', verify cannot find the fact -> nothing is recorded."""
        code, _ = self.fd(["register", "p7", "--title", "t",
                            "--apply", '"$PYTHON" -c "print(\'pretending\')"',
                            "--verify", '"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/never.txt"',
                            "--targets", "HUB-1", "--rollback", "r", "--risk", "safe"])
        self.assertEqual(code, 0)
        code, out = self.fd(["apply", "p7"], node="HUB-1")
        self.assertEqual(code, 1)
        self.assertIn("NOT applied", out)
        self.assertFalse(os.path.exists(os.path.join(self.bus, "state", "HUB-1", "p7.json")))

    def test_failed_apply_writes_no_marker(self):
        code, _ = self.fd(["register", "p8", "--title", "t",
                            "--apply", '"$PYTHON" -c "import sys; sys.exit(3)"',
                            "--verify", '"$PYTHON" "$FLEETDEPLOY" check-file "$FLEET_HOME/never.txt"',
                            "--targets", "HUB-1", "--rollback", "r", "--risk", "safe"])
        self.assertEqual(code, 0)
        code, out = self.fd(["apply", "p8"], node="HUB-1")
        self.assertEqual(code, 1)
        self.assertIn("apply failed", out)
        self.assertFalse(os.path.exists(os.path.join(self.bus, "state", "HUB-1", "p8.json")))

    def test_prose_parcel_is_claimed_not_applied(self):
        code, out = self.fd(["register", "p9", "--title", "t",
                              "--apply", "open the settings dialog and tick the box",
                              "--verify", "look at the dialog again",
                              "--targets", "HUB-1", "--rollback", "r", "--risk", "safe",
                              "--allow-missing-files"])
        self.assertEqual(code, 0)
        self.assertIn("never be counted", out)
        self.assertEqual(self.fd(["apply", "p9", "--confirm", "ticked it"], node="HUB-1")[0], 0)
        _, data = self.board_json()
        self.assertEqual(data["parcels"][0]["cells"]["HUB-1"], "claimed")
        self.assertEqual(data["totals"]["applied"], 0)

    def test_confirm_is_claimed_not_applied(self):
        self.assertEqual(self.register(targets="HUB-1", extra=["--risk", "safe"])[0], 0)
        self.assertEqual(self.fd(["apply", "p1", "--confirm", "by hand"], node="HUB-1")[0], 0)
        _, data = self.board_json()
        self.assertEqual(data["parcels"][0]["cells"]["HUB-1"], "claimed")

    def test_trivial_verify_can_never_be_applied(self):
        code, out = self.fd(["register", "pa", "--title", "t",
                              "--apply", '"$PYTHON" -c "print(1)"',
                              "--verify", "exit 0",
                              "--targets", "HUB-1", "--rollback", "r", "--risk", "safe"])
        self.assertEqual(code, 0)
        self.assertIn("never be counted", out)
        self.assertEqual(self.fd(["apply", "pa"], node="HUB-1")[0], 0)
        _, data = self.board_json()
        self.assertEqual(data["parcels"][0]["cells"]["HUB-1"], "claimed")

    def test_echo_verify_is_not_proof(self):
        """`echo ok` was on the no-op list and `echo hello` was not. Both prove nothing."""
        code, out = self.fd(["register", "pe", "--title", "t",
                             "--apply", '"$PYTHON" -c "print(1)"',
                             "--verify", "echo everything is fine",
                             "--targets", "HUB-1", "--rollback", "r", "--risk", "safe"])
        self.assertEqual(code, 0)
        self.assertIn("never be counted", out)
        self.assertEqual(self.fd(["apply", "pe"], node="HUB-1")[0], 0)
        _, data = self.board_json()
        self.assertEqual(data["parcels"][0]["cells"]["HUB-1"], "claimed")

    # -- refusal, board, drift ---------------------------------------------------------------
    def test_not_for_me_needs_a_reason_and_clears_the_column(self):
        self.assertEqual(self.register()[0], 0)
        code, _ = self.fd(["not-for-me", "p1"], node="NODE-2")
        self.assertEqual(code, 2)                      # argparse: --reason is required
        self.assertEqual(self.fd(["not-for-me", "p1", "--reason", "no browser here"],
                                  node="NODE-2")[0], 0)
        self.assertEqual(self.fd(["apply", "p1"], node="HUB-1")[0], 0)
        code, data = self.board_json()
        self.assertEqual(code, 0)
        self.assertEqual(data["parcels"][0]["cells"]["NODE-2"], "not-for-me")

    def test_board_is_red_until_every_target_is_proven(self):
        self.assertEqual(self.register()[0], 0)
        self.assertEqual(self.fd(["apply", "p1"], node="HUB-1")[0], 0)
        code, data = self.board_json()
        self.assertEqual(code, 1)
        self.assertEqual(data["totals"]["applied"], 1)
        self.assertEqual(data["totals"]["pending"], 1)
        self.assertEqual(self.fd(["apply", "p1"], node="NODE-2")[0], 0)
        code, data = self.board_json()
        self.assertEqual(code, 0)
        self.assertEqual(data["totals"]["applied"], 2)

    def test_silent_node_is_never_green(self):
        self.assertEqual(self.register()[0], 0)
        _, out = self.fd(["board"])
        self.assertIn("NODE-2", out)
        self.assertIn("PENDING", out)

    def test_audit_detects_drift(self):
        self.assertEqual(self.register(targets="HUB-1", extra=["--risk", "safe"])[0], 0)
        self.assertEqual(self.fd(["apply", "p1"], node="HUB-1")[0], 0)
        self.assertEqual(self.fd(["audit"], node="HUB-1")[0], 0)
        os.remove(os.path.join(self.homes["HUB-1"], "inst", "thing.txt"))
        code, out = self.fd(["audit"], node="HUB-1")
        self.assertEqual(code, 1)
        self.assertIn("DRIFT", out)

    def test_payload_corruption_blocks_apply(self):
        self.assertEqual(self.register()[0], 0)
        with open(os.path.join(self.bus, "parcels", "p1", "payload", "thing.txt"),
                  "w", encoding="utf-8") as f:
            f.write("tampered")
        code, out = self.fd(["apply", "p1"], node="HUB-1")
        self.assertEqual(code, 2)
        self.assertIn("not arrived intact", out)

    # -- verdicts ---------------------------------------------------------------------------
    def test_node_down_needs_a_recheck_command(self):
        code, out = self.fd(["node-down", "NODE-2", "--reason", "ssh refused"])
        self.assertEqual(code, 2)
        self.assertIn("--recheck is required", out)

    def test_dead_verdict_expires(self):
        self.assertEqual(self.register()[0], 0)
        self.assertEqual(self.fd(["node-down", "NODE-2", "--reason", "ssh refused",
                                   "--recheck", "ssh node-2 true"],
                                  now="2026-01-01T00:00:00Z")[0], 0)
        _, out = self.fd(["board"], now="2026-01-10T00:00:00Z")
        self.assertIn("unreachable since 2026-01-01", out)
        _, out = self.fd(["board"], now="2026-03-01T00:00:00Z")
        self.assertIn("VERDICT EXPIRED", out)
        self.assertIn("ssh node-2 true", out)

    # -- signing ------------------------------------------------------------------------------
    def test_signature_required_when_key_is_set(self):
        self.assertEqual(self.register(key="s3cret")[0], 0)
        self.assertEqual(self.fd(["apply", "p1"], node="HUB-1", key="s3cret")[0], 0)

    def test_tampered_parcel_is_refused(self):
        self.assertEqual(self.register(key="s3cret")[0], 0)
        mp = os.path.join(self.bus, "parcels", "p1", "manifest.json")
        man = json.load(open(mp, encoding="utf-8"))
        man["apply"] = '"$PYTHON" -c "print(\'evil\')"'
        json.dump(man, open(mp, "w", encoding="utf-8"))
        code, out = self.fd(["apply", "p1"], node="HUB-1", key="s3cret")
        self.assertEqual(code, 2)
        self.assertIn("signature does not match", out)

    def test_unsigned_parcel_refused_when_key_is_set(self):
        self.assertEqual(self.register()[0], 0)                      # registered without a key
        code, out = self.fd(["apply", "p1"], node="HUB-1", key="s3cret")
        self.assertEqual(code, 2)
        self.assertIn("unsigned", out)

    # -- cross-shell expansion ------------------------------------------------------------------
    def test_percent_and_dollar_expand_the_same(self):
        """`$X` is not a variable to cmd.exe and `%X%` is not one to sh. Both must work."""
        self.assertEqual(self.register(pid="pd", targets="HUB-1", extra=["--risk", "safe"],
                                       apply_='"$PYTHON" "$FLEETDEPLOY" install payload/thing.txt '
                                              '"%FLEET_HOME%/pct/thing.txt"',
                                       verify='"%PYTHON%" "%FLEETDEPLOY%" check-file '
                                              '"$FLEET_HOME/pct/thing.txt" --md5 '
                                              + _md5(os.path.join(self.payload, "thing.txt")))[0], 0)
        code, out = self.fd(["apply", "pd"], node="HUB-1")
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.homes["HUB-1"], "pct", "thing.txt")))


def _md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


if __name__ == "__main__":
    unittest.main(verbosity=2)
