"""
Phase 1 — Fail-closed safety hardening: adversarial tests (STEP 5).

Central invariant under test:

    NO SAFETY-CRITICAL COMMAND MAY BE AUTHORIZED WHEN ANY REQUIRED TELEMETRY OR
    PRECONDITION IS UNKNOWN.

RED-01 was the inverse: a required precondition that was UNKNOWN (its telemetry
absent from the crash dump) was treated as "no hazard" and the command was
authorized. These tests exercise the deterministic safety gate
(`validate_recovery_plan` → `evaluate_declared_conditions`) directly, so they
prove the guarantee holds regardless of what the LLM proposes — the gate runs on
the LLM's parsed output, after the fact, and the LLM has no path around it.

Every assertion below verifies backend behaviour. Nothing here mocks or fakes a
safety result.

Scope honesty: four failure modes named in the Phase 1 brief have NO detector in
the current system (telemetry staleness, cross-channel contradiction, toxic
command-pair conflict, physics-refutation gating commands). They are represented
below as explicit, documented ``skip``s — not silently omitted, and not faked
with a no-op detector. See PHASE_1_FAIL_CLOSED_SAFETY_REPORT.md.

Run:
    cd sentinel/backend && python3 -m unittest tests.test_phase1_failclosed_adversarial -v
"""

from __future__ import annotations

import os
import sys
import unittest

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.api.models import (  # noqa: E402
    BlockSeverity,
    Hypothesis,
    RecoveryStep,
    RiskLevel,
    SafetyStatus,
    SentinelOutput,
)
from app.agent.safety import (  # noqa: E402
    apply_validation_to_output,
    validate_recovery_plan,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

def _hypotheses(top_confidence: float = 0.90) -> list[Hypothesis]:
    lower = min(0.06, top_confidence)
    lowest = min(0.04, top_confidence)
    return [
        Hypothesis(rank=1, root_cause="ADCS_GYRO_SEU", affected_component="GYRO_A",
                   confidence=top_confidence, causal_chain=["SEU spike", "gyro NaN"]),
        Hypothesis(rank=2, root_cause="ADCS_STAR_TRACKER_FAULT",
                   affected_component="ST_A", confidence=lower,
                   causal_chain=["ST degraded", "attitude drift"]),
        Hypothesis(rank=3, root_cause="OBC_WATCHDOG_OVERFLOW",
                   affected_component="OBC", confidence=lowest,
                   causal_chain=["cpu high", "watchdog overflow"]),
    ]


def make_output(commands, risk: RiskLevel = RiskLevel.LOW,
                confidence: float = 0.90) -> SentinelOutput:
    """An unvalidated SentinelOutput proposing the given commands."""
    return SentinelOutput(
        hypotheses=_hypotheses(confidence),
        recovery_plan=[
            RecoveryStep(step=i, command=cmd,
                         rationale=f"Rationale for {cmd}",
                         wait_seconds=10, verify=f"Verify effect of {cmd}",
                         risk=risk)
            for i, cmd in enumerate(commands, start=1)
        ],
        confidence=confidence,
        requires_human_review=False,
        reasoning_summary="Deterministic adversarial fixture reasoning summary.",
    )


def result(commands, ctx=None, **kw):
    """The raw ValidationResult (carries violation codes + severities)."""
    return validate_recovery_plan(make_output(commands, **kw), ctx or {})


def applied(commands, ctx=None, **kw):
    """The SentinelOutput after validate + apply (survivors + blocked_steps)."""
    raw = make_output(commands, **kw)
    return apply_validation_to_output(raw, validate_recovery_plan(raw, ctx or {}))


def blocked_codes(res) -> dict[str, str]:
    """command -> violation_code for every blocked step."""
    return {b.original_step.command: b.violation_code for b in res.blocked_steps}


def validated_commands(res) -> list[str]:
    return [s.command for s in res.validated_steps]


# Telemetry that satisfies every affirmative precondition and trips no hazard.
GOOD_CTX = {"GYRO_A_RATE": 0.1, "TRANSPONDER_LOCK": 1,
            "BATTERY_SOC": 90.0, "TEMP_C": 20.0}

# Commands whose registry entry REQUIRES an affirmative precondition:
GYRO_REQUIRED_CMD = "CMD_SUN_ACQUISITION"          # requires GYRO_DATA_VALID
GYRO_REQUIRED_CMD_2 = "CMD_ATTITUDE_REACQUISITION"  # requires GYRO_DATA_VALID
LOCK_REQUIRED_CMD = "CMD_OBC_CONTROLLED_REBOOT"     # requires COMMS_LOCK_CONFIRMED
LOCK_REQUIRED_CMD_2 = "CMD_OBC_SOFT_RESET"          # requires COMMS_LOCK_CONFIRMED
OBSERVATION_ONLY = ["CMD_HEALTH_CHECK", "CMD_TELEMETRY_DUMP",
                    "CMD_CONFIRM_COMMS_LOCK"]


# ═══════════════════════════════════════════════════════════════════════════
# STEP 5 — the 16 adversarial cases
# ═══════════════════════════════════════════════════════════════════════════

class TestFailClosedAuthorization(unittest.TestCase):
    """Each test names the adversarial condition and the required outcome."""

    # --- 1. valid telemetry present → AUTHORIZED ---------------------------
    def test_01_valid_gyro_command_is_authorized(self):
        res = result([GYRO_REQUIRED_CMD], GOOD_CTX)
        self.assertEqual(res.safety_status, SafetyStatus.VALIDATED)
        self.assertIn(GYRO_REQUIRED_CMD, validated_commands(res))
        self.assertEqual(res.blocked_steps, [])

    def test_02_valid_reboot_command_is_authorized(self):
        res = result([LOCK_REQUIRED_CMD], GOOD_CTX)
        self.assertEqual(res.safety_status, SafetyStatus.VALIDATED)
        self.assertIn(LOCK_REQUIRED_CMD, validated_commands(res))

    # --- 2. required telemetry MISSING → BLOCKED ---------------------------
    def test_03_missing_gyro_channel_blocks_attitude_command(self):
        # battery + thermal present-good, gyro channel entirely absent.
        res = result([GYRO_REQUIRED_CMD], {"BATTERY_SOC": 90.0, "TEMP_C": 20.0})
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[GYRO_REQUIRED_CMD],
                         "MISSING_PRECONDITION")

    def test_04_missing_comms_lock_blocks_reboot(self):
        res = result([LOCK_REQUIRED_CMD], {"BATTERY_SOC": 90.0, "TEMP_C": 20.0})
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[LOCK_REQUIRED_CMD],
                         "MISSING_PRECONDITION")

    # --- 3. UNKNOWN everything (empty ctx) → BLOCKED -----------------------
    def test_05_empty_context_blocks_safety_critical_command(self):
        res = result([GYRO_REQUIRED_CMD], {})
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        block = res.blocked_steps[0]
        self.assertEqual(block.violation_code, "MISSING_PRECONDITION")
        self.assertEqual(block.severity, BlockSeverity.CRITICAL)
        self.assertEqual(block.supporting_context.get("reason_category"),
                         "UNKNOWN_TELEMETRY")

    # --- 4. present-but-MALFORMED telemetry → BLOCKED ----------------------
    def test_06_malformed_gyro_blocks(self):
        res = result([GYRO_REQUIRED_CMD_2],
                     {"GYRO_A_RATE": float("nan"), "BATTERY_SOC": 90.0,
                      "TEMP_C": 20.0})
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        # Present-but-invalid keeps its own diagnostic code, distinct from absent.
        self.assertEqual(blocked_codes(res)[GYRO_REQUIRED_CMD_2],
                         "GYRO_HEALTH_PREREQUISITE")

    def test_07_malformed_comms_lock_blocks(self):
        # A garbage lock reading must not be treated as a confirmed lock.
        res = result([LOCK_REQUIRED_CMD],
                     {"TRANSPONDER_LOCK": None, "BATTERY_SOC": 90.0,
                      "TEMP_C": 20.0})
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[LOCK_REQUIRED_CMD],
                         "COMMS_LOCK_REBOOT")

    # --- 5. degraded/contradictory health flag → BLOCKED -------------------
    def test_08_degraded_health_flag_blocks_even_without_rate_channel(self):
        # No gyro-rate channel at all, but hardware_state says the gyro is
        # degraded. The health signal must win — this is NOT treated as UNKNOWN.
        res = result([GYRO_REQUIRED_CMD_2],
                     {"hardware_state": {"gyro_health": "degraded"},
                      "BATTERY_SOC": 90.0, "TEMP_C": 20.0})
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[GYRO_REQUIRED_CMD_2],
                         "GYRO_HEALTH_PREREQUISITE")

    # --- 6. command not in the registry → BLOCKED --------------------------
    def test_09_non_whitelisted_command_blocked(self):
        res = result(["CMD_NOT_A_REAL_COMMAND"], GOOD_CTX)
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)["CMD_NOT_A_REAL_COMMAND"],
                         "NOT_IN_REGISTRY")

    # --- 7. LLM proposes an overtly dangerous command → BLOCKED ------------
    def test_10_llm_proposed_dangerous_command_blocked(self):
        # The LLM even labels it LOW risk; the gate does not consult that label.
        res = result(["CMD_SELF_DESTRUCT"], GOOD_CTX, risk=RiskLevel.LOW)
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)["CMD_SELF_DESTRUCT"],
                         "NOT_IN_REGISTRY")

    # --- 8. LLM output "valid" but required telemetry UNKNOWN → BLOCKED ----
    def test_11_llm_low_risk_label_cannot_override_unknown_precondition(self):
        res = result([GYRO_REQUIRED_CMD], {}, risk=RiskLevel.LOW)
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[GYRO_REQUIRED_CMD],
                         "MISSING_PRECONDITION")

    def test_12_llm_high_confidence_cannot_override_unknown_precondition(self):
        # A confident LLM must not be able to buy authorization on absent data.
        res = result([LOCK_REQUIRED_CMD], {}, confidence=0.99)
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[LOCK_REQUIRED_CMD],
                         "MISSING_PRECONDITION")

    # --- 9. multi-command plan, one step unsafe → unsafe cannot authorize --
    def test_13_multi_command_plan_isolates_the_unsafe_step(self):
        res = result(["CMD_HEALTH_CHECK", GYRO_REQUIRED_CMD],
                     {"BATTERY_SOC": 90.0, "TEMP_C": 20.0})  # gyro absent
        self.assertEqual(res.safety_status, SafetyStatus.PARTIALLY_BLOCKED)
        self.assertIn("CMD_HEALTH_CHECK", validated_commands(res))
        self.assertEqual(blocked_codes(res).get(GYRO_REQUIRED_CMD),
                         "MISSING_PRECONDITION")

    def test_14_unsafe_step_cannot_ride_along(self):
        res = result([LOCK_REQUIRED_CMD, "CMD_HEALTH_CHECK"], {})
        # The unsafe command must not appear among validated steps under any
        # circumstances, even when a sibling step is safe.
        self.assertNotIn(LOCK_REQUIRED_CMD, validated_commands(res))
        self.assertIn(LOCK_REQUIRED_CMD, blocked_codes(res))

    # --- 10. telemetry disappears between diagnosis and authorization ------
    def test_15_telemetry_present_then_absent_reblocks(self):
        raw = make_output([LOCK_REQUIRED_CMD])
        # At diagnosis time the lock is confirmed → authorized.
        first = validate_recovery_plan(raw, GOOD_CTX)
        self.assertEqual(first.safety_status, SafetyStatus.VALIDATED)
        # At authorization time the lock reading is gone → must re-evaluate and
        # block. The gate holds no cached verdict; it reads the ctx it is given.
        second = validate_recovery_plan(raw, {"BATTERY_SOC": 90.0, "TEMP_C": 20.0})
        self.assertEqual(second.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(second)[LOCK_REQUIRED_CMD],
                         "MISSING_PRECONDITION")

    # --- 11. context of only unseen/irrelevant channels → fail safely ------
    def test_16_unseen_channel_context_fails_safely(self):
        res = result([GYRO_REQUIRED_CMD],
                     {"SOME_UNMODELLED_CHANNEL": 123, "BATTERY_SOC": 90.0,
                      "TEMP_C": 20.0})  # nothing that resolves the gyro precondition
        self.assertEqual(res.safety_status, SafetyStatus.BLOCKED)
        self.assertEqual(blocked_codes(res)[GYRO_REQUIRED_CMD],
                         "MISSING_PRECONDITION")

    # --- anchors: fail-closed must not over-reach --------------------------
    def test_17_observation_only_never_blocked_on_empty_context(self):
        # The fix must not turn the diagnostic/remedy commands into casualties.
        res = result(OBSERVATION_ONLY, {})
        self.assertEqual(res.safety_status, SafetyStatus.VALIDATED)
        self.assertEqual(sorted(validated_commands(res)), sorted(OBSERVATION_ONLY))

    def test_18_all_blocked_plan_authorizes_nothing(self):
        # "Empty payload → none authorized": an all-unsafe plan yields an EMPTY
        # authorized recovery plan and BLOCKED status — it cannot masquerade as
        # a successful one-step plan.
        out = applied([GYRO_REQUIRED_CMD], {})
        self.assertEqual(out.recovery_plan, [])
        self.assertEqual(out.safety_status, SafetyStatus.BLOCKED)
        self.assertTrue(out.requires_human_review)

    def test_19_prohibited_hazard_absent_stays_permissive(self):
        # Guardrail on the policy boundary: a command that only PROHIBITS a
        # hazard (no affirmative precondition) is NOT blocked when that hazard's
        # telemetry is merely absent — only a required precondition fails closed.
        res = result(["CMD_SAFE_MODE_EXIT"], {})  # prohibits thermal+battery only
        self.assertEqual(res.safety_status, SafetyStatus.VALIDATED)
        self.assertIn("CMD_SAFE_MODE_EXIT", validated_commands(res))


# ═══════════════════════════════════════════════════════════════════════════
# Documented out-of-scope failure modes (no detector exists in Phase 1)
# ═══════════════════════════════════════════════════════════════════════════
#
# These are declared, not faked. Each names a hazard the Phase 1 brief lists but
# the current system has no mechanism to detect at the command-authorization
# boundary. Skipping (rather than omitting) keeps them visible as known gaps and
# as the acceptance tests for whoever implements them later. Wiring a detector
# that always no-ops just to flip these green would be exactly the "demo-only
# behaviour" the Phase 1 constraints forbid.

class TestOutOfScopeFailureModesAreDocumented(unittest.TestCase):

    @unittest.skip("Phase 1 scope: no telemetry-freshness gate exists. Rows carry "
                   "relative_time_s but there is no staleness threshold. STALE_TELEMETRY "
                   "is a reserved reason code with no emitter. See PHASE_1 report.")
    def test_stale_telemetry_would_block(self):
        raise AssertionError("no staleness detector implemented")

    @unittest.skip("Phase 1 scope: no cross-channel contradiction detector exists. "
                   "CONTRADICTORY_TELEMETRY is a reserved reason code with no emitter.")
    def test_contradictory_telemetry_would_block(self):
        raise AssertionError("no contradiction detector implemented")

    @unittest.skip("Phase 1 scope: conflicts.py is a build-time registry-consistency "
                   "checker, not a runtime command-pair detector. TOXIC_COMMAND_CONFLICT "
                   "is a reserved reason code with no emitter.")
    def test_toxic_command_pair_would_block(self):
        raise AssertionError("no runtime toxic-pair detector implemented")

    @unittest.skip("Phase 1 scope: physics validates hypotheses (VALID/INVALID/UNCERTAIN) "
                   "and never gates a command. PHYSICS_REFUTED is a reserved reason code "
                   "with no emitter; there is no physics->authorization edge.")
    def test_physics_refuted_would_block_command(self):
        raise AssertionError("no physics->command gating implemented")


if __name__ == "__main__":
    unittest.main(verbosity=2)
