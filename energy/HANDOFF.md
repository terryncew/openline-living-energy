# OpenLine Energy Autopilot 001 — Private engineering handoff

## Outcome

**ENGINEERING PILOT: AUTONOMOUS LOCAL-CONTROL LOOP PASS; PHYSICAL CHARGER / REAL MONEY SAVED NOT TESTED.**

A background service (not a dashboard) autonomously computes a deadline-aware charger schedule, sends exact Home Assistant REST switch-on/off calls after explicit owner authorization, observes state, adapts to rate/SOC changes, records tamper-detecting local receipts, and avoids duplicate actuation after uncertain outcomes. It makes no cloud or paid API calls.

The same controller completes a 24-step, unattended **simulated** charging session and meets the fake vehicle target, with a modeled lower-rate cost versus immediate charging. The Home Assistant API transport also completed an authenticated real HTTP integration test against a fake local HA server. A separate scenario moves the low-price window after the original plan, and the controller replans to preserve charging cost while a static timer would charge during a more expensive period. **No physical EV, charger, utility meter, or real household savings have been accessed or measured.**

## Why this is genuinely different from the v0.2.1 solar advisor

- It keeps running in the background after initial authorization.
- It sends actual actuator commands through a documented integration when connected.
- It changes actions as SOC, tariff, and deadline change.
- It never invents savings evidence and records action provenance and safety refusals.
- It can be paused without opening a dashboard, and a user override is respected.

The limits are real: it works only for Home Assistant users with a charger-specific safe switch, vehicle SOC + plug data, and trustworthy price forecast. That is a narrower market than all California households. It is not safe to call it universal plug-and-play; hardware validation remains mandatory.

## Verified local commands

```bash
python3 demo.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

When it is installed in a compatible Home Assistant household, the owner uses `python3 run.py setup`, observes `python3 run.py once`, explicitly authorizes the charger, then runs `python3 run.py loop --live` or a supervisor. No physical actuation is permitted in the demo or default dry-run.

## Independent falsifiers for hardware pilot

- Does the configured entity really control *charger enable* rather than unsafe mains power? Verify manufacturer behavior and vehicle BMS limits.
- Does device readback correlate with actual metered charging power? Test real charging, disconnects, power outages, and HA restarts.
- Is the tariff forecast correct for the household's actual rate and timezone, including DST and price changes? Compare to provider bill or official rate feed.
- Does user override always stop further automation? Test manual pause, revoked config, and actuator control by another integration.
- Are actual monthly costs and energy delivered lower than an ordinary TOU scheduler or mature evcc setup, **net of integration effort and upkeep**? If not, use the existing better system.

**Do not publish, deploy, or attach to a real charger without separate owner approval, physical safety review, and verified entity mapping.** No future hardware interaction was performed in this assignment.
