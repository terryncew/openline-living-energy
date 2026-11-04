"""Pure, dependency-free cost-aware EV charge scheduler.

All times are timezone-aware and the grid-tariff input is an owner-supplied
forecast, not a claim about any particular utility.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Iterable


@dataclass(frozen=True)
class TariffSlot:
    start: datetime
    end: datetime
    usd_kwh: float

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError('Tariff windows require ordered, aware timestamps')
        if not isfinite(self.usd_kwh) or not -2 <= self.usd_kwh <= 5:
            raise ValueError('Tariff price is non-finite or beyond accepted bounds')


@dataclass(frozen=True)
class Plan:
    charge_now: bool
    need_grid_kwh: float
    available_grid_kwh: float
    planned_grid_kwh: float
    estimated_plan_cost: float | None
    immediate_cost: float | None
    modeled_avoided_cost: float | None
    reason: str
    feasible_by_deadline: bool
    selected_slot_starts: tuple[str, ...]


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError('Naive datetime forbidden')
    return dt.astimezone(timezone.utc)


def validate_slots(slots: Iterable[TariffSlot]) -> tuple[TariffSlot, ...]:
    items = sorted(slots, key=lambda x: _utc(x.start))
    if not items or len(items) > 7 * 24 * 12:
        raise ValueError('Tariff forecast empty or excessive')
    for a, b in zip(items, items[1:]):
        if _utc(b.start) < _utc(a.end):
            raise ValueError('Overlapping tariff windows')
    return tuple(items)


def plan_charge(now: datetime, deadline: datetime, need_grid_kwh: float,
                charge_power_kw: float, slots: Iterable[TariffSlot],
                *, usable_fraction: float = 0.85,
                hard_price_cap: float | None = None) -> Plan:
    """Select cheapest future time slices, respecting deadline & reserve margin.

    A reserve fraction reduces planned capacity to handle slight delivery shortfall.
    It is *not* an asserted universal optimum or a physical battery reserve.
    """
    now, deadline = _utc(now), _utc(deadline)
    if deadline <= now:
        return Plan(True, max(0., need_grid_kwh), 0, 0, None, None, None,
                    'deadline_elapsed', False, ())
    if not isfinite(need_grid_kwh) or need_grid_kwh < 0:
        raise ValueError('Energy requirement invalid')
    if not isfinite(charge_power_kw) or not 0.2 <= charge_power_kw <= 22:
        raise ValueError('Charging power outside supported range')
    if not 0.5 <= usable_fraction <= 1.0:
        raise ValueError('Usable fraction outside limits')
    if hard_price_cap is not None and (not isfinite(hard_price_cap) or not -2 <= hard_price_cap <= 5):
        raise ValueError('Invalid hard price cap')
    if need_grid_kwh < 0.01:
        return Plan(False, need_grid_kwh, 0, 0, 0., 0., 0., 'target_met', True, ())
    slots = validate_slots(slots)
    intersected = []
    for slot in slots:
        left, right = max(_utc(slot.start), now), min(_utc(slot.end), deadline)
        if right <= left: continue
        if hard_price_cap is not None and slot.usd_kwh > hard_price_cap: continue
        gross_capacity = charge_power_kw * (right - left).total_seconds() / 3600.
        intersected.append((left, right, slot.usd_kwh, gross_capacity * usable_fraction))
    # Fail closed on coverage gaps: we cannot choose a 'cheap' period if unknown gaps
    # hide price or if the user's owner-supplied tariff is too short.
    all_slots = []
    for slot in slots:
        left, right = max(_utc(slot.start), now), min(_utc(slot.end), deadline)
        if right > left: all_slots.append((left,right))
    cursor = now
    for a,b in all_slots:
        if a > cursor: break
        cursor = max(cursor, b)
    coverage_complete = cursor >= deadline
    if not coverage_complete:
        return Plan(hard_price_cap is None, need_grid_kwh, 0, 0, None, None, None,
                    ('price_forecast_gap_charge_now' if hard_price_cap is None
                     else 'price_forecast_gap_hard_cap_hold'), False, ())
    total = sum(s[3] for s in intersected)
    if not intersected:
        return Plan(False, need_grid_kwh, 0, 0, None, None, None,
                    'hard_price_cap_no_eligible_slot', False, ())
    # A competent conventional cheapest-first planner is the comparator; no proprietary
    # optimality claim is being made.
    chosen = sorted(intersected, key=lambda x: (x[2], x[0]))
    remaining = need_grid_kwh
    cost, selected = 0., []
    for left,right,price,capacity in chosen:
        portion = min(remaining, capacity)
        if portion > 1e-9:
            selected.append((left, right, price, portion))
            cost += portion * price
            remaining -= portion
        if remaining <= 1e-9: break
    chron = sorted(intersected, key=lambda x: x[0])
    left_need, immediate_cost = need_grid_kwh, 0.
    for a,b,price,capacity in chron:
        part = min(left_need, capacity)
        immediate_cost += part*price
        left_need -= part
        if left_need <= 1e-9: break
    feasible = remaining <= 1e-9
    current_selected = any(a <= now < b for a,b,price,portion in selected)
    if not feasible:
        # Charge now if permission allows; delay cannot make an impossible deadline feasible.
        # An owner hard cap remains inviolable and must not be overridden.
        if hard_price_cap is None:
            return Plan(True, need_grid_kwh, total, need_grid_kwh-remaining,
                        cost, immediate_cost, None,
                        'insufficient_remaining_capacity_charge_now', False,
                        tuple(a.isoformat() for a,b,p,e in selected))
        return Plan(False, need_grid_kwh, total, need_grid_kwh-remaining,
                    cost, immediate_cost, None,
                    'hard_price_cap_prevents_deadline', False,
                    tuple(a.isoformat() for a,b,p,e in selected))
    return Plan(current_selected, need_grid_kwh, total, need_grid_kwh,
                round(cost,6), round(immediate_cost,6),
                round(immediate_cost-cost,6), 'lowest_cost_schedule', True,
                tuple(a.isoformat() for a,b,p,e in selected))
