"""Unattended simulation; external coding-agent self-repair is intentionally NOT faked."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,sys,tempfile
from .loop import LivingEnergy


def run(root=None):
    base=datetime(2026,11,4,17,0,tzinfo=timezone.utc)
    if root is None:
        root=Path(tempfile.mkdtemp(prefix='living-energy-sim-'))
    else:root=Path(root)
    root.mkdir(parents=True,exist_ok=True)
    tariff=root/'prices.csv'
    with tariff.open('w') as f:
        f.write('start,end,usd_per_kwh\n')
        for n in range(24):
            start=base+timedelta(minutes=15*n);end=start+timedelta(minutes=15)
            f.write(f'{start.isoformat()},{end.isoformat()},{.5 if n<8 else .1}\n')
    runner=LivingEnergy.create(root,tariff)
    rows=[]
    for i in range(25):
        # Crash/restart of orchestration layer at tick 8; persistence survives.
        if i==8:runner=LivingEnergy(root)
        out=runner.observe(base+timedelta(minutes=15*i))
        rows.append({'step':i,'status':out['status'],'commands':out.get('commands',[]),
                     'soc':round(runner._mission()['state']['soc'],2)})
    result={'simulation_only':True,'ticks':len(rows),'last':rows[-1],
            'restart_at_step':8,'journal_records':runner._mission()['sequence'],
            'repair_agent':'NOT_TESTED_NO_RUNNER','physical_actuation':'NONE',
            'rows':rows,'root':str(root)}
    (root/'demo_result.json').write_text(json.dumps(result,indent=2))
    return result

if __name__=='__main__':
    print(json.dumps(run(Path(sys.argv[1]) if len(sys.argv)>1 else None),indent=2))
