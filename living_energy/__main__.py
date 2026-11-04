"""Local-only project CLI. No 'live' or Home Assistant API mode is implemented."""
from __future__ import annotations
import argparse,json,sys
from datetime import datetime,timezone
from pathlib import Path
from .loop import LivingEnergy
from .demo import run as demo
from .repair_bridge import RepairBlocked

def main(argv=None):
    p=argparse.ArgumentParser(prog='living-energy',description='Simulation-only OpenLine Living Energy project')
    p.add_argument('--root',type=Path,required=True)
    commands=p.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init');init.add_argument('--tariff-csv',required=True,type=Path)
    init.add_argument('--missing-tariff-policy',choices=['HOLD','CHARGE_TO_DEADLINE'],required=True)
    step=commands.add_parser('step');step.add_argument('--at',required=True,help='ISO timestamp with UTC offset')
    stop=commands.add_parser('stop');stop.add_argument('--at',required=True)
    commands.add_parser('pause');commands.add_parser('resume');commands.add_parser('status')
    commands.add_parser('assess');commands.add_parser('repair-status');commands.add_parser('demo')
    opts=p.parse_args(argv)
    try:
        if opts.command=='demo':result=demo(opts.root)
        elif opts.command=='init':
            LivingEnergy.create(opts.root,opts.tariff_csv,tariff_policy=opts.missing_tariff_policy)
            result={'status':'INITIALIZED_SIMULATION_ONLY'}
        else:
            mission=LivingEnergy(opts.root)
            def at():
                value=datetime.fromisoformat(opts.at.replace('Z','+00:00'))
                if value.tzinfo is None:raise ValueError('Time must include an offset')
                return value.astimezone(timezone.utc)
            if opts.command=='step':result=mission.observe(at())
            elif opts.command=='stop':result=mission.stop(at())
            elif opts.command=='pause':result=mission.pause()
            elif opts.command=='resume':result=mission.resume()
            elif opts.command=='status':
                current=mission._mission()
                result={'status':current['status'],'simulation_only':True,'state':current['state'],
                        'records':sum(x['kind'] in ('OBSERVE_DECIDE','CONSTRAINT_FOUND','PAUSE',
                             'STOPPED_OBSERVATION','STOP_PENDING','TICK_UNCERTAIN',
                             'AUTHORITY_CHANGED') for x in current['observations']),
                        'journal_records':current['sequence'],'journal_head':current['prev_receipt']}
            elif opts.command=='assess':result=mission.assess()
            else:result={'status':'AGENT_NOT_TESTED','reason':'No verified isolated provider-metered model agent'}
        print(json.dumps(result,indent=2,sort_keys=True,default=str))
        return 0
    except (OSError,ValueError,RuntimeError,KeyError) as e:
        print(json.dumps({'status':'REFUSED','error':str(e)[:240]}))
        return 2
if __name__=='__main__':sys.exit(main())
