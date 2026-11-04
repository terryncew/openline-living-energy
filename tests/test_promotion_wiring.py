import json,tempfile,unittest
from pathlib import Path
from living_energy.promotion_wiring import demonstration

class PromotionIntegration(unittest.TestCase):
    def test_actual_existing_safe_promotion_git_cas_and_resume(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            report=demonstration(Path(td)/'trial',source_root=root/'energy',
              existing_living_repo=root/'existing/living')
            self.assertEqual(report['status'],'PASS')
            self.assertTrue(report['merged_concurrent_git'])
            self.assertEqual(report['energy_sim_ticks_after_restart'],25)
            self.assertEqual(report['agent_autonomy'],'NOT_TESTED_NO_REAL_MODEL_RUNNER')

if __name__=='__main__':unittest.main()
