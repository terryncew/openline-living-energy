import json,tempfile,unittest,os
from datetime import datetime,timedelta,timezone
from pathlib import Path
from living_energy.loop import LivingEnergy
from living_energy.repair_bridge import RepairBridge,RepairBlocked,load_existing_promotion
from living_energy.demo import run

T0=datetime(2026,11,4,17,0,tzinfo=timezone.utc)

class LivingTests(unittest.TestCase):
    def setUp(self):
        self.d=tempfile.TemporaryDirectory();self.addCleanup(self.d.cleanup)
        self.root=Path(self.d.name)
        self.csv=self.root/'rates.csv'
        with self.csv.open('w') as f:
            f.write('start,end,usd_per_kwh\n')
            for i in range(24):
                s=T0+timedelta(minutes=15*i); e=s+timedelta(minutes=15)
                f.write(f'{s.isoformat()},{e.isoformat()},{.5 if i<8 else .1}\n')
        self.a=LivingEnergy.create(self.root,self.csv)

    def test_no_physical_clients(self):
        x=self.a.observe(T0+timedelta(hours=2))
        self.assertTrue(x['simulated'])
        for c in x['commands']:self.assertEqual(c[0],'SIMULATED')

    def test_unattended_simulated_replanning(self):
        self.a.observe(T0)
        x=self.a.observe(T0+timedelta(hours=2));self.assertIn(x['status'],('COMMITTED','MONITORING'))
        self.assertTrue(self.a._mission()['state']['switch_on'])
        self.a=LivingEnergy(self.root)
        x=self.a.observe(T0+timedelta(hours=4))
        self.assertGreater(self.a._mission()['state']['soc'],20)
        self.assertGreaterEqual(self.a._mission()['sequence'],3)

    def test_pause_vs_stop(self):
        self.a.observe(T0+timedelta(hours=2))
        self.assertTrue(self.a._mission()['state']['switch_on'])
        self.assertEqual(self.a.pause()['status'],'PAUSED_SWITCH_ON')
        self.assertTrue(self.a._mission()['state']['switch_on'])
        self.assertEqual(self.a.stop(T0+timedelta(hours=2,minutes=1))['status'],'STOPPED_SIMULATED')
        self.assertFalse(self.a._mission()['state']['switch_on'])
        with self.assertRaises(ValueError):self.a.resume()

    def test_failed_off_recovery_and_restart(self):
        self.a.observe(T0+timedelta(hours=2))
        x=self.a.stop(T0+timedelta(hours=2,minutes=1),fail_off=True)
        self.assertEqual(x['status'],'STOP_UNCONFIRMED')
        new=LivingEnergy(self.root)
        self.assertEqual(new.observe(T0+timedelta(hours=2,minutes=2))['status'],'STOP_PENDING')
        self.assertEqual(new.reconcile_stop(T0+timedelta(hours=2,minutes=3))['status'],'STOPPED_SIMULATED')

    def test_missing_tariff_refuses_owner_policy_hold(self):
        self.csv.unlink()
        self.assertEqual(self.a.observe(T0+timedelta(hours=3))['status'],'HOLD_TARIFF_MISSING')
        self.assertFalse(self.a._mission()['state']['switch_on'])

    def test_missing_tariff_explicit_deadline_fallback(self):
        policy=json.loads(self.a.cfg_path.read_text())
        policy['missing_tariff_policy']='CHARGE_TO_DEADLINE'
        self.a._mission() # enforce pin check prior to policy change
        # Separate owner creation to select the alternative, not silent mutation.
        root=self.root/'fresh';root.mkdir()
        b=LivingEnergy.create(root,self.csv,tariff_policy='CHARGE_TO_DEADLINE')
        self.csv.unlink()
        x=b.observe(T0+timedelta(hours=3))
        self.assertEqual(x['result']['plan']['reason'],'price_unavailable_charge_now')
        self.assertTrue(b._mission()['state']['switch_on'])

    def test_config_change_cannot_inherit_authorization(self):
        policy=json.loads(self.a.cfg_path.read_text());policy['charger_switch']='switch.unauthorized'
        self.a.cfg_path.write_text(json.dumps(policy))
        x=self.a.observe(T0+timedelta(hours=3))
        self.assertEqual(x['status'],'HOLD_AUTHORITY_CHANGED')
        self.assertEqual(self.a._mission()['state']['switch_on'],False)

    def test_receipt_tampering_blocks_future_commands(self):
        self.a.observe(T0)
        m=json.loads(self.a.mission_path.read_text());m['observations'][0]['payload']['constraint']='FAKE'
        self.a.mission_path.write_text(json.dumps(m))
        with self.assertRaises(RuntimeError):self.a.observe(T0+timedelta(hours=2))

    def test_unplug_after_automated_on_triggers_protective_off(self):
        self.a.observe(T0+timedelta(hours=2))
        self.assertTrue(self.a._mission()['state']['switch_on'])
        self.a.set_plugged(False)
        result=self.a.observe(T0+timedelta(hours=2,minutes=5))
        self.assertFalse(self.a._mission()['state']['switch_on'])
        self.assertEqual(result['result'].get('action'),'charger_off')

    def test_tariff_hold_protectively_stops_on_state(self):
        self.a.observe(T0+timedelta(hours=2))
        self.assertTrue(self.a._mission()['state']['switch_on'])
        self.csv.unlink()
        x=self.a.observe(T0+timedelta(hours=2,minutes=5))
        self.assertEqual(x['status'],'HOLD_TARIFF_MISSING_STOPPED')
        self.assertFalse(self.a._mission()['state']['switch_on'])
        with self.assertRaises(ValueError):self.a.resume()

    def test_owner_authorization_change_invalidates_policy(self):
        cfg=json.loads(self.a.cfg_path.read_text())
        cfg['owner_authorized']=False
        self.a.cfg_path.write_text(json.dumps(cfg))
        self.assertEqual(self.a.observe(T0+timedelta(hours=2))['status'],'HOLD_AUTHORITY_CHANGED')

    def test_owner_fallback_required(self):
        with self.assertRaises(ValueError):
            LivingEnergy.create(self.root/'new',self.csv,tariff_policy='')

    def test_repair_gate_refuses_agent_authored_attestation(self):
        class FakePromo:pass
        oracle=self.root/'oracle.py';oracle.write_text('print(0)')
        bridge=RepairBridge(FakePromo(),trusted_validator=oracle)
        self.assertEqual(bridge.status()['status'],'AGENT_NOT_TESTED')
        with self.assertRaises(RepairBlocked):
            bridge.promote_proposed_source('task',sandbox_receipt={'verified_by_receiver':True},owner_receiver_approval=False)

    def test_bridge_requires_reused_implementation(self):
        with self.assertRaises(RepairBlocked):load_existing_promotion(self.root)

    def test_restart_demo(self):
        root=self.root/'demo'
        x=run(root)
        self.assertEqual(x['ticks'],25)
        self.assertEqual(x['repair_agent'],'NOT_TESTED_NO_RUNNER')

if __name__=='__main__':unittest.main()
