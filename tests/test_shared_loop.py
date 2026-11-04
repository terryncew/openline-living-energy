"""Independent red/green regressions: no implied owner consent; no stale promotion."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from shared_loop import StandingAndImprovementLoop

class Stub:
    def __init__(self):
        self.pin='owner-v1';self.stamp='state-v1';self.called=0
        self.flip_pin=False;self.flip_state=False
    def current_authority_pin(self):return self.pin
    def current_state_digest(self):return self.stamp
    def observe_and_detect_constraint(self):return {'constraint':'NEEDS_FIX'}
    def propose_bounded_improvement(self, constraint):return {'proposal_id':'fix-1','scope':'review-only'}
    def verify_externally(self, proposal):
        if self.flip_pin:self.pin='owner-v2'
        if self.flip_state:self.stamp='state-v2'
        return {'verified':True,'target':'fix-1'}
    def accept_and_promote(self, proposal, attestation, *, owner_approved):
        self.called+=1;return {'status':'PROMOTED'}

class TestReleaseGate(unittest.TestCase):
    def test_explicit_owner_approval_default_is_false(self):
        a=Stub();l=StandingAndImprovementLoop('energy','task',a)
        r=l.run_cycle()
        self.assertEqual(r['status'],'VERIFIED_AWAITING_OWNER_AUTHORITY');self.assertEqual(a.called,0)
    def test_owner_pin_changed_during_verification_blocks_promotion(self):
        a=Stub();a.flip_pin=True;l=StandingAndImprovementLoop('energy','task',a)
        r=l.run_cycle(owner_approved=True)
        self.assertEqual(r['status'],'HALTED_AUTHORITY_CHANGED');self.assertEqual(a.called,0)
    def test_evidence_basis_changed_during_verification_blocks_promotion(self):
        a=Stub();a.flip_state=True;l=StandingAndImprovementLoop('energy','task',a)
        r=l.run_cycle(owner_approved=True)
        self.assertEqual(r['status'],'HALTED_BASIS_CHANGED');self.assertEqual(a.called,0)
    def test_state_tamper_detected_even_if_receipt_entries_untouched(self):
        a=Stub();l=StandingAndImprovementLoop('energy','task',a)
        l.run_cycle(owner_approved=False);self.assertTrue(l.verify_receipt_chain())
        l.state.accepted_version=987
        self.assertFalse(l.verify_receipt_chain())
    def test_pending_proposal_mutation_detected(self):
        a=Stub();l=StandingAndImprovementLoop('energy','task',a)
        l.run_cycle(owner_approved=False);self.assertTrue(l.verify_receipt_chain())
        l.state.proposed_candidate['scope']='unsafe'
        self.assertFalse(l.verify_receipt_chain())

if __name__=='__main__':unittest.main()
