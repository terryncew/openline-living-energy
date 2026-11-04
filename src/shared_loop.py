"""Cross-Domain Reusable Standing & Improvement Loop (`StandingAndImprovementLoop`).

Implements the single shared OpenLine mechanism:
    Maintain Goal -> Detect Lost Standing / Constraint -> Propose Improvement
    -> Verify Externally -> Accept Within Owner Authority -> Preserve Gain -> Repeat

Operates unchanged across both:
1. Domain A: Living Energy autonomous EV charging controller self-healing.
2. Domain B: Mandate Ledger official U.S. House Clerk evidence & review reopening.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


def canonical_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class StandingState:
    domain_id: str
    goal_id: str
    authority_pin: str
    standing_status: str = "IN_STANDING"  # IN_STANDING | LOST_STANDING | HALTED
    active_constraint: str = "NONE"
    accepted_version: int = 1
    accepted_state_digest: str = ""
    proposed_candidate: dict[str, Any] | None = None
    verified_attestation: dict[str, Any] | None = None
    receipt_chain: list[dict[str, Any]] = field(default_factory=list)


class DomainAdapter(Protocol):
    def current_authority_pin(self) -> str: ...
    def current_state_digest(self) -> str: ...
    def observe_and_detect_constraint(self) -> dict[str, Any]: ...
    def propose_bounded_improvement(self, constraint: str) -> dict[str, Any]: ...
    def verify_externally(self, proposal: dict[str, Any]) -> dict[str, Any]: ...
    def accept_and_promote(self, proposal: dict[str, Any], attestation: dict[str, Any], *, owner_approved: bool) -> dict[str, Any]: ...


class StandingAndImprovementLoop:
    """Domain-agnostic recursive constraint-finding, external verification, and safe promotion loop."""

    def __init__(self, domain_id: str, goal_id: str, adapter: DomainAdapter):
        self.adapter = adapter
        self.state = StandingState(
            domain_id=domain_id,
            goal_id=goal_id,
            authority_pin=adapter.current_authority_pin(),
            accepted_state_digest=adapter.current_state_digest(),
        )
        self._append_receipt("LOOP_GENESIS", {"authority_pin": self.state.authority_pin})

    def _state_commitment(self) -> str:
        """Bind *current* accepted, proposed, and verified state to the last receipt."""
        return canonical_digest({
            "domain_id": self.state.domain_id,
            "goal_id": self.state.goal_id,
            "standing_status": self.state.standing_status,
            "active_constraint": self.state.active_constraint,
            "accepted_version": self.state.accepted_version,
            "accepted_state_digest": self.state.accepted_state_digest,
            "authority_pin": self.state.authority_pin,
            "proposed_candidate": self.state.proposed_candidate,
            "verified_attestation": self.state.verified_attestation,
        })

    def _append_receipt(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        prev_hash = self.state.receipt_chain[-1]["hash"] if self.state.receipt_chain else ""
        seq = len(self.state.receipt_chain) + 1
        commitment = self._state_commitment()
        entry = {
            "seq": seq,
            "kind": kind,
            "payload": payload,
            "previous": prev_hash,
            "state_commitment": commitment,
        }
        entry["hash"] = canonical_digest(entry)
        self.state.receipt_chain.append(entry)
        return entry

    def verify_receipt_chain(self) -> bool:
        if not self.state.receipt_chain:
            return False
        prev = ""
        for item in self.state.receipt_chain:
            body = {k: v for k, v in item.items() if k != "hash"}
            if item["previous"] != prev or canonical_digest(body) != item["hash"]:
                return False
            prev = item["hash"]
        # A valid historical chain alone does not authenticate mutable operating state.
        # This is an in-memory consistency check, not a third-party signature.
        return self.state.receipt_chain[-1]["state_commitment"] == self._state_commitment()

    def run_cycle(self, *, owner_approved: bool = False) -> dict[str, Any]:
        # The agent may not heal/accept new state over a damaged receipt history.
        # No new receipt is appended to a tainted chain: preserve evidence for
        # a separately authenticated operator recovery instead of laundering it.
        if not self.verify_receipt_chain():
            return {"status": "HALTED_INTEGRITY_UNKNOWN", "constraint": "RECEIPT_OR_STATE_TAMPERED"}
        # Step 1: Verify owner authority pin before acting
        live_pin = self.adapter.current_authority_pin()
        if live_pin != self.state.authority_pin:
            self.state.standing_status = "HALTED"
            self.state.active_constraint = "AUTHORITY_PIN_CHANGED"
            self._append_receipt("AUTHORITY_REVOKED_HALT", {"expected": self.state.authority_pin, "observed": live_pin})
            return {"status": "HALTED_AUTHORITY_CHANGED", "constraint": "AUTHORITY_PIN_CHANGED"}

        # A previously accepted version cannot silently absorb changes to its
        # evidence or external effect basis. Explicitly reopen before observation.
        live_basis = self.adapter.current_state_digest()
        if live_basis != self.state.accepted_state_digest:
            self.state.standing_status = "LOST_STANDING"
            self.state.active_constraint = "EVIDENCE_BASIS_CHANGED"
            self._append_receipt("EVIDENCE_BASIS_CHANGED_REOPEN", {
                "accepted": self.state.accepted_state_digest, "observed": live_basis,
            })
            return {"status": "HALTED_BASIS_CHANGED", "constraint": "EVIDENCE_BASIS_CHANGED"}

        # Step 2: Observe domain & detect lost standing / constraint
        obs = self.adapter.observe_and_detect_constraint()
        constraint = obs.get("constraint", "NONE")
        if constraint == "NONE":
            self.state.standing_status = "IN_STANDING"
            self.state.active_constraint = "NONE"
            self.state.accepted_state_digest = self.adapter.current_state_digest()
            self._append_receipt("MAINTAIN_GOAL_OK", obs)
            return {"status": "IN_STANDING", "constraint": "NONE", "accepted_version": self.state.accepted_version}

        self.state.standing_status = "LOST_STANDING"
        self.state.active_constraint = constraint
        self._append_receipt("LOST_STANDING_DETECTED", {"constraint": constraint, "details": obs})
        basis_pin = self.adapter.current_state_digest()

        # Step 3: Propose bounded improvement (strictly separated from accepted state)
        proposal = self.adapter.propose_bounded_improvement(constraint)
        self.state.proposed_candidate = proposal
        self._append_receipt("IMPROVEMENT_PROPOSED", {"proposal_id": proposal.get("proposal_id"), "constraint": constraint})

        # Step 4: Verify externally via independent oracle/gate
        attestation = self.adapter.verify_externally(proposal)
        self.state.verified_attestation = attestation
        if not attestation.get("verified", False):
            self._append_receipt("EXTERNAL_VERIFICATION_REFUSED", attestation)
            return {
                "status": "REFUSED_BY_EXTERNAL_VERIFIER",
                "constraint": constraint,
                "refusal_reason": attestation.get("reason", "verification_failed"),
            }
        self._append_receipt("EXTERNAL_VERIFICATION_PASSED", attestation)

        # A proposal can become stale *while* an oracle runs. Reopen rather than
        # use yesterday's approval against tomorrow's authority or evidence.
        fresh_authority = self.adapter.current_authority_pin()
        if fresh_authority != self.state.authority_pin:
            self.state.standing_status = "HALTED"
            self.state.active_constraint = "AUTHORITY_PIN_CHANGED"
            self._append_receipt("AUTHORITY_REVOKED_HALT", {"expected": self.state.authority_pin, "observed": fresh_authority})
            return {"status": "HALTED_AUTHORITY_CHANGED", "constraint": "AUTHORITY_PIN_CHANGED"}
        fresh_basis = self.adapter.current_state_digest()
        if fresh_basis != basis_pin:
            self.state.standing_status = "LOST_STANDING"
            self.state.active_constraint = "EVIDENCE_BASIS_CHANGED"
            self._append_receipt("EVIDENCE_BASIS_CHANGED_REOPEN", {"old": basis_pin, "new": fresh_basis})
            return {"status": "HALTED_BASIS_CHANGED", "constraint": "EVIDENCE_BASIS_CHANGED"}

        # Step 5: Accept within owner authority & preserve gain
        if not owner_approved:
            self._append_receipt("AWAITING_OWNER_AUTHORITY", {"proposal_id": proposal.get("proposal_id")})
            return {"status": "VERIFIED_AWAITING_OWNER_AUTHORITY", "constraint": constraint}

        promoted = self.adapter.accept_and_promote(proposal, attestation, owner_approved=owner_approved)
        if promoted.get("status") != "PROMOTED":
            self._append_receipt("PROMOTION_REFUSED", promoted)
            return {"status": "PROMOTION_REFUSED", "details": promoted}

        self.state.standing_status = "IN_STANDING"
        self.state.active_constraint = "NONE"
        self.state.accepted_version += 1
        self.state.accepted_state_digest = self.adapter.current_state_digest()
        self.state.proposed_candidate = None
        self.state.verified_attestation = None
        self._append_receipt("GAIN_PRESERVED_AND_RESUMED", {"accepted_version": self.state.accepted_version, "promotion": promoted})
        return {
            "status": "RECOVERED_IN_STANDING",
            "repaired_constraint": constraint,
            "accepted_version": self.state.accepted_version,
            "receipt_count": len(self.state.receipt_chain),
        }
