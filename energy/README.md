# OpenLine Energy Autopilot 001 — EV charging that actually schedules itself

**Private engineering prototype. Not published, deployed, or tested with physical charging equipment.**

**Job:** For an existing, compatible Home Assistant EV charger, automatically postpone charging to less expensive owner-verified tariff windows, start it, verify the charger enable state, and stop when the owner's EV SOC target is reached. Replan after price, SOC, and connectivity changes. Never require a dashboard to remain open. Track estimated costs and retain local action receipts.

## Try the actual control loop — no account, no API, no hardware

From this directory, with Python 3.11+ installed:

```bash
python3 demo.py
```

This executes the same controller against a **simulated charger**. In the frozen example, charging 6.667 grid kWh costs about **$0.67** in a hypothetical low-rate window, compared with **$3.33** if charged immediately at the hypothetical high rate. This is **$2.67 of modeled difference**, **not measured household savings**. The simulated charger turns ON and OFF and reaches its target automatically.

Reproduce the 250-case optimality cross-check, real HTTP call protocol against a fake local Home Assistant server, authorization gates, failure recovery, and full 24-tick charging scenario:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Connect once — then run unattended

**Supported initial customer:** an owner already running Home Assistant with (a) a charger-specific *safe charging enable/disable* `switch` (not an ordinary mains outlet), (b) a reliable vehicle plug sensor and SOC sensor, and (c) trustworthy forward price intervals, supplied in a Home Assistant price-forecast sensor or a maintained local timestamped CSV. An optional cumulative charger kWh meter improves cost accounting. The vehicle must enforce its own maximum charge limit. The controller is software scheduling, **not an electrical safety device**.

1. `python3 run.py setup --config ./local/config.json` — the one-time assistant discovers likely charger/vehicle/rate entities from Home Assistant, but requires the owner to confirm the actual charger switch. Enter an HA access token in the hidden prompt. It's saved **locally with owner-only permissions** in `./local/ha-token.txt` and never uploaded. Do not paste tokens in chat or commit them to a repo.
2. `python3 run.py once --config ./local/config.json --state ./local/private/state.json` — dry-run the schedule and inspect the proposed behavior. No write calls.
3. `python3 run.py authorize --config ./local/config.json` — explicitly approve switching exactly the configured charger; the application refuses remote writes otherwise. **Do not approve without verifying the entity is a charger-safe enable control.**
4. `python3 run.py loop --live --config ./local/config.json --state ./local/private/state.json --interval 60` — run unattended under an OS process supervisor. Example Docker Compose is provided (`docker compose up -d --build`) using the same local folder. Docker was **not available to execute in this testing environment**; its runtime behavior is not verified here.

After setup, it operates without customer input on each charge session. The person can `python3 run.py pause` (the default state path matches the installed default config), or enable the optional Home Assistant `pause_entity`;  the service respects manual switch overrides. To resume a manual override or ambiguous hardware effect, inspect the physical charger and reconcile the state before resetting its local private state—do not blindly delete receipts to silence a failure.

### Home Assistant price-forecast contract

Recommended: one local Home Assistant `sensor.*` whose `attributes.prices` is a complete, timezone-aware, **non-overlapping** list of `{start, end, usd_per_kwh}` intervals covering the period from now until the next departure deadline. This is an adapter contract, not a claim every utility integration already exposes this format. See `examples/price_sensor_format.json`. If you have another utility-price integration, a small locally tested adapter must expose this format. No commercial rate lookup, hidden cloud calls, or guaranteed utility savings.

Alternative: owner-supplied timestamped CSV with columns `start,end,usd_per_kwh`. When forecast coverage is missing, the service cannot optimize cost. **Without a hard price cap**, it prioritizes charging by the target time instead. **With an owner hard price cap**, it holds charging until the price can be verified; that may mean missing departure readiness. Either condition raises a receipt warning.

### Policy

The owner specifies the target SOC (maximum 95%), battery usable kWh, power kW, approximate charging efficiency, daily departure time and timezone. The local controller reads the configured HA entities and selects the cheapest available slots. A small capacity margin is applied. Late/insufficient capacity triggers prompt charging when allowed, and is disclosed as a target-at-risk condition. A per-day action budget and minimum dwell time prevent uncontrolled switching; necessary OFF commands are permitted even if the ON command budget is exhausted.

No purchases, bill pay, grid interconnection, EV override outside the explicitly approved entity, cloud service, user tracking, or direct physical circuit control. No electricity market participation or claim of bill reduction is implied by the charger switch itself.

## Design and receipts

- **No dashboard or browser process needed.** A background Python stdlib-only service communicates with Home Assistant's documented REST API over the owner's network. Uses exact entity IDs and hardcoded `switch.turn_on` / `switch.turn_off`, with readback.
- **No surprise autonomy:** Default is read-only; both `owner_authorized=true` and `--live` are needed before any POST.
- **Receiver-controlled gate:** Re-read plug status, SOC, pause flag, and current switch state before issuing an effect. Write an intent receipt first. Do not retry ambiguous commands; reconcile the observed switch state. HA lacks an atomic CAS, so a race can still exist between final read and effect.
- **Manual override:** If the observed switch diverges from the last automatic command, pause. Owner can pause explicitly. An unobserved manual change that is reversed before the next poll cannot be reliably detected.
- **Receipt chain:** Local chained SHA-256 records record evidence, plans, intent and outcome, with automatic hash-pinned log archival when the active log reaches its configured size. They are not third-party signatures, and an attacker with full local-file access can rewrite the chain. Preserve immutable off-device backups if stronger auditability is required.
- **Savings accounting:** Exact tariff math on supplied inputs and modeled differences to *immediate charging*. Actual charging kWh is used for approximate cost if a meter is provided and no rate interval was crossed between readings; otherwise kWh is marked unpriced. This is not an independent counterfactual or verified bill reduction.
- **Security:** HA token stays on the local machine; HTTP access is restricted to local/private hosts and HTTPS for remote. The REST API token may carry broader permissions than this application's exact entity gate. For strongest isolation use a dedicated HA user with the minimum privileges available in your deployment.
- **Failure mode:** Missing critical vehicle telemetry or compromised/tampered local receipts -> HOLD and flag. A real physical charger may not respond even when HA state reports ON; optional charge-power sensor warns when no power is measured after 10 minutes.

## Pause/revoke and restart safety

With the default config, `python3 run.py pause` and `python3 run.py resume` operate on the same local state used by `python3 run.py loop`; if you gave a nondefault `--config`, supply that same value to pause/resume. For Docker, use the configured Home Assistant `pause_entity` or execute the pause command inside the container with `--state /data/private/state.json`. The controller reloads owner policy before each tick and checks it again before each remote switch call. If the configured charger switch or owner authorization changes, it blocks the stale plan. An unresolved remote action stops further effects until a human checks hardware state. Manual stopping through Home Assistant always remains available.

## No claim of universal plug-and-play

An ordinary household without a compatible controllable charger, Home Assistant, reliable SOC telemetry or price feed cannot use this as a working optimizer yet. Those are real hardware/integration bottlenecks, not settings we can pretend away. The one-time connection cannot safely be skipped. We have tested a real local REST command protocol against a fake HA server, **not** a real EV/charger or real-world energy savings.

## Comparison with existing technology

[evcc](https://docs.evcc.io/en/smarthome/home-assistant/) already provides mature smart charging and Home Assistant integrations. OpenLine is **not** claimed to be a novel charging algorithm or superior to evcc. The bounded permission + receipt + fail-safe action loop is what this prototype tests; any real product must compete with evcc on integration effort, reliability and observed customer savings before release.

## Status

**ENGINEERING PILOT — real autonomous decisions and local API commands verified in simulation; physical integration and actual money saved NOT TESTED.** Owner authorization needed for real charger switching and public release.

For optional meter outages, the controller continues charging using verified SOC and deliberately marks the skipped meter interval as unpriced, disabling comparison claims for that session. No default “savings” figure is presented to the household without its verified tariff and energy-meter evidence.


## Independent break-and-repair (private, 2026-11-03)

This version contains a **candidate** safety patch, not an approved physical charger release.
Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` (64/64 local simulation tests).
The patched authorization wizard pins the selected charger entity, Home Assistant origin,
and control policy via a SHA-256 config-scope fingerprint. Editing scope requires
re-running `authorize` and deliberate owner consent. The fingerprint is **not** a
cryptographic owner signature and cannot resist an attacker who can rewrite all local
config files and hashes. Restrict OS permissions and HA token capability.

New onboarding sets `max_sensor_age_minutes=15` and checks `last_updated` for
charger, vehicle SOC, plug, pause, tariff sensor, power and meter. Existing hand-
written configs without this setting lack runtime age verification: add the setting
and reauthorize before any hardware pilot. Missing/upstream timestamps fail closed
when configured. A tariff outage without a hard cap intentionally falls back to
charging for deadline protection and has **no verified cost claim**; review this policy.

Failed OFF commands now receive at most three additional bounded OFF retries for
the **same journaled approved entity**, including when SOC becomes unavailable.
They never retry an uncertain ON. If the REST transport is offline or a charger is
stuck energized, software cannot guarantee physical shutdown. A hardware watchdog
and independently verified current/power are necessary before a physical pilot.

Manual pause freezes automation, **not hardware current**: a paused ON switch may
remain ON and a warning is reported. An unplugged vehicle causes a protective OFF
only if this controller previously enabled that switch; the owner must verify this
is semantically safe for the specific charger enable entity.

DST fall-back ambiguous departure clocks are refused; the owner must choose an
unambiguous local departure time for that day. Meter readings are screened for
physical plausibility, but plausible forged readings can still be misleading.
No real savings or physical charge guarantees have been tested.
