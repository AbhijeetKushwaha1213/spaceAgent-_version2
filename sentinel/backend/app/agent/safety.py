"""
SENTINEL — Deterministic Command Safety Validator (safety.py)

This module is the deterministic backstop for every recovery command the LLM
produces. It enforces:

  1. Registry membership — only commands defined in app/validation/
     command_registry.py may pass, and only while they are enabled
  2. Declared constraint checks — each command's required_preconditions and
     prohibited_conditions, evaluated against the crash dump
  3. Escalation rules — HIGH-risk steps and low-confidence outputs force
     requires_human_review = True

Phase 1 changes
---------------
The command whitelist used to be a literal ``dict[str, set[str]]`` in this file.
It is now DERIVED from the registry, which is the single source of truth shared
with the procedure/RAG layer and the LLM prompt. ``COMMAND_WHITELIST`` remains
available as a derived view so existing callers keep working.

Two behaviours were corrected:

  * Total rejection no longer masquerades as success. When every step is
    blocked, ``apply_validation_to_output`` now returns an EMPTY recovery_plan
    with ``safety_status = BLOCKED``. It used to substitute a fabricated
    ``CMD_HEALTH_CHECK`` step at LOW risk, which rendered as a clean one-step
    recovery.
  * Blocked steps are preserved as structured data on the response
    (``SentinelOutput.blocked_steps``) instead of being flattened into a
    ``[SAFETY: ...]`` suffix on ``reasoning_summary``.

Design rules (unchanged):
  - Pure Python. No AI calls. No LLM dependency. No new packages.
  - Never crashes on missing context — uses safe .get() everywhere.
  - Missing context is permissive; see app/validation/conditions.py for the
    documented tri-state policy and its trade-off.
  - Deterministic, side-effect free.

Public API:
  validate_recovery_plan(sentinel_output, crash_dump_context) -> ValidationResult
  apply_validation_to_output(sentinel_output, validation_result) -> SentinelOutput
  is_command_whitelisted(command, subsystem=None) -> bool
  infer_subsystem(command) -> str | None
  get_whitelist_status() -> dict
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from app.api.models import (
    BlockedCommand,
    BlockSeverity,
    RecoveryStep,
    RiskLevel,
    SafetyStatus,
    SentinelOutput,
)
from app.validation.command_registry import (
    COMMAND_REGISTRY,
    Condition,
    get_command,
    is_enabled,
    is_registered,
    registry_by_subsystem,
    registry_subsystem,
    registry_status,
)
from app.validation.conditions import (
    BATTERY_FLOOR_SOC,
    CONDITION_SUBSYSTEM,
    CONDITION_VIOLATION_CODE,
    THERMAL_SURVIVAL_LIMIT,
    ConditionState,
    describe_condition,
    evaluate_condition,
    get_battery_soc,
    get_gyro_rate,
    get_max_temperature,
    get_transponder_lock,
    is_value_nan_or_missing,
)

logger = logging.getLogger("sentinel.safety")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — RESULT MODELS
# ═══════════════════════════════════════════════════════════════════════════

class ConstraintViolation(BaseModel):
    """A single physical-constraint violation found during validation."""
    code: str = Field(
        ...,
        description="Machine-readable violation code, e.g. BATTERY_FLOOR",
    )
    reason: str = Field(
        ...,
        min_length=5,
        description="Human-readable explanation of why the command was blocked",
    )
    subsystem: str | None = Field(
        default=None,
        description="Subsystem related to the violation (if applicable)",
    )
    condition: str | None = Field(
        default=None,
        description=(
            "The registry Condition that produced this violation, when the "
            "violation came from a declared precondition/prohibition"
        ),
    )
    supporting_context: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Observed values the verdict was based on, e.g. "
            "{'battery_soc_pct': 12.0, 'floor_pct': 15.0}"
        ),
    )


class BlockedStep(BaseModel):
    """A recovery step that was blocked by the safety validator.

    Internal record. ``to_api()`` converts it to the stable API shape
    (``app.api.models.BlockedCommand``) that is returned to the operator.
    """
    original_step: RecoveryStep
    reason: str = Field(
        ...,
        min_length=5,
        description="Why this step was blocked",
    )
    violation_code: str = Field(
        ...,
        description="Machine-readable violation code",
    )
    subsystem: str | None = Field(
        default=None,
        description="Subsystem the blocked command belongs to",
    )
    severity: BlockSeverity = Field(
        default=BlockSeverity.HIGH,
        description="Consequence of executing this command anyway",
    )
    supporting_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Observed values the block decision was based on",
    )

    def to_api(self) -> BlockedCommand:
        """Convert to the API-facing structured form."""
        return BlockedCommand(
            step=self.original_step.step,
            command=self.original_step.command,
            reason=self.reason,
            violated_constraint=self.violation_code,
            severity=self.severity,
            subsystem=self.subsystem,
            supporting_context=dict(self.supporting_context),
        )


class ValidationResult(BaseModel):
    """Output of validate_recovery_plan()."""
    is_safe: bool = Field(
        ...,
        description="True if all steps passed all checks",
    )
    validated_steps: list[RecoveryStep] = Field(
        default_factory=list,
        description="Steps that passed all checks (safe to execute)",
    )
    blocked_steps: list[BlockedStep] = Field(
        default_factory=list,
        description="Steps that were blocked by whitelist or constraint checks",
    )
    requires_human_review: bool = Field(
        ...,
        description=(
            "True if any HIGH-risk step, confidence < 0.70, or blocked step"
        ),
    )
    safety_summary: str = Field(
        ...,
        min_length=1,
        description="Human-readable summary of the validation outcome",
    )
    safety_status: SafetyStatus = Field(
        default=SafetyStatus.NOT_VALIDATED,
        description=(
            "Truthful status derived from the validation outcome. Replaces the "
            "unconditional 'Safety validation passed.' message."
        ),
    )

    @property
    def all_blocked(self) -> bool:
        """True when at least one step was proposed and none survived."""
        return bool(self.blocked_steps) and not self.validated_steps

    def blocked_for_api(self) -> list[BlockedCommand]:
        """The blocked steps in their stable API form."""
        return [b.to_api() for b in self.blocked_steps]


def derive_safety_status(
    validated_count: int,
    blocked_count: int,
    requires_human_review: bool,
) -> SafetyStatus:
    """Map a validation outcome onto a SafetyStatus.

    Precedence (most severe first):
        BLOCKED > PARTIALLY_BLOCKED > REQUIRES_HUMAN_REVIEW > VALIDATED

    NOT_VALIDATED is never returned here — it is the status of a plan that was
    never given to the validator, which only the caller can know.
    """
    if blocked_count and validated_count == 0:
        return SafetyStatus.BLOCKED
    if blocked_count:
        return SafetyStatus.PARTIALLY_BLOCKED
    if requires_human_review:
        return SafetyStatus.REQUIRES_HUMAN_REVIEW
    return SafetyStatus.VALIDATED


# Severity attached to each violation code, i.e. what would happen if the
# command were executed anyway.
_SEVERITY_BY_CODE: dict[str, BlockSeverity] = {
    # An unrecognised or malformed command has no defined behaviour on the bus.
    "INVALID_FORMAT": BlockSeverity.CRITICAL,
    "NOT_IN_REGISTRY": BlockSeverity.CRITICAL,
    "NOT_WHITELISTED": BlockSeverity.CRITICAL,  # legacy alias, kept for callers
    "COMMAND_DISABLED": BlockSeverity.HIGH,
    # Attitude actuation on bad rate data can tumble the vehicle; rebooting the
    # OBC without a confirmed uplink can lose contact permanently.
    "GYRO_HEALTH_PREREQUISITE": BlockSeverity.CRITICAL,
    "COMMS_LOCK_REBOOT": BlockSeverity.CRITICAL,
    # These deepen an existing fault rather than causing immediate loss.
    "BATTERY_FLOOR": BlockSeverity.HIGH,
    "THERMAL_SURVIVAL": BlockSeverity.HIGH,
    # Phase 1 fail-closed: a safety-critical command whose required precondition
    # telemetry is ABSENT (UNKNOWN) cannot be confirmed. The only required
    # preconditions in the registry today are gyro-health and comms-lock, both
    # CRITICAL, so a missing one is reported CRITICAL. If a lower-severity
    # required precondition is ever added, _missing_precondition_violation will
    # not fire for it (see _FAIL_CLOSED_SEVERITIES), so this stays accurate.
    "MISSING_PRECONDITION": BlockSeverity.CRITICAL,
}


def _severity_for(code: str) -> BlockSeverity:
    return _SEVERITY_BY_CODE.get(code, BlockSeverity.HIGH)


# ── Phase 1 fail-closed reason-code vocabulary ────────────────────────────────
# Machine-readable reason codes the deterministic safety gate can attach to a
# blocked step. Two groups:
#
#   EMITTED   — produced by this module today.
#   RESERVED  — name failure modes that are NOT yet detected at the
#               command-authorization boundary. They are declared so the
#               vocabulary is stable for downstream consumers, but nothing emits
#               them. Do NOT wire a detector that always no-ops just to "use" a
#               reserved code — that would be demo-only behaviour. Adding real
#               staleness / contradiction / toxic-pair / physics-refutation
#               detection is out of scope for Phase 1 (see the Phase 1 report).
#
# EMITTED:
REASON_CODE_MISSING_PRECONDITION = "MISSING_PRECONDITION"  # required-precondition telemetry absent (UNKNOWN)
REASON_CODE_UNKNOWN_TELEMETRY = "UNKNOWN_TELEMETRY"        # category tag carried in supporting_context for the above
REASON_CODE_INVALID_TELEMETRY = "INVALID_TELEMETRY"        # present-but-malformed telemetry (reported today via the condition's own code, e.g. GYRO_HEALTH_PREREQUISITE)
REASON_CODE_COMMAND_NOT_WHITELISTED = "NOT_IN_REGISTRY"    # existing behaviour; COMMAND_NOT_WHITELISTED is the spec alias for this code
# RESERVED (not emitted by any current code path):
REASON_CODE_STALE_TELEMETRY = "STALE_TELEMETRY"
REASON_CODE_CONTRADICTORY_TELEMETRY = "CONTRADICTORY_TELEMETRY"
REASON_CODE_TOXIC_COMMAND_CONFLICT = "TOXIC_COMMAND_CONFLICT"
REASON_CODE_PHYSICS_REFUTED = "PHYSICS_REFUTED"

# Severities at which a MISSING (UNKNOWN) required precondition blocks the
# command fail-closed. CRITICAL/HIGH block; anything lower stays permissive on
# absence, matching the approved Phase 1 policy (required-precondition only,
# risk-aware).
_FAIL_CLOSED_SEVERITIES: frozenset[BlockSeverity] = frozenset(
    {BlockSeverity.CRITICAL, BlockSeverity.HIGH}
)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — COMMAND WHITELIST BY SUBSYSTEM
# ═══════════════════════════════════════════════════════════════════════════

# DERIVED, NOT DECLARED.
#
# This used to be a hand-maintained literal. It is now built from
# app/validation/command_registry.py, which is the single source of truth shared
# with the procedure/RAG layer and the LLM prompt. Editing this file can no
# longer make the whitelist disagree with the procedures — add the command to
# the registry instead, and app/validation/conflicts.py will confirm every
# consumer agrees.
#
# Only ENABLED commands appear. A registered-but-disabled command is rejected
# with COMMAND_DISABLED rather than NOT_IN_REGISTRY, so the operator can tell
# "we withdrew this" apart from "we never had this".
COMMAND_WHITELIST: dict[str, set[str]] = registry_by_subsystem(enabled_only=True)

# Flat set for fast O(1) lookup
_ALL_WHITELISTED: set[str] = set()
for _cmds in COMMAND_WHITELIST.values():
    _ALL_WHITELISTED.update(_cmds)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — SUBSYSTEM INFERENCE
# ═══════════════════════════════════════════════════════════════════════════

# Prefix → subsystem mapping (checked in order, first match wins)
_PREFIX_MAP: list[tuple[str, str]] = [
    # ADCS
    ("CMD_GYRO_", "ADCS"),
    ("CMD_ATTITUDE_", "ADCS"),
    ("CMD_REACTION_WHEEL_", "ADCS"),
    ("CMD_SUN_", "ADCS"),
    ("CMD_SEU_", "ADCS"),
    # EPS
    ("CMD_SOLAR_", "EPS"),
    ("CMD_BATTERY_", "EPS"),
    ("CMD_BUS_", "EPS"),
    ("CMD_POWER_", "EPS"),
    # OBC
    ("CMD_OBC_", "OBC"),
    ("CMD_WATCHDOG_", "OBC"),
    ("CMD_CPU_", "OBC"),
    ("CMD_MEMORY_", "OBC"),
    ("CMD_SAFE_MODE_", "OBC"),
    # TCS
    ("CMD_HEATER_", "TCS"),
    ("CMD_THERMAL_", "TCS"),
    # COMMS
    ("CMD_TRANSPONDER_", "COMMS"),
    ("CMD_COMMS_", "COMMS"),
    ("CMD_ANTENNA_", "COMMS"),
    ("CMD_LOW_GAIN_", "COMMS"),
    # SYSTEM (last — catches remaining CMD_VERIFY_*, CMD_HEALTH_*, etc.)
    ("CMD_HEALTH_", "SYSTEM"),
    ("CMD_TELEMETRY_", "SYSTEM"),
    ("CMD_VERIFY_", "SYSTEM"),
]


def infer_subsystem(command: str) -> str | None:
    """Resolve the subsystem a command belongs to.

    Resolution order:
      1. The registry's DECLARED subsystem (authoritative)
      2. The prefix heuristic below, for commands the registry doesn't know

    The heuristic is retained only so that unregistered commands — which are
    blocked anyway — can still be attributed to a subsystem in the operator's
    blocked-step list. It is not a source of truth. Before Phase 1 the heuristic
    was the only mechanism, so registry commands that don't follow a known
    prefix (CMD_DISABLE_HEATER_ZONE, CMD_SWITCH_BACKUP_TRANSPONDER,
    CMD_CONFIRM_COMMS_LOCK, ...) resolved to None.

    Args:
        command: Command string, e.g. "CMD_GYRO_RESET".

    Returns:
        Subsystem string or None.
    """
    if not command or not command.startswith("CMD_"):
        return None

    declared = registry_subsystem(command)
    if declared is not None:
        return declared

    for prefix, subsystem in _PREFIX_MAP:
        if command.startswith(prefix):
            return subsystem

    return None


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — WHITELIST HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def is_command_whitelisted(
    command: str,
    subsystem: str | None = None,
) -> bool:
    """Check whether a command is in the safety whitelist.

    Args:
        command: Command string to check.
        subsystem: If provided, check only that subsystem's whitelist.
            If None, check all subsystems.

    Returns:
        True if the command is whitelisted.
    """
    if not command or not command.startswith("CMD_"):
        return False

    if subsystem:
        sub_cmds = COMMAND_WHITELIST.get(subsystem, set())
        return command in sub_cmds

    return command in _ALL_WHITELISTED


def get_whitelist_status() -> dict:
    """Return diagnostic information about the derived whitelist.

    Useful for tests, debugging, and status panels. ``duplicates`` is now always
    empty by construction: the registry is keyed by command_id, so one command
    cannot be filed under two subsystems. It is kept in the payload for
    backward compatibility with existing callers.
    """
    counts = {sub: len(cmds) for sub, cmds in COMMAND_WHITELIST.items()}
    total = sum(counts.values())

    all_cmds: list[str] = []
    for cmds in COMMAND_WHITELIST.values():
        all_cmds.extend(cmds)
    duplicates = [c for c in set(all_cmds) if all_cmds.count(c) > 1]

    return {
        "subsystems": list(COMMAND_WHITELIST.keys()),
        "counts_per_subsystem": counts,
        "total_commands": total,
        "unique_commands": len(_ALL_WHITELISTED),
        "duplicates": duplicates,
        # Phase 1 additions — the registry is the source of truth.
        "source": "app.validation.command_registry",
        "registry": registry_status(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — CONTEXT HELPERS (safe .get() everywhere)
# ═══════════════════════════════════════════════════════════════════════════

def _is_verify_command(command: str) -> bool:
    """True when a command has no state prerequisites at all.

    Phase 1: this now reads the registry's declared metadata
    (``CommandSpec.is_observation_only``) instead of pattern-matching the name.
    The old implementation was ``command.startswith("CMD_VERIFY_")``, which
    meant a command was treated as safe because of how it was spelled. The name
    check is kept as a fallback for unregistered commands so the function never
    changes answer for a caller that passes something the registry lacks.
    """
    spec = get_command(command)
    if spec is not None:
        return spec.is_observation_only
    return command.startswith("CMD_VERIFY_")


# The extraction helpers moved to app/validation/conditions.py in Phase 1 so
# that the registry's conditions and this validator cannot disagree about how a
# value is read. Private aliases are kept for backward compatibility.
_is_value_nan_or_missing = is_value_nan_or_missing
_get_battery_soc = get_battery_soc
_get_gyro_rate = get_gyro_rate
_get_transponder_lock = get_transponder_lock
_get_max_temperature = get_max_temperature


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — CONSTRAINT CHECKS
# ═══════════════════════════════════════════════════════════════════════════
#
# Each check: (step, crash_dump_context) -> ConstraintViolation | None
# Returns None if the step passes the check.
# ═══════════════════════════════════════════════════════════════════════════

# DERIVED, NOT DECLARED.
#
# These sets used to be hand-maintained here, separately from the whitelist.
# They are now computed from each command's declared conditions in the registry,
# so a command's constraints travel with its definition.
_POWER_REQUIRING_COMMANDS: set[str] = {
    cid for cid, spec in COMMAND_REGISTRY.items()
    if Condition.BATTERY_BELOW_FLOOR in spec.prohibited_conditions
    or Condition.BATTERY_ABOVE_FLOOR in spec.required_preconditions
}

_GYRO_DEPENDENT_COMMANDS: set[str] = {
    cid for cid, spec in COMMAND_REGISTRY.items()
    if Condition.GYRO_DATA_VALID in spec.required_preconditions
    or Condition.GYRO_DATA_INVALID in spec.prohibited_conditions
}

_COMMS_LOCK_COMMANDS: set[str] = {
    cid for cid, spec in COMMAND_REGISTRY.items()
    if Condition.COMMS_LOCK_CONFIRMED in spec.required_preconditions
    or Condition.COMMS_LOCK_ABSENT in spec.prohibited_conditions
}

_THERMAL_CONSTRAINED_COMMANDS: set[str] = {
    cid for cid, spec in COMMAND_REGISTRY.items()
    if Condition.THERMAL_ABOVE_SURVIVAL in spec.prohibited_conditions
    or Condition.THERMAL_WITHIN_SURVIVAL in spec.required_preconditions
}

# BATTERY_FLOOR_SOC and THERMAL_SURVIVAL_LIMIT now live with the predicates that
# use them, in app/validation/conditions.py. They are imported at the top of this
# module, so existing callers doing `from app.agent.safety import
# BATTERY_FLOOR_SOC` keep working unchanged.

# Confidence threshold for human review escalation
CONFIDENCE_REVIEW_THRESHOLD: float = 0.70


def _violation_from_condition(
    command: str,
    condition: Condition,
    support: dict[str, Any],
) -> ConstraintViolation:
    """Build a ConstraintViolation for a predicate that blocked a command."""
    return ConstraintViolation(
        code=CONDITION_VIOLATION_CODE.get(condition, condition.value),
        reason=(
            f"{describe_condition(condition, support)} "
            f"Command '{command}' declares this constraint and is therefore "
            f"blocked."
        ),
        subsystem=CONDITION_SUBSYSTEM.get(condition),
        condition=condition.value,
        supporting_context=support,
    )


def _missing_precondition_violation(
    command: str,
    condition: Condition,
    support: dict[str, Any],
) -> ConstraintViolation | None:
    """Phase 1 fail-closed (RED-01 fix).

    A REQUIRED precondition evaluated to UNKNOWN — the telemetry it depends on is
    absent from the crash dump, so the precondition cannot be confirmed. A
    safety-critical command that declares it must NOT be authorized on absent
    evidence. Returns a blocking ConstraintViolation, or None when the condition
    is not safety-critical (CRITICAL/HIGH), in which case absence stays permissive
    per the approved policy.

    This is deliberately asymmetric: it fires only for REQUIRED preconditions
    (affirmative evidence a command needs to be safe). Prohibited hazards that are
    UNKNOWN remain non-blocking — a present hazard still blocks via
    ``_violation_from_condition``, unchanged.
    """
    underlying = CONDITION_VIOLATION_CODE.get(condition, condition.value)
    if _severity_for(underlying) not in _FAIL_CLOSED_SEVERITIES:
        return None
    return ConstraintViolation(
        code=REASON_CODE_MISSING_PRECONDITION,
        reason=(
            f"Required precondition '{condition.value}' is UNKNOWN: the telemetry "
            f"it depends on is absent from the crash dump, so it cannot be "
            f"confirmed. Command '{command}' is safety-critical, so it is blocked "
            f"fail-closed — absent evidence is not treated as a satisfied "
            f"precondition."
        ),
        subsystem=CONDITION_SUBSYSTEM.get(condition),
        condition=condition.value,
        supporting_context={
            **support,
            "telemetry_state": ConditionState.UNKNOWN.value,
            "reason_category": REASON_CODE_UNKNOWN_TELEMETRY,
            "underlying_condition_code": underlying,
        },
    )


# Fixed evaluation order for the physical quantities, as (positive, hazard).
#
# Deliberately NOT the registry declaration order. When more than one constraint
# is violated at once, the reported violation code must be stable, and it must be
# the SAME code the pre-Phase-1 validator reported — its check order was
# battery, gyro, comms, thermal. Changing which of several simultaneous
# violations gets reported would silently alter the operator-facing reason for
# every multi-fault dump.
_CONDITION_EVALUATION_ORDER: tuple[tuple[Condition, Condition], ...] = (
    (Condition.BATTERY_ABOVE_FLOOR, Condition.BATTERY_BELOW_FLOOR),
    (Condition.GYRO_DATA_VALID, Condition.GYRO_DATA_INVALID),
    (Condition.COMMS_LOCK_CONFIRMED, Condition.COMMS_LOCK_ABSENT),
    (Condition.THERMAL_WITHIN_SURVIVAL, Condition.THERMAL_ABOVE_SURVIVAL),
)


def evaluate_declared_conditions(
    step: RecoveryStep,
    ctx: dict[str, Any],
) -> ConstraintViolation | None:
    """Check a step against the conditions its registry entry declares.

    This is the single constraint gate. Returns the first blocking violation, or
    None if the command may proceed. Evaluation follows
    ``_CONDITION_EVALUATION_ORDER`` so the reported code is stable when several
    constraints are violated simultaneously.
    """
    spec = get_command(step.command)
    if spec is None:
        # Unregistered commands never reach here — validate_recovery_plan blocks
        # them earlier. Returning None keeps this function total.
        return None

    required = set(spec.required_preconditions)
    prohibited = set(spec.prohibited_conditions)

    for positive, hazard in _CONDITION_EVALUATION_ORDER:
        if positive in required:
            state, support = evaluate_condition(positive, ctx)
            if state is ConditionState.VIOLATED:
                return _violation_from_condition(step.command, positive, support)
            if state is ConditionState.UNKNOWN:
                # Phase 1 fail-closed: a required precondition whose telemetry is
                # absent cannot be confirmed. Blocks for safety-critical
                # conditions; returns None (stays permissive) otherwise.
                missing = _missing_precondition_violation(
                    step.command, positive, support
                )
                if missing is not None:
                    return missing
        elif hazard in prohibited:
            state, support = evaluate_condition(hazard, ctx)
            if state is ConditionState.SATISFIED:  # hazard is present
                return _violation_from_condition(step.command, hazard, support)

    return None


def _check_single_condition(
    step: RecoveryStep,
    ctx: dict[str, Any],
    positive: Condition,
    hazard: Condition,
) -> ConstraintViolation | None:
    """Shared body for the four named checks below.

    Only fires when the command's registry entry actually declares the
    condition, so each named check keeps reporting only its own violation code.
    """
    spec = get_command(step.command)
    if spec is None:
        return None

    declares_required = positive in spec.required_preconditions
    declares_prohibited = hazard in spec.prohibited_conditions
    if not (declares_required or declares_prohibited):
        return None

    condition = positive if declares_required else hazard
    state, support = evaluate_condition(condition, ctx)

    if declares_required:
        if state is ConditionState.VIOLATED:
            return _violation_from_condition(step.command, condition, support)
        if state is ConditionState.UNKNOWN:
            # Phase 1 fail-closed for a required precondition whose telemetry is
            # absent. Returns None (permissive) for non-safety-critical conditions.
            return _missing_precondition_violation(step.command, condition, support)
        return None

    # Prohibited hazard: a present hazard blocks; absence stays permissive.
    if state is ConditionState.SATISFIED:
        return _violation_from_condition(step.command, condition, support)
    return None


def check_battery_floor(
    step: RecoveryStep,
    ctx: dict[str, Any],
) -> ConstraintViolation | None:
    """Block commands declaring a battery-floor constraint when SoC is below it.

    Which commands those are is now declared in the registry
    (``prohibited_conditions: BATTERY_BELOW_FLOOR``) rather than listed here.
    Observation-only commands declare no conditions and so are unaffected.
    Missing SoC data is permissive.
    """
    return _check_single_condition(
        step, ctx,
        Condition.BATTERY_ABOVE_FLOOR,
        Condition.BATTERY_BELOW_FLOOR,
    )


def check_gyro_health_prerequisite(
    step: RecoveryStep,
    ctx: dict[str, Any],
) -> ConstraintViolation | None:
    """Block attitude actuation when gyro rate data is invalid.

    Gyro data is invalid when it is present but None, NaN, or non-numeric.
    Phase 1 fail-closed: gyro rate is a REQUIRED precondition (CRITICAL), so
    absent gyro data now blocks with MISSING_PRECONDITION rather than being
    treated as permissive — attitude actuation must not be authorized when the
    sensor's health cannot be confirmed.
    """
    return _check_single_condition(
        step, ctx,
        Condition.GYRO_DATA_VALID,
        Condition.GYRO_DATA_INVALID,
    )


def check_comms_lock_for_reboot(
    step: RecoveryStep,
    ctx: dict[str, Any],
) -> ConstraintViolation | None:
    """Block an OBC reboot when transponder lock is not confirmed.

    Rebooting without a comms lock risks losing the uplink during the reboot
    window, which could make the spacecraft unrecoverable. An explicitly absent
    lock blocks. Phase 1 fail-closed: comms-lock is a REQUIRED precondition
    (CRITICAL), so a MISSING lock reading now also blocks with
    MISSING_PRECONDITION — the lock must be positively confirmed, not assumed
    from silence.
    """
    return _check_single_condition(
        step, ctx,
        Condition.COMMS_LOCK_CONFIRMED,
        Condition.COMMS_LOCK_ABSENT,
    )


def check_thermal_survival(
    step: RecoveryStep,
    ctx: dict[str, Any],
) -> ConstraintViolation | None:
    """Block commands declaring a thermal constraint when over the survival limit.

    Thermal remedies (CMD_DISABLE_HEATER_ZONE, CMD_HEATER_DISABLE,
    CMD_HEATER_OFF, CMD_THERMAL_OVERRIDE_OFF, ...) and observation-only commands
    declare no thermal prohibition, so they are never blocked during an
    over-temperature event — blocking the remedy for the fault being remediated
    was one of the conflicts Phase 1 fixed.
    Missing temperature data is permissive.
    """
    return _check_single_condition(
        step, ctx,
        Condition.THERMAL_WITHIN_SURVIVAL,
        Condition.THERMAL_ABOVE_SURVIVAL,
    )


def check_high_risk_escalation(
    step: RecoveryStep,
    ctx: dict[str, Any],
) -> ConstraintViolation | None:
    """Flag HIGH-risk steps for human review escalation.

    This check does NOT block the step — it only signals that
    requires_human_review should be True. Returns a violation with
    code "HIGH_RISK_ESCALATION" so the caller can set the flag.
    """
    if step.risk in (RiskLevel.HIGH, RiskLevel.BLOCKED):
        return ConstraintViolation(
            code="HIGH_RISK_ESCALATION",
            reason=(
                f"Step {step.step} ('{step.command}') has risk level "
                f"'{step.risk.value}'. Human review required before execution."
            ),
            subsystem=infer_subsystem(step.command),
        )

    return None


# Registry of all constraint checks
_BLOCKING_CHECKS = [
    check_battery_floor,
    check_gyro_health_prerequisite,
    check_comms_lock_for_reboot,
    check_thermal_survival,
]

_ESCALATION_CHECKS = [
    check_high_risk_escalation,
]


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def _blocked(
    step: RecoveryStep,
    reason: str,
    code: str,
    subsystem: str | None,
    support: dict[str, Any] | None = None,
) -> BlockedStep:
    """Build a BlockedStep with its severity resolved from the violation code."""
    return BlockedStep(
        original_step=step,
        reason=reason,
        violation_code=code,
        subsystem=subsystem,
        severity=_severity_for(code),
        supporting_context=support or {},
    )


def validate_recovery_plan(
    sentinel_output: SentinelOutput,
    crash_dump_context: dict[str, Any],
) -> ValidationResult:
    """Validate every recovery step in a SentinelOutput.

    Runs in order:
      1. Whitelist check — is the command CMD_-prefixed and in COMMAND_WHITELIST?
      2. Blocking constraint checks — battery, gyro, comms, thermal
      3. Escalation checks — HIGH-risk step flagging

    Steps that fail any blocking check are removed from validated_steps
    and added to blocked_steps with a reason.

    Args:
        sentinel_output: The LLM's structured output after schema validation.
        crash_dump_context: The crash dump dict (for physical constraint checks).

    Returns:
        ValidationResult with safe/blocked step lists and human review flag.
    """
    ctx = crash_dump_context or {}
    validated: list[RecoveryStep] = []
    blocked: list[BlockedStep] = []
    force_human_review = False

    for step in sentinel_output.recovery_plan:
        step_blocked = False

        # --- Check 1: Command must be CMD_-prefixed ---
        if not step.command.startswith("CMD_"):
            blocked.append(_blocked(
                step,
                reason=(
                    f"Command '{step.command}' does not follow the "
                    f"CMD_UPPER_SNAKE_CASE naming convention."
                ),
                code="INVALID_FORMAT",
                subsystem=None,
                support={"command": step.command},
            ))
            step_blocked = True

        # --- Check 2: Registry membership ---
        elif not is_registered(step.command):
            blocked.append(_blocked(
                step,
                reason=(
                    f"Command '{step.command}' is not defined in the SENTINEL "
                    f"command registry, so its subsystem, risk and "
                    f"preconditions are unknown. It cannot be authorised."
                ),
                code="NOT_IN_REGISTRY",
                subsystem=infer_subsystem(step.command),
                support={"command": step.command,
                         "registry_size": len(COMMAND_REGISTRY)},
            ))
            step_blocked = True

        # --- Check 3: Command is registered but withdrawn ---
        elif not is_enabled(step.command):
            spec = get_command(step.command)
            blocked.append(_blocked(
                step,
                reason=(
                    f"Command '{step.command}' exists in the registry but is "
                    f"disabled. "
                    + (spec.disabled_reason or "No reason recorded.")
                ),
                code="COMMAND_DISABLED",
                subsystem=infer_subsystem(step.command),
                support={"command": step.command,
                         "disabled_reason": spec.disabled_reason},
            ))
            step_blocked = True

        # --- Check 4: Declared preconditions / prohibited conditions ---
        if not step_blocked:
            violation = evaluate_declared_conditions(step, ctx)
            if violation is not None:
                blocked.append(_blocked(
                    step,
                    reason=violation.reason,
                    code=violation.code,
                    subsystem=violation.subsystem,
                    support=violation.supporting_context,
                ))
                step_blocked = True

        # --- Check 5: Escalation checks (non-blocking) ---
        if not step_blocked:
            for check_fn in _ESCALATION_CHECKS:
                violation = check_fn(step, ctx)
                if violation is not None:
                    force_human_review = True
            validated.append(step)

    # --- Confidence escalation ---
    if sentinel_output.confidence < CONFIDENCE_REVIEW_THRESHOLD:
        force_human_review = True

    # --- Any blocked unsafe step → human review ---
    if blocked:
        force_human_review = True

    status = derive_safety_status(
        validated_count=len(validated),
        blocked_count=len(blocked),
        requires_human_review=force_human_review,
    )

    # Build summary. This narrates the outcome; it never asserts success when
    # steps were blocked, and it is NOT the mechanism by which blocked steps
    # reach the operator — that is SentinelOutput.blocked_steps.
    summary_parts: list[str] = []
    if status is SafetyStatus.BLOCKED:
        summary_parts.append(
            f"BLOCKED: all {len(blocked)} proposed recovery step(s) were "
            f"rejected by safety validation. No safe action is available."
        )
    elif not blocked:
        summary_parts.append(
            f"All {len(validated)} recovery step(s) passed safety validation."
        )
    else:
        summary_parts.append(
            f"{len(blocked)} step(s) blocked, "
            f"{len(validated)} step(s) approved."
        )

    if blocked:
        codes = sorted(set(b.violation_code for b in blocked))
        summary_parts.append(f"Violations: {', '.join(codes)}.")

    if force_human_review:
        summary_parts.append("Human review required.")

    return ValidationResult(
        is_safe=len(blocked) == 0,
        validated_steps=validated,
        blocked_steps=blocked,
        requires_human_review=force_human_review,
        safety_summary=" ".join(summary_parts),
        safety_status=status,
    )


def apply_validation_to_output(
    sentinel_output: SentinelOutput,
    validation_result: ValidationResult,
) -> SentinelOutput:
    """Apply safety validation to a SentinelOutput.

    Creates a new SentinelOutput with:
      - recovery_plan replaced by validated_steps (blocked steps removed)
      - requires_human_review set if any escalation triggered
      - reasoning_summary appended with safety info if steps were blocked

    Does not mutate the input objects.

    Args:
        sentinel_output: Original LLM output.
        validation_result: Result from validate_recovery_plan().

    Returns:
        New SentinelOutput with safety-validated recovery plan.
    """
    # Re-number surviving steps sequentially (1, 2, 3, ...)
    renumbered_steps: list[RecoveryStep] = []
    for i, step in enumerate(validation_result.validated_steps, start=1):
        renumbered_steps.append(step.model_copy(update={"step": i}))

    # NOTE ON TOTAL REJECTION
    # -----------------------
    # When every step is blocked the plan stays EMPTY and safety_status is
    # BLOCKED. This function used to substitute a fabricated
    #     RecoveryStep(command="CMD_HEALTH_CHECK", risk=LOW, ...)
    # so that the old min_length=1 constraint on recovery_plan was satisfied.
    # The effect was that a completely rejected plan rendered as a clean
    # one-step recovery at LOW risk. SentinelOutput invariant 6 now enforces
    # the opposite: an empty plan is legal ONLY when safety_status is BLOCKED,
    # and a BLOCKED plan must carry blocked_steps explaining what was refused.

    # Reasoning summary is left ALONE. Blocked steps used to be flattened into
    # it as a "[SAFETY: ...]" suffix; they are now returned as structured data
    # in SentinelOutput.blocked_steps so the frontend can render them properly.
    reasoning = sentinel_output.reasoning_summary

    requires_review = (
        sentinel_output.requires_human_review
        or validation_result.requires_human_review
    )

    # Build new output (do NOT mutate the original)
    new_data = sentinel_output.model_dump()
    new_data["recovery_plan"] = [s.model_dump() for s in renumbered_steps]
    new_data["requires_human_review"] = requires_review
    new_data["reasoning_summary"] = reasoning
    new_data["safety_status"] = validation_result.safety_status.value
    new_data["blocked_steps"] = [
        b.model_dump() for b in validation_result.blocked_for_api()
    ]

    return SentinelOutput(**new_data)
