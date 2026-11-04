"""Verify authorization/pausing defaults and one-time entity discovery logic."""
import unittest,sys,tempfile,os,subprocess,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autopilot.onboard import candidates

class SetupTests(unittest.TestCase):
    def test_discovery_prefers_plausible_charger(self):
        xs=[{'entity_id':'switch.lamp','attributes':{'friendly_name':'Lamp'}},
            {'entity_id':'switch.ev_charging_enable','attributes':{'friendly_name':'EV charger enable'}}]
        got=candidates(xs,'switch',('ev','charge','charger','wallbox'))
        self.assertEqual(got[0][1],'switch.ev_charging_enable')
    def test_cli_pause_resume_status_default_same_file(self):
        with tempfile.TemporaryDirectory() as d:
            env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
            base=[sys.executable,'-m','autopilot']
            def run(cmd):
                return subprocess.run(base+[cmd],cwd=d,env=env,capture_output=True,text=True,check=True)
            self.assertTrue(json.loads(run('pause').stdout)['paused'])
            store=Path(d)/'local/private/state.json'
            self.assertTrue(store.exists())
            self.assertTrue(json.loads(run('status').stdout)['paused'])
            self.assertFalse(json.loads(run('resume').stdout)['paused'])
            self.assertFalse(json.loads(run('status').stdout)['paused'])

if __name__=='__main__':unittest.main()
