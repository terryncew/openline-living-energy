"""One-time private setup: discover HA entities, confirm one controllable charger.

Does not actuate anything and starts with authorization=false.
"""
from __future__ import annotations
from pathlib import Path
from getpass import getpass
import json, os, re
from .hass import HomeAssistant, check_entity
from .core import canon,load_config,authorization_scope

FIELDS=(
    ('charger_switch','switch',('ev','charge','charger','wallbox')),
    ('plugged_sensor','binary_sensor',('plug','connect','cable','vehicle')),
    ('soc_sensor','sensor',('soc','battery_level','battery_percent','state_of_charge')),
    ('energy_meter_sensor','sensor',('charge_energy','charger_energy','ev_energy')),
    ('price_forecast_entity','sensor',('price','tariff','rate')),
    ('pause_entity','input_boolean',('pause','autopilot','ev_control')), 
)

def candidates(states, domain, terms):
    found=[]
    for entry in states:
        if not isinstance(entry,dict):continue
        e=entry.get('entity_id','')
        try:check_entity(e,domain)
        except ValueError:continue
        title=str((entry.get('attributes') or {}).get('friendly_name',''))
        hay=(e+' '+title).lower().replace(' ','_')
        score=sum(2 if term in e else 1 for term in terms if term in hay)
        if domain=='sensor' and terms==('price','tariff','rate'):
            if isinstance((entry.get('attributes') or {}).get('prices'),list):score+=10
        if score:found.append((score,e,title))
    return sorted(found,key=lambda x:(-x[0],x[1]))[:12]


def ask_float(title,default,lo,hi):
    while True:
        raw=input(f'{title} [{default}]: ').strip() or str(default)
        try:v=float(raw)
        except ValueError:continue
        if lo<=v<=hi:return v
        print(f'Enter number between {lo} and {hi}.')


def ask_choice(title, suggestions,*, optional=False):
    print('\n'+title)
    for i,(_,name,friendly) in enumerate(suggestions[:8],1):print(f'  {i}: {name} {"("+friendly+")" if friendly else ""}')
    print('  Or type full entity_id'+(' (Enter to skip)' if optional else ''))
    while True:
        raw=input('Choice: ').strip()
        if not raw and optional:return None
        if raw.isdigit() and 1<=int(raw)<=min(8,len(suggestions)):
            return suggestions[int(raw)-1][1]
        if raw and raw in [s[1] for s in suggestions]:return raw
        if raw.startswith(title.split()[0].lower()+'.'):
            return raw
        if raw and '.' in raw:return raw
        print('Select one of the discovered entities or enter an exact entity ID.')


def wizard(dest: str='config.json'):
    out=Path(dest).resolve();out.parent.mkdir(parents=True,exist_ok=True)
    if out.exists():raise RuntimeError('Config already exists. Refusing to overwrite owner configuration.')
    print('OpenLine Energy Autopilot — one-time Home Assistant connection')
    print('Only chargers with a genuine charge-enable switch are supported. NEVER use a generic mains outlet switch.')
    url=input('Home Assistant URL [http://homeassistant.local:8123]: ').strip() or 'http://homeassistant.local:8123'
    token=getpass('Home Assistant access token (hidden): ').strip()
    ha=HomeAssistant(url,token)
    states=ha.states()
    try:zone=ha.configuration().get('time_zone') or 'UTC'
    except Exception:zone='UTC'
    cfg={'home_assistant_url':url,'timezone':zone,'owner_authorized':False,'minimum_dwell_minutes':10,
         'max_actions_per_day':24,'efficiency':.9,'departure_local':'07:00'}
    for key,domain,terms in FIELDS:
        sug=candidates(states,domain,terms)
        cfg[key]=ask_choice(domain+' '+key,sug,optional=key in ('energy_meter_sensor','price_forecast_entity','pause_entity'))
        if cfg[key]:check_entity(cfg[key],domain)
    if not cfg.get('price_forecast_entity'):
        print('No price-forecast entity selected. Provide an owner-verified, timestamped tariff CSV; rates must cover the full charging horizon.')
        cfg['tariff_csv']=input('Path to tariff CSV: ').strip()
        if not cfg['tariff_csv']:raise RuntimeError('A price source is required. No pricing invented.')
    cfg['battery_kwh']=ask_float('Usable battery capacity, kWh',60,1,250)
    cfg['charge_power_kw']=ask_float('Verified charger power (kW)',7.2,.2,22)
    cfg['target_soc_percent']=ask_float('Target EV state of charge percent',80,1,95)
    cfg['departure_local']=input('Daily departure time local HH:MM [07:00]: ').strip() or '07:00'
    cfg['timezone']=input(f'Local timezone [{zone}]: ').strip() or zone
    cfg['owner_authorized']=False
    cfg['max_sensor_age_minutes']=15
    out.write_text(json.dumps(cfg,indent=2)+'\n',encoding='utf-8');os.chmod(out,0o600)
    tfile=out.parent/'ha-token.txt'
    if tfile.exists():raise RuntimeError('Token file exists. Refusing to overwrite.')
    fd=os.open(tfile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(token+'\n')
    load_config(out)
    print('\nConnection saved locally; hardware control is OFF. Run dry-run, then explicitly authorize.')
    return str(out)


def authorize(dest):
    p=Path(dest);cfg=load_config(p,allow_stale_authorization=True)
    if cfg['owner_authorized'] and cfg.get('authorization_scope_sha256')==authorization_scope(cfg):
        print('Already explicitly authorized for this config.');return
    print('\nSCOPE: charger enable switch: '+cfg['charger_switch'])
    print('Target SOC: '+str(cfg['target_soc_percent']))
    print('Departure time local: '+cfg['departure_local'])
    print('The system can toggle this exact charger control. It cannot guarantee vehicle charge.')
    print('No direct grid control, purchases, or household mains switching permitted.')
    resp=input('Type APPROVE CHARGER AUTOMATION to authorize physical switch commands: ').strip()
    if resp!='APPROVE CHARGER AUTOMATION':raise RuntimeError('Authorization not granted')
    cfg['owner_authorized']=True
    cfg['authorization_scope_sha256']=authorization_scope(cfg)
    cfg.setdefault('max_sensor_age_minutes',15.0)
    cfg['authorization_scope_sha256']=authorization_scope(cfg)
    p.write_text(json.dumps(cfg,indent=2)+'\n',encoding='utf-8');os.chmod(p,0o600)
    print('Hardware control permitted when --live is supplied. Keep user override accessible.')
