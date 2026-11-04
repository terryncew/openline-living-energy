"""Private-to-public release falsifiers: damaged journals and basis drift must not be accepted."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from shared_loop import StandingAndImprovementLoop

class Adapter:
    def __init__(self):
        self.pin='owner-1';self.digest='basis-1';self.effects=0
    def current_authority_pin(self):return self.pin
    def current_state_digest(self):return self.digest
    def observe_and_detect_constraint(self):return {'constraint':'NONE'}
    def propose_bounded_improvement(self, constraint):return {'proposal_id':'x'}
    def verify_externally(self,proposal):return {'verified':True}
    def accept_and_promote(self,proposal,attestation,*,owner_approved):
        self.effects+=1;return {'status':'PROMOTED'}

class Gate(unittest.TestCase):
    def test_corrupt_historical_receipt_cannot_be_laundered_by_new_cycle(self):
        a=Adapter();loop=StandingAndImprovementLoop('energy','save-money',a)
        loop.state.receipt_chain[0]['payload']['authority_pin']='forged'
        self.assertFalse(loop.verify_receipt_chain())
        before=len(loop.state.receipt_chain)
        out=loop.run_cycle(owner_approved=True)
        self.assertEqual(out['status'],'HALTED_INTEGRITY_UNKNOWN')
        self.assertEqual(len(loop.state.receipt_chain),before)
        self.assertEqual(a.effects,0)

    def test_mismatched_accepted_state_must_reopen_not_silently_rebase(self):
        a=Adapter();loop=StandingAndImprovementLoop('energy','save-money',a)
        a.digest='basis-changed-underneath'
        out=loop.run_cycle(owner_approved=True)
        self.assertEqual(out['status'],'HALTED_BASIS_CHANGED')
        self.assertEqual(loop.state.accepted_state_digest,'basis-1')
        self.assertEqual(a.effects,0)
        self.assertTrue(loop.verify_receipt_chain())

    def test_same_basis_can_continue_without_work(self):
        a=Adapter();loop=StandingAndImprovementLoop('ledger','source-check',a)
        out=loop.run_cycle(owner_approved=True)
        self.assertEqual(out['status'],'IN_STANDING')
        self.assertTrue(loop.verify_receipt_chain())

if __name__=='__main__':unittest.main()
