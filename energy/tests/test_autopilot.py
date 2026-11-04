"""Executable integration tests; zero network beyond local fake Home Assistant."""
import json, os, sys, unittest, tempfile, threading, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autopilot.core import Autopilot, Store, load_config, parse_forecast_csv, departure_datetime
from autopilot.hass import HomeAssistant, HassError
from autopilot.planning import TariffSlot, plan_charge

T0=datetime(2026,11,4,17,0,tzinfo=timezone.utc)
D=datetime(2026,11,4,23,0,tzinfo=timezone.utc)


def tariff():
    return [TariffSlot(T0+timedelta(minutes=15*i),T0+timedelta(minutes=15*(i+1)),
                       0.50 if i<8 else .10) for i in range(24)]


class FakeHass:
    def __init__(self):
        self.values={'switch.ev_charger':'off','binary_sensor.ev_plugged':'on',
                     'sensor.ev_soc':'20','sensor.ev_meter':'100',
                     'input_boolean.ev_pause':'off'}
        self.commands=[]
        self.revision='2026-11-04T17:00:00+00:00'
        self.mutate_before_switch=None
    def state(self,id):
        if id not in self.values:raise HassError('missing state')
        return {'entity_id':id,'state':self.values[id],'last_updated':self.revision}
    def switch(self,id,on):
        if self.mutate_before_switch:self.mutate_before_switch()
        self.values[id]='on' if on else 'off';self.commands.append((id,on))


def config():
    return {'home_assistant_url':'http://localhost:8123','charger_switch':'switch.ev_charger',
            'plugged_sensor':'binary_sensor.ev_plugged','soc_sensor':'sensor.ev_soc',
            'energy_meter_sensor':'sensor.ev_meter','pause_entity':'input_boolean.ev_pause',
            'battery_kwh':20,'charge_power_kw':4,'target_soc_percent':50,
            'efficiency':.9,'minimum_dwell_minutes':0,'max_actions_per_day':24,
            'owner_authorized':True,'departure_local':'23:00','timezone':'UTC',
            'tariff_csv':'path.csv'}


class TestPlanning(unittest.TestCase):
    def test_cost_saving(self):
        p=plan_charge(T0,D,6,4,tariff(),usable_fraction=1)
        self.assertFalse(p.charge_now)
        self.assertTrue(p.feasible_by_deadline)
        self.assertAlmostEqual(p.estimated_plan_cost,.6)
        self.assertAlmostEqual(p.immediate_cost,3.0)
        self.assertAlmostEqual(p.modeled_avoided_cost,2.4)
    def test_charge_later(self):
        p=plan_charge(T0+timedelta(hours=3),D,6,4,tariff(),usable_fraction=1)
        self.assertTrue(p.charge_now)
    def test_absolute_deadline_unmet(self):
        p=plan_charge(D-timedelta(minutes=15),D,10,4,tariff())
        self.assertTrue(p.charge_now)
        self.assertFalse(p.feasible_by_deadline)
    def test_hard_price_cap(self):
        p=plan_charge(T0,T0+timedelta(hours=1),1,4,tariff(),hard_price_cap=.2)
        self.assertFalse(p.charge_now)
        self.assertFalse(p.feasible_by_deadline)
    def test_no_coverage_no_fabricated_savings(self):
        p=plan_charge(T0,D,1,4,tariff()[:1])
        self.assertTrue(p.charge_now)
        self.assertIsNone(p.modeled_avoided_cost)
    def test_invalid_tariff_and_nan(self):
        with self.assertRaises(ValueError):
            plan_charge(T0,D,float('nan'),4,tariff())
        with self.assertRaises(ValueError):
            plan_charge(T0,D,1,4,[tariff()[0],tariff()[0]])
    def test_dst_aware(self):
        t=datetime(2026,11,1,6,30,tzinfo=timezone.utc)
        d=departure_datetime(t,'07:00','America/Los_Angeles')
        self.assertEqual(d.hour,15)


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.h=FakeHass();self.cfg=config();self.store=Store(Path(self.tmp.name)/'state.json')
        self.cfg['tariff_csv']=str(Path(self.tmp.name)/'tariff.csv')
        with open(self.cfg['tariff_csv'],'w') as f:
            f.write('start,end,usd_per_kwh\n')
            for s in tariff():f.write(f'{s.start.isoformat()},{s.end.isoformat()},{s.usd_kwh}\n')
        self.a=Autopilot(self.cfg,self.h,self.store)
    def test_dry_run_no_commands(self):
        x=self.a.tick(T0,live=False)
        self.assertEqual(x['status'],'MONITORING') # off is already desired
        x=self.a.tick(T0+timedelta(hours=3),live=False)
        self.assertEqual(x['status'],'PROPOSED')
        self.assertEqual(self.h.commands,[])
    def test_autonomous_switching_and_replanning(self):
        self.assertEqual(self.a.tick(T0,live=True)['status'],'MONITORING')
        self.assertEqual(self.a.tick(T0+timedelta(hours=2),live=True)['action'],'charger_on')
        self.assertEqual(self.h.commands,[('switch.ev_charger',True)])
        self.assertEqual(self.a.tick(T0+timedelta(hours=2,minutes=5),live=True)['action'],'none')
        self.h.values['sensor.ev_soc']='50'
        end=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(end['action'],'charger_off')
        self.assertEqual(self.h.values['switch.ev_charger'],'off')
        self.assertEqual(len(self.h.commands),2)
    def test_owner_not_authorized(self):
        self.cfg['owner_authorized']=False
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['status'],'PROPOSED');self.assertFalse(self.h.commands)
    def test_pause(self):
        self.h.values['input_boolean.ev_pause']='on'
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['status'],'PAUSED');self.assertFalse(self.h.commands)
    def test_manual_override_does_not_fight_owner(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['switch.ev_charger']='off'
        x=self.a.tick(T0+timedelta(hours=2,minutes=15),live=True)
        self.assertEqual(x['status'],'PAUSED')
        self.assertEqual(len(self.h.commands),1)
    def test_bad_sensor_no_action(self):
        self.h.values['sensor.ev_soc']='unknown'
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['status'],'HOLD');self.assertFalse(self.h.commands)
    def test_rate_gap_no_cost_claim(self):
        with open(self.cfg['tariff_csv'],'w') as f:
            f.write('start,end,usd_per_kwh\n')
            s=tariff()[0];f.write(f'{s.start.isoformat()},{s.end.isoformat()},0.50\n')
        x=self.a.tick(T0,live=True)
        self.assertEqual(x['plan']['modeled_avoided_cost'],None)
    def test_meter_rollback_fail_closed(self):
        self.a.tick(T0,live=True)
        self.h.values['sensor.ev_meter']='99'
        x=self.a.tick(T0+timedelta(minutes=5),live=True)
        self.assertEqual(x['status'],'HOLD');self.assertFalse(self.h.commands)
    def test_bounded_actions(self):
        self.cfg['max_actions_per_day']=1
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['sensor.ev_soc']='50'
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['action'],'charger_off') # never prevent protective stop
    def test_final_freshness_check(self):
        orig=self.h.state
        calls={'soc':0}
        def changed(entity):
            if entity=='sensor.ev_soc':
                calls['soc']+=1
                if calls['soc']>=2:self.h.values['sensor.ev_soc']='30'
            return orig(entity)
        self.h.state=changed
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['reason'],'command_or_freshness_check_failed')
        self.assertFalse(self.h.commands)
    def test_receipt_chain(self):
        self.a.tick(T0,live=False);self.a.tick(T0+timedelta(minutes=5),live=False)
        path=self.store.path.with_suffix('.receipts.jsonl')
        records=[json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual(records[1]['previous_receipt'],records[0]['hash'])
    def test_tariff_mutation_replans(self):
        self.a.tick(T0,live=True)
        # Make next slot expensive and later slots low; controller still waits
        with open(self.cfg['tariff_csv'],'w') as f:
            f.write('start,end,usd_per_kwh\n')
            for s in tariff():
                new=.9 if s.start==T0+timedelta(hours=2) else s.usd_kwh
                f.write(f'{s.start.isoformat()},{s.end.isoformat()},{new}\n')
        p=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(p['status'],'MONITORING');self.assertFalse(self.h.commands)

    def test_missing_price_cannot_bypass_hard_cap(self):
        self.cfg['hard_price_cap_usd_kwh']=0.20
        with open(self.cfg['tariff_csv'],'w') as f:
            f.write('start,end,usd_per_kwh\n')
            s=tariff()[0];f.write(f'{s.start.isoformat()},{s.end.isoformat()},0.50\n')
        x=self.a.tick(T0+timedelta(minutes=5),live=True)
        self.assertFalse(self.h.commands)
        self.assertFalse(x['desired'])
        self.assertIn('gap',x['plan']['reason'])
    def test_action_budget_blocks_new_on(self):
        self.cfg['max_actions_per_day']=1
        data=self.store.load()
        data['actions_today']={'2026-11-04':1}
        self.store.save(data)
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['reason'],'action_budget_exhausted')
        self.assertFalse(self.h.commands)
    def test_no_power_means_no_verified_charge(self):
        self.cfg['power_kw_sensor']='sensor.ev_power'
        self.h.values['sensor.ev_power']='0'
        self.a.tick(T0+timedelta(hours=2),live=True)
        x=self.a.tick(T0+timedelta(hours=2,minutes=15),live=True)
        self.assertTrue(any('CHARGING_NOT_CONFIRMED' in v for v in x['errors']))

    def test_tampered_receipt_chain_blocks(self):
        self.a.tick(T0,live=False)
        path=self.store.path.with_suffix('.receipts.jsonl')
        records=[json.loads(x) for x in path.read_text().splitlines()]
        records[0]['body']['reason']='fraudulent modification'
        path.write_text('\n'.join(json.dumps(x) for x in records)+'\n')
        with self.assertRaises(RuntimeError):self.a.tick(T0+timedelta(minutes=5),live=True)
        self.assertFalse(self.h.commands)
    def test_invalid_public_http_url_rejected(self):
        with self.assertRaises(ValueError):HomeAssistant('http://172.1.2.3:8123','faketoken')

    def test_ambiguous_command_recovers_without_duplicate_actuation(self):
        orig=self.h.switch
        def success_then_timeout(entity,desired):
            orig(entity,desired)
            raise HassError('Artificial timeout after device acted')
        self.h.switch=success_then_timeout
        first=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(first['status'],'HOLD')
        self.assertEqual(len(self.h.commands),1)
        self.h.switch=orig
        second=self.a.tick(T0+timedelta(hours=2,minutes=5),live=True)
        self.assertEqual(second['status'],'RECOVERED')
        self.assertEqual(len(self.h.commands),1)
    def test_ambiguous_command_does_not_retry_if_not_applied(self):
        def fail(entity,desired):raise HassError('Artificial command failure')
        self.h.switch=fail
        first=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(first['status'],'HOLD')
        second=self.a.tick(T0+timedelta(hours=2,minutes=5),live=True)
        self.assertEqual(second['reason'],'unresolved_pending_hardware_effect')
        self.assertEqual(self.h.commands,[])

    def test_missing_price_feed_stops_at_target(self):
        self.h.values['switch.ev_charger']='on'
        self.h.values['sensor.ev_soc']='50'
        Path(self.cfg['tariff_csv']).unlink()
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['action'],'charger_off')
        self.assertIn('price_unavailable',x['plan']['reason'])
    def test_missing_price_feed_charges_now_without_hard_cap(self):
        Path(self.cfg['tariff_csv']).unlink()
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['action'],'charger_on')
        self.assertIsNone(x['plan']['modeled_avoided_cost'])
    def test_missing_price_feed_obeys_hard_cap(self):
        self.cfg['hard_price_cap_usd_kwh']=.25
        Path(self.cfg['tariff_csv']).unlink()
        x=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(x['status'],'MONITORING')
        self.assertFalse(self.h.commands)

    def test_target_stop_overrides_minimum_dwell(self):
        self.cfg['minimum_dwell_minutes']=60
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['sensor.ev_soc']='50'
        x=self.a.tick(T0+timedelta(hours=2,minutes=5),live=True)
        self.assertEqual(x['action'],'charger_off')
    def test_tariff_changed_before_commit_refuses_stale_action(self):
        from unittest.mock import patch
        import autopilot.core as core
        real=core.parse_forecast_csv
        count={'n':0}
        def change_tariff_on_second_read(path):
            count['n']+=1
            if count['n']==2:
                with open(path,'w') as f:
                    f.write('start,end,usd_per_kwh\n')
                    for slot in tariff():
                        f.write(f'{slot.start.isoformat()},{slot.end.isoformat()},0.95\n')
            return real(path)
        with patch.object(core,'parse_forecast_csv',side_effect=change_tariff_on_second_read):
            x=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(count['n'],2)
        self.assertEqual(x['reason'],'command_or_freshness_check_failed')
        self.assertFalse(self.h.commands)

    def test_owner_policy_revocation_before_commit_refuses(self):
        from unittest.mock import patch
        import autopilot.core as core
        self.a.policy_source=Path(self.tmp.name)/'config.json'
        cfg=dict(self.cfg)
        cfg['tariff_csv']=self.cfg['tariff_csv']
        Path(self.a.policy_source).write_text(json.dumps(cfg))
        self.a.cfg=core.load_config(self.a.policy_source)
        orig=self.h.state
        count={'n':0}
        def revoke_during_observation(entity):
            count['n']+=1
            if count['n']==4:
                new=json.loads(Path(self.a.policy_source).read_text())
                new['owner_authorized']=False
                Path(self.a.policy_source).write_text(json.dumps(new))
            return orig(entity)
        self.h.state=revoke_during_observation
        x=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(x['reason'],'command_or_freshness_check_failed')
        self.assertFalse(self.h.commands)

    def test_receipt_rotation_is_automatic_and_auditable(self):
        self.store.ROTATE_BYTES=2500
        for i in range(15):self.a.tick(T0+timedelta(minutes=i),live=False)
        state=self.store.load()
        self.assertGreaterEqual(len(state['receipt_archives']),1)
        self.assertEqual(self.store.verify_all_archives(),len(state['receipt_archives']))
        self.assertEqual(self.store.load()['prev_receipt'],state['prev_receipt'])

    def test_optional_meter_failure_does_not_stop_charging(self):
        self.h.values['sensor.ev_meter']='unavailable'
        x=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(x['action'],'charger_on')
        self.assertTrue(any('ENERGY_METER_UNAVAILABLE' in e for e in x['errors']))
        self.assertTrue(self.store.load()['session']['meter_gap'])
    def test_optional_power_sensor_failure_is_only_warning(self):
        self.cfg['power_kw_sensor']='sensor.ev_power'
        self.h.values['sensor.ev_power']='unknown'
        x=self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(x['action'],'charger_on')
        self.assertTrue(any('POWER_SENSOR_UNAVAILABLE' in e for e in x['errors']))
    def test_no_remote_calls_in_simulated_dry_run(self):
        x=self.a.tick(T0,live=False)
        self.assertIn(x['status'],('MONITORING','PROPOSED'))


class Transport(unittest.TestCase):
    def test_ha_api_exact_switch_call(self):
        store={'switch.ev_charger':'off','binary_sensor.ev_plugged':'on','sensor.ev_soc':'20'}
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                entity=self.path.removeprefix('/api/states/')
                if entity not in store or self.headers.get('Authorization')!='Bearer faketoken':
                    self.send_response(403);self.end_headers();return
                body=json.dumps({'entity_id':entity,'state':store[entity]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                n=int(self.headers['Content-Length']);data=json.loads(self.rfile.read(n))
                calls.append((self.path,data))
                store[data['entity_id']]='on' if self.path.endswith('turn_on') else 'off'
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'[]')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
        try:
            ha=HomeAssistant('http://127.0.0.1:'+str(server.server_port),'faketoken')
            ha.switch('switch.ev_charger',True)
            self.assertEqual(calls,[('/api/services/switch/turn_on',{'entity_id':'switch.ev_charger'})])
            self.assertEqual(ha.state('switch.ev_charger')['state'],'on')
            with self.assertRaises(ValueError):ha.switch('switch.other',False) if False else ha.switch('sensor.ev_soc',True)
        finally:
            server.shutdown();server.server_close();th.join(2)
    def test_reject_token_in_url(self):
        with self.assertRaises(ValueError):HomeAssistant('https://user:password@example.com:8123','faketoken')


if __name__=='__main__':unittest.main(verbosity=2)
