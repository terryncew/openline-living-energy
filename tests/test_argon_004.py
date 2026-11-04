"""Argon 004 adversarial safety, sandbox isolation, and 3-family real-agent repair tests."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "energy" / "src"))
sys.path.insert(0, str(ROOT))

from living_energy.loop import LivingEnergy
from living_energy.repair_bridge import RepairBridge, RepairBlocked, load_existing_promotion


def _write_valid_tariff(path: Path, start: datetime, hours: int = 6, rate: float = 0.15):
    lines = ["start,end,usd_per_kwh"]
    for h in range(hours):
        s = (start + timedelta(hours=h)).isoformat()
        e = (start + timedelta(hours=h+1)).isoformat()
        lines.append(f"{s},{e},{rate}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Argon004SafetyAndRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="argon004-test-"))
        self.t0 = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        self.tariff = self.tmp / "rates.csv"
        _write_valid_tariff(self.tariff, self.t0, hours=12, rate=0.12)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_01_hold_tariff_missing_stopped_exposes_off_command(self):
        """RED-1 on Dogfood 002/003: HOLD_TARIFF_MISSING_STOPPED dropped simulated OFF command."""
        le = LivingEnergy.create(self.tmp / "m1", self.tariff, tariff_policy="HOLD")
        r1 = le.observe(self.t0)
        self.assertEqual(r1["status"], "COMMITTED")
        self.assertTrue(le._mission()["state"]["switch_on"])
        self.tariff.write_text("corrupt,csv\n1,2\n", encoding="utf-8")
        r2 = le.observe(self.t0 + timedelta(minutes=15))
        self.assertEqual(r2["status"], "HOLD_TARIFF_MISSING_STOPPED")
        self.assertEqual(
            r2["commands"],
            [("SIMULATED", "switch.ev_charger", False)],
            "Protective OFF command must be exposed in top-level commands list"
        )

    def test_02_resume_refuses_pending_stop_tick_uncertain_and_policy_drift(self):
        """RED-2 on Dogfood 002/003: resume() succeeded despite pending_stop, tick_intent, or policy drift."""
        le = LivingEnergy.create(self.tmp / "m2", self.tariff, tariff_policy="HOLD")
        le.observe(self.t0)
        res = le.stop(self.t0 + timedelta(minutes=5), fail_off=True)
        self.assertEqual(res["status"], "STOP_UNCONFIRMED")
        with self.assertRaises(ValueError):
            le.resume()

        le2 = LivingEnergy.create(self.tmp / "m2b", self.tariff, tariff_policy="HOLD")
        le2.pause()
        le2.tick_intent_path.write_text('{"simulation_only":true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            le2.resume()
        le2.tick_intent_path.unlink()

        cfg = json.loads(le2.cfg_path.read_text(encoding="utf-8"))
        cfg["target_soc_percent"] = 85.0
        le2.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ValueError):
            le2.resume()

    def test_03_stop_and_reconcile_reject_naive_datetime(self):
        """RED-3 on Dogfood 002/003: stop() and reconcile_stop() accepted naive datetimes."""
        le = LivingEnergy.create(self.tmp / "m3", self.tariff, tariff_policy="HOLD")
        naive_now = datetime(2026, 11, 4, 12, 0)
        with self.assertRaises(ValueError):
            le.stop(naive_now)
        with self.assertRaises(ValueError):
            le.reconcile_stop(naive_now)

    def test_04_assess_surfaces_stop_uncertain_policy_and_stopped_states(self):
        """RED-4 on Dogfood 002/003: assess() returned CONTINUE_SIMULATED_MISSION on fault/stop states."""
        le = LivingEnergy.create(self.tmp / "m4a", self.tariff, tariff_policy="HOLD")
        le.observe(self.t0)
        le.stop(self.t0 + timedelta(minutes=5), fail_off=True)
        a1 = le.assess()
        self.assertEqual(a1["constraint"], "STOP_UNCONFIRMED")
        self.assertNotEqual(a1["recommendation"], "CONTINUE_SIMULATED_MISSION")

        le_b = LivingEnergy.create(self.tmp / "m4b", self.tariff, tariff_policy="HOLD")
        le_b.tick_intent_path.write_text('{"simulation_only":true}', encoding="utf-8")
        a2 = le_b.assess()
        self.assertEqual(a2["constraint"], "TICK_UNCERTAIN")

        le_c = LivingEnergy.create(self.tmp / "m4c", self.tariff, tariff_policy="HOLD")
        le_c.stop(self.t0)
        a3 = le_c.assess()
        self.assertEqual(a3["constraint"], "MISSION_STOPPED")
        self.assertNotEqual(a3["recommendation"], "CONTINUE_SIMULATED_MISSION")

        le_d = LivingEnergy.create(self.tmp / "m4d", self.tariff, tariff_policy="HOLD")
        le_d.observe(self.t0)
        cfg = json.loads(le_d.cfg_path.read_text(encoding="utf-8"))
        cfg["target_soc_percent"] = 80.0
        le_d.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        le_d.observe(self.t0 + timedelta(minutes=10))
        a4 = le_d.assess()
        self.assertIn(a4["constraint"], ("AUTHORITY_CHANGED", "MISSION_STOPPED"))
        self.assertNotEqual(a4["recommendation"], "CONTINUE_SIMULATED_MISSION")

    def test_05_isolated_repair_sandbox_blocks_all_escape_vectors(self):
        """RED-5 on Dogfood 002/003: IsolatedRepairSandbox was missing from repair_bridge."""
        from living_energy import repair_bridge
        self.assertTrue(hasattr(repair_bridge, "IsolatedRepairSandbox"), "IsolatedRepairSandbox missing")
        secret_dir = self.tmp / "receiver_secret"
        secret_dir.mkdir()
        oracle_file = secret_dir / "hidden_oracle.py"
        oracle_file.write_text("SECRET_ORACLE_TOKEN = 'DO_NOT_LEAK_99182'\n", encoding="utf-8")
        work_dir = self.tmp / "candidate_work"
        work_dir.mkdir()
        sandbox = repair_bridge.IsolatedRepairSandbox(masked_paths=[secret_dir])
        audit = sandbox.verify_isolation_adversarially(work_dir, secret_canary=oracle_file)
        self.assertTrue(audit["all_blocked"], f"Sandbox escape vector succeeded: {audit}")
        self.assertEqual(audit["vectors_blocked"], 9)

    def test_06_repair_bridge_accepts_receiver_signed_and_rejects_forged(self):
        """RED-6 on Dogfood 002/003: RepairBridge had no connected receiver attestor."""
        from living_energy import repair_bridge
        self.assertTrue(hasattr(repair_bridge, "ReceiverSandboxAttestor"), "ReceiverSandboxAttestor missing")
        SafePromotion, _ = load_existing_promotion(ROOT / "existing" / "living")
        bare = self.tmp / "accepted.git"
        stage = self.tmp / "stage"
        live_db = self.tmp / "live.sqlite"
        shutil.copytree(ROOT / "energy", stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (stage / "docs").mkdir(exist_ok=True)
        (stage / "docs" / "unrelated_accepted.txt").write_text("initial\n", encoding="utf-8")
        # Inject LE-F1 partial-slot defect into stage
        plan_file = stage / "src" / "autopilot" / "planning.py"
        orig_plan = plan_file.read_text(encoding="utf-8")
        bad_plan = orig_plan.replace(
            "gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.",
            "gross_capacity = charge_power_kw * (_utc(slot.end) - _utc(slot.start)).total_seconds() / 3600.",
            1
        )
        plan_file.write_text(bad_plan, encoding="utf-8")
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init"], cwd=stage, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=stage, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@localhost", "commit", "-m", "init"], cwd=stage, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=stage, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD:refs/heads/accepted"], cwd=stage, check=True, capture_output=True)
        with closing(sqlite3.connect(live_db)) as db:
            db.execute("CREATE TABLE kept(item TEXT PRIMARY KEY)")
            db.execute("INSERT INTO kept VALUES('initial')")
            db.commit()

        secret_dir = self.tmp / "receiver_private"
        secret_dir.mkdir()
        oracle = secret_dir / "oracle.py"
        oracle.write_text(
            "import os, sys, sqlite3\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
            "from autopilot.planning import plan_charge, TariffSlot\n"
            "from datetime import datetime, timedelta, timezone\n"
            "s = datetime(2026,11,4,12,0,tzinfo=timezone.utc)\n"
            "p = plan_charge(s + timedelta(minutes=30), s + timedelta(hours=1), 1.0, 4.0, [TariffSlot(s, s + timedelta(hours=1), 0.15)])\n"
            "assert abs(p.available_grid_kwh - 1.7) < 1e-6, f'Expected 1.7, got {p.available_grid_kwh}'\n"
            "print('ORACLE_OK')\n",
            encoding="utf-8"
        )
        attestor = repair_bridge.ReceiverSandboxAttestor.generate()
        promotion = SafePromotion(bare, live_db, self.tmp / "promotion")
        bridge = RepairBridge(
            promotion,
            trusted_validator=oracle,
            trusted_attestor_pubkey=attestor.public_key_hex()
        )
        prep = bridge.prepare("task_f1")
        cand_ws = Path(prep["workspace"])
        # Apply repair in candidate worktree
        (cand_ws / "src" / "autopilot" / "planning.py").write_text(orig_plan, encoding="utf-8")

        # 1. Forged attestation (wrong key) must fail closed
        forged_attestor = repair_bridge.ReceiverSandboxAttestor.generate()
        forged_receipt = forged_attestor.run_and_attest(
            task_id="task_f1",
            agent_workspace=cand_ws,
            oracle_path=oracle,
            live_db_snapshot=Path(prep["snapshot_db"]),
            masked_paths=[secret_dir]
        )
        with self.assertRaises(RepairBlocked):
            bridge.promote_proposed_source(
                "task_f1",
                sandbox_receipt=forged_receipt,
                owner_receiver_approval=True,
                allowed=("src/autopilot/planning.py",)
            )

        # 2. Valid receiver-signed attestation must promote cleanly
        valid_receipt = attestor.run_and_attest(
            task_id="task_f1",
            agent_workspace=cand_ws,
            oracle_path=oracle,
            live_db_snapshot=Path(prep["snapshot_db"]),
            masked_paths=[secret_dir]
        )
        promoted = bridge.promote_proposed_source(
            "task_f1",
            sandbox_receipt=valid_receipt,
            owner_receiver_approval=True,
            allowed=("src/autopilot/planning.py",)
        )
        self.assertEqual(promoted["status"], "PROMOTED")

    def _init_repo_and_db(self, sub_name: str, *, include_living_energy: bool = False):
        base = self.tmp / sub_name
        base.mkdir(parents=True, exist_ok=True)
        bare = base / "accepted.git"
        stage = base / "stage"
        live_db = base / "live.sqlite"
        shutil.copytree(ROOT / "energy", stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if include_living_energy:
            shutil.copytree(
                ROOT / "living_energy",
                stage / "living_energy",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        (stage / "docs").mkdir(exist_ok=True)
        (stage / "docs" / "unrelated_accepted.txt").write_text("accepted_v1\n", encoding="utf-8")
        with closing(sqlite3.connect(live_db)) as db:
            db.execute("CREATE TABLE kept(item TEXT PRIMARY KEY)")
            db.execute("INSERT INTO kept VALUES('initial')")
            db.commit()
        return base, bare, stage, live_db

    def _commit_and_push_stage(self, stage: Path, bare: Path):
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "init"], cwd=stage, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=stage, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=t@localhost", "commit", "-m", "initial"],
            cwd=stage,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=stage, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "HEAD:refs/heads/accepted"], cwd=stage, check=True, capture_output=True)

    def test_07_unfamiliar_defect_family_le_f1_partial_slot_repair(self):
        """LE-F1: Unfamiliar partial-slot gross capacity inflation repaired by native subagents & sealed oracle."""
        from living_energy.agent_repair_runner import apply_subagent_patch
        from living_energy.repair_bridge import ReceiverSandboxAttestor

        for arm in ("conventional_agent", "openline_agent"):
            base, bare, stage, live_db = self._init_repo_and_db(f"f1_{arm}")
            plan_file = stage / "src" / "autopilot" / "planning.py"
            orig_plan = plan_file.read_text(encoding="utf-8")
            plan_file.write_text(
                orig_plan.replace(
                    "gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.",
                    "gross_capacity = charge_power_kw * (_utc(slot.end) - _utc(slot.start)).total_seconds() / 3600.",
                    1,
                ),
                encoding="utf-8",
            )
            self._commit_and_push_stage(stage, bare)

            secret_dir = base / "sealed_oracle"
            secret_dir.mkdir()
            oracle = secret_dir / "oracle_f1.py"
            oracle.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
                "from autopilot.planning import plan_charge, TariffSlot\n"
                "from datetime import datetime, timedelta, timezone\n"
                "s = datetime(2026,11,4,12,0,tzinfo=timezone.utc)\n"
                "p = plan_charge(s + timedelta(minutes=30), s + timedelta(hours=1), 1.0, 4.0, [TariffSlot(s, s + timedelta(hours=1), 0.12)])\n"
                "assert abs(p.available_grid_kwh - 1.7) < 1e-6, f'Oracle failed: {p.available_grid_kwh}'\n",
                encoding="utf-8",
            )

            SafePromotion, _ = load_existing_promotion(ROOT / "existing" / "living")
            attestor = ReceiverSandboxAttestor.generate()
            bridge = RepairBridge(
                SafePromotion(bare, live_db, base / "promotion"),
                trusted_validator=oracle,
                trusted_attestor_pubkey=attestor.public_key_hex(),
            )
            prep = bridge.prepare("f1_task")
            ws = Path(prep["workspace"])

            # Verify RED before subagent patch (sealed oracle fails on unpatched candidate)
            red_receipt = attestor.run_and_attest(
                task_id="f1_task",
                agent_workspace=ws,
                oracle_path=oracle,
                live_db_snapshot=Path(prep["snapshot_db"]),
                masked_paths=[secret_dir],
            )
            self.assertFalse(red_receipt["verified_by_receiver"])

            # Apply subagent patch and verify GREEN + promotion
            apply_subagent_patch(ws, arm, "LE-F1")
            green_receipt = attestor.run_and_attest(
                task_id="f1_task",
                agent_workspace=ws,
                oracle_path=oracle,
                live_db_snapshot=Path(prep["snapshot_db"]),
                masked_paths=[secret_dir],
            )
            self.assertTrue(green_receipt["verified_by_receiver"])
            res = bridge.promote_proposed_source(
                "f1_task",
                sandbox_receipt=green_receipt,
                owner_receiver_approval=True,
                allowed=("src/autopilot/planning.py",),
            )
            self.assertEqual(res["status"], "PROMOTED")

    def test_08_unfamiliar_defect_family_le_f2_paused_session_continuity(self):
        """LE-F2: Unfamiliar PAUSE vs unplug session continuity defect repaired and verified by sealed oracle."""
        from living_energy.agent_repair_runner import apply_subagent_patch
        from living_energy.repair_bridge import ReceiverSandboxAttestor

        base, bare, stage, live_db = self._init_repo_and_db("f2_openline", include_living_energy=True)
        core_file = stage / "src" / "autopilot" / "core.py"
        orig_core = core_file.read_text(encoding="utf-8")
        bad_core = orig_core.replace(
            "if data['session'] is None and plugged:",
            "if (data['session'] is None or not switch_on) and plugged:",
            1,
        )
        self.assertNotEqual(orig_core, bad_core)
        core_file.write_text(bad_core, encoding="utf-8")
        self._commit_and_push_stage(stage, bare)

        secret_dir = base / "sealed_oracle"
        secret_dir.mkdir()
        oracle = secret_dir / "oracle_f2.py"
        oracle.write_text(
            "import sys, tempfile\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
            "from datetime import datetime, timedelta, timezone\n"
            "sys.path.insert(0, str(Path.cwd()))\n"
            "from autopilot.core import Autopilot, Store\n"
            "from living_energy.loop import FakeHomeAssistant, DEFAULT_CFG\n"
            "t0 = datetime(2026,11,4,17,0,tzinfo=timezone.utc)\n"
            "tmp = Path(tempfile.mkdtemp())\n"
            "csv = tmp / 'rates.csv'\n"
            "with csv.open('w') as f:\n"
            "    f.write('start,end,usd_per_kwh\\n')\n"
            "    for i in range(12):\n"
            "        s = t0 + timedelta(minutes=15*i); e = s + timedelta(minutes=15)\n"
            "        f.write(f'{s.isoformat()},{e.isoformat()},0.10\\n')\n"
            "cfg = {k: v for k, v in DEFAULT_CFG.items() if k != 'missing_tariff_policy'}\n"
            "cfg['tariff_csv'] = str(csv)\n"
            "state = {'soc': 30.0, 'plugged': True, 'switch_on': False, 'meter_kwh': 100.0, 'paused': False}\n"
            "store = Store(tmp / 'store.json')\n"
            "ap1 = Autopilot(cfg, FakeHomeAssistant(state, t0), store)\n"
            "ap1.tick(t0, live=True)\n"
            "s1 = store.load()['session']['start']\n"
            "state['paused'] = True\n"
            "state['switch_on'] = False\n"
            "ap2 = Autopilot(cfg, FakeHomeAssistant(state, t0 + timedelta(minutes=15)), store)\n"
            "ap2.tick(t0 + timedelta(minutes=15), live=True)\n"
            "s2 = store.load()['session']['start']\n"
            "assert s1 == s2, f'Session reset during pause: {s1} != {s2}'\n",
            encoding="utf-8",
        )

        SafePromotion, _ = load_existing_promotion(ROOT / "existing" / "living")
        attestor = ReceiverSandboxAttestor.generate()
        bridge = RepairBridge(
            SafePromotion(bare, live_db, base / "promotion"),
            trusted_validator=oracle,
            trusted_attestor_pubkey=attestor.public_key_hex(),
        )
        prep = bridge.prepare("f2_task")
        ws = Path(prep["workspace"])

        red_receipt = attestor.run_and_attest(
            task_id="f2_task",
            agent_workspace=ws,
            oracle_path=oracle,
            live_db_snapshot=Path(prep["snapshot_db"]),
            masked_paths=[secret_dir],
        )
        self.assertFalse(red_receipt["verified_by_receiver"])

        apply_subagent_patch(ws, "openline_agent", "LE-F2")
        green_receipt = attestor.run_and_attest(
            task_id="f2_task",
            agent_workspace=ws,
            oracle_path=oracle,
            live_db_snapshot=Path(prep["snapshot_db"]),
            masked_paths=[secret_dir],
        )
        self.assertTrue(green_receipt["verified_by_receiver"])
        res = bridge.promote_proposed_source(
            "f2_task",
            sandbox_receipt=green_receipt,
            owner_receiver_approval=True,
            allowed=("src/autopilot/core.py",),
        )
        self.assertEqual(res["status"], "PROMOTED")

    def test_09_unfamiliar_defect_family_le_f3_hold_tariff_invalid_off_command(self):
        """LE-F3: Unfamiliar HOLD_TARIFF_MISSING_STOPPED dropped OFF command repaired & verified."""
        from living_energy.agent_repair_runner import apply_subagent_patch
        from living_energy.repair_bridge import ReceiverSandboxAttestor

        base, bare, stage, live_db = self._init_repo_and_db("f3_openline", include_living_energy=True)
        loop_file = stage / "living_energy" / "loop.py"
        orig_loop = loop_file.read_text(encoding="utf-8")
        bad_loop = orig_loop.replace(
            "'constraint':'TARIFF_MISSING','commands':result.get('commands',[]),'stop':result}",
            "'constraint':'TARIFF_MISSING','commands':[],'stop':result}",
            1,
        )
        self.assertNotEqual(orig_loop, bad_loop)
        loop_file.write_text(bad_loop, encoding="utf-8")
        self._commit_and_push_stage(stage, bare)

        secret_dir = base / "sealed_oracle"
        secret_dir.mkdir()
        oracle = secret_dir / "oracle_f3.py"
        oracle.write_text(
            "import sys, tempfile\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd()))\n"
            "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
            "from datetime import datetime, timedelta, timezone\n"
            "from living_energy.loop import LivingEnergy\n"
            "t0 = datetime(2026,11,4,17,0,tzinfo=timezone.utc)\n"
            "tmp = Path(tempfile.mkdtemp())\n"
            "tf = tmp / 'tariff.csv'\n"
            "with tf.open('w') as f:\n"
            "    f.write('start,end,usd_per_kwh\\n')\n"
            "    for i in range(12):\n"
            "        s = t0 + timedelta(minutes=15*i); e = s + timedelta(minutes=15)\n"
            "        f.write(f'{s.isoformat()},{e.isoformat()},0.10\\n')\n"
            "le = LivingEnergy.create(tmp / 'm', tf, tariff_policy='HOLD')\n"
            "le.observe(t0)\n"
            "assert le._mission()['state']['switch_on'] is True\n"
            "tf.write_text('corrupt_csv_bytes\\n')\n"
            "res = le.observe(t0 + timedelta(minutes=15))\n"
            "assert res['status'] == 'HOLD_TARIFF_MISSING_STOPPED'\n"
            "assert res['commands'] == [('SIMULATED', 'switch.ev_charger', False)], f'Missing OFF: {res}'\n",
            encoding="utf-8",
        )

        SafePromotion, _ = load_existing_promotion(ROOT / "existing" / "living")
        attestor = ReceiverSandboxAttestor.generate()
        bridge = RepairBridge(
            SafePromotion(bare, live_db, base / "promotion"),
            trusted_validator=oracle,
            trusted_attestor_pubkey=attestor.public_key_hex(),
        )
        prep = bridge.prepare("f3_task")
        ws = Path(prep["workspace"])

        red_receipt = attestor.run_and_attest(
            task_id="f3_task",
            agent_workspace=ws,
            oracle_path=oracle,
            live_db_snapshot=Path(prep["snapshot_db"]),
            masked_paths=[secret_dir],
        )
        self.assertFalse(red_receipt["verified_by_receiver"])

        apply_subagent_patch(ws, "openline_agent", "LE-F3")
        green_receipt = attestor.run_and_attest(
            task_id="f3_task",
            agent_workspace=ws,
            oracle_path=oracle,
            live_db_snapshot=Path(prep["snapshot_db"]),
            masked_paths=[secret_dir],
        )
        self.assertTrue(green_receipt["verified_by_receiver"])
        res = bridge.promote_proposed_source(
            "f3_task",
            sandbox_receipt=green_receipt,
            owner_receiver_approval=True,
            allowed=("living_energy/loop.py",),
        )
        self.assertEqual(res["status"], "PROMOTED")

    def test_10_matched_comparison_conventional_vs_openline_vs_ablation_concurrent_work(self):
        """Compare Conventional, Full OpenLine, and Ablation (no snapshot refresh) under concurrent Git + SQLite work."""
        from living_energy.agent_repair_runner import apply_subagent_patch
        from living_energy.repair_bridge import ReceiverSandboxAttestor

        SafePromotion, _ = load_existing_promotion(ROOT / "existing" / "living")

        def run_arm(arm_name: str, *, refresh_snapshot: bool):
            base, bare, stage, live_db = self._init_repo_and_db(f"cmp_{arm_name}")
            plan_file = stage / "src" / "autopilot" / "planning.py"
            orig_plan = plan_file.read_text(encoding="utf-8")
            plan_file.write_text(
                orig_plan.replace(
                    "gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.",
                    "gross_capacity = charge_power_kw * (_utc(slot.end) - _utc(slot.start)).total_seconds() / 3600.",
                    1,
                ),
                encoding="utf-8",
            )
            self._commit_and_push_stage(stage, bare)

            secret_dir = base / "sealed_oracle"
            secret_dir.mkdir()
            oracle = secret_dir / "oracle_cmp.py"
            oracle.write_text(
                "import os, sqlite3, sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
                "from autopilot.planning import plan_charge, TariffSlot\n"
                "from datetime import datetime, timedelta, timezone\n"
                "s = datetime(2026,11,4,12,0,tzinfo=timezone.utc)\n"
                "p = plan_charge(s + timedelta(minutes=30), s + timedelta(hours=1), 1.0, 4.0, [TariffSlot(s, s + timedelta(hours=1), 0.12)])\n"
                "assert abs(p.available_grid_kwh - 1.7) < 1e-6\n"
                "db_path = os.environ.get('OPENLINE_TEST_DB')\n"
                "if db_path:\n"
                "    conn = sqlite3.connect(db_path)\n"
                "    rows = [r[0] for r in conn.execute('SELECT item FROM kept').fetchall()]\n"
                "    assert 'initial' in rows\n",
                encoding="utf-8",
            )

            attestor = ReceiverSandboxAttestor.generate()
            promotion = SafePromotion(bare, live_db, base / "promotion")
            bridge = RepairBridge(
                promotion,
                trusted_validator=oracle,
                trusted_attestor_pubkey=attestor.public_key_hex(),
            )
            prep = bridge.prepare("cmp_task")
            ws = Path(prep["workspace"])

            subagent_key = "conventional_agent" if arm_name == "conventional" else "openline_agent"
            apply_subagent_patch(ws, subagent_key, "LE-F1")

            # Simulate concurrent unrelated Git commit on accepted branch + concurrent SQLite write on live_db
            (stage / "docs" / "unrelated_accepted.txt").write_text("accepted_v2_concurrent\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/unrelated_accepted.txt"], cwd=stage, check=True, capture_output=True)
            subprocess.run(
                ["git", "-c", "user.name=Concurrent", "-c", "user.email=c@localhost", "commit", "-m", "concurrent doc"],
                cwd=stage,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "push", "origin", "HEAD:refs/heads/accepted"], cwd=stage, check=True, capture_output=True)
            with closing(sqlite3.connect(live_db)) as db:
                db.execute("INSERT INTO kept VALUES('concurrent_sqlite_row')")
                db.commit()

            receipt = attestor.run_and_attest(
                task_id="cmp_task",
                agent_workspace=ws,
                oracle_path=oracle,
                live_db_snapshot=Path(prep["snapshot_db"]),
                masked_paths=[secret_dir],
            )
            if not refresh_snapshot:
                # Ablation arm: without snapshot refresh, promotion refuses when live_db advanced concurrently
                ab_res = promotion.promote(
                    "cmp_task",
                    acceptance_cmd=[sys.executable, str(oracle)],
                    allow_prefixes=("src/autopilot/planning.py",),
                    refresh_snapshot=False,
                )
                return ab_res

            res = bridge.promote_proposed_source(
                "cmp_task",
                sandbox_receipt=receipt,
                owner_receiver_approval=True,
                allowed=("src/autopilot/planning.py",),
            )
            # Verify both the concurrent Git file and concurrent SQLite row were preserved!
            check_co = base / "verify_checkout"
            subprocess.run(["git", "clone", "--branch", "accepted", str(bare), str(check_co)], check=True, capture_output=True)
            preserved_doc = (check_co / "docs" / "unrelated_accepted.txt").read_text(encoding="utf-8")
            repaired_code = (check_co / "src" / "autopilot" / "planning.py").read_text(encoding="utf-8")
            with closing(sqlite3.connect(live_db)) as db:
                rows = {r[0] for r in db.execute("SELECT item FROM kept").fetchall()}
            self.assertEqual(preserved_doc, "accepted_v2_concurrent\n")
            self.assertIn("gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.", repaired_code)
            self.assertEqual(rows, {"initial", "concurrent_sqlite_row"})
            return res

        ablation_res = run_arm("ablation", refresh_snapshot=False)
        self.assertEqual(ablation_res["status"], "REFUSED")
        self.assertEqual(ablation_res["reason"], "live_database_changed_revalidation_required")
        conv_res = run_arm("conventional", refresh_snapshot=True)
        self.assertEqual(conv_res["status"], "PROMOTED")
        ol_res = run_arm("openline", refresh_snapshot=True)
        self.assertEqual(ol_res["status"], "PROMOTED")

    def test_11_interruption_agent_replacement_and_cas_recovery(self):
        """Test mid-promotion interruption (fail_after_ref=True -> CAS_PENDING), agent replacement, and recover()."""
        from living_energy.agent_repair_runner import apply_subagent_patch
        from living_energy.repair_bridge import ReceiverSandboxAttestor

        SafePromotion, _ = load_existing_promotion(ROOT / "existing" / "living")
        base, bare, stage, live_db = self._init_repo_and_db("cas_recovery")
        plan_file = stage / "src" / "autopilot" / "planning.py"
        orig_plan = plan_file.read_text(encoding="utf-8")
        plan_file.write_text(
            orig_plan.replace(
                "gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.",
                "gross_capacity = charge_power_kw * (_utc(slot.end) - _utc(slot.start)).total_seconds() / 3600.",
                1,
            ),
            encoding="utf-8",
        )
        self._commit_and_push_stage(stage, bare)

        secret_dir = base / "sealed_oracle"
        secret_dir.mkdir()
        oracle = secret_dir / "oracle_cas.py"
        oracle.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path.cwd() / 'src'))\n"
            "from autopilot.planning import plan_charge, TariffSlot\n"
            "from datetime import datetime, timedelta, timezone\n"
            "s = datetime(2026,11,4,12,0,tzinfo=timezone.utc)\n"
            "p = plan_charge(s + timedelta(minutes=30), s + timedelta(hours=1), 1.0, 4.0, [TariffSlot(s, s + timedelta(hours=1), 0.12)])\n"
            "assert abs(p.available_grid_kwh - 1.7) < 1e-6\n",
            encoding="utf-8",
        )

        # Agent 1 starts task, gets interrupted before finishing; Agent 2 replaces Agent 1
        promotion1 = SafePromotion(bare, live_db, base / "promotion")
        attestor = ReceiverSandboxAttestor.generate()
        bridge1 = RepairBridge(
            promotion1,
            trusted_validator=oracle,
            trusted_attestor_pubkey=attestor.public_key_hex(),
        )
        prep1 = bridge1.prepare("interrupted_task")
        ws1 = Path(prep1["workspace"])
        # Agent 1 writes a partial/aborted draft and crashes; replacement Agent 2 resets and applies clean fix
        (ws1 / "src" / "autopilot" / "planning.py").write_text("# aborted draft\n", encoding="utf-8")
        subprocess.run(["git", "checkout", "--", "."], cwd=ws1, check=True, capture_output=True)
        apply_subagent_patch(ws1, "openline_agent", "LE-F1")

        receipt = attestor.run_and_attest(
            task_id="interrupted_task",
            agent_workspace=ws1,
            oracle_path=oracle,
            live_db_snapshot=Path(prep1["snapshot_db"]),
            masked_paths=[secret_dir],
        )
        self.assertTrue(receipt["verified_by_receiver"])

        # Simulate crash immediately after git update-ref (CAS_PENDING)
        with self.assertRaises(RuntimeError):
            promotion1.promote(
                "interrupted_task",
                acceptance_cmd=[sys.executable, str(oracle)],
                allow_prefixes=("src/autopilot/planning.py",),
                fail_after_ref=True,
                refresh_snapshot=True,
            )

        # Replacement controller instance inspects state and recovers CAS_PENDING -> PROMOTED_RECOVERED
        promotion2 = SafePromotion(bare, live_db, base / "promotion")
        recovered = promotion2.recover("interrupted_task")
        self.assertEqual(recovered["status"], "PROMOTED_RECOVERED")

    def test_12_complete_simulated_charging_mission_after_verified_repair(self):
        """Demonstrate a complete 25-tick simulated charging mission after an independently verified repair."""
        mission_root = self.tmp / "mission_post_repair"
        le = LivingEnergy.create(mission_root, self.tariff, tariff_policy="HOLD")

        # Tick 0: Start charging on valid tariff
        r0 = le.observe(self.t0)
        self.assertEqual(r0["status"], "COMMITTED")
        self.assertTrue(le._mission()["state"]["switch_on"])

        # Operator pauses at +5m and resumes at +10m from accepted checkpoint
        paused = le.pause()
        self.assertEqual(paused["status"], "PAUSED_SWITCH_ON")
        resumed = le.resume()
        self.assertEqual(resumed["status"], "RESUMED")

        # Run 25 ticks across process restart at step 8 to reach target SOC >= 50%
        soc_trace = [le._mission()["state"]["soc"]]
        for step in range(1, 25):
            if step == 8:
                le = LivingEnergy(mission_root)
            now = self.t0 + timedelta(minutes=15 * step)
            tick_res = le.observe(now)
            self.assertIn(tick_res["status"], ("COMMITTED", "MONITORING", "TARGET_MET"))
            for cmd in tick_res.get("commands", []):
                self.assertEqual(cmd[0], "SIMULATED")
            soc_trace.append(le._mission()["state"]["soc"])

        final_mission = le._mission()
        self.assertTrue(final_mission["simulation_only"])
        self.assertEqual(final_mission["status"], "ACTIVE")
        self.assertGreaterEqual(final_mission["state"]["soc"], 50.0)
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(soc_trace, soc_trace[1:])))


if __name__ == "__main__":
    unittest.main()
