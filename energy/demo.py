#!/usr/bin/env python3
"""One-command standalone demonstration of an autonomous charging session.

The device is simulated. No hardware connected. All tariffs are examples.
"""
from datetime import datetime,timezone,timedelta
from pathlib import Path
import sys,json,tempfile
sys.path.insert(0,str(Path(__file__).parent/'src'))
from autopilot.core import Autopilot,Store

START=datetime(2026,11,4,17,0,tzinfo=timezone.utc)
class DemoHomeAssistant:
    def __init__(self):
        self.states={'switch.ev_charger':'off','binary_sensor.ev_plugged':'on',
                     'sensor.ev_soc':'20','sensor.ev_meter':'100', 'input_boolean.pause_autopilot':'off'}
        self.commands=[]
    def state(self,e):return {'entity_id':e,'state':self.states[e]}
    def switch(self,e,on):
        assert e=='switch.ev_charger'
        self.states[e]='on' if on else 'off'
        self.commands.append({'desired':'on' if on else 'off'})

def simulate():
    with tempfile.TemporaryDirectory() as td:
        td=Path(td)
        rates=[]
        with (td/'prices.csv').open('w') as f:
            f.write('start,end,usd_per_kwh\n')
            for i in range(24):
                left=START+timedelta(minutes=15*i)
                right=left+timedelta(minutes=15)
                price=.50 if i<8 else .10
                rates.append(price)
                f.write(f'{left.isoformat()},{right.isoformat()},{price}\n')
        config={'charger_switch':'switch.ev_charger','plugged_sensor':'binary_sensor.ev_plugged',
                'soc_sensor':'sensor.ev_soc','energy_meter_sensor':'sensor.ev_meter',
                'pause_entity':'input_boolean.pause_autopilot','owner_authorized':True,
                'home_assistant_url':'http://127.0.0.1:8123','battery_kwh':20,
                'charge_power_kw':4,'target_soc_percent':50,'efficiency':.9,
                'minimum_dwell_minutes':5,'max_actions_per_day':24,
                'departure_local':'23:00','timezone':'UTC','tariff_csv':str(td/'prices.csv')}
        ha=DemoHomeAssistant();service=Autopilot(config,ha,Store(td/'controller.json'))
        cost=0.;grid_kwh=0.;timeline=[]
        for i in range(24):
            current=START+timedelta(minutes=15*i)
            if i>0 and ha.states['switch.ev_charger']=='on':
                delta=min(1,max(0,(50-float(ha.states['sensor.ev_soc']))/100*20/.9))
                ha.states['sensor.ev_soc']=str(min(50,float(ha.states['sensor.ev_soc'])+delta*.9/20*100))
                ha.states['sensor.ev_meter']=str(float(ha.states['sensor.ev_meter'])+delta)
                grid_kwh+=delta;cost+=delta*rates[i-1]
            receipt=service.tick(current,live=True)
            if receipt['action']!='none':timeline.append({'at':current.isoformat(),'action':receipt['action']})
        immediate_cost=grid_kwh*.50
        result={'device':'SIMULATED charger only','tariff':'SYNTHETIC 50c high / 10c low',
                'user_input_after_initial_setup':'NONE','target_soc':50,
                'final_soc':round(float(ha.states['sensor.ev_soc']),1),
                'charged_grid_kwh':round(grid_kwh,3),
                'modeled_cost_if_charged_immediately_usd':round(immediate_cost,2),
                'modeled_autopilot_cost_usd':round(cost,2),
                'modeled_difference_usd':round(immediate_cost-cost,2),
                'commands':timeline,'receipts_verified':bool(service.store.load()['prev_receipt'])}
        assert result['final_soc']==50 and len(timeline)==2
        return result
if __name__=='__main__':
    print(json.dumps(simulate(),indent=2))
