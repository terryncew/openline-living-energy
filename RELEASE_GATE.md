# Release gate — v0.4.2

- Experimental **source publication**: READY FOR MUSE REVIEW once manifest and CI reproduce from exact source ZIP.
- Unattended simulated energy/controller operation: PASS on independent local Linux replay (64+59 tests).
- Cross-project standing-loop gates: PASS for 8 portable tests, including three new red-to-green standing/integrity regressions.
- Full isolated Linux tests: **67/67 PASS** on exact ZIP extraction after allowing sufficient runtime (111.242 seconds for Living suite; plus 64/64 controller). Twelve `test_argon_004` cases, including the namespace-isolation/receiver-attestation cases, passed. This is tested behavior, not a certification of production isolation. CI on a different host must explicitly report skipped namespace tests if they cannot run.
- The fallback portable runner's recursive multiprocessing spawn failure was reproduced and fixed, then **55/55 portable** tests passed.
- Fresh independent coding-agent repair: **NOT REPRODUCED**. Archived patches replay deterministically; native-agent metadata is not provider-attested here.
- Production sandbox isolation: **NOT CERTIFIED**. Tested user/mount/net/PID isolation only under bounded adversarial probes.
- Real charging, verified kWh/bill savings, native app/device support: **NOT TESTED — HOLD**.
- Live deployment, real chargers, new owner authority, payments, public outreach: **not authorized**.

Do not make a public claim of real self-improving model-agent autonomy based solely on the replay.
