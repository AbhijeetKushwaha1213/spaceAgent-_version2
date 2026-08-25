"""
SENTINEL — Spacecraft Command Registry (command_registry.py)

Phase 1. THIS FILE IS THE SINGLE SOURCE OF TRUTH for every command SENTINEL is
permitted to propose. Nothing else in the system may define a command.

Consumers:
  app/agent/safety.py     — derives its whitelist and constraint checks here
  app/agent/rag.py        — procedure text may only cite command_ids from here
  app/agent/prompts.py    — the LLM is shown the enabled command_ids from here
  app/validation/conflicts.py — proves every consumer agrees with this file

Why a registry instead of a set of strings
------------------------------------------
Before Phase 1 the procedure knowledge base recommended 12 commands that the
safety whitelist then blocked. A well-behaved model that followed the retrieved
procedure exactly had its whole plan rejected — including
``CMD_DISABLE_HEATER_ZONE``, the command the thermal-runaway procedure says to
issue immediately. Two independent lists of command names cannot be kept in
agreement by hand, so there is now one list, and a checker that fails if any
consumer drifts from it.

Condition semantics (tri-state, risk-aware fail-closed)
-------------------------------------------------------
Each ``Condition`` is a predicate about spacecraft state that evaluates to
SATISFIED, VIOLATED, or UNKNOWN against the crash-dump context.

  required_preconditions   Command is BLOCKED if any listed predicate is
                           VIOLATED. As of Phase 1 fail-closed hardening, a
                           required precondition that is UNKNOWN also BLOCKS when
                           the command is safety-critical (CRITICAL/HIGH), with
                           reason code MISSING_PRECONDITION — a safety-critical
                           action is not authorized on the absence of the
                           affirmative evidence it needs.
  prohibited_conditions    Command is BLOCKED if any listed hazard predicate
                           is SATISFIED (i.e. the hazard is present).
                           UNKNOWN does NOT block — absence of a hazard reading
                           stays permissive.

The trade-off is now asymmetric and deliberate. Missing HAZARD data is
permissive (a ground operator may have confirmed the state out of band, and
refusing to act on absent hazard data would make the tool useless on partial
dumps). Missing REQUIRED-PRECONDITION data for a safety-critical command is
fail-closed (absence of evidence is NOT treated as a satisfied precondition).
A command with BOTH lists empty is unconditionally executable
(observation-only) and is never affected by either rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from app.api.models import RiskLevel, SubsystemID


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONDITION VOCABULARY
# ═══════════════════════════════════════════════════════════════════════════

class Condition(str, Enum):
    """Predicates about spacecraft state, evaluated against a crash dump.

    Positive predicates (good states) are used in ``required_preconditions``.
    Hazard predicates (bad states) are used in ``prohibited_conditions``.
    Each positive predicate has exactly one hazard counterpart, which lets the
    consistency checker detect a command that can never run.
    """

    # --- Positive predicates: required_preconditions ---
    BATTERY_ABOVE_FLOOR = "BATTERY_ABOVE_FLOOR"
    GYRO_DATA_VALID = "GYRO_DATA_VALID"
    COMMS_LOCK_CONFIRMED = "COMMS_LOCK_CONFIRMED"
    THERMAL_WITHIN_SURVIVAL = "THERMAL_WITHIN_SURVIVAL"

    # --- Hazard predicates: prohibited_conditions ---
    BATTERY_BELOW_FLOOR = "BATTERY_BELOW_FLOOR"
    GYRO_DATA_INVALID = "GYRO_DATA_INVALID"
    COMMS_LOCK_ABSENT = "COMMS_LOCK_ABSENT"
    THERMAL_ABOVE_SURVIVAL = "THERMAL_ABOVE_SURVIVAL"


#: Positive predicate → its hazard counterpart. Used by conflicts.py to detect
#: an unsatisfiable command (requires X while prohibiting NOT-X).
CONDITION_NEGATION: dict[Condition, Condition] = {
    Condition.BATTERY_ABOVE_FLOOR: Condition.BATTERY_BELOW_FLOOR,
    Condition.GYRO_DATA_VALID: Condition.GYRO_DATA_INVALID,
    Condition.COMMS_LOCK_CONFIRMED: Condition.COMMS_LOCK_ABSENT,
    Condition.THERMAL_WITHIN_SURVIVAL: Condition.THERMAL_ABOVE_SURVIVAL,
}

#: Predicates valid in ``required_preconditions``.
POSITIVE_CONDITIONS: frozenset[Condition] = frozenset(CONDITION_NEGATION.keys())

#: Predicates valid in ``prohibited_conditions``.
HAZARD_CONDITIONS: frozenset[Condition] = frozenset(CONDITION_NEGATION.values())


class CommandSource(str, Enum):
    """Where a command's definition came from.

    Deliberately coarse. SENTINEL does not have clause-level ECSS citations for
    its command set, so it does not claim any. Do not add a source value that
    asserts a citation the repository cannot produce.
    """

    BASELINE_WHITELIST = "BASELINE_WHITELIST"
    """Inherited from the original safety.py whitelist. No external citation."""

    PROCEDURE_KB = "PROCEDURE_KB"
    """Introduced by SENTINEL's procedure knowledge base (app/agent/rag.py),
    which is written from ECSS-E-ST-70-11C / ECSS-Q-ST-30-02C *principles*
    without clause-level attribution."""


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — COMMAND SPECIFICATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CommandSpec:
    """The authoritative definition of one spacecraft command."""

    command_id: str
    subsystem: SubsystemID
    description: str
    risk_level: RiskLevel
    expected_effect: str
    source: CommandSource
    source_reference: str
    required_preconditions: tuple[Condition, ...] = ()
    prohibited_conditions: tuple[Condition, ...] = ()
    enabled: bool = True
    disabled_reason: str | None = None

    @property
    def is_observation_only(self) -> bool:
        """True when the command has no state prerequisites at all.

        Such a command is unconditionally executable. This replaces the old
        ``command.startswith("CMD_VERIFY_")`` name heuristic with a property
        derived from declared metadata.
        """
        return not self.required_preconditions and not self.prohibited_conditions


# Shorthand used heavily below to keep the table readable.
_LOW = RiskLevel.LOW
_MED = RiskLevel.MEDIUM
_BASE = CommandSource.BASELINE_WHITELIST
_KB = CommandSource.PROCEDURE_KB

_REF_BASE = (
    "Inherited from the pre-Phase-1 safety.py COMMAND_WHITELIST. "
    "No external standard citation available."
)
_REF_KB = (
    "Referenced by a SENTINEL procedure KB entry (app/agent/rag.py), written "
    "from ECSS-E-ST-70-11C / ECSS-Q-ST-30-02C principles. No clause-level "
    "citation available."
)

# Hazard set carried by any command that actuates hardware or changes mode.
# Preserves the pre-Phase-1 thermal-survival rule, which applied to every
# command except observation-only ones and the thermal remedies themselves.
_T = (Condition.THERMAL_ABOVE_SURVIVAL,)

# Hazard set for commands that draw significant power.
_TB = (Condition.THERMAL_ABOVE_SURVIVAL, Condition.BATTERY_BELOW_FLOOR)

# Attitude actuation requires trustworthy rate data.
_GYRO = (Condition.GYRO_DATA_VALID,)

# An OBC reboot without a confirmed uplink risks permanent loss of contact.
_LOCK = (Condition.COMMS_LOCK_CONFIRMED,)


def _spec(
    command_id: str,
    subsystem: SubsystemID,
    description: str,
    risk_level: RiskLevel,
    expected_effect: str,
    source: CommandSource = _BASE,
    required: Iterable[Condition] = (),
    prohibited: Iterable[Condition] = (),
    enabled: bool = True,
    disabled_reason: str | None = None,
) -> CommandSpec:
    return CommandSpec(
        command_id=command_id,
        subsystem=subsystem,
        description=description,
        risk_level=risk_level,
        expected_effect=expected_effect,
        source=source,
        source_reference=_REF_BASE if source is _BASE else _REF_KB,
        required_preconditions=tuple(required),
        prohibited_conditions=tuple(prohibited),
        enabled=enabled,
        disabled_reason=disabled_reason,
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — THE REGISTRY
# ═══════════════════════════════════════════════════════════════════════════
#
# Entries marked "Phase 1 addition" resolve a documented conflict between the
# procedure KB and the old whitelist. See docs/phase1_command_conflicts.md.
# ═══════════════════════════════════════════════════════════════════════════

_SPECS: tuple[CommandSpec, ...] = (

    # ─────────────────────────── ADCS ───────────────────────────
    _spec("CMD_GYRO_RESET", SubsystemID.ADCS,
          "Software reset of the active gyroscope driver.",
          _LOW, "Gyro driver re-initialises; rate telemetry returns a valid float.",
          prohibited=_T),
    _spec("CMD_GYRO_A_DRIVER_RESET", SubsystemID.ADCS,
          "Software reset of gyroscope A's driver, clearing SEU-corrupted state.",
          _LOW, "GYRO_A_RATE returns a valid float within ~30s.",
          source=_KB, prohibited=_T),
    _spec("CMD_GYRO_B_DRIVER_RESET", SubsystemID.ADCS,
          "Software reset of gyroscope B's driver.",
          _LOW, "GYRO_B_RATE returns a valid float within ~30s.",
          prohibited=_T),
    _spec("CMD_GYRO_A_RESET", SubsystemID.ADCS,
          "Reset gyroscope A.",
          _LOW, "Gyro A resumes reporting rate data.",
          prohibited=_T),
    _spec("CMD_GYRO_B_RESET", SubsystemID.ADCS,
          "Reset gyroscope B.",
          _LOW, "Gyro B resumes reporting rate data.",
          prohibited=_T),
    _spec("CMD_GYRO_SWITCH_TO_BACKUP", SubsystemID.ADCS,
          "Switch the ADCS rate source to the redundant gyroscope unit.",
          _MED, "Backup gyro becomes the active rate source.",
          prohibited=_T),
    _spec("CMD_GYRO_BACKUP_SWITCH", SubsystemID.ADCS,
          "Switch to the backup gyroscope (alternate spelling retained for "
          "backward compatibility with existing plans).",
          _MED, "Backup gyro becomes the active rate source.",
          prohibited=_T),
    _spec("CMD_ATTITUDE_REACQUISITION", SubsystemID.ADCS,
          "Re-establish attitude knowledge using star tracker and sun sensor.",
          _MED, "ATTITUDE_ERROR converges below 1 deg.",
          source=_KB, required=_GYRO, prohibited=_TB),
    _spec("CMD_ATTITUDE_RESET", SubsystemID.ADCS,
          "Reset the attitude determination filter.",
          _MED, "Attitude filter restarts from current sensor data.",
          prohibited=_TB),
    _spec("CMD_ATTITUDE_HOLD", SubsystemID.ADCS,
          "Hold current attitude without manoeuvring.",
          _LOW, "Attitude is maintained at its present value.",
          prohibited=_T),
    _spec("CMD_REACTION_WHEEL_DESAT", SubsystemID.ADCS,
          "Offload accumulated momentum from the reaction wheels.",
          _MED, "Wheel speeds move away from saturation.",
          required=_GYRO, prohibited=_TB),
    _spec("CMD_REACTION_WHEEL_RESET", SubsystemID.ADCS,
          "Reset the reaction wheel assembly controller.",
          _MED, "Wheel controller re-initialises.",
          required=_GYRO, prohibited=_TB),
    _spec("CMD_REACTION_WHEEL_SPEED_CHECK", SubsystemID.ADCS,
          "Read current reaction wheel speeds.",
          _LOW, "Wheel speed telemetry is returned.",
          required=_GYRO, prohibited=_T),
    _spec("CMD_SUN_ACQUISITION", SubsystemID.ADCS,
          "Slew to a sun-pointing attitude to maximise array illumination.",
          _MED, "Sun sensor angle decreases; array current recovers.",
          required=_GYRO, prohibited=_TB),
    _spec("CMD_SUN_SENSOR_CHECK", SubsystemID.ADCS,
          "Read the sun sensor angle.",
          _LOW, "Sun sensor angle telemetry is returned.",
          prohibited=_T),
    _spec("CMD_SEU_CHECK", SubsystemID.ADCS,
          "Read the single-event-upset counter.",
          _LOW, "SEU counter value is returned.",
          prohibited=_T),

    # ─────────────────────────── EPS ────────────────────────────
    _spec("CMD_SOLAR_ARRAY_VERIFY", SubsystemID.EPS,
          "Verify solar array deployment and orientation.",
          _LOW, "Array deployment/orientation telemetry is returned.",
          prohibited=_T),
    _spec("CMD_SOLAR_ARRAY_REDEPLOY", SubsystemID.EPS,
          "Command a solar array redeployment attempt.",
          _MED, "Array deployment mechanism actuates.",
          prohibited=_TB),
    _spec("CMD_SOLAR_ARRAY_CHECK", SubsystemID.EPS,
          "Read solar array current and health status.",
          _LOW, "I_sa and array health telemetry are returned.",
          prohibited=_T),
    _spec("CMD_SOLAR_PANEL_RESET", SubsystemID.EPS,
          "Reset the solar panel drive electronics.",
          _LOW, "Panel drive electronics re-initialise.",
          prohibited=_T),
    # Phase 1 addition — resolves PROCEDURE_KB reference (EPS_SOLAR_UNDERVOLT).
    _spec("CMD_SOLAR_ARRAY_A_RESET", SubsystemID.EPS,
          "Re-initialise solar array A's drive electronics.",
          _LOW, "I_sa on array A recovers above 2A within ~30s.",
          source=_KB, prohibited=_T),
    # Phase 1 addition — resolves PROCEDURE_KB reference (EPS_SOLAR_UNDERVOLT).
    _spec("CMD_SWITCH_SOLAR_ARRAY", SubsystemID.EPS,
          "Switch the power path to the redundant solar array.",
          _MED, "I_sa recovers on the alternate array.",
          source=_KB, prohibited=_T),
    _spec("CMD_BATTERY_VERIFY", SubsystemID.EPS,
          "Verify battery pack health and state of charge.",
          _LOW, "V_bat and SoC telemetry are returned.",
          prohibited=_T),
    _spec("CMD_BATTERY_CHECK", SubsystemID.EPS,
          "Read battery voltage and state of charge.",
          _LOW, "V_bat and SoC telemetry are returned.",
          prohibited=_T),
    # Thermal only — matching pre-Phase-1 behaviour. The battery heater was not
    # in the old _POWER_REQUIRING_COMMANDS set, and Phase 1 does not add
    # constraints that did not previously exist.
    _spec("CMD_BATTERY_HEATER_ENABLE", SubsystemID.EPS,
          "Enable the battery heater.",
          _LOW, "Battery temperature rises toward its operating band.",
          prohibited=_T),
    _spec("CMD_BATTERY_HEATER_DISABLE", SubsystemID.EPS,
          "Disable the battery heater.",
          _LOW, "Battery heater power drops to zero.",
          prohibited=()),
    _spec("CMD_BUS_VOLTAGE_CHECK", SubsystemID.EPS,
          "Read the main bus voltage.",
          _LOW, "V_bus telemetry is returned.",
          prohibited=_T),
    _spec("CMD_BUS_VOLTAGE_VERIFY", SubsystemID.EPS,
          "Verify the main bus voltage is within its safe operating range.",
          _LOW, "V_bus is confirmed inside limits.",
          prohibited=_T),
    _spec("CMD_POWER_SHED_NONESSENTIAL", SubsystemID.EPS,
          "Shed non-essential electrical loads to conserve battery capacity.",
          _LOW, "Load current drops; SoC decline slows or stabilises.",
          prohibited=()),
    _spec("CMD_POWER_RESTORE", SubsystemID.EPS,
          "Restore previously shed non-essential loads.",
          _MED, "Shed loads are re-energised.",
          prohibited=_TB),
    _spec("CMD_POWER_CHECK", SubsystemID.EPS,
          "Read the EPS power status summary.",
          _LOW, "EPS status telemetry is returned.",
          prohibited=_T),

    # ─────────────────────────── OBC ────────────────────────────
    _spec("CMD_OBC_CONTROLLED_REBOOT", SubsystemID.OBC,
          "Perform a clean, controlled reboot of the onboard computer "
          "(not a power cycle).",
          _MED, "CPU load returns below 70%; flight software restarts cleanly.",
          source=_KB, required=_LOCK, prohibited=_TB),
    _spec("CMD_OBC_WATCHDOG_CLEAR", SubsystemID.OBC,
          "Clear the OBC watchdog fault latch.",
          _LOW, "Watchdog fault latch is cleared.",
          prohibited=_T),
    _spec("CMD_OBC_SOFT_RESET", SubsystemID.OBC,
          "Soft-reset the onboard computer's application layer.",
          _MED, "Application layer restarts; kernel state is preserved.",
          required=_LOCK, prohibited=_TB),
    _spec("CMD_WATCHDOG_CLEAR", SubsystemID.OBC,
          "Clear the watchdog timer counter.",
          _LOW, "Watchdog counter returns to zero.",
          prohibited=_T),
    _spec("CMD_WATCHDOG_RESET", SubsystemID.OBC,
          "Reset the watchdog timer subsystem.",
          _LOW, "Watchdog timer subsystem re-initialises.",
          prohibited=_T),
    _spec("CMD_CPU_LOAD_CHECK", SubsystemID.OBC,
          "Read current CPU load.",
          _LOW, "CPU_LOAD telemetry is returned.",
          prohibited=_T),
    _spec("CMD_CPU_TEMP_CHECK", SubsystemID.OBC,
          "Read the OBC processor temperature.",
          _LOW, "OBC_TEMP telemetry is returned.",
          prohibited=_T),
    _spec("CMD_MEMORY_DUMP", SubsystemID.OBC,
          "Downlink a dump of OBC memory for ground analysis.",
          _LOW, "Memory image is queued for downlink.",
          prohibited=_T),
    _spec("CMD_MEMORY_CHECK", SubsystemID.OBC,
          "Read OBC memory usage and integrity status.",
          _LOW, "Memory usage and checksum telemetry are returned.",
          prohibited=_T),
    # Phase 17: Enforce battery floor on safe-mode exit (_TB = Thermal + Battery).
    # If battery SoC is below the safe floor (15%), safe-mode exit is BLOCKED.
    _spec("CMD_SAFE_MODE_EXIT", SubsystemID.OBC,
          "Return the spacecraft from safe mode to nominal operations.",
          _MED, "normal_mode_flag is set; nominal operations resume.",
          source=_KB, prohibited=_TB),
    _spec("CMD_SAFE_MODE_ENTRY", SubsystemID.OBC,
          "Command a deliberate entry into safe mode.",
          _MED, "Spacecraft transitions to safe mode.",
          prohibited=()),

    # ─────────────────────────── TCS ────────────────────────────
    _spec("CMD_HEATER_ENABLE", SubsystemID.TCS,
          "Enable a heater zone.",
          _LOW, "Heater power rises; zone temperature increases.",
          prohibited=_TB),
    _spec("CMD_HEATER_DISABLE", SubsystemID.TCS,
          "Disable a heater zone.",
          _LOW, "Heater power drops to zero; zone begins cooling.",
          prohibited=()),
    _spec("CMD_HEATER_OFF", SubsystemID.TCS,
          "Switch a heater off.",
          _LOW, "Heater power drops to zero.",
          prohibited=()),
    _spec("CMD_HEATER_ON", SubsystemID.TCS,
          "Switch a heater on.",
          _LOW, "Heater power rises.",
          prohibited=_TB),
    _spec("CMD_HEATER_RESET", SubsystemID.TCS,
          "Reset a heater zone controller.",
          _LOW, "Heater controller re-initialises and resumes cycling.",
          prohibited=_T),
    _spec("CMD_HEATER_CHECK", SubsystemID.TCS,
          "Read heater zone status and power draw.",
          _LOW, "Heater status telemetry is returned.",
          prohibited=_T),
    # Phase 1 addition — resolves PROCEDURE_KB reference (TCS_THERMAL_RUNAWAY).
    # This is the remedy for thermal runaway, so it carries NO thermal
    # prohibition: blocking it during an over-temperature event was the most
    # dangerous of the conflicts Phase 1 fixes.
    _spec("CMD_DISABLE_HEATER_ZONE", SubsystemID.TCS,
          "Immediately disable the affected heater zone (thermal runaway remedy).",
          _LOW, "HEATER_ZONE status reads OFF; the zone begins cooling.",
          source=_KB, prohibited=()),
    # Phase 1 addition — resolves PROCEDURE_KB reference (TCS_THERMAL_RUNAWAY).
    _spec("CMD_MONITOR_TEMPERATURE", SubsystemID.TCS,
          "Monitor a component temperature for a cooling trend.",
          _LOW, "A temperature time series is returned.",
          source=_KB, prohibited=()),
    _spec("CMD_THERMAL_MONITOR_CHECK", SubsystemID.TCS,
          "Read the thermal monitor status.",
          _LOW, "Thermal monitor status is returned.",
          prohibited=()),
    _spec("CMD_THERMAL_CHECK", SubsystemID.TCS,
          "Read component temperatures.",
          _LOW, "Temperature telemetry is returned.",
          prohibited=()),
    _spec("CMD_THERMAL_OVERRIDE_OFF", SubsystemID.TCS,
          "Disable a thermal control override, restoring automatic control.",
          _LOW, "Automatic thermal control resumes.",
          prohibited=()),
    _spec("CMD_THERMAL_OVERRIDE_ON", SubsystemID.TCS,
          "Enable a manual thermal control override.",
          _MED, "Automatic thermal control is suspended.",
          prohibited=_T),

    # ────────────────────────── COMMS ───────────────────────────
    _spec("CMD_TRANSPONDER_LOCK_VERIFY", SubsystemID.COMMS,
          "Verify transponder carrier lock.",
          _LOW, "TRANSPONDER_LOCK telemetry is returned.",
          prohibited=_T),
    _spec("CMD_TRANSPONDER_RESET", SubsystemID.COMMS,
          "Reset the transponder.",
          _MED, "Transponder re-initialises and attempts to reacquire lock.",
          prohibited=_T),
    _spec("CMD_TRANSPONDER_CHECK", SubsystemID.COMMS,
          "Read transponder status and signal quality.",
          _LOW, "Transponder status and SNR telemetry are returned.",
          prohibited=_T),
    # Phase 1 addition — resolves PROCEDURE_KB reference (COMMS_TRANSPONDER_LOSS).
    _spec("CMD_SWITCH_BACKUP_TRANSPONDER", SubsystemID.COMMS,
          "Switch to the redundant transponder unit.",
          _MED, "TRANSPONDER_LOCK is re-established on the backup unit.",
          source=_KB, prohibited=_T),
    # Phase 1 addition — observation-only prerequisite cited by the OBC
    # watchdog procedure before any reboot.
    _spec("CMD_CONFIRM_COMMS_LOCK", SubsystemID.COMMS,
          "Confirm communications lock on the low-gain antenna before any "
          "OBC operation.",
          _LOW, "TRANSPONDER_LOCK is confirmed set.",
          source=_KB, prohibited=()),
    # Phase 1 addition — resolves PROCEDURE_KB reference (COMMS_TRANSPONDER_LOSS).
    _spec("CMD_CONFIRM_GROUND_CONTACT", SubsystemID.COMMS,
          "Confirm a two-way link with the ground station.",
          _LOW, "Ground station confirms telemetry reception.",
          source=_KB, prohibited=()),
    _spec("CMD_COMMS_SIGNAL_CHECK", SubsystemID.COMMS,
          "Read the communications signal-to-noise ratio.",
          _LOW, "SNR telemetry is returned.",
          prohibited=_T),
    _spec("CMD_COMMS_RESET", SubsystemID.COMMS,
          "Reset the communications subsystem.",
          _MED, "Comms subsystem re-initialises.",
          prohibited=_T),
    _spec("CMD_COMMS_CHECK", SubsystemID.COMMS,
          "Read the communications subsystem status.",
          _LOW, "Comms status telemetry is returned.",
          prohibited=_T),
    _spec("CMD_ANTENNA_SWITCH", SubsystemID.COMMS,
          "Switch to an alternate antenna.",
          _MED, "The alternate antenna becomes the active RF path.",
          prohibited=_T),
    _spec("CMD_LOW_GAIN_ANTENNA_SWITCH", SubsystemID.COMMS,
          "Switch to the low-gain antenna.",
          _MED, "The low-gain antenna becomes the active RF path.",
          prohibited=_T),
    _spec("CMD_ANTENNA_CHECK", SubsystemID.COMMS,
          "Read antenna pointing and status.",
          _LOW, "Antenna status telemetry is returned.",
          prohibited=_T),

    # ───────────────────────── SYSTEM ───────────────────────────
    # Observation-only by construction: both condition lists are empty, so
    # these are unconditionally executable. This replaces the old
    # startswith("CMD_VERIFY_") heuristic with declared metadata.
    _spec("CMD_HEALTH_CHECK", SubsystemID.SYSTEM,
          "Run a spacecraft-wide health check.",
          _LOW, "A health summary across all subsystems is returned.",
          prohibited=()),
    _spec("CMD_TELEMETRY_DUMP", SubsystemID.SYSTEM,
          "Downlink the stored telemetry buffer.",
          _LOW, "Stored telemetry is queued for downlink.",
          prohibited=()),
    _spec("CMD_TELEMETRY_CHECK", SubsystemID.SYSTEM,
          "Verify the telemetry subsystem is streaming.",
          _LOW, "Telemetry stream status is returned.",
          prohibited=()),
    _spec("CMD_VERIFY_STATUS", SubsystemID.SYSTEM,
          "Read the overall spacecraft status word.",
          _LOW, "Status word is returned.",
          prohibited=()),
    _spec("CMD_VERIFY_HEALTH", SubsystemID.SYSTEM,
          "Verify subsystem health flags.",
          _LOW, "Health flags are returned.",
          prohibited=()),
    _spec("CMD_VERIFY_POWER", SubsystemID.SYSTEM,
          "Verify power subsystem readings.",
          _LOW, "Power readings are returned.",
          prohibited=()),
    _spec("CMD_VERIFY_ATTITUDE", SubsystemID.SYSTEM,
          "Verify attitude determination output.",
          _LOW, "Attitude estimate and error are returned.",
          prohibited=()),
    _spec("CMD_VERIFY_THERMAL", SubsystemID.SYSTEM,
          "Verify thermal subsystem readings.",
          _LOW, "Temperature readings are returned.",
          prohibited=()),
    _spec("CMD_VERIFY_COMMS", SubsystemID.SYSTEM,
          "Verify communications subsystem readings.",
          _LOW, "Link status and SNR are returned.",
          prohibited=()),
    _spec("CMD_VERIFY_SEU_COUNTER", SubsystemID.SYSTEM,
          "Read the SEU counter to confirm or rule out a radiation event.",
          _LOW, "SEU_COUNTER value is returned.",
          source=_KB, prohibited=()),
    _spec("CMD_VERIFY_GYRO_RATE", SubsystemID.SYSTEM,
          "Read the gyroscope rate to confirm sensor recovery.",
          _LOW, "GYRO rate value is returned.",
          prohibited=()),
    # Phase 1 additions — observation-only commands the procedure KB cited but
    # the whitelist lacked. All four were blocked as NOT_WHITELISTED despite
    # being incapable of changing spacecraft state.
    _spec("CMD_VERIFY_SUN_ANGLE", SubsystemID.SYSTEM,
          "Read the sun sensor angle to confirm valid sun pointing.",
          _LOW, "sun_sensor_angle is returned.",
          source=_KB, prohibited=()),
    _spec("CMD_VERIFY_MEMORY_STATE", SubsystemID.SYSTEM,
          "Check that OBC memory usage is stable and not growing monotonically.",
          _LOW, "Memory usage trend is returned.",
          source=_KB, prohibited=()),
    _spec("CMD_VERIFY_SIGNAL_ACQUISITION", SubsystemID.SYSTEM,
          "Confirm signal quality after a communications recovery action.",
          _LOW, "SNR is returned for comparison against the acquisition threshold.",
          source=_KB, prohibited=()),
    _spec("CMD_VERIFY_THERMAL_MARGIN", SubsystemID.SYSTEM,
          "Confirm a component temperature has returned to its safe range.",
          _LOW, "Temperature and its margin to the limit are returned.",
          source=_KB, prohibited=()),
)


#: The registry, keyed by command_id. Authoritative.
COMMAND_REGISTRY: dict[str, CommandSpec] = {s.command_id: s for s in _SPECS}

# Fail loudly at import time on a duplicate command_id. A duplicate would mean
# two definitions of the same command, which is the exact class of drift this
# module exists to prevent.
if len(COMMAND_REGISTRY) != len(_SPECS):
    _seen: set[str] = set()
    _dupes = sorted({s.command_id for s in _SPECS
                     if s.command_id in _seen or _seen.add(s.command_id)})
    raise RuntimeError(f"Duplicate command_id in COMMAND_REGISTRY: {_dupes}")


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — LOOKUP API
# ═══════════════════════════════════════════════════════════════════════════

def get_command(command_id: str) -> CommandSpec | None:
    """Return the CommandSpec for ``command_id``, or None if unregistered."""
    if not command_id:
        return None
    return COMMAND_REGISTRY.get(command_id)


def is_registered(command_id: str) -> bool:
    """True if the command exists in the registry, enabled or not."""
    return get_command(command_id) is not None


def is_enabled(command_id: str) -> bool:
    """True if the command exists AND is enabled for use."""
    spec = get_command(command_id)
    return spec is not None and spec.enabled


def registry_subsystem(command_id: str) -> str | None:
    """Return the declared subsystem for a command, or None if unregistered."""
    spec = get_command(command_id)
    return spec.subsystem.value if spec is not None else None


def all_command_ids() -> list[str]:
    """All registered command IDs, sorted."""
    return sorted(COMMAND_REGISTRY)


def enabled_command_ids() -> list[str]:
    """Only the enabled command IDs, sorted.

    This is what the LLM prompt and the procedure layer are allowed to use.
    """
    return sorted(cid for cid, s in COMMAND_REGISTRY.items() if s.enabled)


def registry_by_subsystem(enabled_only: bool = True) -> dict[str, set[str]]:
    """Group command IDs by declared subsystem.

    ``app.agent.safety.COMMAND_WHITELIST`` is derived from this, which is what
    makes the registry the single source of truth for the whitelist.
    """
    grouped: dict[str, set[str]] = {}
    for cid, spec in COMMAND_REGISTRY.items():
        if enabled_only and not spec.enabled:
            continue
        grouped.setdefault(spec.subsystem.value, set()).add(cid)
    return grouped


def registry_status() -> dict:
    """Diagnostic summary of the registry, for tests and status endpoints."""
    grouped = registry_by_subsystem(enabled_only=False)
    disabled = sorted(cid for cid, s in COMMAND_REGISTRY.items() if not s.enabled)
    observation_only = sorted(
        cid for cid, s in COMMAND_REGISTRY.items() if s.is_observation_only
    )
    return {
        "total_commands": len(COMMAND_REGISTRY),
        "enabled_commands": len(enabled_command_ids()),
        "disabled_commands": disabled,
        "subsystems": sorted(grouped),
        "counts_per_subsystem": {k: len(v) for k, v in sorted(grouped.items())},
        "observation_only_commands": observation_only,
        "sources": {
            src.value: sorted(
                cid for cid, s in COMMAND_REGISTRY.items() if s.source is src
            )
            for src in CommandSource
        },
    }
