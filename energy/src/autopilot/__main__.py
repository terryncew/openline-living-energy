"""CLI for observe-only or explicitly owner-authorized autonomous service."""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from .core import Autopilot, Store, load_config
from .hass import HomeAssistant
from .onboard import wizard,authorize


def main(argv=None):
    ap=argparse.ArgumentParser(description='OpenLine Energy Autopilot (local Home Assistant charger scheduler)')
    ap.add_argument('command',choices=('once','loop','pause','resume','status','audit','setup','authorize'))
    ap.add_argument('--config',default='./local/config.json')
    ap.add_argument('--state',default=None)
    ap.add_argument('--token-file',default=None,help='File containing HA token; created by setup wizard')
    ap.add_argument('--live',action='store_true',help='Permit owner-approved HA switch commands; default is read-only')
    ap.add_argument('--interval',type=int,default=60,help='Loop poll interval in seconds, 30..900')
    args=ap.parse_args(argv)
    if args.state is None:
        args.state=str(Path(args.config).resolve().parent/'private/state.json')
    if args.command=='setup':wizard(args.config);return 0
    if args.command=='authorize':authorize(args.config);return 0
    store=Store(args.state)
    if args.command in ('pause','resume'):
        with store.locked():
            obj=store.load();obj['paused']=args.command=='pause';store.save(obj)
        print(json.dumps({'paused':obj['paused']}));return 0
    if args.command=='audit':
        with store.locked():
            count=store.verify_all_archives()
            tip=store.load()['prev_receipt']
        print(json.dumps({'receipt_integrity':'VERIFIED_LOCAL_HASH_CHAIN', 'archive_count':count, 'tip':tip, 'note':'Not externally signed or independently anchored'}))
        return 0
    if args.command=='status':
        s=store.load();print(json.dumps({'paused':s['paused'], 'session':s['session'], 'last_command':s['last_command'], 'receipt_tip':s['prev_receipt']},indent=2));return 0
    if args.interval<30 or args.interval>900:ap.error('--interval range 30..900 seconds')
    cfg=load_config(args.config)
    token=os.environ.get('HA_TOKEN','')
    if not token:
        path=Path(args.token_file) if args.token_file else Path(args.config).resolve().parent/'ha-token.txt'
        if path.exists():
            if path.stat().st_mode & 0o077:ap.error('Token file permissions must be owner-only (chmod 600)')
            token=path.read_text(encoding='utf-8').strip()
    if not token:ap.error('HA_TOKEN must be supplied via environment, never config file')
    ha=HomeAssistant(cfg['home_assistant_url'],token)
    controller=Autopilot(cfg,ha,store,policy_source=args.config)
    def iteration():
        controller.cfg=load_config(args.config)  # Notice owner revocations between polls.
        doc=controller.tick(datetime.now(timezone.utc),live=args.live)
        # Never log token, raw HA responses, or customer entity attributes.
        print(json.dumps({k:doc[k] for k in ('time','status','action','reason','errors','plan') if k in doc},default=str),flush=True)
        return doc
    if args.command=='once':iteration();return 0
    # A supervised process is required for autonomous background operation.
    # Container/systemd can restart this command after failure.
    while True:
        iteration()
        time.sleep(args.interval)

if __name__=='__main__':
    sys.exit(main())
