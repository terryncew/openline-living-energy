"""Independent RED/GREEN safety falsifiers — simulation only, no model-agent claim."""
from __future__ import annotations
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from living_energy.loop import LivingEnergy

T0=datetime(2026,11,4,17,0,tzinfo=timezone.utc)


class SafeHalt003(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.prices=self.root/'prices.csv'
        with self.prices.open('w') as f:
            f.write('start,end,usd_per_kwh\n')
            for i in range(24):
                a=T0+timedelta(minutes=15*i)
                b=a+timedelta(minutes=15)
                f.write(f'{a.isoformat()},{b.isoformat()},{.5 if i<8 else .1}\n')
        self.controller=LivingEnergy.create(self.root,self.prices)

    def charge_on(self):
        self.controller.observe(T0)
        r=self.controller.observe(T0+timedelta(hours=2))
        self.assertTrue(self.controller._mission()['state']['switch_on'],r)

    def test_corrupt_owner_policy_during_on_does_not_leave_on(self):
        self.charge_on()
        self.controller.cfg_path.write_text('{invalid-json')
        r=LivingEnergy(self.root).observe(T0+timedelta(hours=2,minutes=15))
        self.assertIn(r['status'],('HOLD_CONFIG_INVALID_STOPPED','STOP_UNCONFIRMED'),r)
        self.assertFalse(LivingEnergy(self.root)._mission()['state']['switch_on'])
        self.assertEqual(LivingEnergy(self.root)._mission()['status'],'STOPPED')

    def test_missing_owner_policy_during_on_protective_off(self):
        self.charge_on()
        self.controller.cfg_path.unlink()
        r=LivingEnergy(self.root).observe(T0+timedelta(hours=2,minutes=15))
        self.assertIn(r['status'],('HOLD_CONFIG_INVALID_STOPPED','STOP_UNCONFIRMED'),r)
        self.assertFalse(LivingEnergy(self.root)._mission()['state']['switch_on'])

    def test_invalid_owner_policy_while_off_does_not_command_on(self):
        self.controller.cfg_path.write_text('[]')
        r=self.controller.observe(T0)
        self.assertEqual(r['status'],'HOLD_CONFIG_INVALID')
        self.assertFalse(self.controller._mission()['state']['switch_on'])
        self.assertEqual(r['commands'],[])

    def test_stopped_mission_reenabled_externally_is_stopped_again(self):
        self.charge_on()
        self.controller.stop(T0+timedelta(hours=2,minutes=1))
        self.controller.override_simulated_switch(True)
        r=LivingEnergy(self.root).observe(T0+timedelta(hours=2,minutes=15))
        self.assertIn(r['status'],('STOPPED_REPROTECTED','STOP_UNCONFIRMED'),r)
        self.assertFalse(LivingEnergy(self.root)._mission()['state']['switch_on'])
        self.assertEqual(LivingEnergy(self.root)._mission()['status'],'STOPPED')
        self.assertIn(('SIMULATED','switch.ev_charger',False),r['commands'])

    def test_stopped_reenabled_failed_off_is_not_falsely_reported_stopped(self):
        from unittest.mock import patch
        from living_energy.loop import FakeHomeAssistant
        from autopilot.hass import HassError
        self.charge_on()
        self.controller.stop(T0+timedelta(hours=2,minutes=1))
        self.controller.override_simulated_switch(True)
        def fail(self, entity, on):
            raise HassError('Injected physical communication failure')
        with patch.object(FakeHomeAssistant,'switch',fail):
            r=LivingEnergy(self.root).observe(T0+timedelta(hours=2,minutes=15))
        self.assertEqual(r['status'],'STOP_UNCONFIRMED',r)
        self.assertTrue(LivingEnergy(self.root)._mission()['pending_stop'])
        self.assertTrue(LivingEnergy(self.root)._mission()['state']['switch_on'])

    def test_stopped_mission_does_not_accrue_energy_after_external_on(self):
        self.charge_on()
        at=T0+timedelta(hours=2,minutes=1)
        self.controller.stop(at)
        before=self.controller._mission()['state']['meter_kwh']
        self.controller.override_simulated_switch(True)
        LivingEnergy(self.root).observe(at+timedelta(hours=2))
        self.assertEqual(LivingEnergy(self.root)._mission()['state']['meter_kwh'],before,
                         'A stopped mission cannot silently claim charging energy')


if __name__=='__main__': unittest.main()

class InitializationRace003(unittest.TestCase):
    def test_two_simultaneous_creators_never_invalidate_committed_owner_scope(self):
        # Pause creator B after it has passed the initial "mission exists"
        # check but before it writes owner policy. Creator A can otherwise
        # commit and B later overwrite its authorization without a mission edit.
        import concurrent.futures
        import threading
        import time
        from unittest.mock import patch
        import living_energy.loop as code
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            prices=root/'prices.csv'
            prices.write_text('start,end,usd_per_kwh\n'
                              +T0.isoformat()+','+(T0+timedelta(hours=1)).isoformat()+',.2\n')
            reached=threading.Event()
            original=code.write_atomic
            def delayed_write(path,obj):
                if Path(path).name=='owner_policy.json' and obj.get('missing_tariff_policy')=='CHARGE_TO_DEADLINE':
                    reached.set()
                    time.sleep(.25)
                return original(path,obj)
            def create(policy):
                try:
                    code.LivingEnergy.create(root,prices,tariff_policy=policy)
                    return 'CREATED'
                except (RuntimeError,ValueError):
                    return 'REFUSED'
            with patch.object(code,'write_atomic',side_effect=delayed_write):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    slow=pool.submit(create,'CHARGE_TO_DEADLINE')
                    self.assertTrue(reached.wait(4),'Did not reach controlled race point')
                    fast=pool.submit(create,'HOLD')
                    outcomes=[slow.result(timeout=7),fast.result(timeout=7)]
            self.assertEqual(outcomes.count('CREATED'),1,outcomes)
            instance=code.LivingEnergy(root)
            mission=instance._mission()
            cfg=instance._config()
            self.assertEqual(instance._policy_pin(cfg),mission['owner_policy_pin'],
                             'A losing creator rewrote authority after another completed')

class RecursiveProperty003(unittest.TestCase):
    def test_120_seed_policy_loss_and_stop_reactivation(self):
        import random
        from unittest.mock import patch
        from living_energy.loop import FakeHomeAssistant
        from autopilot.hass import HassError
        for seed in range(120):
            r=random.Random(seed)
            with tempfile.TemporaryDirectory() as td:
                root=Path(td)
                tariff=root/'prices.csv'
                tariff.write_text('start,end,usd_per_kwh\n'+''.join(
                    f'{(T0+timedelta(minutes=15*i)).isoformat()},'
                    f'{(T0+timedelta(minutes=15*(i+1))).isoformat()},'
                    f'{.5 if i<8 else .1}\n' for i in range(24)))
                operator=LivingEnergy.create(root,tariff)
                operator.observe(T0)
                operator.observe(T0+timedelta(hours=2))
                self.assertTrue(operator._mission()['state']['switch_on'])
                what=r.choice(('missing','invalid_json','array','missing_preference'))
                if what=='missing':operator.cfg_path.unlink()
                elif what=='invalid_json':operator.cfg_path.write_text('{')
                elif what=='array':operator.cfg_path.write_text('[]')
                else:
                    d=json.loads(operator.cfg_path.read_text());d.pop('missing_tariff_policy')
                    operator.cfg_path.write_text(json.dumps(d))
                bad_link=r.random()<.5
                if bad_link:
                    def refuse(self,entity,on):
                        raise HassError('Temporary link loss')
                    with patch.object(FakeHomeAssistant,'switch',refuse):
                        first=LivingEnergy(root).observe(T0+timedelta(hours=2,minutes=5))
                    self.assertEqual(first['status'],'STOP_UNCONFIRMED')
                    self.assertTrue(LivingEnergy(root)._mission()['pending_stop'])
                final=LivingEnergy(root).observe(T0+timedelta(hours=2,minutes=10))
                self.assertIn(final['status'],('HOLD_CONFIG_INVALID_STOPPED',))
                self.assertFalse(LivingEnergy(root)._mission()['state']['switch_on'])
                # Exercise an independently simulated external reactivation while
                # stopped, restoring the owner policy (without changing its scope).
                fresh=LivingEnergy(root)
                # Keep corrupted policy: stopped mission must be safe even when
                # owner configuration remains unusable.
                fresh.override_simulated_switch(True)
                result=LivingEnergy(root).observe(T0+timedelta(hours=2,minutes=15))
                self.assertIn(result['status'],('HOLD_CONFIG_INVALID_STOPPED',))
                self.assertFalse(LivingEnergy(root)._mission()['state']['switch_on'])
                self.assertTrue(LivingEnergy(root)._mission()['simulation_only'])
                self.assertEqual(LivingEnergy(root)._mission()['status'],'STOPPED')
