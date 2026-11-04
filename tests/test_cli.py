import json,tempfile,subprocess,sys,unittest,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class CLI(unittest.TestCase):
    def test_simulation_only_cli_full_mission(self):
        with tempfile.TemporaryDirectory() as d:
            env={**os.environ,'PYTHONPATH':str(ROOT)+':'+str(ROOT/'energy'/'src')}
            p=subprocess.run([sys.executable,'-m','living_energy','--root',d,'demo'],env=env,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            result=json.loads(p.stdout)
            self.assertEqual(result['last']['soc'],51.5)
            p=subprocess.run([sys.executable,'-m','living_energy','--root',d,'status'],env=env,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            current=json.loads(p.stdout)
            self.assertTrue(current['simulation_only'])
            self.assertEqual(current['records'],25)
    def test_cli_does_not_have_live_charger_mode(self):
        with tempfile.TemporaryDirectory() as d:
            env={**os.environ,'PYTHONPATH':str(ROOT)+':'+str(ROOT/'energy'/'src')}
            p=subprocess.run([sys.executable,'-m','living_energy','--root',d,'--live'],env=env,capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0)
if __name__=='__main__':unittest.main()
