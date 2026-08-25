"""
Phase 1 regression tests — blocked-plan behaviour and safety status.

The central guarantee under test:

    AN ENTIRELY UNSAFE PLAN CANNOT BE PRESENTED AS A SUCCESSFUL PLAN.

Before Phase 1 it could. When every step was blocked, apply_validation_to_output
substituted a fabricated ``CMD_HEALTH_CHECK`` step at LOW risk to satisfy
``recovery_plan``'s min_length=1, and the pipeline emitted the literal status
"Analysis complete. Safety validation passed." So a plan of pure garbage
rendered as a clean one-step recovery that claimed to have passed validation.

Also covers:
  * SafetyStatus derivation and its precedence
  * blocked steps as structured API data, never flattened into reasoning_summary
  * each procedure's full recovery sequence now surviving validation
  * the physical constraint guards still firing after the registry refactor

Run:
    cd sentinel/backend && python3 -m unittest tests.test_phase1_blocked_plans -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from pydantic import ValidationError  # noqa: E402

from app.api.models import (  # noqa: E402
    BlockedCommand,
    BlockSeverity,
    Hypothesis,
    RecoveryStep,
    RiskLevel,
    SafetyStatus,
    SentinelOutput,
)
from app.agent.safety import (  # noqa: E402
    apply_validation_to_output,
    derive_safety_status,
    validate_recovery_plan,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

def _hypotheses(top_confidence: float = 0.90) -> list[Hypothesis]:
    """Three ranked hypotheses with a controllable rank-1 confidence.

    The rank-1 confidence must track the output-level confidence: SentinelOutput
    invariant 5 silently rewrites ``confidence`` to match the rank-1 hypothesis,
    so a fixture that sets only the top-level value has it overwritten.
    """
    lower = min(0.06, top_confidence)
    lowest = min(0.04, top_confidence)
    return [
        Hypothesis(rank=1, root_cause="ADCS_GYRO_SEU", affected_component="GYRO_A",
                   confidence=top_confidence,
                   causal_chain=["SEU spike", "gyro NaN"]),
        Hypothesis(rank=2, root_cause="ADCS_STAR_TRACKER_FAULT",
                   affected_component="ST_A", confidence=lower,
                   causal_chain=["ST degraded", "attitude drift"]),
        Hypothesis(rank=3, root_cause="OBC_WATCHDOG_OVERFLOW",
                   affected_component="OBC", confidence=lowest,
                   causal_chain=["cpu high", "watchdog overflow"]),
    ]


def make_output(
    commands: list[str],
    confidence: float = 0.90,
    risk: RiskLevel = RiskLevel.LOW,
) -> SentinelOutput:
    """An unvalidated SentinelOutput proposing the given commands."""
    return SentinelOutput(
        hypotheses=_hypotheses(confidence),
        recovery_plan=[
            RecoveryStep(
                step=i, command=cmd,
                rationale=f"Rationale for {cmd}",
                wait_seconds=10,
                verify=f"Verify effect of {cmd}",
                risk=risk,
            )
            for i, cmd in enumerate(commands, start=1)
        ],
        confidence=confidence,
        requires_human_review=False,
        reasoning_summary="Deterministic fixture reasoning summary for tests.",
    )


def validated(commands, ctx=None, **kwargs) -> SentinelOutput:
    """Run the full validate + apply cycle."""
    raw = make_output(commands, **kwargs)
    result = validate_recovery_plan(raw, ctx or {})
    return apply_validation_to_output(raw, result)


#: Commands that must never be authorised.
UNSAFE_COMMANDS = [
    "CMD_LAUNCH_MISSILE",
    "CMD_SELF_DESTRUCT",
    "CMD_FORMAT_DISK",
    "CMD_DELETE_LOGS",
    "CMD_OVERRIDE_SAFETY",
    "CMD_DEPLOY_PAYLOAD_UNAUTHORIZED",
]


# ═══════════════════════════════════════════════════════════════════════════
# THE CENTRAL GUARANTEE
# ═══════════════════════════════════════════════════════════════════════════

class TestUnsafePlanCannotLookSuccessful(unittest.TestCase):
    """An entirely unsafe plan must not become a successful-looking plan."""

    def setUp(self):
        self.result = validated(UNSAFE_COMMANDS)

    def test_status_is_blocked(self):
        self.assertEqual(self.result.safety_status, SafetyStatus.BLOCKED)

    def test_recovery_plan_is_empty(self):
        self.assertEqual(self.result.recovery_plan, [])

    def test_no_fabricated_health_check_substitution(self):
        """The specific pre-Phase-1 defect."""
        commands = [s.command for s in self.result.recovery_plan]
        self.assertNotIn("CMD_HEALTH_CHECK", commands)
        self.assertEqual(commands, [])

    def test_every_unsafe_command_is_accounted_for(self):
        blocked = {b.command for b in self.result.blocked_steps}
        self.assertEqual(blocked, set(UNSAFE_COMMANDS))

    def test_requires_human_review(self):
        self.assertTrue(self.result.requires_human_review)

    def test_status_is_not_any_success_state(self):
        self.assertNotIn(
            self.result.safety_status,
            (SafetyStatus.VALIDATED, SafetyStatus.NOT_VALIDATED),
        )

    def test_serialised_response_cannot_be_read_as_success(self):
        """A client reading the JSON must not be able to conclude success."""
        payload = json.loads(self.result.model_dump_json())
        self.assertEqual(payload["safety_status"], "BLOCKED")
        self.assertEqual(payload["recovery_plan"], [])
        self.assertEqual(len(payload["blocked_steps"]), len(UNSAFE_COMMANDS))
        self.assertTrue(payload["requires_human_review"])
        self.assertNotIn("passed safety validation", payload["reasoning_summary"])

    def test_blocked_status_cannot_coexist_with_steps(self):
        """The model itself refuses the contradiction."""
        with self.assertRaises(ValidationError):
            SentinelOutput(
                hypotheses=_hypotheses(),
                recovery_plan=[RecoveryStep(
                    step=1, command="CMD_HEALTH_CHECK",
                    rationale="Should not be allowed alongside BLOCKED",
                    wait_seconds=5, verify="nominal", risk=RiskLevel.LOW,
                )],
                confidence=0.9,
                requires_human_review=True,
                reasoning_summary="Contradictory output under test.",
                safety_status=SafetyStatus.BLOCKED,
                blocked_steps=[BlockedCommand(
                    command="CMD_LAUNCH_MISSILE", reason="not registered",
                    violated_constraint="NOT_IN_REGISTRY",
                    severity=BlockSeverity.CRITICAL,
                )],
            )

    def test_empty_plan_requires_blocked_status(self):
        """An empty plan cannot be passed off as VALIDATED."""
        for status in (
            SafetyStatus.VALIDATED,
            SafetyStatus.NOT_VALIDATED,
            SafetyStatus.PARTIALLY_BLOCKED,
            SafetyStatus.REQUIRES_HUMAN_REVIEW,
        ):
            with self.subTest(status=status.value):
                with self.assertRaises(ValidationError):
                    SentinelOutput(
                        hypotheses=_hypotheses(),
                        recovery_plan=[],
                        confidence=0.9,
                        requires_human_review=False,
                        reasoning_summary="Empty plan under test.",
                        safety_status=status,
                    )

    def test_blocked_status_requires_an_explanation(self):
        """BLOCKED with an empty plan and no blocked_steps is rejected."""
        with self.assertRaises(ValidationError):
            SentinelOutput(
                hypotheses=_hypotheses(),
                recovery_plan=[],
                confidence=0.9,
                requires_human_review=True,
                reasoning_summary="Blocked with no explanation under test.",
                safety_status=SafetyStatus.BLOCKED,
                blocked_steps=[],
            )


# ═══════════════════════════════════════════════════════════════════════════
# SAFETY STATUS
# ═══════════════════════════════════════════════════════════════════════════

class TestSafetyStatusDerivation(unittest.TestCase):

    def test_default_is_not_validated(self):
        """A raw LLM output must never claim to have passed safety checks."""
        self.assertEqual(
            make_output(["CMD_HEALTH_CHECK"]).safety_status,
            SafetyStatus.NOT_VALIDATED,
        )

    def test_all_clean_gives_validated(self):
        r = validated(["CMD_VERIFY_SEU_COUNTER", "CMD_GYRO_A_DRIVER_RESET"],
                      ctx={"GYRO_A_RATE": 0.12})
        self.assertEqual(r.safety_status, SafetyStatus.VALIDATED)
        self.assertEqual(r.blocked_steps, [])

    def test_some_blocked_gives_partially_blocked(self):
        r = validated(
            ["CMD_VERIFY_SEU_COUNTER", "CMD_LAUNCH_MISSILE", "CMD_GYRO_RESET"],
        )
        self.assertEqual(r.safety_status, SafetyStatus.PARTIALLY_BLOCKED)
        self.assertEqual(len(r.recovery_plan), 2)
        self.assertEqual(len(r.blocked_steps), 1)

    def test_all_blocked_gives_blocked(self):
        r = validated(["CMD_LAUNCH_MISSILE"])
        self.assertEqual(r.safety_status, SafetyStatus.BLOCKED)

    def test_low_confidence_gives_requires_human_review(self):
        r = validated(["CMD_HEALTH_CHECK"], confidence=0.40)
        self.assertEqual(r.safety_status, SafetyStatus.REQUIRES_HUMAN_REVIEW)
        self.assertEqual(r.blocked_steps, [])
        self.assertTrue(r.requires_human_review)

    def test_high_risk_gives_requires_human_review(self):
        r = validated(["CMD_HEALTH_CHECK"], risk=RiskLevel.HIGH)
        self.assertEqual(r.safety_status, SafetyStatus.REQUIRES_HUMAN_REVIEW)
        self.assertTrue(r.requires_human_review)

    def test_precedence_blocked_beats_review(self):
        """Most severe status wins; the review flag is preserved separately."""
        r = validated(["CMD_LAUNCH_MISSILE"], confidence=0.30)
        self.assertEqual(r.safety_status, SafetyStatus.BLOCKED)
        self.assertTrue(r.requires_human_review)

    def test_precedence_partial_beats_review(self):
        r = validated(["CMD_HEALTH_CHECK", "CMD_LAUNCH_MISSILE"], confidence=0.30)
        self.assertEqual(r.safety_status, SafetyStatus.PARTIALLY_BLOCKED)
        self.assertTrue(r.requires_human_review)

    def test_derive_safety_status_matrix(self):
        cases = [
            # (validated, blocked, review) -> status
            ((3, 0, False), SafetyStatus.VALIDATED),
            ((3, 0, True), SafetyStatus.REQUIRES_HUMAN_REVIEW),
            ((2, 1, False), SafetyStatus.PARTIALLY_BLOCKED),
            ((2, 1, True), SafetyStatus.PARTIALLY_BLOCKED),
            ((0, 2, False), SafetyStatus.BLOCKED),
            ((0, 2, True), SafetyStatus.BLOCKED),
        ]
        for (v, b, hr), expected in cases:
            with self.subTest(validated=v, blocked=b, review=hr):
                self.assertEqual(derive_safety_status(v, b, hr), expected)

    def test_status_reaches_the_validation_result_too(self):
        raw = make_output(["CMD_LAUNCH_MISSILE"])
        vr = validate_recovery_plan(raw, {})
        self.assertEqual(vr.safety_status, SafetyStatus.BLOCKED)
        self.assertTrue(vr.all_blocked)
        self.assertFalse(vr.is_safe)

    def test_summary_never_claims_success_when_blocked(self):
        raw = make_output(["CMD_LAUNCH_MISSILE"])
        vr = validate_recovery_plan(raw, {})
        self.assertIn("BLOCKED", vr.safety_summary)
        self.assertNotIn("passed safety validation", vr.safety_summary)


# ═══════════════════════════════════════════════════════════════════════════
# BLOCKED STEPS AS STRUCTURED DATA
# ═══════════════════════════════════════════════════════════════════════════

class TestBlockedStepsAreStructured(unittest.TestCase):

    def test_reasoning_summary_is_not_used_as_a_carrier(self):
        raw = make_output(["CMD_LAUNCH_MISSILE", "CMD_HEALTH_CHECK"])
        r = apply_validation_to_output(raw, validate_recovery_plan(raw, {}))
        self.assertNotIn("[SAFETY:", r.reasoning_summary)
        self.assertNotIn("CMD_LAUNCH_MISSILE", r.reasoning_summary)
        self.assertEqual(r.reasoning_summary, raw.reasoning_summary)

    def test_every_required_field_is_present(self):
        r = validated(["CMD_OBC_CONTROLLED_REBOOT"], ctx={"TRANSPONDER_LOCK": 0})
        self.assertEqual(len(r.blocked_steps), 1)
        b = r.blocked_steps[0]
        self.assertEqual(b.command, "CMD_OBC_CONTROLLED_REBOOT")
        self.assertTrue(b.reason)
        self.assertEqual(b.violated_constraint, "COMMS_LOCK_REBOOT")
        self.assertIsInstance(b.severity, BlockSeverity)
        self.assertEqual(b.subsystem, "COMMS")
        self.assertIn("transponder_lock", b.supporting_context)
        self.assertEqual(b.step, 1)

    def test_supporting_context_reports_observed_values(self):
        r = validated(["CMD_SUN_ACQUISITION"], ctx={"SoC_pct": 7.5})
        b = r.blocked_steps[0]
        self.assertEqual(b.violated_constraint, "BATTERY_FLOOR")
        self.assertEqual(b.supporting_context["battery_soc_pct"], 7.5)
        self.assertEqual(b.supporting_context["floor_pct"], 15.0)

    def test_severity_reflects_consequence(self):
        # Loss-of-contact and loss-of-vehicle risks are CRITICAL.
        critical = [
            (["CMD_LAUNCH_MISSILE"], {}, "NOT_IN_REGISTRY"),
            (["CMD_OBC_CONTROLLED_REBOOT"], {"TRANSPONDER_LOCK": 0},
             "COMMS_LOCK_REBOOT"),
            (["CMD_ATTITUDE_REACQUISITION"], {"GYRO_A_RATE": None},
             "GYRO_HEALTH_PREREQUISITE"),
        ]
        for cmds, ctx, code in critical:
            with self.subTest(command=cmds[0]):
                b = validated(cmds, ctx=ctx).blocked_steps[0]
                self.assertEqual(b.violated_constraint, code)
                self.assertEqual(b.severity, BlockSeverity.CRITICAL)

        # Margin-eroding conditions are HIGH.
        b = validated(["CMD_SUN_ACQUISITION"], ctx={"SoC_pct": 5.0}).blocked_steps[0]
        self.assertEqual(b.severity, BlockSeverity.HIGH)

    def test_blocked_steps_survive_serialisation(self):
        r = validated(["CMD_LAUNCH_MISSILE"])
        payload = json.loads(r.model_dump_json())
        b = payload["blocked_steps"][0]
        for key in ("command", "reason", "violated_constraint", "severity",
                    "subsystem", "supporting_context", "step"):
            self.assertIn(key, b)

    def test_original_step_number_is_preserved(self):
        r = validated(["CMD_HEALTH_CHECK", "CMD_HEALTH_CHECK", "CMD_LAUNCH_MISSILE"])
        self.assertEqual(r.blocked_steps[0].step, 3)

    def test_surviving_steps_are_renumbered(self):
        r = validated(["CMD_LAUNCH_MISSILE", "CMD_HEALTH_CHECK",
                       "CMD_SELF_DESTRUCT", "CMD_TELEMETRY_DUMP"])
        self.assertEqual([s.step for s in r.recovery_plan], [1, 2])
        self.assertEqual(
            [s.command for s in r.recovery_plan],
            ["CMD_HEALTH_CHECK", "CMD_TELEMETRY_DUMP"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# THE 22 AUDIT CONFLICTS, AS END-TO-END PLANS
# ═══════════════════════════════════════════════════════════════════════════

class TestKnownConflictRegressions(unittest.TestCase):
    """Each recovery sequence that used to be rejected must now validate.

    These are the procedures as the RAG knowledge base actually writes them, so
    a passing test means a model that follows the retrieved procedure verbatim
    gets an executable plan.
    """

    #: fault_class -> the procedure's recovery sequence, in order
    KB_SEQUENCES = {
        "ADCS_GYRO_SEU": [
            "CMD_VERIFY_SEU_COUNTER",
            "CMD_GYRO_A_DRIVER_RESET",
            "CMD_ATTITUDE_REACQUISITION",
            "CMD_SAFE_MODE_EXIT",
        ],
        "EPS_SOLAR_UNDERVOLT": [
            "CMD_VERIFY_SUN_ANGLE",
            "CMD_SOLAR_ARRAY_A_RESET",
            "CMD_SWITCH_SOLAR_ARRAY",
            "CMD_POWER_SHED_NONESSENTIAL",
            "CMD_SAFE_MODE_EXIT",
        ],
        "OBC_WATCHDOG_OVERFLOW": [
            "CMD_CONFIRM_COMMS_LOCK",
            "CMD_OBC_CONTROLLED_REBOOT",
            "CMD_VERIFY_MEMORY_STATE",
            "CMD_SAFE_MODE_EXIT",
        ],
        "TCS_THERMAL_RUNAWAY": [
            "CMD_DISABLE_HEATER_ZONE",
            "CMD_MONITOR_TEMPERATURE",
            "CMD_VERIFY_THERMAL_MARGIN",
            "CMD_SAFE_MODE_EXIT",
        ],
        "COMMS_TRANSPONDER_LOSS": [
            "CMD_SWITCH_BACKUP_TRANSPONDER",
            "CMD_VERIFY_SIGNAL_ACQUISITION",
            "CMD_CONFIRM_GROUND_CONTACT",
            "CMD_SAFE_MODE_EXIT",
        ],
    }

    def test_each_kb_sequence_validates_cleanly(self):
        # The context supplies the telemetry the safety-critical steps legitimately
        # require: a valid gyro rate (for the ADCS attitude step) and a confirmed
        # transponder lock (for the OBC controlled reboot). Phase 1 fail-closed
        # hardening requires those preconditions to be PRESENT in telemetry, not
        # merely un-refuted, so this test now proves procedure/registry agreement on
        # the good path where the preconditions are actually met. (Previously the
        # context was {"GYRO_A_RATE": 0.1} and the OBC reboot validated only because
        # an absent lock was treated permissively — the RED-01 behavior.)
        ctx = {"GYRO_A_RATE": 0.1, "TRANSPONDER_LOCK": 1}
        for fault, sequence in sorted(self.KB_SEQUENCES.items()):
            with self.subTest(fault=fault):
                r = validated(sequence, ctx=ctx)
                self.assertEqual(
                    r.safety_status, SafetyStatus.VALIDATED,
                    msg=f"{fault}: {[b.command for b in r.blocked_steps]}",
                )
                self.assertEqual(len(r.recovery_plan), len(sequence))
                self.assertEqual(r.blocked_steps, [])

    def test_thermal_procedure_survives_an_actual_thermal_event(self):
        """The remedy must not be blocked by the fault it remedies."""
        r = validated(
            self.KB_SEQUENCES["TCS_THERMAL_RUNAWAY"][:3],
            ctx={"Component_temp_C": 118.0},
        )
        self.assertEqual(r.safety_status, SafetyStatus.VALIDATED)
        self.assertEqual(r.blocked_steps, [])

    def test_safe_mode_exit_is_still_blocked_while_overheating(self):
        """Remedies pass; returning to nominal ops while hot does not."""
        r = validated(["CMD_SAFE_MODE_EXIT"], ctx={"Component_temp_C": 118.0})
        self.assertEqual(r.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(
            r.blocked_steps[0].violated_constraint, "THERMAL_SURVIVAL",
        )

    def test_dataset_generator_plans_all_validate(self):
        from simulation.dataset_generator import _RECOVERY_COMMANDS

        # Supply the preconditions the safety-critical steps require (gyro rate +
        # confirmed comms lock). Phase 1 fail-closed hardening requires these to be
        # present in telemetry rather than assumed absent-therefore-safe.
        ctx = {"GYRO_A_RATE": 0.1, "TRANSPONDER_LOCK": 1}
        for fault, sequence in sorted(_RECOVERY_COMMANDS.items()):
            with self.subTest(fault=fault):
                r = validated(list(sequence), ctx=ctx)
                self.assertEqual(
                    r.blocked_steps, [],
                    msg=f"{fault}: {[b.command for b in r.blocked_steps]}",
                )

    def test_demo_cache_plans_contain_only_authorised_commands(self):
        """The cache is replayed to operators, so it must survive live safety."""
        cache_dir = os.path.join(_BACKEND_ROOT, "data", "demo_cache")
        if not os.path.isdir(cache_dir):
            self.skipTest("demo cache not present")

        files = sorted(f for f in os.listdir(cache_dir) if f.endswith(".json"))
        self.assertGreater(len(files), 0)

        for name in files:
            with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            commands = [
                s["command"]
                for s in payload["sentinel_output"]["recovery_plan"]
            ]
            with self.subTest(file=name):
                self.assertGreater(len(commands), 0)
                r = validated(commands, ctx={"GYRO_A_RATE": 0.1})
                self.assertEqual(
                    r.blocked_steps, [],
                    msg=f"{name}: {[b.command for b in r.blocked_steps]}",
                )
                self.assertEqual(payload["sentinel_output"]["safety_status"],
                                 SafetyStatus.VALIDATED.value)


# ═══════════════════════════════════════════════════════════════════════════
# THE PHYSICAL GUARDS STILL WORK AFTER THE REFACTOR
# ═══════════════════════════════════════════════════════════════════════════

class TestConstraintGuardsStillFire(unittest.TestCase):

    def test_battery_floor(self):
        r = validated(["CMD_ATTITUDE_REACQUISITION"],
                      ctx={"SoC_pct": 9.0, "GYRO_A_RATE": 0.1})
        self.assertEqual(r.blocked_steps[0].violated_constraint, "BATTERY_FLOOR")

    def test_battery_floor_permissive_when_absent(self):
        r = validated(["CMD_ATTITUDE_REACQUISITION"], ctx={"GYRO_A_RATE": 0.1})
        self.assertEqual(r.safety_status, SafetyStatus.VALIDATED)

    def test_gyro_prerequisite(self):
        for bad in (None, "NaN", float("nan"), ""):
            with self.subTest(gyro=bad):
                r = validated(["CMD_SUN_ACQUISITION"], ctx={"GYRO_A_RATE": bad})
                self.assertEqual(
                    r.blocked_steps[0].violated_constraint,
                    "GYRO_HEALTH_PREREQUISITE",
                )

    def test_gyro_required_fails_closed_when_absent(self):
        # Phase 1 (fail-closed): CMD_SUN_ACQUISITION requires GYRO_DATA_VALID. An
        # empty context means the gyro channel is absent, so that precondition is
        # UNKNOWN. A CRITICAL command must not be authorized on absent evidence, so
        # it now blocks with MISSING_PRECONDITION. This previously asserted VALIDATED
        # — the permissive-UNKNOWN behavior (RED-01) this phase removes.
        r = validated(["CMD_SUN_ACQUISITION"], ctx={})
        self.assertEqual(r.safety_status, SafetyStatus.BLOCKED)
        codes = {b.violated_constraint for b in r.blocked_steps}
        self.assertIn("MISSING_PRECONDITION", codes)

    def test_comms_lock_before_reboot(self):
        for bad in (0, False, "0", "false", "no"):
            with self.subTest(lock=bad):
                r = validated(["CMD_OBC_CONTROLLED_REBOOT"],
                              ctx={"TRANSPONDER_LOCK": bad})
                self.assertEqual(
                    r.blocked_steps[0].violated_constraint, "COMMS_LOCK_REBOOT",
                )

    def test_comms_lock_satisfied_allows_reboot(self):
        r = validated(["CMD_OBC_CONTROLLED_REBOOT"], ctx={"TRANSPONDER_LOCK": 1})
        self.assertEqual(r.safety_status, SafetyStatus.VALIDATED)

    def test_thermal_survival(self):
        r = validated(["CMD_GYRO_RESET"], ctx={"Component_temp_C": 92.0})
        self.assertEqual(
            r.blocked_steps[0].violated_constraint, "THERMAL_SURVIVAL",
        )

    def test_observation_only_commands_are_never_blocked(self):
        hostile = {
            "SoC_pct": 1.0,
            "GYRO_A_RATE": None,
            "TRANSPONDER_LOCK": 0,
            "Component_temp_C": 140.0,
        }
        observation_only = [
            "CMD_HEALTH_CHECK", "CMD_VERIFY_STATUS", "CMD_VERIFY_SEU_COUNTER",
            "CMD_VERIFY_SUN_ANGLE", "CMD_VERIFY_THERMAL_MARGIN",
            "CMD_MONITOR_TEMPERATURE", "CMD_DISABLE_HEATER_ZONE",
            "CMD_CONFIRM_COMMS_LOCK", "CMD_POWER_SHED_NONESSENTIAL",
        ]
        r = validated(observation_only, ctx=hostile)
        self.assertEqual(
            r.blocked_steps, [],
            msg=f"blocked: {[(b.command, b.violated_constraint) for b in r.blocked_steps]}",
        )

    def test_documented_thermal_relaxations(self):
        """Six commands are intentionally no longer blocked when overheating.

        The pre-Phase-1 thermal rule exempted only ``CMD_VERIFY_*`` (matched by
        name prefix) plus a hardcoded remedy list, so these six were blocked
        during an over-temperature event purely because of how they were spelled.
        Phase 1 derives the exemption from declared metadata, which unblocks
        them. Verified against the pre-Phase-1 validator over 28,000
        command x context comparisons: these are the ONLY verdict changes for
        pre-existing commands.
        """
        relaxed = {
            "CMD_HEALTH_CHECK": "observation-only",
            "CMD_TELEMETRY_DUMP": "observation-only",
            "CMD_TELEMETRY_CHECK": "observation-only",
            "CMD_POWER_SHED_NONESSENTIAL": "reduces load, therefore heat",
            "CMD_BATTERY_HEATER_DISABLE": "turns a heater off",
            "CMD_SAFE_MODE_ENTRY": "safe mode is the protective action",
        }
        for cmd, why in sorted(relaxed.items()):
            with self.subTest(command=cmd, rationale=why):
                r = validated([cmd], ctx={"Component_temp_C": 130.0})
                self.assertEqual(
                    r.safety_status, SafetyStatus.VALIDATED,
                    msg=f"{cmd} ({why}) must not be thermally blocked",
                )

    def test_no_constraints_were_added_to_pre_existing_commands(self):
        """Phase 1 must not tighten commands it was not asked to tighten.

        CMD_BATTERY_HEATER_ENABLE does not have a battery-floor prohibition
        so that emergency battery warming remains possible at low SoC.
        (Note: CMD_SAFE_MODE_EXIT was tightened with BATTERY_BELOW_FLOOR in Phase 17).
        """
        for cmd in ("CMD_BATTERY_HEATER_ENABLE",):
            with self.subTest(command=cmd):
                r = validated([cmd], ctx={"SoC_pct": 5.0})
                self.assertEqual(r.safety_status, SafetyStatus.VALIDATED)

    def test_violation_code_is_stable_under_simultaneous_violations(self):
        """Order is battery, gyro, comms, thermal — matching the old validator.

        If this order changed, the operator-facing reason would silently change
        for every multi-fault dump.
        """
        hostile = {
            "SoC_pct": 5.0,
            "GYRO_A_RATE": None,
            "TRANSPONDER_LOCK": 0,
            "Component_temp_C": 130.0,
        }
        cases = [
            # command, expected code given ALL constraints are violated
            ("CMD_ATTITUDE_REACQUISITION", "BATTERY_FLOOR"),
            ("CMD_OBC_CONTROLLED_REBOOT", "BATTERY_FLOOR"),
            ("CMD_REACTION_WHEEL_SPEED_CHECK", "GYRO_HEALTH_PREREQUISITE"),
            ("CMD_GYRO_RESET", "THERMAL_SURVIVAL"),
        ]
        for cmd, expected in cases:
            with self.subTest(command=cmd):
                r = validated([cmd], ctx=hostile)
                self.assertEqual(
                    r.blocked_steps[0].violated_constraint, expected,
                )

    def test_malformed_command_is_blocked_as_invalid_format(self):
        for bad in ("REBOOT_NOW", "gyro_reset", "DROP TABLE commands"):
            with self.subTest(command=bad):
                r = validated([bad])
                self.assertEqual(
                    r.blocked_steps[0].violated_constraint, "INVALID_FORMAT",
                )
                self.assertEqual(r.safety_status, SafetyStatus.BLOCKED)


# ═══════════════════════════════════════════════════════════════════════════
# DISABLED COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

class TestDisabledCommandHandling(unittest.TestCase):
    """A withdrawn command must be distinguishable from an unknown one."""

    def setUp(self):
        import app.agent.safety as safety
        import app.validation.command_registry as reg
        from app.api.models import SubsystemID
        from app.validation.command_registry import CommandSource, CommandSpec

        self.reg = reg
        self.safety = safety
        self.cid = "CMD_TEST_WITHDRAWN"
        self.spec = CommandSpec(
            command_id=self.cid,
            subsystem=SubsystemID.OBC,
            description="Synthetic withdrawn command for tests.",
            risk_level=RiskLevel.LOW,
            expected_effect="None.",
            source=CommandSource.BASELINE_WHITELIST,
            source_reference="test fixture",
            enabled=False,
            disabled_reason="Withdrawn pending review.",
        )
        reg.COMMAND_REGISTRY[self.cid] = self.spec

    def tearDown(self):
        self.reg.COMMAND_REGISTRY.pop(self.cid, None)

    def test_disabled_command_is_blocked_with_its_own_code(self):
        r = validated([self.cid])
        self.assertEqual(r.safety_status, SafetyStatus.BLOCKED)
        b = r.blocked_steps[0]
        self.assertEqual(b.violated_constraint, "COMMAND_DISABLED")
        self.assertIn("Withdrawn pending review.", b.reason)
        self.assertEqual(b.severity, BlockSeverity.HIGH)

    def test_disabled_is_distinguishable_from_unknown(self):
        disabled = validated([self.cid]).blocked_steps[0]
        unknown = validated(["CMD_NEVER_EXISTED"]).blocked_steps[0]
        self.assertNotEqual(
            disabled.violated_constraint, unknown.violated_constraint,
        )
        self.assertEqual(unknown.violated_constraint, "NOT_IN_REGISTRY")

    def test_disabled_command_is_absent_from_the_derived_whitelist(self):
        # COMMAND_WHITELIST is built at import time, so recompute it here.
        from app.validation.command_registry import registry_by_subsystem

        flat: set[str] = set()
        for cmds in registry_by_subsystem(enabled_only=True).values():
            flat.update(cmds)
        self.assertNotIn(self.cid, flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
