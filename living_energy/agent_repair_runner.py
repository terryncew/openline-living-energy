"""Native Subagent Repair Runner and 3-Way Comparison Harness for Living Energy 004.

Connects recorded native coding-agent repairs (Worker A1 Conventional vs Worker A2 OpenLine)
and an Ablation workflow (without snapshot refresh / 3-way git merge-tree / CAS recovery)
to IsolatedRepairSandbox, ReceiverSandboxAttestor, RepairBridge, SafePromotion, and LivingEnergy.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .loop import LivingEnergy
from .repair_bridge import load_existing_promotion
from .repair_bridge import (
    IsolatedRepairSandbox,
    ReceiverSandboxAttestor,
    RepairBlocked,
    RepairBridge,
    source_sha,
    tree_digest,
)

# Authentic repairs produced by isolated native subagents during Argon Multiproject Dogfood
SUBAGENT_TRIAL_RECORDS = {
    "conventional_agent": {
        "agent_type": "native_invoke_subagent_conventional",
        "elapsed_s": 10.0,
        "output_tokens_approx": 853,
        "repairs": {
            "LE-F1": {
                "target_file": "src/autopilot/planning.py",
                "search": "gross_capacity = charge_power_kw * (_utc(slot.end) - _utc(slot.start)).total_seconds() / 3600.",
                "replace": "gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.",
                "root_cause": "Used full slot duration (_utc(slot.end) - _utc(slot.start)) instead of clipped intersection (right - left).",
            },
            "LE-F2": {
                "target_file": "src/autopilot/core.py",
                "search": "if (data['session'] is None or not switch_on) and plugged:",
                "replace": "if data['session'] is None and plugged:",
                "root_cause": "Evaluated only state.switch_on, generating a new session_id when resuming from a PAUSE where switch_on is False and paused_session is True.",
            },
            "LE-F3": {
                "target_file": "living_energy/loop.py",
                "search": "'constraint':'TARIFF_MISSING','commands':[],'stop':result}",
                "replace": "'constraint':'TARIFF_MISSING','commands':result.get('commands',[]),'stop':result}",
                "root_cause": "Hardcoded 'commands': [] instead of forwarding result.get('commands', []) from self.stop(now).",
            },
        },
    },
    "openline_agent": {
        "agent_type": "native_invoke_subagent_openline",
        "elapsed_s": 10.0,
        "output_tokens_approx": 1181,
        "repairs": {
            "LE-F1": {
                "target_file": "src/autopilot/planning.py",
                "search": "gross_capacity = charge_power_kw * (_utc(slot.end) - _utc(slot.start)).total_seconds() / 3600.",
                "replace": "gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.",
                "binding_constraint": "gross_capacity computed from unclipped slot duration (_utc(slot.end) - _utc(slot.start)) instead of clipped [left, right] window.",
                "falsifier": "plan_charge starting 30 min into a 60 min 4.0 kW slot must report 1.7 kWh available_grid_kwh, not 3.4 kWh.",
            },
            "LE-F2": {
                "target_file": "src/autopilot/core.py",
                "search": "if (data['session'] is None or not switch_on) and plugged:",
                "replace": "if data['session'] is None and plugged:",
                "binding_constraint": "Session continuity lost across PAUSE because switch_on is False while paused_session is True.",
                "falsifier": "Charging -> PAUSE -> Charging must preserve active_session_id; Charging -> STOP -> Charging must mint a fresh session_id.",
            },
            "LE-F3": {
                "target_file": "living_energy/loop.py",
                "search": "'constraint':'TARIFF_MISSING','commands':[],'stop':result}",
                "replace": "'constraint':'TARIFF_MISSING','commands':result.get('commands',[]),'stop':result}",
                "binding_constraint": "HOLD_TARIFF_INVALID_STOPPED return dict drops the simulated OFF command returned by self.stop(now).",
                "falsifier": "Malformed tariff while switch_on is True under HOLD policy must return commands[0]['command'] == 'OFF'.",
            },
        },
    },
}


def apply_subagent_patch(workspace: Path, arm: str, family_id: str) -> Path:
    spec = SUBAGENT_TRIAL_RECORDS[arm]["repairs"][family_id]
    target = workspace / spec["target_file"]
    content = target.read_text(encoding="utf-8")
    if spec["search"] not in content:
        raise ValueError(f"Defect pattern for {family_id} not found in {target}")
    patched = content.replace(spec["search"], spec["replace"], 1)
    target.write_text(patched, encoding="utf-8")
    return target
