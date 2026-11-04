"""Deterministic, model-free fuzz sweep over the owner/scheduler boundary."""
import json, random, tempfile, unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from living_energy.loop import LivingEnergy
T0=datetime(2026,11,4,17,tzinfo=timezone.utc)

class RandomizedDogfood(unittest.TestCase):
    def test_80_missions_adversarial_state_transitions(self):
        for seed in range(80):
            r=random.Random(seed)
            with tempfile.TemporaryDirectory() as td:
                root=Path(td); tariff=root/'prices.csv'
                tariff.write_text('start,end,usd_per_kwh\n'+''.join(
                    f'{(T0+timedelta(minutes=15*i)).isoformat()},'
                    f'{(T0+timedelta(minutes=15*(i+1))).isoformat()},'
                    f'{.1 if i>=8 else .5}\n' for i in range(30)))
                m=LivingEnergy.create(root,tariff)
                action=r.choice(['no-op','change_policy','corrupt_tariff','unplug','manual_on','pause','stop','power_loss'])
                change_step=r.randint(2,16)
                old_cmds=[]
                for i in range(23):
                    at=T0+timedelta(minutes=15*i)
                    if i==change_step:
                        if action=='change_policy':
                            cfg=json.loads(m.cfg_path.read_text());cfg['target_soc_percent']=70
                            m.cfg_path.write_text(json.dumps(cfg))
                        elif action=='corrupt_tariff':tariff.write_text('broken,feed\ninvalid,price\n')
                        elif action=='unplug':m.set_plugged(False)
                        elif action=='manual_on':m.override_simulated_switch(True)
                        elif action=='pause':m.pause()
                        elif action=='stop':m.stop(at)
                        elif action=='power_loss':
                            # Explicit separate unresolved intent = durable uncertainty.
                            m.tick_intent_path.write_text('{"unresolved":true}')
                    before=m._mission()
                    result=m.observe(at)
                    if result.get('commands'):
                        old_cmds.extend(result['commands'])
                    current=m._mission()
                    self.assertTrue(current['simulation_only'])
                    self.assertEqual(current['observations'][-1]['state_commitment'],m._state_commitment(current))
                    self.assertTrue(all(cmd[0]=='SIMULATED' and cmd[1]=='switch.ev_charger' for cmd in result['commands']))
                    if action=='change_policy' and i>=change_step:
                        self.assertNotIn(('SIMULATED','switch.ev_charger',True),result['commands'])
                    if action=='corrupt_tariff' and i>=change_step and before['status']!='STOPPED':
                        self.assertNotIn(('SIMULATED','switch.ev_charger',True),result['commands'])
                    if action=='power_loss' and i>=change_step:
                        self.assertNotIn(('SIMULATED','switch.ev_charger',True),result['commands'])
                    if i%7==0:m=LivingEnergy(root)

if __name__=='__main__':unittest.main()
