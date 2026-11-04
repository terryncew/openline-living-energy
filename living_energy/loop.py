"""Local-only Living Energy mission loop; no physical charger or network clients.

The Energy Autopilot remains the scheduler and durable effect controller.
This layer records observations, constraints, pause-vs-stop, and agent handoffs.
It deliberately does not possess authority to approve arbitrary code repairs.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import hashlib, json, os, tempfile, fcntl, functools, threading
from contextlib import contextmanager
from autopilot.core import Autopilot, Store, parse_forecast_csv
from autopilot.hass import HassError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def write_atomic(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name=tempfile.mkstemp(dir=str(path.parent), prefix='.next-')
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(obj,f,sort_keys=True,allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
        # Ensure the directory entry is durable across process/power interruption.
        dirfd=os.open(str(path.parent),os.O_RDONLY)
        try: os.fsync(dirfd)
        finally: os.close(dirfd)
    finally:
        if os.path.exists(name): os.unlink(name)


def exclusive_mission(fn):
    """Serialize all mission mutations across instances and processes."""
    @functools.wraps(fn)
    def wrapped(self,*args,**kwargs):
        with self._exclusive():
            return fn(self,*args,**kwargs)
    return wrapped


class FakeHomeAssistant:
    """Explicitly simulated hardware API; no network or real HA credentials."""
    def __init__(self, state: dict, now: datetime):
        self.values=state
        self.now=now
        self.commands=[]
        self.fail_next_off=False
    def state(self,entity):
        sensors={
            'switch.ev_charger':'on' if self.values['switch_on'] else 'off',
            'binary_sensor.ev_plugged':'on' if self.values['plugged'] else 'off',
            'sensor.ev_soc':str(self.values['soc']),
            'sensor.ev_meter':str(self.values['meter_kwh']),
            'input_boolean.ev_pause':'on' if self.values['paused'] else 'off',
        }
        if entity not in sensors: raise HassError('Simulation entity not present')
        return {'entity_id':entity,'state':sensors[entity],
                'last_updated':self.now.isoformat()}
    def switch(self,entity,on):
        if entity!='switch.ev_charger': raise HassError('Simulation exact target mismatch')
        if not on and self.fail_next_off:
            self.fail_next_off=False
            raise HassError('Injected simulated OFF failure')
        self.values['switch_on']=bool(on)
        self.commands.append(('SIMULATED',entity,bool(on)))
        if self.state(entity)['state']!=('on' if on else 'off'):
            raise HassError('Simulated readback mismatch')


DEFAULT_CFG={
 'home_assistant_url':'http://localhost:8123',
 'charger_switch':'switch.ev_charger','plugged_sensor':'binary_sensor.ev_plugged',
 'soc_sensor':'sensor.ev_soc','energy_meter_sensor':'sensor.ev_meter',
 'pause_entity':'input_boolean.ev_pause', 'battery_kwh':20.0,
 'charge_power_kw':4.0,'efficiency':0.9,'target_soc_percent':50.0,
 'minimum_dwell_minutes':0,'max_actions_per_day':24,'max_sensor_age_minutes':10,
 'owner_authorized':True,'timezone':'UTC','departure_local':'23:00',
 # Every simulated owner must choose; absent is a refusal, not hidden default.
 'missing_tariff_policy':'HOLD',
}
OWNER_FIELDS=tuple(sorted(DEFAULT_CFG))+('tariff_csv',)


class LivingEnergy:
    def __init__(self, root: Path):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.mission_path=self.root/'mission.json'
        self.cfg_path=self.root/'owner_policy.json'
        self.tick_intent_path=self.root/'pending-controller-tick.json'
        self.store=Store(self.root/'energy-controller.json')
        self._thread_lock=threading.RLock()
        self._exclusive_depth=0
        self._exclusive_fd=None

    @contextmanager
    def _exclusive(self):
        with self._thread_lock:
            if self._exclusive_depth == 0:
                fd=os.open(str(self.root/'.mission.lock'),os.O_CREAT|os.O_RDWR,0o600)
                try:fcntl.flock(fd,fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                self._exclusive_fd=fd
            self._exclusive_depth+=1
            try: yield
            finally:
                self._exclusive_depth-=1
                if self._exclusive_depth==0:
                    fcntl.flock(self._exclusive_fd,fcntl.LOCK_UN)
                    os.close(self._exclusive_fd)
                    self._exclusive_fd=None

    @classmethod
    def create(cls, root, tariff_csv, *, tariff_policy='HOLD'):
        inst=cls(Path(root))
        # The owner mandate and the genesis checkpoint are one initialization.
        # Competing creators must not rewrite the policy after the winning
        # mission commits, even if the losing creator passed an earlier exists().
        with inst._exclusive():
            if inst.mission_path.exists(): raise ValueError('Mission already exists; use resume')
            if tariff_policy not in ('HOLD','CHARGE_TO_DEADLINE'):
                raise ValueError('Explicit owner tariff fallback required')
            cfg=dict(DEFAULT_CFG,tariff_csv=str(Path(tariff_csv).resolve()),missing_tariff_policy=tariff_policy)
            write_atomic(inst.cfg_path,cfg)
            policy_pin=inst._policy_pin(cfg)
            genesis=dict(simulation_only=True,status='ACTIVE',
                observations=[],sequence=0,prev_receipt='',owner_policy_pin=policy_pin,
                state=dict(soc=20.0,plugged=True,switch_on=False,meter_kwh=100.0,paused=False,
                           last_at=None),pending_stop=False)
            inst._receipt(genesis,'SIMULATION_GENESIS',{'policy_pin':policy_pin})
        return inst

    def _config(self):
        cfg=json.loads(self.cfg_path.read_text())
        if not isinstance(cfg,dict):
            raise ValueError('Owner policy must be JSON object')
        if cfg.get('missing_tariff_policy') not in ('HOLD','CHARGE_TO_DEADLINE'):
            raise ValueError('Owner tariff preference missing or invalid')
        return cfg

    @staticmethod
    def _policy_pin(cfg):
        return digest({k:cfg.get(k) for k in OWNER_FIELDS})

    @staticmethod
    def _state_commitment(m):
        # Bind consequential *current* mission state to the receipt chain. Receipt
        # hashes alone do not detect edits to fields outside observations[].
        return digest({k:m.get(k) for k in ('simulation_only','status','state',
                                              'pending_stop','owner_policy_pin')})

    def _mission(self):
        obj=json.loads(self.mission_path.read_text())
        if not obj.get('simulation_only') or not isinstance(obj.get('state'),dict):
            raise RuntimeError('No verified simulation mission')
        prev=''
        for i,record in enumerate(obj['observations']):
            item={k:v for k,v in record.items() if k!='hash'}
            if record.get('previous')!=prev or digest(item)!=record.get('hash'):
                raise RuntimeError('Living Energy receipt chain invalid')
            prev=record['hash']
        if prev!=obj['prev_receipt'] or len(obj['observations'])!=obj['sequence']:
            raise RuntimeError('Living Energy journal mismatch')
        if not obj['observations'] or obj['observations'][-1].get('state_commitment') != self._state_commitment(obj):
            raise RuntimeError('Living Energy state commitment invalid')
        return obj

    @exclusive_mission
    def _receipt(self, m, kind, payload):
        if self.mission_path.exists():
            disk=json.loads(self.mission_path.read_text())
            if (disk.get('sequence')!=m['sequence'] or
                disk.get('prev_receipt')!=m['prev_receipt']):
                raise RuntimeError('CONCURRENT_MISSION_UPDATE_REFUSED')
        body=dict(seq=m['sequence']+1,kind=kind,payload=payload,previous=m['prev_receipt'],
                  state_commitment=self._state_commitment(m))
        body['hash']=digest(body)
        m['observations'].append(body);m['sequence']+=1;m['prev_receipt']=body['hash']
        write_atomic(self.mission_path,m)
        return body

    @exclusive_mission
    def set_plugged(self, plugged):
        if type(plugged) is not bool: raise TypeError('simulator plugged event must be boolean')
        m=self._mission()
        m['state']['plugged']=bool(plugged)
        self._receipt(m,'SIMULATOR_EVENT',{'plugged':bool(plugged)})

    @exclusive_mission
    def override_simulated_switch(self,on):
        if type(on) is not bool: raise TypeError('simulator switch override must be boolean')
        m=self._mission()
        m['state']['switch_on']=bool(on)
        self._receipt(m,'SIMULATOR_EXTERNAL_OVERRIDE',{'on':bool(on)})

    def _advance_simulation(self,m,now,cfg):
        state=m['state']
        if state['last_at']:
            old=datetime.fromisoformat(state['last_at'])
            delta=(now-old).total_seconds()/3600
            if delta<0 or delta>24:raise ValueError('Simulation clock must advance by 0..24 hours')
            if state['switch_on'] and state['plugged']:
                grid=cfg['charge_power_kw']*delta
                state['meter_kwh']+=grid
                state['soc']=min(100.,state['soc']+grid*cfg['efficiency']/cfg['battery_kwh']*100)
        state['last_at']=now.isoformat()

    @exclusive_mission
    def observe(self,now:datetime):
        if now.tzinfo is None:raise ValueError('Aware time required')
        now=now.astimezone(timezone.utc)
        m=self._mission()
        try:
            cfg=self._config()
        except (OSError, ValueError, TypeError, AttributeError):
            # An unreadable/invalid owner mandate never silently leaves a previously
            # enabled simulated charger running. The protective OFF addresses ONLY
            # the original hard-pinned simulation entity; no new authorization.
            affected=m['state']['switch_on'] or m.get('pending_stop') or self.tick_intent_path.exists()
            self._receipt(m,'OWNER_POLICY_INVALID',{'action':'PROTECTIVE_OFF' if affected else 'HOLD',
                                                     'simulation_only':True})
            if affected:
                off=self.stop(now)
                return {'status':'HOLD_CONFIG_INVALID_STOPPED' if off['status']=='STOPPED_SIMULATED'
                        else 'STOP_UNCONFIRMED', 'commands':off.get('commands',[]),
                        'reason':'owner_policy_unreadable','stop':off}
            return {'status':'HOLD_CONFIG_INVALID','commands':[],
                    'reason':'owner_policy_unreadable'}
        if self._policy_pin(cfg)!=m['owner_policy_pin']:
            # No new ON under altered authority. A protective OFF targets the
            # original, receiver-pinned simulated charger only.
            switch_on=m['state']['switch_on']
            self._receipt(m,'AUTHORITY_CHANGED',{'status':'HOLD','reason':'owner_policy_modified',
                                                'switch_on':switch_on})
            if switch_on or m.get('pending_stop'):
                off=self.stop(now)
                return {'status':'HOLD_AUTHORITY_CHANGED_STOPPED' if off['status']=='STOPPED_SIMULATED'
                        else 'STOP_UNCONFIRMED','reason':'owner_policy_modified',
                        'commands':off.get('commands',[]),'stop':off}
            return {'status':'HOLD_AUTHORITY_CHANGED','reason':'owner_policy_modified','commands':[]}
        if self.tick_intent_path.exists():
            self._receipt(m,'TICK_UNCERTAIN',{'reason':'previous_controller_effect_not_reconciled'})
            return {'status':'TICK_UNCERTAIN','commands':[],
                    'reason':'explicit_simulated_protective_stop_or_owner_recovery_required'}
        if m.get('pending_stop'):
            # An interrupted protective OFF cannot be called complete.
            self._receipt(m,'STOP_PENDING',{'reason':'reconcile_device_state_before_resuming'})
            return {'status':'STOP_PENDING','commands':[]}
        state=m['state']
        # A stopped project cannot be silently re-enabled by another actor.
        # Protect before simulation energy accrual, otherwise a stopped mission
        # manufactures meter/SOC progress while the switch is already ON.
        if m['status']=='STOPPED':
            if state['switch_on']:
                self._receipt(m,'STOPPED_SWITCH_REENABLED',{'action':'PROTECTIVE_OFF',
                                      'simulation_only':True})
                off=self.stop(now)
                return {'status':'STOPPED_REPROTECTED' if off['status']=='STOPPED_SIMULATED'
                        else 'STOP_UNCONFIRMED','commands':off.get('commands',[]),
                        'reason':'switch_reenabled_after_stop','stop':off}
            self._receipt(m,'STOPPED_OBSERVATION',{'switch_on':False})
            return {'status':'STOPPED','commands':[]}
        self._advance_simulation(m,now,cfg)
        fake=FakeHomeAssistant(state,now)
        if state['paused']:
            self._receipt(m,'PAUSE',{'switch_on':state['switch_on'],'warning':'PAUSE_DOES_NOT_STOP_SWITCH' if state['switch_on'] else None})
            return {'status':'PAUSED_SWITCH_ON' if state['switch_on'] else 'PAUSED','commands':[]}
        # An existent but unreadable / expired tariff is still unavailable.
        # Do not allow the lower-level generic deadline fallback to override the
        # owner's explicit project-level HOLD preference.
        try:
            slots=parse_forecast_csv(cfg['tariff_csv'])
            missing_tariff=not slots or slots[-1].end <= now
        except (ValueError,OSError,KeyError,TypeError):
            missing_tariff=True
        if missing_tariff and cfg['missing_tariff_policy']=='HOLD':
            self._receipt(m,'CONSTRAINT_FOUND',{'constraint':'TARIFF_MISSING','action':'HOLD','switch_on':state['switch_on']})
            if state['switch_on']:
                # HOLD cannot pretend it stopped an already-enabled charger.
                # Halt the simulation with explicit protective OFF and require
                # owner reauthorization to resume after the feed returns.
                result=self.stop(now)
                return {'status':'HOLD_TARIFF_MISSING_STOPPED' if result['status']=='STOPPED_SIMULATED' else 'STOP_UNCONFIRMED',
                        'constraint':'TARIFF_MISSING','commands':result.get('commands',[]),'stop':result}
            return {'status':'HOLD_TARIFF_MISSING','constraint':'TARIFF_MISSING','commands':[]}
        # Explicitly simulated actuation. No Home Assistant network client is instantiated.
        controller_cfg={k:v for k,v in cfg.items() if k!='missing_tariff_policy'}
        # Durable prepare-before-effect boundary. If the process dies after
        # acting but before mission acknowledgment, a replacement refuses
        # to issue a second ON until an explicit safe reconciliation.
        if self.tick_intent_path.exists():
            raise RuntimeError('Unreconciled prior controller intent')
        write_atomic(self.tick_intent_path,{'simulation_only':True,'at':now.isoformat(),
                                            'policy_pin':m['owner_policy_pin'],
                                            'last_receipt':m['prev_receipt']})
        result=Autopilot(controller_cfg,fake,self.store).tick(now,live=True)
        constraint=('CHARGER_EFFECT_UNCERTAIN' if 'UNCONFIRMED' in str(result) else
                    'EVIDENCE_INVALID' if result['status']=='HOLD' else 'NONE')
        self._receipt(m,'OBSERVE_DECIDE',{'result':result,'constraint':constraint,
                    'commands':fake.commands,'simulated':True,'state':dict(state)})
        self.tick_intent_path.unlink()
        return {'status':result['status'],'result':result,'constraint':constraint,'commands':fake.commands,
                'simulated':True,'receipt':m['prev_receipt']}

    @exclusive_mission
    def pause(self):
        m=self._mission()
        m['state']['paused']=True
        self._receipt(m,'OWNER_PAUSE',{'switch_on':m['state']['switch_on'],
                  'warning':'PAUSE_DOES_NOT_STOP_SWITCH' if m['state']['switch_on'] else None})
        return {'status':'PAUSED_SWITCH_ON' if m['state']['switch_on'] else 'PAUSED',
                'switch_on':m['state']['switch_on']}

    @exclusive_mission
    def resume(self):
        m=self._mission()
        if m['status']=='STOPPED':raise ValueError('Stopped mission requires new owner authorization')
        if m.get('pending_stop'):raise ValueError('Cannot resume while protective stop is unconfirmed')
        if self.tick_intent_path.exists():raise ValueError('Cannot resume while prior controller tick intent is uncertain')
        try:
            cfg=self._config()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError('Cannot resume with unreadable or invalid owner policy') from exc
        if self._policy_pin(cfg)!=m.get('owner_policy_pin'):
            raise ValueError('Owner policy modified; reauthorization required before resume')
        m['state']['paused']=False
        self._receipt(m,'OWNER_RESUME',{'simulation_only':True})
        return {'status':'RESUMED'}

    @exclusive_mission
    def stop(self,now:datetime, *, fail_off=False):
        if getattr(now,'tzinfo',None) is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError('Aware time required')
        now=now.astimezone(timezone.utc)
        m=self._mission();state=m['state']
        state['paused']=True
        m['pending_stop']=True
        self._receipt(m,'STOP_INTENT',{'switch_on':state['switch_on'],'simulation_only':True})
        fake=FakeHomeAssistant(state,now);fake.fail_next_off=fail_off
        try:
            fake.switch('switch.ev_charger',False)
            if fake.state('switch.ev_charger')['state']!='off':
                raise HassError('OFF not confirmed')
        except HassError:
            self._receipt(m,'STOP_UNCONFIRMED',{'switch_on':state['switch_on'],'owner_action_required':True})
            return {'status':'STOP_UNCONFIRMED','switch_on':state['switch_on'],
                    'commands':fake.commands,'simulation_only':True}
        m['pending_stop']=False;m['status']='STOPPED'
        self._receipt(m,'STOP_CONFIRMED_SIMULATION',{'commands':fake.commands})
        if self.tick_intent_path.exists():
            self.tick_intent_path.unlink()  # protective stop reconciles uncertain tick
        return {'status':'STOPPED_SIMULATED','switch_on':False,
                'commands':fake.commands,'simulation_only':True}

    @exclusive_mission
    def reconcile_stop(self,now:datetime):
        if getattr(now,'tzinfo',None) is None or now.tzinfo.utcoffset(now) is None:
            raise ValueError('Aware time required')
        m=self._mission()
        if not m['pending_stop']:return {'status':m['status']}
        return self.stop(now)

    def assess(self):
        m=self._mission()
        if m.get('pending_stop'):
            return {'constraint':'STOP_UNCONFIRMED','recommendation':'CONFIRM_SAFE_STOP_BEFORE_FURTHER_ACTION'}
        if self.tick_intent_path.exists():
            return {'constraint':'TICK_UNCERTAIN','recommendation':'QUARANTINE_AND_RECONCILE_TICK_INTENT'}
        try:
            cfg=self._config()
            policy_drift=(self._policy_pin(cfg)!=m.get('owner_policy_pin'))
        except (OSError, ValueError, TypeError, AttributeError):
            return {'constraint':'OWNER_POLICY_INVALID','recommendation':'REVALIDATE_OWNER_POLICY_AND_AUTHORITY'}
        if policy_drift:
            return {'constraint':'AUTHORITY_CHANGED','recommendation':'AWAIT_OWNER_POLICY_REAUTHORIZATION'}
        if m.get('status')=='STOPPED':
            for obs in reversed(m.get('observations',[])):
                k=obs.get('kind','')
                p=obs.get('payload',{})
                if k in ('OWNER_POLICY_INVALID','AUTHORITY_CHANGED'):
                    return {'constraint':k,'recommendation':'AWAIT_OWNER_POLICY_REAUTHORIZATION'}
                if p.get('constraint')=='TARIFF_MISSING':
                    return {'constraint':'TARIFF_MISSING','recommendation':'SEEK_OWNER_TARIFF_PREFERENCE_OR_RESTORE_FEED'}
            return {'constraint':'MISSION_STOPPED','recommendation':'AWAIT_NEW_OWNER_AUTHORIZATION'}
        recent=m['observations'][-1] if m['observations'] else None
        if not recent:return {'constraint':'NONE','recommendation':'OBSERVE'}
        kind=recent.get('kind','')
        p=recent.get('payload',{})
        typ=p.get('constraint') or p.get('status','') or kind
        if kind in ('OWNER_POLICY_INVALID','AUTHORITY_CHANGED') or typ in ('OWNER_POLICY_INVALID','AUTHORITY_CHANGED'):
            return {'constraint':typ if typ in ('OWNER_POLICY_INVALID','AUTHORITY_CHANGED') else kind,
                    'recommendation':'AWAIT_OWNER_POLICY_REAUTHORIZATION'}
        if typ=='TARIFF_MISSING' or p.get('constraint')=='TARIFF_MISSING':
            return {'constraint':'TARIFF_MISSING','recommendation':'SEEK_OWNER_TARIFF_PREFERENCE_OR_RESTORE_FEED'}
        if typ in ('EVIDENCE_INVALID','CHARGER_EFFECT_UNCERTAIN','STOP_UNCONFIRMED','TICK_UNCERTAIN'):
            return {'constraint':typ,'recommendation':'QUARANTINE_AND_PREPARE_REPAIR_PROPOSAL'}
        return {'constraint':'NONE','recommendation':'CONTINUE_SIMULATED_MISSION'}
