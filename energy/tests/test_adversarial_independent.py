"""Independent adversarial regressions. Local fake Home Assistant, no hardware."""
import sys, tempfile, unittest, json
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autopilot.core import Autopilot, Store, departure_datetime
from autopilot.hass import HassError
from test_autopilot import FakeHass, config, tariff, T0

class IndependentAttack(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.h=FakeHass();self.cfg=config()
        p=Path(self.tmp.name)/'rates.csv'
        p.write_text('start,end,usd_per_kwh\n'+''.join(f'{s.start.isoformat()},{s.end.isoformat()},{s.usd_kwh}\n' for s in tariff()))
        self.cfg['tariff_csv']=str(p)
        self.s=Store(Path(self.tmp.name)/'state.json')
        self.a=Autopilot(self.cfg,self.h,self.s)
    def test_failed_off_remains_on_and_next_tick_must_retry_protective_off(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(self.h.values['switch.ev_charger'],'on')
        self.h.values['sensor.ev_soc']='50'
        orig=self.h.switch
        def reject_off(entity,on):
            if not on:raise HassError('command rejected')
            return orig(entity,on)
        self.h.switch=reject_off
        fail=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertEqual(fail['status'],'HOLD')
        self.assertTrue(self.s.load()['pending_effect'])
        self.h.switch=orig
        recovered=self.a.tick(T0+timedelta(hours=3,minutes=5),live=True)
        self.assertEqual(self.h.values['switch.ev_charger'],'off','Controller must not leave its own failed OFF pending forever')
        self.assertEqual(recovered['status'],'RECOVERED')
    def test_stale_soc_blocks_new_charger_on(self):
        self.cfg['max_sensor_age_minutes']=10
        self.h.revision=(T0-timedelta(hours=1)).isoformat()
        r=self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertNotEqual(r.get('action'),'charger_on')
        self.assertEqual(self.h.values['switch.ev_charger'],'off')
        self.assertIn('stale',str(r).lower())
    def test_unreliable_meter_jump_cannot_create_cost_or_savings(self):
        self.a.tick(T0,live=False)
        self.h.values['sensor.ev_meter']='10000'
        self.h.values['sensor.ev_soc']='50'
        r=self.a.tick(T0+timedelta(minutes=15),live=False)
        self.assertNotIn('modeled_difference_to_immediate_usd',r)
        self.assertTrue(self.s.load()['session'].get('meter_gap'))
    def test_ambiguous_dst_fall_back_departure_rejected(self):
        t=datetime(2026,11,1,7,30,tzinfo=timezone.utc) # 00:30 PDT before fallback
        with self.assertRaises(ValueError):
            departure_datetime(t,'01:30','America/Los_Angeles')
    def test_target_changed_after_approval_requires_reauthorization(self):
        # Owner-approved config fields are user input; a new entity should invalidate prior scope.
        from autopilot.core import load_config
        from autopilot.onboard import authorization_scope
        c=dict(self.cfg)
        f=Path(self.tmp.name)/'config.json';f.write_text(json.dumps(c))
        c=load_config(f);c['authorization_scope_sha256']=authorization_scope(c)
        f.write_text(json.dumps(c))
        good=load_config(f)
        self.assertEqual(good['charger_switch'],'switch.ev_charger')
        c['charger_switch']='switch.neighbor_charger';f.write_text(json.dumps(c))
        with self.assertRaises(ValueError):load_config(f)
    def test_unplugged_after_controller_on_disables_approved_switch(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertEqual(self.h.values['switch.ev_charger'],'on')
        self.h.values['binary_sensor.ev_plugged']='off'
        r=self.a.tick(T0+timedelta(hours=2,minutes=5),live=True)
        self.assertEqual(self.h.values['switch.ev_charger'],'off')
        self.assertEqual(r['action'],'charger_off')
    def test_failed_off_plus_missing_soc_must_attempt_protective_off(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['sensor.ev_soc']='50'
        original=self.h.switch
        self.h.switch=lambda entity,on: (_ for _ in ()).throw(HassError('offline')) if not on else original(entity,on)
        self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertTrue(self.s.load()['pending_effect'])
        self.h.switch=original
        self.h.values['sensor.ev_soc']='unavailable'
        r=self.a.tick(T0+timedelta(hours=3,minutes=5),live=True)
        self.assertEqual(self.h.values['switch.ev_charger'],'off')
        self.assertEqual(r['action'],'charger_off')
    def test_malicious_config_url_change_revokes_prior_scope(self):
        from autopilot.core import load_config, authorization_scope
        c=dict(self.cfg)
        p=Path(self.tmp.name)/'cfg.json';p.write_text(json.dumps(c))
        c=load_config(p)
        c['authorization_scope_sha256']=authorization_scope(c)
        p.write_text(json.dumps(c))
        c['home_assistant_url']='https://attacker.example'
        p.write_text(json.dumps(c))
        with self.assertRaises(ValueError):load_config(p)
    def test_owner_can_explicitly_reapprove_changed_scope(self):
        from autopilot.core import load_config, authorization_scope
        from autopilot.onboard import authorize
        from unittest.mock import patch
        c=dict(self.cfg)
        p=Path(self.tmp.name)/'cfg.json';p.write_text(json.dumps(c))
        c=load_config(p);c['authorization_scope_sha256']=authorization_scope(c)
        c['charger_switch']='switch.changed_charger'
        p.write_text(json.dumps(c))
        with patch('builtins.input',return_value='APPROVE CHARGER AUTOMATION'):
            authorize(p)
        self.assertEqual(load_config(p)['charger_switch'],'switch.changed_charger')
    def test_manual_pause_reports_switch_still_on(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['input_boolean.ev_pause']='on'
        r=self.a.tick(T0+timedelta(hours=2,minutes=5),live=True)
        self.assertEqual(r['status'],'PAUSED')
        self.assertEqual(self.h.values['switch.ev_charger'],'on')
        self.assertTrue(any('ON' in t for t in r['errors']))
    def test_stale_pause_sensor_cannot_authorize_on(self):
        self.cfg['max_sensor_age_minutes']=10
        now=T0+timedelta(hours=3)
        self.h.revision=now.isoformat()
        orig=self.h.state
        def altered(entity):
            r=orig(entity)
            if entity=='input_boolean.ev_pause':
                r['last_updated']=(now-timedelta(hours=2)).isoformat()
            return r
        self.h.state=altered
        r=self.a.tick(now,live=True)
        self.assertEqual(self.h.values['switch.ev_charger'],'off')
        self.assertNotEqual(r.get('action'),'charger_on')
    def test_stale_price_sensor_never_certifies_forecast(self):
        self.cfg['max_sensor_age_minutes']=10
        self.cfg['price_forecast_entity']='sensor.ev_rates'
        now=T0+timedelta(hours=3)
        self.h.revision=now.isoformat()
        orig=self.h.state
        def altered(entity):
            if entity=='sensor.ev_rates':
                return {'entity_id':entity,'state':'ok',
                        'last_updated':(now-timedelta(hours=2)).isoformat(),
                        'attributes':{'prices':[{'start':p.start.isoformat(),
                              'end':p.end.isoformat(),'usd_per_kwh':p.usd_kwh} for p in tariff()]}}
            return orig(entity)
        self.h.state=altered
        r=self.a.tick(now,live=True)
        self.assertTrue(any('PRICE_EVIDENCE_UNAVAILABLE' in e for e in r['errors']))
        self.assertIsNone(r['plan']['modeled_avoided_cost'])
    def test_crash_after_off_command_reconciles_without_duplicate_off(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['sensor.ev_soc']='50'
        old=self.h.switch
        def crash_after_off(entity,on):
            old(entity,on)
            if not on:raise KeyboardInterrupt('simulate process kill after actuation')
        self.h.switch=crash_after_off
        with self.assertRaises(KeyboardInterrupt):
            self.a.tick(T0+timedelta(hours=3),live=True)
        self.assertTrue(self.s.load().get('pending_effect'))
        self.h.switch=old
        n=len(self.h.commands)
        r=Autopilot(self.cfg,self.h,Store(self.s.path)).tick(T0+timedelta(hours=3,minutes=5),live=True)
        self.assertEqual(r['status'],'RECOVERED')
        self.assertEqual(n,len(self.h.commands))
    def test_crash_after_on_command_never_duplicates_on(self):
        old=self.h.switch
        def crash_after_on(entity,on):
            old(entity,on)
            if on:raise KeyboardInterrupt('simulate kill after hardware action')
        self.h.switch=crash_after_on
        with self.assertRaises(KeyboardInterrupt):
            self.a.tick(T0+timedelta(hours=2),live=True)
        self.assertTrue(self.s.load().get('pending_effect'))
        self.h.switch=old
        n=len(self.h.commands)
        r=Autopilot(self.cfg,self.h,Store(self.s.path)).tick(T0+timedelta(hours=2,minutes=5),live=True)
        self.assertEqual(r['status'],'RECOVERED')
        self.assertEqual(n,len(self.h.commands))
    def test_failed_off_retries_are_bounded_then_owner_alerted(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['sensor.ev_soc']='50'
        attempts={'off':0}
        original=self.h.switch
        def never_off(entity,on):
            if not on:
                attempts['off']+=1
                raise HassError('still on')
            return original(entity,on)
        self.h.switch=never_off
        outputs=[self.a.tick(T0+timedelta(hours=3,minutes=i*5),live=True) for i in range(8)]
        self.assertEqual(attempts['off'],4)  # initial + three bounded retries
        self.assertEqual(self.h.values['switch.ev_charger'],'on')
        self.assertEqual(outputs[-1]['status'],'HOLD')
        self.assertTrue(any('CHARGER MAY REMAIN ON' in e for e in outputs[-1]['errors']))
    def test_pending_off_does_not_cross_entity_change(self):
        self.a.tick(T0+timedelta(hours=2),live=True)
        self.h.values['sensor.ev_soc']='50'
        original=self.h.switch
        self.h.switch=lambda entity,on: (_ for _ in ()).throw(HassError('off failed')) if not on else original(entity,on)
        self.a.tick(T0+timedelta(hours=3),live=True)
        self.h.switch=original
        self.h.values['switch.other_charger']='on'
        self.cfg['charger_switch']='switch.other_charger'
        r=self.a.tick(T0+timedelta(hours=3,minutes=5),live=True)
        self.assertEqual(r['reason'],'pending_target_changed_owner_review_required')
        self.assertEqual(self.h.values['switch.other_charger'],'on')
        self.assertFalse(any(entity=='switch.other_charger' for entity,on in self.h.commands))
if __name__=='__main__':unittest.main()
