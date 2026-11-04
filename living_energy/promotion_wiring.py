"""Unscored wiring proof using the exact pinned SafePromotion implementation.

The editing action below is DETERMINISTIC and explicitly not a model agent.
It tests the receiver's post-repair acceptance, CAS, concurrent Git/SQLite
preservation, crash recovery and resumed simulated energy mission.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import hashlib, importlib.util, json, os, shutil, sqlite3, subprocess, sys, tempfile
from contextlib import closing
from .repair_bridge import load_existing_promotion
from .demo import run as run_mission

BAD='if hard_price_cap is not None and slot.usd_kwh >= hard_price_cap: continue'
GOOD='if hard_price_cap is not None and slot.usd_kwh > hard_price_cap: continue'

def git(*args, cwd=None, check=True):
    p=subprocess.run(['git', *map(str,args)],cwd=cwd,text=True,capture_output=True,check=False,
        env={**os.environ,'GIT_CONFIG_NOSYSTEM':'1','GIT_TERMINAL_PROMPT':'0'})
    if check and p.returncode:
        raise RuntimeError(f'Git returned {p.returncode}: {p.stderr[-500:]}')
    return p.stdout.strip()


def verifier_source():
    return '''"""Trusted hidden acceptance; never put in agent workspace."""
import os,sys,sqlite3
from pathlib import Path
from datetime import datetime,timedelta,timezone
root=Path.cwd();sys.path.insert(0,str(root/'src'))
from autopilot.planning import plan_charge,TariffSlot
start=datetime(2026,11,4,17,0,tzinfo=timezone.utc)
slots=[TariffSlot(start,start+timedelta(hours=1),.20)]
p=plan_charge(start,start+timedelta(hours=1),1.,4.,slots,hard_price_cap=.20)
assert p.feasible_by_deadline and p.charge_now, 'Equality at owner price cap wrongly denied'
assert (root/'docs'/'unrelated_accepted.txt').read_text().strip()=='unrelated accepted work', 'Concurrent accepted work lost'
db=sqlite3.connect(os.environ['OPENLINE_TEST_DB'])
assert db.execute("select count(*) from kept where item='external-accepted-row'").fetchone()[0]==1, 'Concurrent accepted database state lost'
print('HIDDEN_ACCEPTANCE_PASS')
'''


def demonstration(out_dir: Path,*,source_root:Path,existing_living_repo:Path):
    """Executes real Git/SQLite source promotion; source edit is prewritten test stimulus."""
    out_dir=Path(out_dir).resolve();out_dir.mkdir(parents=True,exist_ok=True)
    source_root=Path(source_root).resolve()
    SafePromotion,sha=load_existing_promotion(existing_living_repo)
    workspace=out_dir/'git-stage'; bare=out_dir/'accepted.git';live_db=out_dir/'live.sqlite'
    if workspace.exists() or bare.exists():raise ValueError('Use a fresh output directory')
    shutil.copytree(source_root,workspace,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    planning=workspace/'src'/'autopilot'/'planning.py'
    original=planning.read_text()
    if GOOD not in original or BAD in original:raise ValueError('Unexpected planning source; refuse fixture injection')
    planning.write_text(original.replace(GOOD,BAD,1))
    (workspace/'docs').mkdir(exist_ok=True)
    (workspace/'docs'/'unrelated_accepted.txt').write_text('initial notes\n')
    git('init','--bare',bare)
    git('init',cwd=workspace)
    git('add','.',cwd=workspace)
    git('-c','user.name=OpenLine Test','-c','user.email=test@localhost','commit','-m','Injected defect baseline',cwd=workspace)
    git('remote','add','origin',bare,cwd=workspace)
    git('push','origin','HEAD:refs/heads/accepted',cwd=workspace)
    with closing(sqlite3.connect(live_db)) as db:
        db.execute('create table kept(item text primary key)');db.execute("insert into kept values('initial')");db.commit()
    trusted=out_dir/'receiver-private';trusted.mkdir()
    oracle=trusted/'acceptance.py';oracle.write_text(verifier_source())
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','OPENLINE_TEST_DB':str(live_db)}
    first=subprocess.run([sys.executable,str(oracle)],cwd=workspace,capture_output=True,text=True,env=env)
    (out_dir/'RED_INITIAL_HIDDEN.log').write_text(first.stdout+first.stderr)
    assert first.returncode!=0,'Injected bug must fail independent acceptance'
    promotion=SafePromotion(bare,live_db,out_dir/'promotion')
    prepared=promotion.prepare('deterministic_harness_001')
    candidate=Path(prepared['workspace'])
    proposed=candidate/'src/autopilot/planning.py'
    # STOP: this is harness plumbing, not model autonomy.
    source=proposed.read_text()
    if BAD not in source:raise RuntimeError('Bug injection missing')
    proposed.write_text(source.replace(BAD,GOOD,1))
    # Another authorized author advances accepted Git ref independently.
    other=out_dir/'concurrent-editor'
    git('--git-dir',bare,'worktree','add','--detach',other,promotion.accepted())
    (other/'docs'/'unrelated_accepted.txt').write_text('unrelated accepted work\n')
    git('add','docs/unrelated_accepted.txt',cwd=other)
    git('-c','user.name=Independent Editor','-c','user.email=editor@localhost',
        'commit','-m','Accepted unrelated change',cwd=other)
    previous=promotion.accepted()
    git('--git-dir',bare,'update-ref','refs/heads/accepted',git('rev-parse','HEAD',cwd=other),previous)
    git('--git-dir',bare,'worktree','remove','--force',other)
    # Concurrent unrelated SQLite state is also accepted before promotion.
    with closing(sqlite3.connect(live_db)) as db:
        db.execute("insert into kept values('external-accepted-row')");db.commit()
    result=promotion.promote('deterministic_harness_001',acceptance_cmd=[sys.executable,str(oracle)],
                             allow_prefixes=('src/autopilot/planning.py',),refresh_snapshot=True)
    assert result['status']=='PROMOTED',result
    accepted=promotion.accepted()
    # Restart with replacement operator, reusing existing durable ledger.
    restart=SafePromotion(bare,live_db,out_dir/'promotion')
    recovered=restart.recover('deterministic_harness_001')
    assert restart.accepted()==accepted and recovered['status']=='PROMOTED'
    verify=out_dir/'accepted-checkout'
    git('--git-dir',bare,'worktree','add','--detach',verify,accepted)
    assert GOOD in (verify/'src/autopilot/planning.py').read_text()
    assert (verify/'docs/unrelated_accepted.txt').read_text().strip()=='unrelated accepted work'
    with closing(sqlite3.connect(live_db)) as db:
        assert db.execute("select count(*) from kept where item='external-accepted-row'").fetchone()[0]==1
    resumed=run_mission(out_dir/'resumed-energy-mission')
    report={'trial_type':'DETERMINISTIC_WIRING_ONLY_NOT_REAL_MODEL','status':'PASS',
      'source_promotion_git_sha':accepted,'original_injected_git_sha':prepared['base'],
      'safe_promotion_sha256':sha,'safe_promotion_git_blob_sha':'88bba71ac73ce71674b6d11f73eac7e0fcd25225','oracle_initial_failure':first.returncode,
      'oracle_final_exit':result['verification']['exit'],'merged_concurrent_git':True,
      'concurrent_db_row_retained':True,'restart_recovery':recovered['status'],
      'energy_sim_ticks_after_restart':resumed['ticks'],
      'agent_autonomy':'NOT_TESTED_NO_REAL_MODEL_RUNNER',
      'physical_hardware':'NOT_ACCESSED','promoted_to_public_repo':False}
    (out_dir/'WIRING_RECEIPT.json').write_text(json.dumps(report,indent=2,sort_keys=True))
    return report

if __name__=='__main__':
    work=Path(__file__).resolve().parents[1]
    path=Path(sys.argv[1]) if len(sys.argv)>1 else Path(tempfile.mkdtemp(prefix='openline-wiring-'))
    print(json.dumps(demonstration(path,source_root=work/'energy',
        existing_living_repo=work/'existing'/'living'),indent=2))
