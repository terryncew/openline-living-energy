# OpenLine Living Energy — Experimental Self-Healing Energy Project

**Public-source candidate v0.4.2 | MIT (with Apache-2.0 SafePromotion component) | Simulation only**

A Living Project that runs an autonomous EV charging **simulation**, observes interruptions and lost evidence, safely stops and resumes, and can gate proposed source repairs through an isolated test environment and owner-controlled acceptance. It combines OpenLine Energy Autopilot with Living Projects continuity/repair primitives. It does not require a dashboard to stay open.

## The OpenLine Energy family

Three separate repositories, one story. Start where your question is.

- **OpenLine Energy** ([terryncew/openline-energy](https://github.com/terryncew/openline-energy)) is the solar planning tool. An advisory, local-first web app that helps a California household explore plug-in solar options. Open `index.html`, enter one monthly usage number, and see a labeled illustration. It is not a savings quote, not installation advice, and no hosted public site is deployed.
- **Energy Autopilot** ([terryncew/openline-energy-autopilot](https://github.com/terryncew/openline-energy-autopilot)) is the experimental autonomous EV charging controller. By default it runs against a simulated charger only. Its demo shows a $2.67 modeled cost difference between charging windows. That is a modeled difference, not measured household savings. Physical charger activation is unvalidated, stays disabled unless the owner deliberately authorizes it, and requires a separate safety review.
- **Living Energy (this repository)** adds a bounded project-improvement and recovery layer on top of the charging controller. It detects failures, refuses to act on damaged receipt history or changed owner authority, gates proposed repairs through receiver-controlled promotion, and preserves accepted work across interruptions and agent replacement. The charging mission here is simulated. No charger is connected and no savings are measured.

The controller code packaged here matches the public Autopilot planner and charger interface, with a hardened controller core for the integrated mission. The three repositories do not modify each other. Nothing in this family operates a physical charger on its own authority.

## Try it (no vehicle, account, API key, or network)

Requires **Linux, Python 3.11+, git**. Independent sandbox/replay also requires unprivileged Linux namespaces, `unshare`, `iproute2`, and `cryptography`. The local demo itself never touches a charger.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run_demo.sh
./REPLAY_EXPERIMENTAL.sh
```

The 25-tick scenario is entirely simulated, includes an interruption, and reaches 51.5% SOC against a 50% target. **No real electricity, EV, Home Assistant account, bill, customer, or money is involved.** No measured savings claim.

## What is verified

- Original Energy controller tests: **64/64** on the independent review host.
- Fresh-extraction independent replay on Linux with working namespaces: **64/64** original Energy controller and **67/67** Living Energy integration tests PASSED (131 total). The 67 include 12 Argon safety/isolation/recorded-repair tests, five existing shared-loop tests, and the three new Dogfood 006 standing regressions. The alternate portable fallback separately passed **55/55**; it now avoids multiprocessing spawn recursion on hosts without namespaces.
- Shared-state safety regressions: 8 tests total, including damaged-history refusal, external accepted-basis drift reopening, and normal operation.
- Source preservation and restart in simulated conditions. Mechanical accepted-source promotion and receiver-attestation replay pass.
- Matched conventional and OpenLine **recorded patch replay**: both repair 3/3 fixed injected defects. The test suite **does not execute a fresh LLM agent**; previously generated edits are applied by `apply_subagent_patch`. The reported native subagent trial is historical evidence, not independently rerunnable here.

## Boundaries that matter

**Public source does not mean live hardware is approved.** This integrated project never instantiates a real Home Assistant client. Hardware activation and live usage require separate engineering validation and explicit owner consent. Never use an ordinary mains smart plug as a charging control.

The `owner_approved` flag in the demonstration's generalized standing loop is a local caller assertion, **not an identity signature**. It defaults to false, rechecks authority and evidence basis before promotion, and refuses stale changes. Each new loop cycle now refuses a damaged receipt history and reopens unaccepted changes in the external evidence basis rather than silently adopting them. The chain remains a local integrity check, not a receiver authentication signature. Do not expose its API as production authorization without a receiver-owned authenticated approval mechanism and independently evaluated sandbox.

The namespace sandbox permits loopback-only network traffic for fake local test servers and blocks the tested escapes. This **is not a complete security proof** of a production sandbox, and the host must allow user namespaces. No automatic agent-generated code is run against real equipment.

Energy planning matches a competent conventional scheduler on tested equal-input cases. OpenLine's exploratory value is evidence, scoped recovery, and accepted-work continuity, not superior tariff math. Source-hash chains are not third-party signed proof of physical outcomes.

## Separation and provenance

Original public code remains untouched:
- https://github.com/terryncew/openline-energy-autopilot
- https://github.com/terryncew/openline-living-projects

This candidate packages the code necessary to reproduce the *source-only* simulation and selected SafePromotion source, without private original ZIPs or proprietary files. See `THIRD_PARTY_NOTICES.md`.

See `RELEASE_GATE.md` for the exact publication versus live-operation distinction, `DEDICATION.md` for the founder's dedication, and `SHA256SUMS.txt` for the content audit.
