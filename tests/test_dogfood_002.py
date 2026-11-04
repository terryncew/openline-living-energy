"""New independent dogfood security regressions; simulated hardware only."""
from __future__ import annotations
import json, tempfile, unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from living_energy.loop import LivingEnergy, FakeHomeAssistant

T0=datetime(2026,11,4,17,0,tzinfo=timezone.utc)

class Dogfood002(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        tariff=self.root/'tariff.csv'
        with tariff.open('w') as f:
            f.write('start,end,usd_per_kwh\n')
            for i in range(24):
                t=T0+timedelta(minutes=15*i)
                f.write(f'{t.isoformat()},{(t+timedelta(minutes=15)).isoformat()},{.5 if i<8 else .1}\n')
        self.m=LivingEnergy.create(self.root,tariff)

    def on(self):
        for i in range(9): self.m.observe(T0+timedelta(minutes=i*15))
        self.assertTrue(self.m._mission()['state']['switch_on'])

    def test_outer_state_tamper_soc_refused(self):
        self.m.observe(T0)
        j=json.loads(self.m.mission_path.read_text()); j['state']['soc']=98.5
        self.m.mission_path.write_text(json.dumps(j))
        with self.assertRaisesRegex(RuntimeError,'state commitment'):self.m.observe(T0+timedelta(minutes=15))

    def test_outer_state_tamper_status_refused(self):
        self.m.observe(T0)
        j=json.loads(self.m.mission_path.read_text()); j['status']='STOPPED'
        self.m.mission_path.write_text(json.dumps(j))
        with self.assertRaisesRegex(RuntimeError,'state commitment'):self.m._mission()

    def test_outer_state_tamper_pending_stop_refused(self):
        self.m.observe(T0)
        j=json.loads(self.m.mission_path.read_text()); j['pending_stop']=True
        self.m.mission_path.write_text(json.dumps(j))
        with self.assertRaisesRegex(RuntimeError,'state commitment'):self.m._mission()

    def test_outer_state_tamper_policy_pin_refused(self):
        j=json.loads(self.m.mission_path.read_text()); j['owner_policy_pin']='0'*64
        self.m.mission_path.write_text(json.dumps(j))
        with self.assertRaisesRegex(RuntimeError,'state commitment'):self.m._mission()

    def test_no_legacy_uncommitted_state_accepted(self):
        from living_energy.loop import digest
        j=json.loads(self.m.mission_path.read_text()); del j['observations'][-1]['state_commitment']
        # Recompute the receipt hash as it would appear in the pre-hardening format.
        j['observations'][-1]['hash']=digest({k:v for k,v in j['observations'][-1].items() if k!='hash'})
        j['prev_receipt']=j['observations'][-1]['hash']
        self.m.mission_path.write_text(json.dumps(j))
        with self.assertRaises(RuntimeError):self.m._mission()

    def test_revocation_while_on_protectively_off(self):
        self.on()
        cfg=json.loads(self.m.cfg_path.read_text());cfg['owner_authorized']=False
        self.m.cfg_path.write_text(json.dumps(cfg))
        r=self.m.observe(T0+timedelta(minutes=135))
        self.assertEqual(r['status'],'HOLD_AUTHORITY_CHANGED_STOPPED')
        self.assertEqual(self.m._mission()['status'],'STOPPED')
        self.assertFalse(self.m._mission()['state']['switch_on'])
        with self.assertRaises(ValueError):self.m.resume()

    def test_revocation_with_failed_off_remains_uncertain(self):
        self.on()
        cfg=json.loads(self.m.cfg_path.read_text());cfg['owner_authorized']=False
        self.m.cfg_path.write_text(json.dumps(cfg))
        def off_fails(self, entity, on):
            from autopilot.hass import HassError
            raise HassError('simulated link loss')
        with patch.object(FakeHomeAssistant,'switch',off_fails):
            r=self.m.observe(T0+timedelta(minutes=135))
        self.assertEqual(r['status'],'STOP_UNCONFIRMED')
        self.assertTrue(self.m._mission()['pending_stop'])
        self.assertEqual(LivingEnergy(self.root).observe(T0+timedelta(minutes=150))['status'],'HOLD_AUTHORITY_CHANGED_STOPPED')

    def test_unknown_effect_survives_restart_without_second_on(self):
        self.m.observe(T0)
        from autopilot.core import Autopilot
        original=Autopilot.tick
        def crash_after_effect(ctrl, *a, **k):
            original(ctrl,*a,**k)
            raise RuntimeError('simulated power loss after effect before ACK')
        with patch.object(Autopilot,'tick',crash_after_effect):
            with self.assertRaisesRegex(RuntimeError,'simulated power loss'):
                self.m.observe(T0+timedelta(hours=2))
        self.assertTrue(self.m.tick_intent_path.exists())
        new=LivingEnergy(self.root)
        x=new.observe(T0+timedelta(hours=2,minutes=15))
        self.assertEqual(x['status'],'TICK_UNCERTAIN')
        self.assertEqual(x['commands'],[])
        self.assertEqual(new.stop(T0+timedelta(hours=2,minutes=20))['status'],'STOPPED_SIMULATED')
        self.assertFalse(new.tick_intent_path.exists())
        self.assertEqual(LivingEnergy(self.root).observe(T0+timedelta(hours=2,minutes=30))['status'],'STOPPED')

    def test_bool_type_required_for_simulator_events(self):
        with self.assertRaises(TypeError):self.m.set_plugged('false')
        with self.assertRaises(TypeError):self.m.override_simulated_switch('false')


# Additional independent concurrency checks, intentionally not using the agent runner.
class Dogfood002Concurrency(unittest.TestCase):
    setUp=Dogfood002.setUp
    def test_stale_operator_cannot_erase_accepted_receipt(self):
        a=self.m._mission()
        b=LivingEnergy(self.root)._mission()
        self.m._receipt(a,'OPERATOR_A',{'status':'accepted'})
        with self.assertRaisesRegex(RuntimeError,'CONCURRENT_MISSION_UPDATE_REFUSED'):
            LivingEnergy(self.root)._receipt(b,'OPERATOR_B',{'status':'erase_A'})
        kinds=[rec['kind'] for rec in self.m._mission()['observations']]
        self.assertIn('OPERATOR_A',kinds)
        self.assertNotIn('OPERATOR_B',kinds)

    def test_competing_observers_cannot_double_command_on(self):
        import concurrent.futures
        self.m.observe(T0)
        at=T0+timedelta(hours=2)
        def run(_): return LivingEnergy(self.root).observe(at)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,range(2)))
        ons=[cmd for r in results for cmd in r['commands'] if cmd[-1] is True]
        self.assertEqual(len(ons),1)
        self.assertEqual(self.m._mission()['state']['switch_on'],True)

class Dogfood002PricePolicy(unittest.TestCase):
    setUp=Dogfood002.setUp
    def test_corrupt_tariff_explicit_deadline_policy_has_no_savings_claim(self):
        root=self.root/'alternate'; root.mkdir()
        tariff=root/'rates.csv'
        tariff.write_bytes((self.root/'tariff.csv').read_bytes())
        m=LivingEnergy.create(root,tariff,tariff_policy='CHARGE_TO_DEADLINE')
        m.observe(T0)
        tariff.write_text('broken,feed\ninvalid,price\n')
        r=m.observe(T0+timedelta(minutes=15))
        self.assertNotIn(r['status'],('HOLD_TARIFF_MISSING','HOLD_TARIFF_MISSING_STOPPED'))
        self.assertEqual(r['result']['plan']['reason'],'price_unavailable_charge_now')
        self.assertIsNone(r['result']['plan']['modeled_avoided_cost'])

    def test_changed_entity_while_on_stops_original_not_new_target(self):
        for i in range(9): self.m.observe(T0+timedelta(minutes=15*i))
        cfg=json.loads(self.m.cfg_path.read_text());cfg['charger_switch']='switch.unauthorized'
        self.m.cfg_path.write_text(json.dumps(cfg))
        r=self.m.observe(T0+timedelta(minutes=135))
        self.assertEqual(r['status'],'HOLD_AUTHORITY_CHANGED_STOPPED')
        commands=r['stop'].get('commands',[])
        self.assertEqual(commands,[('SIMULATED','switch.ev_charger',False)])

    def test_malformed_present_tariff_holds_instead_of_on(self):
        self.m.observe(T0)
        (self.root/'tariff.csv').write_text('wrong,headers\nno,values\n')
        outcome=self.m.observe(T0+timedelta(minutes=15))
        self.assertEqual(outcome['status'],'HOLD_TARIFF_MISSING')
        self.assertFalse(self.m._mission()['state']['switch_on'])
        self.assertEqual(outcome['commands'],[])

    def test_malformed_present_tariff_protectively_off(self):
        for i in range(9): self.m.observe(T0+timedelta(minutes=15*i))
        self.assertTrue(self.m._mission()['state']['switch_on'])
        (self.root/'tariff.csv').write_text('wrong,headers\nno,values\n')
        outcome=self.m.observe(T0+timedelta(minutes=135))
        self.assertEqual(outcome['status'],'HOLD_TARIFF_MISSING_STOPPED')
        self.assertFalse(self.m._mission()['state']['switch_on'])

    def test_expired_tariff_holds(self):
        self.m.observe(T0)
        stale_start=T0-timedelta(days=2)
        (self.root/'tariff.csv').write_text('start,end,usd_per_kwh\n'+stale_start.isoformat()+','+(stale_start+timedelta(hours=1)).isoformat()+',0.1\n')
        r=self.m.observe(T0+timedelta(minutes=15))
        self.assertEqual(r['status'],'HOLD_TARIFF_MISSING')
        self.assertFalse(self.m._mission()['state']['switch_on'])


def _observe_process(path, iso):
    return LivingEnergy(Path(path)).observe(datetime.fromisoformat(iso))

class Dogfood002Processes(unittest.TestCase):
    setUp=Dogfood002.setUp
    def test_two_real_os_processes_serialized(self):
        import concurrent.futures, multiprocessing
        self.m.observe(T0)
        t=(T0+timedelta(hours=2)).isoformat()
        with concurrent.futures.ProcessPoolExecutor(max_workers=2,mp_context=multiprocessing.get_context("spawn")) as pool:
            replies=list(pool.map(_observe_process,[str(self.root)]*2,[t]*2))
        ons=[cmd for result in replies for cmd in result['commands'] if cmd[-1] is True]
        self.assertEqual(len(ons),1)
        self.assertTrue(LivingEnergy(self.root)._mission()['state']['switch_on'])

if __name__=='__main__': unittest.main()
