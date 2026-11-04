"""Randomized brute-force cost-optimality check for small slot sets."""
import unittest,random,sys,itertools
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autopilot.planning import plan_charge,TariffSlot

class PropertyTests(unittest.TestCase):
    def test_cheapest_eligible_slots_match_bruteforce_250_cases(self):
        r=random.Random(1122001)
        t=datetime(2026,11,8,0,0,tzinfo=timezone.utc)
        for seed in range(250):
            prices=[r.choice((-.05,.0,.1,.2,.4,.6,.9)) for _ in range(6)]
            slots=[TariffSlot(t+timedelta(hours=i),t+timedelta(hours=i+1),prices[i]) for i in range(6)]
            need=r.randint(1,6)
            p=plan_charge(t,t+timedelta(hours=6),need,1,slots,usable_fraction=1)
            expected=min(sum(prices[i] for i in ids) for ids in itertools.combinations(range(6),need))
            self.assertTrue(p.feasible_by_deadline)
            self.assertAlmostEqual(p.estimated_plan_cost,expected,places=6)
            self.assertLessEqual(p.estimated_plan_cost,p.immediate_cost+1e-7)
    def test_cost_plan_honors_aware_timestamps(self):
        tz=timezone(timedelta(hours=-8))
        t=datetime(2026,11,8,8,0,tzinfo=timezone.utc)
        p=plan_charge(t,t+timedelta(hours=1),.5,2,
                      [TariffSlot(t.astimezone(tz),(t+timedelta(hours=1)).astimezone(tz),.15)],usable_fraction=1)
        self.assertAlmostEqual(p.estimated_plan_cost,.075)
    def test_stale_deadline_is_not_success(self):
        t=datetime(2026,11,8,8,0,tzinfo=timezone.utc)
        p=plan_charge(t,t,4,2,[TariffSlot(t,t+timedelta(hours=1),.2)])
        self.assertFalse(p.feasible_by_deadline)

if __name__=='__main__': unittest.main()
