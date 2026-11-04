# Living Energy — Dogfood 006 release audit

Scope: public experimental **source only**, simulation-only. No live charger, no measured electricity savings, no independent fresh model agent session.

## Independent RED → GREEN findings

1. **Receipt-history laundering:** a modified historic receipt could remain in the state chain while `run_cycle()` continued. New gate returns `HALTED_INTEGRITY_UNKNOWN` with no new receipt or external proposal/promotion.
2. **Accepted-basis laundering:** an adapter could change its externally accepted state digest and report `constraint=NONE`; the loop silently rebased acceptance. New gate halts and emits `EVIDENCE_BASIS_CHANGED_REOPEN`, preserving the previous accepted digest.
3. **Fallback process recursion:** `scripts/run_portable_tests.py` executed discovery at import time; multiprocessing with `spawn` recursively relaunched the suite on hosts lacking unshare. Entry is now protected under `if __name__ == "__main__"`; verified by a full portable run with actual OS process concurrency.

## Reproducible local evidence

- Exact frozen ZIP extracted to a fresh directory and `bash REPLAY_EXPERIMENTAL.sh` executed on independent Linux host with working user/mount/network namespaces: **64/64 Energy + 67/67 Living Energy PASS** (131 total). Full Living suite finished in 111.242 seconds. Includes 12 Argon isolation, receiver-attestation and stored-repair replay tests.
- `PYTHONPATH=.:energy/src:src python3 -B scripts/run_portable_tests.py` was also executed separately on the working tree: **55/55 PASS**. It now correctly guards process entry for Python multiprocessing spawn.
- Full sandbox replay being green does **not** prove production sandbox completeness, model-generated patches, or safe physical charger operation.

## Unresolved boundaries

- Owner approval is a local boolean in the generic reusable loop, not a cryptographic receiver-owned authorization. Never use as a remote authorization endpoint.
- Hash-chain integrity is not tamper-resistant against a host file owner able to replace all records and hashes.
- Prior text-patch replays are deterministic replays, not fresh model-agent executions. Live electrical control and real customer savings remain NOT TESTED.
- A state digest can change after the pre-promotion recheck; a true atomic receiver commit requires a domain-specific pin/CAS and an independent physical effect gate. Source-only experimental scope remains the release boundary.
