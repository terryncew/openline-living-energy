"""Runs existing, SHA-verified SafePromotion on real local Git/SQLite objects."""
from pathlib import Path
import sqlite3,sys,tempfile,unittest
from contextlib import closing
from living_energy.promotion_wiring import git
from living_energy.repair_bridge import load_existing_promotion

class ExistingPromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        source=Path(__file__).resolve().parents[1]/'existing'/'living'
        SafePromotion,sha=load_existing_promotion(source)
        self.assertEqual(sha,'7917760825909124afc064e9eaf67e96323fc2f0c46dbdea930fb0251cb68dc0')
        self.bare=self.root/'accepted.git';self.stage=self.root/'initial'
        git('init','--bare',self.bare);git('init',self.stage)
        (self.stage/'app').mkdir();(self.stage/'app'/'charger.py').write_text('LIMIT=1\n')
        (self.stage/'docs').mkdir();(self.stage/'docs'/'notes.txt').write_text('stable\n')
        git('add','.',cwd=self.stage)
        git('-c','user.name=Trusted Fixture','-c','user.email=fixture@localhost',
            'commit','-m','seed',cwd=self.stage)
        git('remote','add','origin',self.bare,cwd=self.stage)
        git('push','origin','HEAD:refs/heads/accepted',cwd=self.stage)
        self.db=self.root/'live.sqlite'
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('create table kept (value text)');d.commit()
        self.engine=SafePromotion(self.bare,self.db,self.root/'promotion')
        self.oracle=self.root/'hidden_oracle.py'
        self.oracle.write_text('from pathlib import Path\nassert "LIMIT=2" in Path("app/charger.py").read_text()\n')

    def test_refuses_out_of_scope_candidate(self):
        state=self.engine.prepare('scope')
        wd=Path(state['workspace'])
        (wd/'docs'/'notes.txt').write_text('unapproved edit\n')
        result=self.engine.promote('scope',acceptance_cmd=[sys.executable,str(self.oracle)],
                                   allow_prefixes=('app/',))
        self.assertEqual(result['status'],'REFUSED')
        self.assertEqual(result['reason'],'repair_outside_allowed_source')

    def test_independent_hidden_oracle_rejects_wrong_repair(self):
        state=self.engine.prepare('badfix')
        (Path(state['workspace'])/'app'/'charger.py').write_text('LIMIT=3\n')
        result=self.engine.promote('badfix',acceptance_cmd=[sys.executable,str(self.oracle)],
                                   allow_prefixes=('app/',))
        self.assertEqual(result['status'],'REFUSED')
        self.assertEqual(result['reason'],'independent_acceptance_failed')

    def test_crash_after_git_cas_is_recovered(self):
        state=self.engine.prepare('crash')
        (Path(state['workspace'])/'app'/'charger.py').write_text('LIMIT=2\n')
        with self.assertRaises(RuntimeError):
            self.engine.promote('crash',acceptance_cmd=[sys.executable,str(self.oracle)],
                                allow_prefixes=('app/',),fail_after_ref=True)
        SafePromotion,_=load_existing_promotion(Path(__file__).resolve().parents[1]/'existing'/'living')
        replacement=SafePromotion(self.bare,self.db,self.root/'promotion')
        result=replacement.recover('crash')
        self.assertEqual(result['status'],'PROMOTED_RECOVERED')
        self.assertEqual(replacement.accepted(),result['head'])

if __name__=='__main__':unittest.main()
