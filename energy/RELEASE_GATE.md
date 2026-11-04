# OpenLine Energy Autopilot 001 — Acceptance / Falsifier Gate

## Supported claim

**Autonomous local EV charging scheduling has been demonstrated against a simulated EV and Home Assistant-compatible local REST endpoint.** After one-time permission, the controller decides and executes when to turn a configured charger-enable switch on/off. It adapts to SOC and tariff changes, bounds actions, persists receipts, handles ambiguous acknowledgements, and can run unattended as a Linux service.

## Explicitly NOT supported

- No physical EV or charger was connected. No true consumption savings verified; simulated $2.67 is an illustration.
- Not a universal solar/autonomy platform, not household electrical/grid controller, not equipment-certified.
- No claim of optimization superiority to evcc or other mature smart charging solutions.
- Docker itself not executed here. No macOS/Windows/hardware validation.
- Real customer onboarding is conditional on Home Assistant and compatible integrations; zero-touch setup is not possible without safe permission and device mapping.
- Local receipt hashes are not externally signed, and Home Assistant does not expose a commit-time atomic CAS for generic switch calls.
- Charger state ON is insufficient to prove power transfer; real meter/power-sensor validation remains needed.

## Next required independent acceptance

1. On a consenting test rig, verify the exact HA charger entity safely enables/disables vehicle charging without mains power cycling and respects manufacturer charge limits.
2. Repeat at least 10 real charging sessions under applicable real owner tariff data, including price forecast outages, reconnects, time-zone/DST changes, network timeouts, power loss and user overrides.
3. Compare independently verified battery energy delivered by deadline and measured electricity costs against ordinary scheduled charging and evcc, including integration/maintenance cost.
4. Review receipt continuity and config revocation under OS/user concurrency; verify correctness with native HA command semantics.
5. Promote to a separate public preview only with owner's explicit approval after hardware and safety checks.

**Current decision:** ENGINEERING PASS / PRODUCT PILOT HOLD / REAL SAVINGS NOT TESTED / HARDWARE CONTROL NOT ACTIVATED.
