"""Independent, conventional adaptive TOU baseline -- scheduling only.

Unlike a frozen timer, an ordinary scheduler reoptimizes when prices change.
No claim that these tests compare physical charger safety or economic outcomes.
"""
from datetime import timedelta
from pathlib import Path
import sys,unittest,random
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from autopilot.planning import TariffSlot,plan_charge
from test_autopilot import T0


def conventional_greedy(now, deadline, need_kwh, power, slots):
    """Separate straightforward greedy formulation; no OpenLine/controller imports."""
    options=[]
    for x in slots:
        a=max(now,x.start); b=min(deadline,x.end)
        if a<b:
            options.append((a,b,x.usd_kwh,(b-a).total_seconds()*power*.85/3600))
    if not options or need_kwh<=.009: return False,0.
    remaining=need_kwh
    selected=[];cost=0.
    for a,b,c,capacity in sorted(options,key=lambda z:(z[2],z[0])):
        portion=min(remaining,capacity)
        if portion>1e-9: selected.append((a,b));cost+=portion*c;remaining-=portion
        if remaining<=1e-9:break
    return (any(a<=now<b for a,b in selected),round(cost,6))

class CompetentConventionalComparator(unittest.TestCase):
    def test_no_false_superiority_when_rate_window_moves(self):
        now=T0
        slots=[]
        for i in range(24):
            a=now+timedelta(minutes=15*i)
            slots.append(TariffSlot(a,a+timedelta(minutes=15),.5 if i<8 else .1))
        for mutate in (False,True):
            if mutate:
                slots=[TariffSlot(p.start,p.end,.5 if i<16 else .1) for i,p in enumerate(slots)]
            at=now+timedelta(hours=1,minutes=45)
            deadline=now+timedelta(hours=6)
            conventional,cost=conventional_greedy(at,deadline,6.,4.,slots)
            openline=plan_charge(at,deadline,6.,4.,slots)
            self.assertEqual(openline.charge_now,conventional)
            self.assertAlmostEqual(openline.estimated_plan_cost,cost,places=4)
    def test_competent_scheduler_has_price_parity_on_200_random_horizons(self):
        rng=random.Random(7062026)
        start=T0
        for _ in range(200):
            prices=[rng.choice([0.05,.15,.2,.4,.8]) for _ in range(16)]
            slots=[TariffSlot(start+timedelta(minutes=15*i),start+timedelta(minutes=15*(i+1)),prices[i]) for i in range(16)]
            need=rng.uniform(.1,3)
            plan=plan_charge(start,start+timedelta(hours=4),need,4.,slots)
            desired,cost=conventional_greedy(start,start+timedelta(hours=4),need,4.,slots)
            self.assertTrue(plan.feasible_by_deadline)
            self.assertEqual(plan.charge_now,desired)
            self.assertAlmostEqual(plan.estimated_plan_cost,cost,places=5)
if __name__=='__main__':unittest.main()
