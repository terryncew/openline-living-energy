"""24-tick autonomous device simulation with a real command policy and local receipts.

Results are simulated electricity costs, not measured household savings.
"""
import tempfile,unittest,sys,json
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autopilot.core import Autopilot,Store
from test_autopilot import FakeHass,config,tariff,T0


class EndToEnd(unittest.TestCase):
    def test_autonomous_overnight_saving_session(self):
        with tempfile.TemporaryDirectory() as d:
            h=FakeHass();cfg=config();cfg['minimum_dwell_minutes']=5
            path=Path(d)/'tariff.csv';cfg['tariff_csv']=str(path)
            with path.open('w') as f:
                f.write('start,end,usd_per_kwh\n')
                for p in tariff():f.write(f'{p.start.isoformat()},{p.end.isoformat()},{p.usd_kwh}\n')
            store=Store(Path(d)/'state.json')
            pilot=Autopilot(cfg,h,store)
            energy_delivered=0.; actual_tariff_cost=0.; records=[]
            for i in range(24):
                now=T0+timedelta(minutes=15*i)
                if i>0 and h.values['switch.ev_charger']=='on':
                    # last 15 minutes drew power. Fake vehicle follows its battery target.
                    grid=min(1.,max(0.,(50-float(h.values['sensor.ev_soc']))/100*20/.9))
                    h.values['sensor.ev_meter']=str(float(h.values['sensor.ev_meter'])+grid)
                    h.values['sensor.ev_soc']=str(min(50.,float(h.values['sensor.ev_soc'])+grid*.9/20*100))
                    energy_delivered+=grid
                    actual_tariff_cost+=grid*tariff()[i-1].usd_kwh
                doc=pilot.tick(now,live=True)
                records.append(doc)
            self.assertTrue(any(r['action']=='charger_on' for r in records))
            self.assertTrue(any(r['action']=='charger_off' for r in records))
            self.assertEqual(float(h.values['sensor.ev_soc']),50)
            self.assertAlmostEqual(energy_delivered,6/.9,places=4)
            self.assertAlmostEqual(actual_tariff_cost,energy_delivered*.1,places=4)
            counterfactual_immediate=energy_delivered*.5
            self.assertGreater(counterfactual_immediate-actual_tariff_cost,2.5)
            self.assertFalse(any(r['status']=='HOLD' for r in records))
            final_receipts=[r for r in records if r.get('counterfactual_status')=='MODELED_NOT_OBSERVED_SAVINGS']
            self.assertTrue(final_receipts)
            self.assertAlmostEqual(final_receipts[-1]['modeled_difference_to_immediate_usd'],counterfactual_immediate-actual_tariff_cost,places=3)
            log_path=store.path.with_suffix('.receipts.jsonl')
            self.assertTrue(log_path.exists())
            s=store.load()
            self.assertAlmostEqual(s['session']['estimated_cost_usd'],actual_tariff_cost,places=4)
            log=[json.loads(line) for line in log_path.read_text().splitlines()]
            self.assertTrue(len(log)>=24)
            self.assertTrue(any(x['body']['status']=='INTENT' for x in log))
            self.assertTrue(any(x['body']['status']=='COMMITTED' for x in log))
            result={'kind':'SIMULATED_LOCAL_CHARGER_NOT_REAL_HOME','charging_kwh':round(energy_delivered,3),
                    'scheduled_cost_usd':round(actual_tariff_cost,3),
                    'modeled_immediate_cost_usd':round(counterfactual_immediate,3),
                    'modeled_difference_usd':round(counterfactual_immediate-actual_tariff_cost,3),
                    'number_of_hardware_commands_simulated':len(h.commands),
                    'sessions':24,'all_required_charge_delivered':True}
            (Path(__file__).resolve().parents[1]/'evidence'/'full_session_simulation.json').write_text(json.dumps(result,indent=2)+'\n')


    def test_adapts_when_cheapest_window_moves_after_initial_forecast(self):
        with tempfile.TemporaryDirectory() as d:
            h=FakeHass();cfg=config();cfg['minimum_dwell_minutes']=5
            path=Path(d)/'tariff.csv';cfg['tariff_csv']=str(path)
            def publish_prices(shifted):
                with path.open('w') as f:
                    f.write('start,end,usd_per_kwh\n')
                    for i,slot in enumerate(tariff()):
                        price=(.5 if i<16 else .1) if shifted else slot.usd_kwh
                        f.write(f'{slot.start.isoformat()},{slot.end.isoformat()},{price}\n')
            publish_prices(False)
            pilot=Autopilot(cfg,h,Store(Path(d)/'state.json'))
            cost=0.;grid=0.;starts=[]
            for i in range(24):
                t=T0+timedelta(minutes=15*i)
                if i==7:publish_prices(True) # before the original cheaper window
                if i>0 and h.values['switch.ev_charger']=='on':
                    e=min(1.,max(0.,(50-float(h.values['sensor.ev_soc']))/100*20/.9))
                    h.values['sensor.ev_meter']=str(float(h.values['sensor.ev_meter'])+e)
                    h.values['sensor.ev_soc']=str(min(50.,float(h.values['sensor.ev_soc'])+e*.9/20*100))
                    grid+=e;cost+=e*(.5 if i-1<16 else .1)
                receipt=pilot.tick(t,live=True)
                if receipt['action']=='charger_on':starts.append(t)
            self.assertEqual(starts,[T0+timedelta(hours=4)])
            self.assertEqual(float(h.values['sensor.ev_soc']),50.)
            self.assertAlmostEqual(cost,.1*grid,places=4)
            static_schedule_cost=.5*grid # static 19:00 charger assumed old forecast
            self.assertGreater(static_schedule_cost-cost,2.)

    def test_flat_tariff_does_not_claim_savings_opportunity(self):
        now=T0
        from autopilot.planning import TariffSlot,plan_charge
        slots=[TariffSlot(s.start,s.end,.2) for s in tariff()]
        p=plan_charge(now,T0+timedelta(hours=6),6,4,slots)
        self.assertAlmostEqual(p.modeled_avoided_cost,0.,places=4)

if __name__=='__main__':unittest.main(verbosity=2)
