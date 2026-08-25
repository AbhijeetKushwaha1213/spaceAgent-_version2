"""
SENTINEL — Condition Evaluation (conditions.py)

Phase 1. Evaluates the ``Condition`` predicates declared in the command
registry against a crash-dump context.

Every predicate is TRI-STATE:

    SATISFIED  the predicate demonstrably holds
    VIOLATED   the predicate demonstrably does not hold
    UNKNOWN    the context does not contain the data needed to decide

Policy (as of Phase 1 fail-closed hardening):

This module only computes the tri-state verdict; how each state is treated is
decided by the safety gate (``app/agent/safety.py``). The gate's policy is
risk-aware:

  * A **prohibited hazard** that is UNKNOWN does NOT block — a present hazard
    still blocks, but absence of a hazard reading stays permissive, because a
    ground operator may have confirmed the state out of band and refusing to act
    on absent hazard data would make the tool unusable on partial dumps.
  * A **required precondition** that is UNKNOWN BLOCKS when the command is
    safety-critical (CRITICAL/HIGH consequence), with reason code
    ``MISSING_PRECONDITION``. A safety-critical action must not be authorized on
    the *absence* of the affirmative evidence it needs. Lower-severity required
    preconditions remain permissive on UNKNOWN.

The consequence: a blocked-step list is evidence of a detected hazard OR of an
unconfirmable safety-critical precondition — never proof that no hazard exists.

The extraction helpers in this module were moved here from safety.py unchanged,
so the tri-state verdicts reproduce the previous behaviour exactly; only the
gate's treatment of UNKNOWN for required preconditions changed in Phase 1.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from app.validation.command_registry import Condition

# ── Thresholds ──────────────────────────────────────────────────────────────
# Kept at their pre-Phase-1 values. safety.py re-exports these names.

# Phase 5: read from app/ingest/channel_dict.py, the authoritative channel
# dictionary, instead of being declared here.
#
# These are SAFETY thresholds, not hard limits, and the dictionary keeps the two
# apart: SoC_pct has a hard minimum of 20% while the validator refuses
# power-hungry commands below 15%, and Component_temp_C has a hard maximum of
# 65 degC while the validator permits only thermal remedies above 85 degC. Both
# numbers used to appear in three places — here, in the LLM prompt, and in the RAG
# procedure text — with nothing checking they agreed.
#
# The values are unchanged; only their source moved. test_safety.py asserts they
# are still exactly 15.0 and 85.0.


def _policy_threshold(channel: str, kind: str, expected: float) -> float:
    """Fetch a safety threshold from the channel dictionary.

    Falls back to the documented value if the dictionary is unavailable, so this
    module keeps working standalone — but logs, because a safety threshold
    silently falling back to a hardcoded constant is exactly the drift Phase 5
    set out to remove.
    """
    try:
        from app.ingest.channel_dict import safety_ceiling, safety_floor

        value = (safety_floor(channel) if kind == "floor"
                 else safety_ceiling(channel))
        if value is None:
            raise ValueError(f"{channel} declares no safety {kind}")
        return float(value)
    except Exception as exc:  # pragma: no cover — dictionary is in-tree
        import logging

        logging.getLogger("sentinel.validation").error(
            "channel dictionary unavailable for %s safety %s (%s); falling back "
            "to the documented value %s",
            channel, kind, exc, expected,
        )
        return expected


BATTERY_FLOOR_SOC: float = _policy_threshold("SoC_pct", "floor", 15.0)
"""Battery state-of-charge floor, percent. Below this, power-hungry commands
are refused. Source: SoC_pct.safety_limits in app/ingest/channel_dict.py."""

THERMAL_SURVIVAL_LIMIT: float = _policy_threshold(
    "Component_temp_C", "ceiling", 85.0)
"""Component temperature limit, Celsius. Above this, only thermal remedies and
observations are permitted. Source: Component_temp_C.safety_limits in
app/ingest/channel_dict.py."""


class ConditionState(str, Enum):
    """Tri-state result of evaluating a predicate."""
    SATISFIED = "SATISFIED"
    VIOLATED = "VIOLATED"
    UNKNOWN = "UNKNOWN"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONTEXT EXTRACTION (moved verbatim from safety.py)
# ═══════════════════════════════════════════════════════════════════════════

def is_value_nan_or_missing(value: Any) -> bool:
    """True if a value is missing, None, NaN, or not usable as sensor data."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().upper() in ("NAN", "NONE", "", "N/A", "NULL")
    if isinstance(value, (int, float)):
        return math.isnan(value) if isinstance(value, float) else False
    return True  # Any other type is considered invalid for sensor data


def canonical_readings(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the crash dump's CANONICAL telemetry window.

    Phase 3. The extractors below used to scan ``ctx["pre_fault_telemetry"]``
    only, so a value carried solely in ``pre_fault_telemetry_window`` was
    invisible to safety validation — a battery SoC present in the window but not
    the legacy list would read as "absent", and the documented policy maps absent
    to permissive. A precondition silently skipped is the worst failure mode a
    safety layer has.

    Falls back to a direct read if the adapter is unavailable, so this module
    keeps working standalone.
    """
    if not isinstance(ctx, dict):
        return []
    try:
        from app.api.adapters import canonical_window_dicts

        rows = canonical_window_dicts(ctx)
        if rows:
            return rows
    except Exception:  # pragma: no cover — adapter always available in-tree
        pass

    merged: list[dict[str, Any]] = []
    for field in ("pre_fault_telemetry_window", "pre_fault_telemetry"):
        source = ctx.get(field)
        if isinstance(source, list):
            merged.extend(r for r in source if isinstance(r, dict))
    return merged


# ── Time ordering ───────────────────────────────────────────────────────────
#
# Why this exists: the extractors below were written when telemetry was a single
# snapshot, so they returned the FIRST matching row. Against the canonical
# window — a time series — first-match returns the OLDEST sample. Measured on
# preset scenario 1, whose gyro reads 0.5 (T-120s) -> MISSING (T-60s) -> NaN
# (T-0s): first-match reported a healthy 0.5 and GYRO_DATA_VALID flipped from
# VIOLATED to SATISFIED, unblocking attitude actuation on a spacecraft whose gyro
# had actually dropped out. A precondition asks about the CURRENT state, so the
# LATEST sample decides.
#
# The legacy list carries no timestamps; the adapter places those rows at T-0s,
# which is what they mean (state at fault time). "Latest wins" therefore also
# reproduces the pre-Phase-3 verdicts.


def _time_rank(index: int, row: dict[str, Any]) -> tuple[int, float, int]:
    """Sort key making the most recent reading the maximum.

    Untimed rows rank below every timed row, and ties resolve to the earliest
    list position, so a set of untimed rows degenerates to the old first-match
    behaviour instead of silently picking a different one.
    """
    rel = row.get("relative_time_s")
    if isinstance(rel, (int, float)) and not (
        isinstance(rel, float) and math.isnan(rel)
    ):
        return (1, float(rel), -index)
    return (0, 0.0, -index)


def _matching_rows(
    ctx: dict[str, Any],
    names: tuple[str, ...],
) -> list[tuple[tuple[int, float, int], dict[str, Any]]]:
    """Ranked (key, row) pairs for every reading of the named channels."""
    ranked = []
    for index, row in enumerate(canonical_readings(ctx)):
        if isinstance(row, dict) and row.get("parameter") in names:
            ranked.append((_time_rank(index, row), row))
    return ranked


def _latest_value(ctx: dict[str, Any], names: tuple[str, ...]) -> Any:
    """Value of the most recent reading, usable or not.

    For validity checks (gyro, transponder lock) an unusable latest reading IS
    the answer — skipping past it to an older good value would assert a state
    that is no longer observed.
    """
    ranked = _matching_rows(ctx, names)
    if not ranked:
        return _ABSENT
    return max(ranked, key=lambda pair: pair[0])[1].get("value")


def _latest_usable_number(
    ctx: dict[str, Any],
    names: tuple[str, ...],
) -> float | None:
    """Most recent numerically usable reading of the named channels.

    Used where the check is about a magnitude rather than data validity (battery
    SoC, temperature). A dropout must not erase a known-bad magnitude: if SoC
    read 10% and the newest sample is NaN, reporting "unknown" would permit the
    power-hungry commands the 10% is there to block, because this module's
    documented policy is that UNKNOWN never blocks.
    """
    ranked = [
        (key, row) for key, row in _matching_rows(ctx, names)
        if not is_value_nan_or_missing(row.get("value"))
    ]
    for key, row in sorted(ranked, key=lambda pair: pair[0], reverse=True):
        try:
            return float(row.get("value"))
        except (ValueError, TypeError):
            continue
    return None


#: Sentinel for "no reading of this channel exists anywhere in the dump".
#: Distinct from a present-but-unusable reading, which must stay visible.
_ABSENT = "NOT_FOUND"


def get_battery_soc(ctx: dict[str, Any]) -> float | None:
    """Extract battery state-of-charge. None when absent."""
    for key in ("SOC", "BATTERY_SOC", "battery_soc", "SoC_pct", "soc_pct"):
        val = ctx.get(key)
        if val is not None and not is_value_nan_or_missing(val):
            try:
                return float(val)
            except (ValueError, TypeError):
                pass

    soc = _latest_usable_number(
        ctx, ("SoC_pct", "SOC", "battery_soc", "BATTERY_SOC"),
    )
    if soc is not None:
        return soc

    hw = ctx.get("hardware_state")
    if isinstance(hw, dict):
        for key in ("battery_soc", "SOC", "BATTERY_SOC", "SoC_pct"):
            val = hw.get(key)
            if val is not None and not is_value_nan_or_missing(val):
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass

    return None


def get_gyro_rate(ctx: dict[str, Any]) -> Any:
    """Extract the gyro rate value. Returns "NOT_FOUND" when absent."""
    for key in ("GYRO_A_RATE", "gyro_a_rate", "Gyro_rate_degs",
                "GYRO_B_RATE", "gyro_b_rate"):
        if key in ctx:
            return ctx[key]

    # Validity check: the latest reading decides, NaN included.
    value = _latest_value(
        ctx, ("Gyro_rate_degs", "GYRO_A_RATE", "gyro_a_rate",
              "GYRO_B_RATE", "gyro_b_rate"),
    )
    if value is not _ABSENT:
        return value

    hw = ctx.get("hardware_state")
    if isinstance(hw, dict):
        if hw.get("gyro_health") == "degraded":
            return None  # Degraded = treat as invalid

    return "NOT_FOUND"


def get_transponder_lock(ctx: dict[str, Any]) -> Any:
    """Extract transponder lock status. Returns "NOT_FOUND" when absent."""
    for key in ("TRANSPONDER_LOCK", "transponder_lock", "Transponder_lock"):
        if key in ctx:
            return ctx[key]

    # Validity check: a lock lost at the latest sample must not be masked by an
    # earlier sample that still had lock.
    value = _latest_value(
        ctx, ("Transponder_lock", "TRANSPONDER_LOCK", "transponder_lock"),
    )
    if value is not _ABSENT:
        return value

    return "NOT_FOUND"


def get_max_temperature(ctx: dict[str, Any]) -> float | None:
    """Extract the maximum component temperature. None when absent."""
    temps: list[float] = []

    for key in ("Component_temp_C", "component_temp_c", "TEMP_C",
                "temperature_c", "temp_c", "OBC_temp_C"):
        val = ctx.get(key)
        if val is not None and not is_value_nan_or_missing(val):
            try:
                temps.append(float(val))
            except (ValueError, TypeError):
                pass

    # Latest usable reading PER temperature channel, then the max across
    # channels — i.e. the current hottest component. Taking the max over the
    # whole window instead would block on a transient that has already passed,
    # including blocking the thermal remedy that resolved it.
    latest_per_channel: dict[str, tuple[tuple[int, float, int], float]] = {}
    for index, entry in enumerate(canonical_readings(ctx)):
        if not isinstance(entry, dict):
            continue
        param = str(entry.get("parameter", ""))
        if "temp" not in param.lower():
            continue
        val = entry.get("value")
        if val is None or is_value_nan_or_missing(val):
            continue
        try:
            numeric = float(val)
        except (ValueError, TypeError):
            continue
        rank = _time_rank(index, entry)
        current = latest_per_channel.get(param)
        if current is None or rank > current[0]:
            latest_per_channel[param] = (rank, numeric)
    temps.extend(value for _, value in latest_per_channel.values())

    for key in ("temperatures", "temp_readings"):
        val = ctx.get(key)
        if isinstance(val, list):
            for v in val:
                if isinstance(v, (int, float)) and not (
                    isinstance(v, float) and math.isnan(v)
                ):
                    temps.append(float(v))
        elif isinstance(val, dict):
            for v in val.values():
                if isinstance(v, (int, float)) and not (
                    isinstance(v, float) and math.isnan(v)
                ):
                    temps.append(float(v))

    return max(temps) if temps else None


_NO_LOCK_VALUES = (0, False, "0", "false", "False", "no", "NO")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — PREDICATE EVALUATION
# ═══════════════════════════════════════════════════════════════════════════
#
# Each evaluator returns (state, supporting_context). The supporting context is
# the observed data the verdict was based on — it is surfaced to the operator on
# a blocked step, so it must contain only values actually read from the dump.
# ═══════════════════════════════════════════════════════════════════════════

def _eval_battery(ctx: dict[str, Any]) -> tuple[ConditionState, dict[str, Any]]:
    soc = get_battery_soc(ctx)
    if soc is None:
        return ConditionState.UNKNOWN, {"battery_soc_pct": None,
                                        "floor_pct": BATTERY_FLOOR_SOC}
    support = {"battery_soc_pct": round(soc, 2), "floor_pct": BATTERY_FLOOR_SOC}
    if soc < BATTERY_FLOOR_SOC:
        return ConditionState.VIOLATED, support
    return ConditionState.SATISFIED, support


def _eval_gyro(ctx: dict[str, Any]) -> tuple[ConditionState, dict[str, Any]]:
    value = get_gyro_rate(ctx)
    if value == "NOT_FOUND":
        return ConditionState.UNKNOWN, {"gyro_rate": None}
    support = {"gyro_rate": value}
    if is_value_nan_or_missing(value):
        return ConditionState.VIOLATED, support
    return ConditionState.SATISFIED, support


def _eval_comms_lock(ctx: dict[str, Any]) -> tuple[ConditionState, dict[str, Any]]:
    value = get_transponder_lock(ctx)
    if value == "NOT_FOUND":
        return ConditionState.UNKNOWN, {"transponder_lock": None}
    support = {"transponder_lock": value}
    if is_value_nan_or_missing(value):
        # Present but malformed (None / NaN / "NaN" / ""): the reading cannot
        # confirm a lock. A CRITICAL required precondition must not be treated as
        # SATISFIED on garbage — symmetric with _eval_gyro. (A wholly ABSENT lock
        # is UNKNOWN, handled above; that path is fail-closed for safety-critical
        # commands by the safety gate.)
        return ConditionState.VIOLATED, support
    if value in _NO_LOCK_VALUES:
        return ConditionState.VIOLATED, support
    return ConditionState.SATISFIED, support


def _eval_thermal(ctx: dict[str, Any]) -> tuple[ConditionState, dict[str, Any]]:
    max_temp = get_max_temperature(ctx)
    if max_temp is None:
        return ConditionState.UNKNOWN, {"max_temperature_c": None,
                                        "survival_limit_c": THERMAL_SURVIVAL_LIMIT}
    support = {"max_temperature_c": round(max_temp, 2),
               "survival_limit_c": THERMAL_SURVIVAL_LIMIT}
    if max_temp > THERMAL_SURVIVAL_LIMIT:
        return ConditionState.VIOLATED, support
    return ConditionState.SATISFIED, support


#: Positive predicate → evaluator. Hazard predicates reuse the same evaluator
#: and invert the verdict, so a hazard is SATISFIED exactly when its positive
#: counterpart is VIOLATED. One evaluator per physical quantity.
_EVALUATORS = {
    Condition.BATTERY_ABOVE_FLOOR: _eval_battery,
    Condition.GYRO_DATA_VALID: _eval_gyro,
    Condition.COMMS_LOCK_CONFIRMED: _eval_comms_lock,
    Condition.THERMAL_WITHIN_SURVIVAL: _eval_thermal,
}

_HAZARD_TO_POSITIVE = {
    Condition.BATTERY_BELOW_FLOOR: Condition.BATTERY_ABOVE_FLOOR,
    Condition.GYRO_DATA_INVALID: Condition.GYRO_DATA_VALID,
    Condition.COMMS_LOCK_ABSENT: Condition.COMMS_LOCK_CONFIRMED,
    Condition.THERMAL_ABOVE_SURVIVAL: Condition.THERMAL_WITHIN_SURVIVAL,
}

_INVERT = {
    ConditionState.SATISFIED: ConditionState.VIOLATED,
    ConditionState.VIOLATED: ConditionState.SATISFIED,
    ConditionState.UNKNOWN: ConditionState.UNKNOWN,
}

#: Violation code reported when a predicate blocks a command. These codes are
#: unchanged from the pre-Phase-1 validator so existing consumers keep working.
CONDITION_VIOLATION_CODE: dict[Condition, str] = {
    Condition.BATTERY_ABOVE_FLOOR: "BATTERY_FLOOR",
    Condition.BATTERY_BELOW_FLOOR: "BATTERY_FLOOR",
    Condition.GYRO_DATA_VALID: "GYRO_HEALTH_PREREQUISITE",
    Condition.GYRO_DATA_INVALID: "GYRO_HEALTH_PREREQUISITE",
    Condition.COMMS_LOCK_CONFIRMED: "COMMS_LOCK_REBOOT",
    Condition.COMMS_LOCK_ABSENT: "COMMS_LOCK_REBOOT",
    Condition.THERMAL_WITHIN_SURVIVAL: "THERMAL_SURVIVAL",
    Condition.THERMAL_ABOVE_SURVIVAL: "THERMAL_SURVIVAL",
}

#: Subsystem attributed to each predicate, for operator display.
CONDITION_SUBSYSTEM: dict[Condition, str] = {
    Condition.BATTERY_ABOVE_FLOOR: "EPS",
    Condition.BATTERY_BELOW_FLOOR: "EPS",
    Condition.GYRO_DATA_VALID: "ADCS",
    Condition.GYRO_DATA_INVALID: "ADCS",
    Condition.COMMS_LOCK_CONFIRMED: "COMMS",
    Condition.COMMS_LOCK_ABSENT: "COMMS",
    Condition.THERMAL_WITHIN_SURVIVAL: "TCS",
    Condition.THERMAL_ABOVE_SURVIVAL: "TCS",
}


def evaluate_condition(
    condition: Condition,
    ctx: dict[str, Any],
) -> tuple[ConditionState, dict[str, Any]]:
    """Evaluate one predicate against a crash-dump context.

    Returns ``(state, supporting_context)``. Unknown predicates evaluate to
    UNKNOWN rather than raising, so an unrecognised condition can never cause a
    command to be silently permitted OR silently blocked without the
    consistency checker flagging it first.
    """
    ctx = ctx or {}

    if condition in _EVALUATORS:
        return _EVALUATORS[condition](ctx)

    positive = _HAZARD_TO_POSITIVE.get(condition)
    if positive is not None:
        state, support = _EVALUATORS[positive](ctx)
        return _INVERT[state], support

    return ConditionState.UNKNOWN, {}


def describe_condition(condition: Condition, support: dict[str, Any]) -> str:
    """Build the operator-facing reason text for a blocking predicate."""
    if condition in (Condition.BATTERY_ABOVE_FLOOR, Condition.BATTERY_BELOW_FLOOR):
        soc = support.get("battery_soc_pct")
        return (
            f"Battery SoC is {soc:.1f}% (below the "
            f"{BATTERY_FLOOR_SOC:.0f}% floor)."
            if isinstance(soc, (int, float)) else
            "Battery state of charge is below the safe floor."
        )
    if condition in (Condition.GYRO_DATA_VALID, Condition.GYRO_DATA_INVALID):
        return (
            f"Gyro rate data is invalid (value={support.get('gyro_rate')!r}); "
            f"attitude actuation requires valid rate data."
        )
    if condition in (Condition.COMMS_LOCK_CONFIRMED, Condition.COMMS_LOCK_ABSENT):
        return (
            f"Transponder lock is not confirmed "
            f"(value={support.get('transponder_lock')!r})."
        )
    if condition in (Condition.THERMAL_WITHIN_SURVIVAL,
                     Condition.THERMAL_ABOVE_SURVIVAL):
        temp = support.get("max_temperature_c")
        return (
            f"Component temperature is {temp:.1f}°C (exceeds the "
            f"{THERMAL_SURVIVAL_LIMIT:.0f}°C survival limit)."
            if isinstance(temp, (int, float)) else
            "Component temperature exceeds the survival limit."
        )
    return f"Condition {condition.value} is not met."
