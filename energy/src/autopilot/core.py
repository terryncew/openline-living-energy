"""Bounded autonomous EV cost optimizer and evidence ledger.

No UI, no cloud dependency. Owner authorizes exactly one charger switch.
Does not assume HA switch state proves vehicle charging, and records evidence limits.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
from pathlib import Path
import csv, hashlib, json, math, os, re, tempfile
from .hass import check_entity, HassError
from .planning import TariffSlot, plan_charge, validate_slots


def iso(t): return t.astimezone(timezone.utc).isoformat()

def canon(d): return json.dumps(d,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def pin(v):return hashlib.sha256(canon(v).encode('utf-8')).hexdigest()

# A policy-scope fingerprint detects an approved config being edited after consent.
# This is not a cryptographic signature: anyone with permission to rewrite both
# config and fingerprint can forge it. Use protected OS files and limited HA tokens.
APPROVAL_FIELDS = ('home_assistant_url','charger_switch','plugged_sensor','soc_sensor',
                   'energy_meter_sensor','power_kw_sensor','pause_entity',
                   'price_forecast_entity','tariff_csv','battery_kwh','charge_power_kw',
                   'target_soc_percent','efficiency','departure_local','timezone',
                   'hard_price_cap_usd_kwh','minimum_dwell_minutes','max_actions_per_day',
                   'max_sensor_age_minutes')
def authorization_scope(data):
    return pin({name:data.get(name) for name in APPROVAL_FIELDS})


def aware(s):
    dt=datetime.fromisoformat(s.replace('Z','+00:00'))
    if dt.tzinfo is None: raise ValueError('Timestamp must include UTC offset')
    return dt.astimezone(timezone.utc)


def number(raw, label, low, high):
    try: n=float(raw)
    except (ValueError,TypeError):raise ValueError(f'{label} is not numeric') from None
    if not math.isfinite(n) or n<low or n>high:raise ValueError(f'{label} outside expected range')
    return n


def load_config(path, *, allow_stale_authorization=False):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data,dict):raise ValueError('Config must be JSON object')
    for name,domain in (('charger_switch','switch'),('plugged_sensor','binary_sensor'),('soc_sensor','sensor')):
        check_entity(data[name],domain)
    for name,domain in (('energy_meter_sensor','sensor'),('power_kw_sensor','sensor'),('pause_entity','input_boolean'),('price_forecast_entity','sensor')):
        if data.get(name):check_entity(data[name],domain)
    for name,lo,hi in (('battery_kwh',1,250),('charge_power_kw',0.2,22),('target_soc_percent',1,95),('efficiency',.5,1),('minimum_dwell_minutes',0,120)):
        data[name]=number(data[name],name,lo,hi)
    data['timezone']=str(data.get('timezone','UTC'))
    ZoneInfo(data['timezone'])
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',data['departure_local']):raise ValueError('departure_local must be HH:MM')
    if 'owner_authorized' not in data or not isinstance(data['owner_authorized'],bool):raise ValueError('owner_authorized must be explicit boolean')
    data['max_actions_per_day']=int(number(data.get('max_actions_per_day',24),'max_actions_per_day',1,96))
    if data.get('hard_price_cap_usd_kwh') is not None:
        data['hard_price_cap_usd_kwh']=number(data['hard_price_cap_usd_kwh'],'hard_price_cap_usd_kwh',-2,5)
    if data.get('home_assistant_url') is None: raise ValueError('Missing Home Assistant URL')
    if not data.get('price_forecast_entity') and not data.get('tariff_csv'):
        raise ValueError('tariff_csv or price_forecast_entity required')
    if data.get('tariff_csv'):
        p=Path(data['tariff_csv'])
        if not p.is_absolute():data['tariff_csv']=str((Path(path).parent/p).resolve())
    if data.get('max_sensor_age_minutes') is not None:
        data['max_sensor_age_minutes']=number(data['max_sensor_age_minutes'],'max_sensor_age_minutes',0.1,120)
    if 'authorization_scope_sha256' in data:
        if data['authorization_scope_sha256']!=authorization_scope(data):
            if not allow_stale_authorization:
                raise ValueError('Owner approval scope does not match configured charger or policy; reauthorize')
            data['owner_authorized']=False
            data.pop('authorization_scope_sha256',None)
    return data


def parse_forecast_csv(path):
    p=Path(path)
    if p.stat().st_size>1_000_000:raise ValueError('Tariff file exceeds 1 MB')
    with p.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not set(('start','end','usd_per_kwh')).issubset(set(reader.fieldnames or [])):
            raise ValueError('Tariff CSV columns: start,end,usd_per_kwh')
        slots=[]
        for row in reader:
            slots.append(TariffSlot(aware(row['start']),aware(row['end']),number(row['usd_per_kwh'],'tariff price',-2,5)))
            if len(slots)>7*24*12:raise ValueError('Tariff CSV excessive')
    return validate_slots(slots)


def parse_forecast_entity(state):
    attr=state.get('attributes') or {}
    rates=attr.get('prices')
    if not isinstance(rates,list) or len(rates)<1 or len(rates)>7*24*12:
        raise ValueError('Price sensor must expose attributes.prices list')
    return validate_slots([TariffSlot(aware(row['start']),aware(row['end']),number(row['usd_per_kwh'],'tariff price',-2,5)) for row in rates])


def departure_datetime(now, local_clock, zone):
    here=now.astimezone(ZoneInfo(zone))
    hour,minute=map(int,local_clock.split(':'))
    cand=here.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if cand<=here:cand=cand+timedelta(days=1)
    # Refuse fold ambiguity. A local departure clock does not define which of
    # two distinct UTC instants the owner intended on fall-back night.
    if cand.replace(fold=0).utcoffset()!=cand.replace(fold=1).utcoffset():
        raise ValueError('Departure local time ambiguous during DST fall-back')
    # Validate a DST-normalized instant, and fail closed on ambiguous local time.
    back=cand.astimezone(timezone.utc).astimezone(ZoneInfo(zone))
    if (back.hour,back.minute)!=(hour,minute):raise ValueError('Departure local time falls into DST gap')
    return cand.astimezone(timezone.utc)


def state_num(state,label,lo,hi):
    if state.get('state') in ('unknown','unavailable',None):raise ValueError(label+' unavailable')
    return number(state['state'],label,lo,hi)


def state_bool(state,label):
    s=state.get('state')
    if s not in ('on','off'):raise ValueError(label+' unavailable')
    return s=='on'


def verify_fresh_state(state,label,now,config):
    maximum=config.get('max_sensor_age_minutes')
    if maximum is None: return  # Legacy inputs; pilot release still requires configured freshness.
    stamp=state.get('last_updated')
    if not stamp: raise ValueError(label+' freshness unavailable')
    try: age=(now-aware(stamp)).total_seconds()
    except (TypeError,ValueError): raise ValueError(label+' freshness invalid') from None
    if age < -60 or age > 60*maximum:
        raise ValueError(label+' stale: last_updated outside authorized freshness window')


class Store:
    ROTATE_BYTES=1_000_000

    @contextmanager
    def locked(self):
        lockpath=self.path.with_suffix('.lock')
        fd=os.open(lockpath,os.O_CREAT|os.O_RDWR,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)

    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)

    def _archive_ok(self,item):
        file=self.path.parent/item['name']
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest()!=item['sha256']:
            raise RuntimeError('Receipt archive missing or corrupted; refuse writes')

    def verify_all_archives(self):
        obj=self.load()
        for item in obj.get('receipt_archives',[]):self._archive_ok(item)
        return len(obj.get('receipt_archives',[]))

    def load(self):
        if not self.path.exists():
            return {'session':None,'last_command':None,'actions_today':{},'paused':False,
                    'prev_receipt':'','receipt_anchor':'','receipt_archives':[]}
        obj=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(obj,dict) or not all(k in obj for k in ('session','last_command','actions_today','paused','prev_receipt')):
            raise RuntimeError('Corrupt controller state; refuse writes')
        # Hash chain detects accidental corruption and ordinary tampering, not an
        # attacker who rewrites state and logs together (not a third-party signature).
        archives=obj.get('receipt_archives',[])
        if archives:self._archive_ok(archives[-1])
        previous=obj.get('receipt_anchor','')
        rp=self.path.with_suffix('.receipts.jsonl')
        if rp.exists():
            if rp.stat().st_size>self.ROTATE_BYTES+20000:
                raise RuntimeError('Receipt log unexpectedly oversized; refuse writes')
            for line in rp.read_text(encoding='utf-8').splitlines():
                record=json.loads(line)
                h=record.pop('hash',None)
                if record.get('previous_receipt')!=previous or h!=pin(record):
                    raise RuntimeError('Receipt chain invalid; refuse writes')
                previous=h
        if previous!=obj['prev_receipt']:
            raise RuntimeError('State/receipt mismatch; refuse writes')
        return obj

    def save(self,obj):
        with tempfile.NamedTemporaryFile(mode='w',dir=self.path.parent,prefix='.state-',delete=False,encoding='utf-8') as f:
            os.chmod(f.name,0o600)
            f.write(canon(obj)+'\n');f.flush();os.fsync(f.fileno());tmp=f.name
        os.replace(tmp,self.path)

    def receipt(self,doc,obj):
        record={'seq_time':doc['time'],'previous_receipt':obj['prev_receipt'],'body':doc}
        record['hash']=pin(record)
        rp=self.path.with_suffix('.receipts.jsonl')
        with open(rp,'a',encoding='utf-8') as f:
            os.chmod(rp,0o600)
            f.write(canon(record)+'\n');f.flush();os.fsync(f.fileno())
        obj['prev_receipt']=record['hash']
        if rp.stat().st_size>=self.ROTATE_BYTES:
            digest=hashlib.sha256(rp.read_bytes()).hexdigest()
            archived=rp.with_name(rp.stem+'.archived.'+digest[:16]+'.jsonl')
            if archived.exists():raise RuntimeError('Refusing to overwrite prior receipt archive')
            os.replace(rp,archived)
            obj.setdefault('receipt_archives',[]).append({'name':archived.name,'sha256':digest,'end_tip':record['hash']})
            obj['receipt_anchor']=record['hash']
        self.save(obj)
        return record


class Autopilot:
    def __init__(self, cfg, hass, store, policy_source=None):
        self.cfg,self.hass,self.store=cfg,hass,store
        self.policy_source=policy_source

    def tick(self,now:datetime, *, live=False):
        with self.store.locked():
            return self._tick(now,live=live)

    def _protective_off_on_sensor_loss(self, now, data, config, live, doc):
        """Only turn OFF a switch previously turned ON by this controller.

        This cannot guarantee safe physical current cutoff when HA is offline.
        Durable intent must precede all attempts. Manual pause always wins.
        """
        # A crashed/failed OFF still needs a bounded path to safety even when
        # another sensor becomes unavailable. No independent sensor is needed
        # to confirm the exact approved switch is ON and attempt an idempotent OFF.
        pending=data.get('pending_effect') or {}
        if (live and config.get('owner_authorized') and pending.get('desired') is False
            and pending.get('target')==config['charger_switch']
            and int(pending.get('off_retries',0))<3 and not data.get('paused')):
            try:
                if self.policy_source is not None and (
                    not config.get('authorization_scope_sha256') or
                    pin(load_config(self.policy_source))!=pin(config)):
                    raise HassError('Owner policy/authorization changed')
                if config.get('pause_entity') and state_bool(self.hass.state(config['pause_entity']),'pause'):
                    return False
                if state_bool(self.hass.state(config['charger_switch']),'charger'):
                    pending['off_retries']=int(pending.get('off_retries',0))+1
                    self.store.receipt(dict(doc,status='RETRY_INTENT',action='retry_off',
                        reason='critical_sensor_missing_pending_off'),data)
                    self.hass.switch(config['charger_switch'],False)
                data['last_command']={'at':iso(now),'desired':False,
                                      'session_start':pending.get('session_start')}
                data['pending_effect']=None
                doc.update(status='HOLD',action='charger_off',reason='pending_off_safely_reconciled_sensor_unavailable')
            except (HassError,ValueError,OSError,KeyError) as e:
                doc['errors'].append('PENDING_OFF_NOT_CONFIRMED: '+type(e).__name__)
                doc.update(status='HOLD',reason='pending_off_sensor_loss_owner_action_required')
            return True
        last=data.get('last_command') or {}
        if not (live and config.get('owner_authorized') and last.get('desired') is True
                and last.get('session_start')==(data.get('session') or {}).get('start')
                and not data.get('pending_effect') and not data.get('paused')):
            return False
        try:
            if self.policy_source is not None and pin(load_config(self.policy_source))!=pin(config):
                return False
            if config.get('pause_entity') and state_bool(self.hass.state(config['pause_entity']),'pause'):
                return False
            if not state_bool(self.hass.state(config['charger_switch']),'charger'):
                return False
            data['pending_effect']={'at':iso(now),'desired':False,
                                    'session_start':last.get('session_start'),
                                    'target':config['charger_switch'],'off_retries':0}
            self.store.receipt(dict(doc,status='INTENT',action='pending_off',
                  reason='protective_off_after_critical_sensor_loss'),data)
            self.hass.switch(config['charger_switch'],False)
            data['pending_effect']=None
            data['last_command']={'at':iso(now),'desired':False,
                                  'session_start':last.get('session_start')}
            doc.update(status='HOLD',action='charger_off',
                       reason='protective_off_after_critical_sensor_loss')
        except (HassError,ValueError,OSError,KeyError) as e:
            doc['errors'].append('PROTECTIVE_OFF_NOT_CONFIRMED: '+type(e).__name__)
            doc.update(status='HOLD',reason='protective_off_unconfirmed_owner_action_required')
        return True

    def _tick(self,now:datetime, *, live=False):
        now=now.astimezone(timezone.utc)
        data=self.store.load()
        config=self.cfg
        doc={'time':iso(now),'status':'unknown','desired':None,'observed':None,'action':'none',
             'reason':'','errors':[],'savings_usd':'NOT VERIFIED', 'energy_meter_kwh':None}
        # No network actions in observe-only mode; GET-only observation is authorized on live HA.
        try:
            plugged_state=self.hass.state(config['plugged_sensor'])
            soc_state=self.hass.state(config['soc_sensor'])
            switch_state=self.hass.state(config['charger_switch'])
            for label, reading in (('plugged',plugged_state),('vehicle SOC',soc_state),
                                   ('charger',switch_state)):
                verify_fresh_state(reading,label,now,config)
            plugged=state_bool(plugged_state,'plugged')
            soc=state_num(soc_state,'vehicle SOC',0,100)
            switch_on=state_bool(switch_state,'charger')
            pause_state=self.hass.state(config['pause_entity']) if config.get('pause_entity') else None
            if pause_state:
                verify_fresh_state(pause_state,'pause flag',now,config)
            paused_entity=state_bool(pause_state,'pause flag') if pause_state else False
            power_kw=None
            if config.get('power_kw_sensor'):
                try:
                    reading=self.hass.state(config['power_kw_sensor'])
                    verify_fresh_state(reading,'charger power',now,config)
                    power_kw=state_num(reading,'charger power',0,25)
                except (HassError,ValueError):doc['errors'].append('POWER_SENSOR_UNAVAILABLE: charge confirmation disabled')
            meter=None
            if config.get('energy_meter_sensor'):
                try:
                    reading=self.hass.state(config['energy_meter_sensor'])
                    verify_fresh_state(reading,'charger meter',now,config)
                    meter=state_num(reading,'charger meter',0,1e10)
                except (HassError,ValueError):doc['errors'].append('ENERGY_METER_UNAVAILABLE: cost evidence incomplete')
            doc.update({'observed':switch_on,'soc_percent':soc,'plugged':plugged,'observed_charger_power_kw':power_kw})
        except (ValueError,HassError,OSError,KeyError) as e:
            doc.update(status='HOLD',reason='missing_or_invalid_evidence',errors=[str(e)[:130]])
            self._protective_off_on_sensor_loss(now,data,config,live,doc)
            self.store.receipt(doc,data)
            return doc
        prices=()
        price_error=None
        try:
            if config.get('price_forecast_entity'):
                reading=self.hass.state(config['price_forecast_entity'])
                verify_fresh_state(reading,'tariff forecast',now,config)
                prices=parse_forecast_entity(reading)
            else:
                prices=parse_forecast_csv(config['tariff_csv'])
        except (ValueError,HassError,OSError,KeyError) as e:
            price_error=type(e).__name__
            doc['errors'].append('PRICE_EVIDENCE_UNAVAILABLE: no savings optimization or cost claim')
        tariff_pin=pin([(iso(x.start),iso(x.end),x.usd_kwh) for x in prices]) if prices else ''
        # Recovery of uncertain effects: do not duplicate a command after a crash or
        # an ambiguous HA response. A matching HA state is a limited readback, not
        # physical proof of energy transfer. Otherwise hold for explicit owner review.
        pending=data.get('pending_effect')
        if pending:
            if pending.get('target') and pending['target']!=config['charger_switch']:
                doc.update(status='HOLD',reason='pending_target_changed_owner_review_required')
                self.store.receipt(doc,data)
                return doc
            if switch_on==pending.get('desired'):
                data['last_command']={'at':pending['at'],'desired':switch_on,
                                      'session_start':pending.get('session_start')}
                data['pending_effect']=None
                doc.update(status='RECOVERED',reason='pending_effect_matches_switch_readback',
                           action='reconciled_no_new_command')
            elif (pending.get('desired') is False and switch_on is True and live
                  and config.get('owner_authorized') and
                  pending.get('target')==config['charger_switch'] and
                  int(pending.get('off_retries',0))<3 and
                  not data.get('paused') and not paused_entity):
                # A failed OFF is idempotent. Retry only an already-journaled OFF
                # for the exact approved entity; never retry a doubtful ON.
                try:
                    if self.policy_source is not None:
                        if (not config.get('authorization_scope_sha256') or
                            pin(load_config(self.policy_source))!=pin(config)):
                            raise HassError('Owner policy/authorization changed')
                    pending['off_retries']=int(pending.get('off_retries',0))+1
                    self.store.receipt(dict(doc,status='RETRY_INTENT',action='retry_off',
                                            reason='unresolved_prior_off_command'),data)
                    self.hass.switch(config['charger_switch'],False)
                    data['last_command']={'at':iso(now),'desired':False,
                                          'session_start':pending.get('session_start')}
                    data['pending_effect']=None
                    doc.update(status='RECOVERED',reason='confirmed_protective_off_retry',action='charger_off')
                except (HassError,ValueError,OSError,KeyError) as e:
                    doc.update(status='HOLD',reason='protective_off_retry_unconfirmed',
                               errors=['CHARGER_MAY_REMAIN_ON: '+type(e).__name__])
            else:
                doc.update(status='HOLD',reason='unresolved_pending_hardware_effect',
                           errors=['CHARGER MAY REMAIN ON: owner must inspect and explicitly resolve'])
            self.store.receipt(doc,data)
            return doc
        if data['session'] is None and plugged:
            data['session']={'start':iso(now),'start_soc':soc,'start_meter':meter,
                             'last_meter':meter,'estimated_cost_usd':0.,'unpriced_kwh':0.,
                             'initial_immediate_cost_usd':None,'initial_estimated_need_grid_kwh':None,
                             'last_meter_at':iso(now),'tariff_pin':tariff_pin}
        if not plugged:
            # A charger-enable left ON can re-energize unexpectedly on replug.
            # Disable only our own prior ON, without fighting a human override.
            safety_attempted=(switch_on and self._protective_off_on_sensor_loss(now,data,config,live,doc))
            if data['session']:
                data.setdefault('completed_sessions',[]).append(data['session'])
                data['completed_sessions']=data['completed_sessions'][-25:]
            data['session']=None
            data['last_command']=None
            if not safety_attempted:
                doc.update(status='IDLE',reason='vehicle_not_plugged',energy_meter_kwh=meter)
            else:
                doc['energy_meter_kwh']=meter
            self.store.receipt(doc,data)
            return doc
        session=data['session']
        if meter is None and config.get('energy_meter_sensor'):
            session['meter_gap']=True
        if meter is not None and session.get('last_meter') is None:
            session['meter_gap']=True
            session['last_meter']=meter
            session['last_meter_at']=iso(now)
        if meter is not None and session.get('last_meter') is not None:
            delta=meter-session['last_meter']
            if delta< -1e-6:
                doc.update(status='HOLD',reason='energy_meter_rolled_back')
                self.store.receipt(doc,data)
                return doc
            if delta>0:
                old=aware(session['last_meter_at'])
                elapsed_hours=max(0,(now-old).total_seconds()/3600)
                # Meter spikes / off-state energy cannot certify charger savings.
                # Tolerance permits normal interval measurements with 20% margin.
                if (elapsed_hours<=0 or
                    delta > config['charge_power_kw']*elapsed_hours*1.2+0.5 or
                    (not switch_on and not (data.get('last_command') or {}).get('desired'))):
                    session['meter_gap']=True
                    doc['errors'].append('METER_INCONSISTENT_WITH_PHYSICAL_POWER_OR_SWITCH_STATE')
                matching=[p for p in prices if p.start<=old and p.end>=now]
                if matching and session['tariff_pin']==tariff_pin and not session.get('meter_gap'):
                    session['estimated_cost_usd']+=round(delta*matching[0].usd_kwh,6)
                else:
                    session['unpriced_kwh']+=delta
            session['last_meter']=meter
            session['last_meter_at']=iso(now)
        doc['energy_meter_kwh']=meter
        # A HA switch reading ON is not evidence that the vehicle drew power.
        if switch_on and power_kw is not None and power_kw < .05 and data.get('last_command') and data['last_command'].get('desired'):
            if (now-aware(data['last_command']['at'])).total_seconds() >= 600:
                doc['errors'].append('CHARGING_NOT_CONFIRMED: charger switch on but no measured power for at least 10 minutes')
        deadline=departure_datetime(now,config['departure_local'],config['timezone'])
        need_battery=max(0,(config['target_soc_percent']-soc)/100*config['battery_kwh'])
        need_grid=need_battery/config['efficiency']
        doc.update({'deadline':iso(deadline),'needed_grid_kwh':round(need_grid,4),
                    'customer_target_soc':config['target_soc_percent'],
                    'session_estimated_charge_cost_usd':round(session['estimated_cost_usd'],4),
                    'session_unpriced_kwh':round(session['unpriced_kwh'],4)})
        if data['paused'] or paused_entity:
            if switch_on:
                doc['errors'].append('SWITCH_STILL_ON: pausing automation does not stop an energized charger')
            doc.update(status='PAUSED',reason='owner_pause',desired=switch_on)
            self.store.receipt(doc,data);return doc
        # Respect manual overrides. If a human changed the switch, do not fight it.
        last=data.get('last_command')
        if last is not None and last.get('session_start')==session['start'] and switch_on != last['desired']:
            data['paused']=True
            doc.update(status='PAUSED',reason='external_override_detected',desired=switch_on)
            self.store.receipt(doc,data);return doc
        try:
            if price_error and need_grid>=.01:
                from .planning import Plan
                cap=config.get('hard_price_cap_usd_kwh')
                p=Plan(cap is None,need_grid,0.,0.,None,None,None,
                       ('price_unavailable_charge_now' if cap is None else 'hard_price_cap_no_price_hold'),
                       False,())
            elif price_error and need_grid<.01:
                from .planning import Plan
                p=Plan(False,need_grid,0.,0.,None,None,None,'target_met_price_unavailable',True,())
            else:
                p=plan_charge(now,deadline,need_grid,config['charge_power_kw'],prices,
                              hard_price_cap=config.get('hard_price_cap_usd_kwh'))
            doc['plan']={k:getattr(p,k) for k in ('need_grid_kwh','estimated_plan_cost','immediate_cost','modeled_avoided_cost','reason','feasible_by_deadline','selected_slot_starts')}
            desired=p.charge_now
            # Freeze the modeled 'charge immediately' comparator at plug-in.
            # It is not an observed counterfactual; meter evidence remains separate.
            if session.get('initial_immediate_cost_usd') is None and session['start']==iso(now) and p.feasible_by_deadline and p.immediate_cost is not None:
                session['initial_immediate_cost_usd']=p.immediate_cost
                session['initial_estimated_need_grid_kwh']=p.need_grid_kwh
            if (p.reason in ('target_met','target_met_price_unavailable') and
                session.get('initial_immediate_cost_usd') is not None and
                session.get('last_meter') is not None and not session.get('meter_gap') and session.get('unpriced_kwh',0)<1e-9 and
                tariff_pin and tariff_pin==session['tariff_pin']):
                doc['modeled_difference_to_immediate_usd']=round(session['initial_immediate_cost_usd']-session['estimated_cost_usd'],4)
                doc['counterfactual_status']='MODELED_NOT_OBSERVED_SAVINGS'
        except ValueError as e:
            doc.update(status='HOLD',reason='bad_forecast',errors=[str(e)[:130]])
            self.store.receipt(doc,data);return doc
        doc['desired']=desired
        if not p.feasible_by_deadline:
            doc['errors'].append('Target charge may not be achievable by departure under current limits')
        if p.reason=='hard_price_cap_prevents_deadline':
            doc['errors'].append('Owner hard price cap prevents target; cap was not overridden')
        if desired==switch_on:
            doc.update(status='MONITORING',reason='switch_already_in_desired_state')
            self.store.receipt(doc,data);return doc
        if last:
            elapsed=(now-aware(last['at'])).total_seconds()/60
            if (elapsed<config['minimum_dwell_minutes'] and
                p.reason not in ('insufficient_remaining_capacity_charge_now','deadline_elapsed','target_met','target_met_price_unavailable')):
                doc.update(status='DWELL',reason='minimum_dwell_time',desired=switch_on)
                self.store.receipt(doc,data);return doc
        key=now.date().isoformat()
        # Protective OFF is always permitted even if the ON action budget is exhausted.
        if desired and int(data['actions_today'].get(key,0)) >= config['max_actions_per_day']:
            doc.update(status='HOLD',reason='action_budget_exhausted')
            self.store.receipt(doc,data);return doc
        if not live or not config['owner_authorized']:
            doc.update(status='PROPOSED',reason='dry_run_or_owner_not_authorized',action='proposed_'+('on' if desired else 'off'))
            self.store.receipt(doc,data);return doc
        # Final receiver-side check: compare exact entity readings before consequential call.
        # HA does not expose atomic compare-and-swap; race remains documented.
        try:
            if self.policy_source is not None:
                if not config.get('authorization_scope_sha256'):
                    raise HassError('Exact charger approval scope absent; reauthorize')
                # Config is authority-bearing. Revocation or modified mandate must
                # invalidate the plan before each consequential command.
                if pin(load_config(self.policy_source))!=pin(config):
                    raise HassError('Owner policy changed during planning; must replan')
            if not price_error:
                if config.get('price_forecast_entity'):
                    price_reading=self.hass.state(config['price_forecast_entity'])
                    verify_fresh_state(price_reading,'tariff forecast',now,config)
                    fresh_rates=parse_forecast_entity(price_reading)
                else:
                    fresh_rates=parse_forecast_csv(config['tariff_csv'])
                if pin([(iso(x.start),iso(x.end),x.usd_kwh) for x in fresh_rates])!=tariff_pin:
                    raise HassError('Tariff basis changed before command; must replan')
            if config.get('max_sensor_age_minutes') is not None:
                for label,entity in (('plugged',config['plugged_sensor']),
                                     ('vehicle SOC',config['soc_sensor']),
                                     ('charger',config['charger_switch'])):
                    verify_fresh_state(self.hass.state(entity),label,now,config)
            if not state_bool(self.hass.state(config['plugged_sensor']),'plugged'):
                raise HassError('plugged state changed before command')
            if abs(state_num(self.hass.state(config['soc_sensor']),'SOC',0,100)-soc)>.01:
                raise HassError('SOC changed before command; replan')
            if state_bool(self.hass.state(config['charger_switch']),'charger')!=switch_on:
                raise HassError('switch changed before command')
            if config.get('pause_entity'):
                pause_reading=self.hass.state(config['pause_entity'])
                verify_fresh_state(pause_reading,'pause flag',now,config)
                if state_bool(pause_reading,'pause'):
                    raise HassError('pause active before command')
            data['actions_today'][key]=int(data['actions_today'].get(key,0))+1
            data['actions_today']={k:v for k,v in data['actions_today'].items() if k>=iso(now-timedelta(days=2))[:10]}
            data['pending_effect']={'at':iso(now),'desired':desired,'session_start':session['start'],
                                    'target':config['charger_switch'],'off_retries':0}
            intent=dict(doc,status='INTENT',action='pending_'+('on' if desired else 'off'),reason='durable_intent_before_remote_action')
            self.store.receipt(intent,data)
            self.hass.switch(config['charger_switch'],desired)
            data['last_command']={'at':iso(now),'desired':desired,'session_start':session['start']}
            data['pending_effect']=None
            doc.update(status='COMMITTED',action='charger_on' if desired else 'charger_off',reason='readback_verified_switch_command')
        except (HassError,ValueError,OSError,KeyError) as e:
            doc.update(status='HOLD',reason='command_or_freshness_check_failed',errors=[str(e)[:130]])
        self.store.receipt(doc,data)
        return doc
