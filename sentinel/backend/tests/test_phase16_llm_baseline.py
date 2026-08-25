"""
Phase 16 — Controlled LLM baseline pin tests.

These tests pin the CURRENT behaviour of the LLM evidence contract and the
safety boundary so future changes to either are visible. They are baseline
pins, not quality claims: several assertions document gaps (empty evidence
tuples, missing residual detail) that the LLM_BASELINE_REPORT.md records as
findings.

No model inference is involved; all LLM-shaped inputs are deterministic stubs.
"""

import json

import pytest

from app.api.scenarios import get_all_scenarios
from app.detection import run_detection_on_crash_dump
from app.diagnosis.candidates import generate_hypotheses
from app.validation.physics import validate_crash_dump
from app.procedures.retrieval import retrieve_procedures as p9_retrieve
from app.llm.ranker import (
    build_ranking_input,
    build_constrained_prompt,
    validate_ranking_output,
    convert_to_sentinel_output,
)
from app.llm.models import LLMRankingOutput
from app.agent.safety import validate_recovery_plan, apply_validation_to_output
from app.api.models import SentinelOutput

SCENARIOS = {str(s["scenario_id"]): s for s in get_all_scenarios()}


def _pipeline(sid: str):
    crash = SCENARIOS[sid]
    det = run_detection_on_crash_dump(crash)
    hyp = generate_hypotheses(det, crash)
    physics, _, resid, seq = validate_crash_dump(crash)
    fmap = {"MULTI_SUBSYSTEM_CASCADE": "MULTI_CASCADE"}
    ff = fmap.get(hyp.top.fault_id, hyp.top.fault_id) if hyp.top else None
    procs = p9_retrieve(
        query=crash.get("fault_type", ""),
        fault_cues=det.anomalous_channel_names() or None,
        fault_filter=ff,
        min_relevance=0.2,
    )
    ri = build_ranking_input(
        crash_dump=crash, anomaly_report=det, hypothesis_set=hyp,
        physics_report=physics, residual_report=resid,
        state_sequence=seq, procedure_results=procs,
    )
    return crash, ri, physics


# ═══════════════════════════════════════════════════════════════════════════
# PART 2 — LLM evidence contract pins
# ═══════════════════════════════════════════════════════════════════════════

def test_contract_top_level_keys_stable():
    _, ri, _ = _pipeline("1")
    keys = set(ri.as_prompt_dict().keys())
    # Phase 21 added ``evidence_status`` — the machine-readable evidence
    # state (ADEQUATE / PARTIAL / INSUFFICIENT / CONTRADICTORY).
    assert keys == {
        "anomaly_summary", "anomalous_channels", "anomaly_count",
        "hypotheses", "valid_fault_ids", "physics", "spacecraft_state",
        "procedures", "valid_procedure_ids", "safety_constraints",
        "scenario_id", "fault_type", "safe_mode_trigger",
        "evidence_status",
    }


def test_contract_hypothesis_fields_stable():
    _, ri, _ = _pipeline("1")
    h = ri.as_prompt_dict()["hypotheses"][0]
    assert set(h.keys()) == {
        "hypothesis_id", "fault_id", "fault_name", "subsystem",
        "deterministic_rank", "deterministic_score", "supporting_evidence",
        "contradicting_evidence", "causal_chain", "affected_channels",
        "physics_status",
    }


def test_contract_pins_scenario1_content():
    _, ri, _ = _pipeline("1")
    d = ri.as_prompt_dict()
    assert d["scenario_id"] == 1
    assert d["fault_type"] == "ADCS_GYRO_SEU"
    assert d["anomaly_count"] == 11
    assert d["hypotheses"][0]["fault_id"] == "ADCS_GYRO_SEU"
    assert d["hypotheses"][0]["deterministic_rank"] == 1
    assert d["physics"]["validated"] == ["AOCS_EXTERNAL_DISTURBANCE"]
    assert "Gyro_rate_degs" in d["spacecraft_state"]["residual_summary"]


def test_contract_evidence_ids_reach_llm():
    """Phase 17: deterministic evidence IDs reach the LLM."""
    _, ri, _ = _pipeline("1")
    assert len(ri.hypotheses[0].supporting_evidence) > 0
    assert all(isinstance(eid, str) and eid.startswith("EVID-") for eid in ri.hypotheses[0].supporting_evidence)


def test_contract_quantitative_residuals_serialized():
    """Phase 17: quantitative residuals and window adequacy reach the LLM."""
    _, ri, _ = _pipeline("2")
    d = ri.as_prompt_dict()
    assert "residuals" in d["spacecraft_state"]
    assert "window_adequacy" in d["spacecraft_state"]
    soc_res = next((r for r in d["spacecraft_state"]["residuals"] if r["channel"] == "SoC_pct"), None)
    assert soc_res is not None
    assert soc_res["observed"] is not None


def test_contract_gap_no_rag_text_in_prompt():
    """Baseline pin: retrieved RAG procedure CONTENT never enters the
    constrained prompt — only procedure metadata (id/title/...)."""
    _, ri, _ = _pipeline("1")
    messages = build_constrained_prompt(ri)
    user = messages[1]["content"]
    assert "ECSS Retrieved" not in user
    assert "PROC-ADCS-SEU-001" in user
    assert ri.as_prompt_dict()["procedures"][0]["procedure_id"] == "PROC-ADCS-SEU-001"


def test_contract_valid_procedure_ids_restricted_to_retrieved():
    """Phase 17: valid_procedure_ids contains only retrieved procedures."""
    _, ri, _ = _pipeline("1")
    assert len(ri.valid_procedure_ids) == 1
    assert ri.valid_procedure_ids[0] == "PROC-ADCS-SEU-001"


def test_contract_safety_command_ids_not_in_prompt():
    """Baseline pin: valid command IDs are not serialized into the prompt —
    only a textual note."""
    _, ri, _ = _pipeline("1")
    d = ri.as_prompt_dict()
    assert "valid_command_ids" not in d["safety_constraints"]
    assert d["safety_constraints"]["notes"]


def test_prompt_is_structured_json_not_raw_telemetry():
    _, ri, _ = _pipeline("1")
    d = ri.as_prompt_dict()
    raw = json.dumps(d)
    # Raw telemetry values (e.g. gyro 4.5 deg/s, SEU counter values) must not
    # appear in the prompt as sampled values.
    assert '"4.5"' not in raw
    assert "pre_fault_telemetry" not in raw


# ═══════════════════════════════════════════════════════════════════════════
# PART 8 — safety boundary pins (6 required cases)
# ═══════════════════════════════════════════════════════════════════════════

def _sentinel_from_raw(ri, physics, raw: str):
    parsed = json.loads(raw)
    out = LLMRankingOutput.from_dict(parsed)
    gr = validate_ranking_output(out, ri, physics, raw_parsed=parsed, raw_response=raw)
    final = gr.corrected_output or out
    return gr, final


def _run_safety(crash, sentinel_dict):
    result = SentinelOutput.model_validate(sentinel_dict)
    val = validate_recovery_plan(result, crash)
    result = apply_validation_to_output(result, val)
    return result, val


def test_safety_1_obc_reboot_blocked_when_lock_unconfirmed():
    # Phase 1 (fail-closed safety hardening) intentionally changed this outcome.
    # Scenario 3 (OBC_WATCHDOG_OVERFLOW) carries NO transponder-lock telemetry, so
    # COMMS_LOCK_CONFIRMED evaluates to UNKNOWN. CMD_OBC_CONTROLLED_REBOOT declares
    # that lock as a required (CRITICAL) precondition, so it now fails closed with
    # MISSING_PRECONDITION rather than being authorized on absent evidence — the
    # old permissive-UNKNOWN behavior (RED-01) this phase exists to remove. The
    # other steps (confirm lock, verify memory, exit safe mode) declare no required
    # precondition and still validate, so the plan is PARTIALLY_BLOCKED and routed
    # to human review. Previously this asserted a clean 4-step VALIDATED plan.
    crash, ri, physics = _pipeline("3")
    raw = json.dumps({
        "ranked_hypotheses": [{
            "fault_id": "OBC_WATCHDOG_OVERFLOW", "rank": 1, "confidence": 0.7,
            "justification": "CPU saturated, watchdog overflowed",
            "affected_component": "OBC", "causal_chain": ["CPU saturated"],
        }],
        "reasoning_summary": "OBC software fault likely.",
        "supporting_evidence_ids": [], "contradicting_evidence_ids": [],
        "selected_procedure_ids": ["PROC-OBC-WATCHDOG-001"],
        "uncertainty": "Low", "requires_human_review": False,
    })
    gr, final = _sentinel_from_raw(ri, physics, raw)
    result, val = _run_safety(crash, convert_to_sentinel_output(final, None))
    assert val.safety_status.value == "PARTIALLY_BLOCKED"
    blocked = {b.original_step.command: b.violation_code for b in val.blocked_steps}
    assert blocked == {"CMD_OBC_CONTROLLED_REBOOT": "MISSING_PRECONDITION"}
    validated = [s.command for s in val.validated_steps]
    assert "CMD_OBC_CONTROLLED_REBOOT" not in validated
    assert len(validated) == 3  # confirm-lock, verify-memory, exit-safe-mode
    assert result.requires_human_review


def test_safety_2_invalid_command_blocked():
    crash, _, _ = _pipeline("3")
    out = {
        "hypotheses": [
            {"rank": 1, "root_cause": "OBC_WATCHDOG_OVERFLOW", "affected_component": "OBC",
             "confidence": 0.8, "causal_chain": ["a", "b"]},
            {"rank": 2, "root_cause": "INSUFFICIENT_EVIDENCE", "affected_component": "UNKNOWN",
             "confidence": 0.2, "causal_chain": ["a", "b"]},
            {"rank": 3, "root_cause": "INSUFFICIENT_EVIDENCE", "affected_component": "UNKNOWN",
             "confidence": 0.1, "causal_chain": ["a", "b"]},
        ],
        "recovery_plan": [
            {"step": 1, "command": "CMD_OBC_CONTROLLED_REBOOT", "description": "reboot",
             "rationale": "recovery", "risk": "LOW", "wait_seconds": 10, "verify": "verified ok"},
            {"step": 2, "command": "CMD_FIRE_THRUSTERS_90", "description": "fire",
             "rationale": "maneuver", "risk": "HIGH", "wait_seconds": 1, "verify": "verified ok"},
        ],
        "confidence": 0.8, "requires_human_review": False,
        "reasoning_summary": "recovery plan proposed",
    }
    result, val = _run_safety(crash, out)
    codes = {b.violation_code for b in val.blocked_steps}
    # CMD_FIRE_THRUSTERS_90 is not in the registry (unchanged behavior).
    assert "NOT_IN_REGISTRY" in codes
    # Phase 1 (fail-closed): scenario 3 has no confirmed comms lock, so the OBC
    # reboot ALSO blocks with MISSING_PRECONDITION rather than validating on absent
    # evidence. With no step surviving, the plan is fully BLOCKED. Previously the
    # reboot was the lone validated step.
    assert "MISSING_PRECONDITION" in codes
    assert [s.command for s in val.validated_steps] == []
    assert val.safety_status.value == "BLOCKED"
    assert result.requires_human_review


def test_safety_3_unknown_command_blocked_and_guardrailed():
    crash, ri, physics = _pipeline("3")
    raw = json.dumps({
        "ranked_hypotheses": [{
            "fault_id": "OBC_WATCHDOG_OVERFLOW", "rank": 1, "confidence": 0.7,
            "justification": "CPU saturated", "affected_component": "OBC",
            "causal_chain": [],
        }],
        "reasoning_summary": "OBC software fault likely.",
        "supporting_evidence_ids": [], "contradicting_evidence_ids": [],
        "selected_procedure_ids": [],
        "commands": [{"name": "CMD_TOTALLY_MADE_UP"}],
        "uncertainty": "", "requires_human_review": False,
    })
    gr, final = _sentinel_from_raw(ri, physics, raw)
    assert not gr.is_valid
    assert any(v.violation_type.value == "UNKNOWN_COMMAND" for v in gr.violations)
    out = dict({
        "hypotheses": [
            {"rank": 1, "root_cause": "OBC_WATCHDOG_OVERFLOW", "affected_component": "OBC",
             "confidence": 0.7, "causal_chain": ["a", "b"]},
            {"rank": 2, "root_cause": "INSUFFICIENT_EVIDENCE", "affected_component": "UNKNOWN",
             "confidence": 0.2, "causal_chain": ["a", "b"]},
            {"rank": 3, "root_cause": "INSUFFICIENT_EVIDENCE", "affected_component": "UNKNOWN",
             "confidence": 0.1, "causal_chain": ["a", "b"]},
        ],
        "recovery_plan": [{"step": 1, "command": "CMD_TOTALLY_MADE_UP", "description": "x",
                           "rationale": "recovery", "risk": "LOW", "wait_seconds": 1,
                           "verify": "verified ok"}],
        "confidence": 0.7, "requires_human_review": False,
        "reasoning_summary": "recovery plan proposed",
    })
    result, val = _run_safety(crash, out)
    assert val.safety_status.value == "BLOCKED"
    assert len(val.validated_steps) == 0
    assert len(val.blocked_steps) == 1


def test_safety_4_physically_inconsistent_recovery_blocked():
    """Scenario 1: gyro is NaN at T-0, so attitude actuation must be blocked
    while the rest of the SEU procedure is approved."""
    crash, ri, physics = _pipeline("1")
    raw = json.dumps({
        "ranked_hypotheses": [{
            "fault_id": "ADCS_GYRO_SEU", "rank": 1, "confidence": 0.85,
            "justification": "gyro NaN after SEU", "affected_component": "GYRO_A",
            "causal_chain": [],
        }],
        "reasoning_summary": "Gyro SEU likely.",
        "supporting_evidence_ids": [], "contradicting_evidence_ids": [],
        "selected_procedure_ids": ["PROC-ADCS-SEU-001"],
        "uncertainty": "", "requires_human_review": False,
    })
    gr, final = _sentinel_from_raw(ri, physics, raw)
    result, val = _run_safety(crash, convert_to_sentinel_output(final, None))
    codes = {b.violation_code for b in val.blocked_steps}
    assert "GYRO_HEALTH_PREREQUISITE" in codes
    blocked = {b.original_step.command for b in val.blocked_steps}
    assert "CMD_ATTITUDE_REACQUISITION" in blocked
    assert len(val.validated_steps) == 3
    assert result.requires_human_review


def test_safety_5_missing_evidence_degrades_safely():
    crash, ri, physics = _pipeline("3")
    raw = json.dumps({
        "ranked_hypotheses": [], "reasoning_summary": "",
        "supporting_evidence_ids": [], "contradicting_evidence_ids": [],
        "selected_procedure_ids": [], "uncertainty": "",
        "requires_human_review": True,
    })
    gr, final = _sentinel_from_raw(ri, physics, raw)
    result, val = _run_safety(crash, convert_to_sentinel_output(final, None))
    assert result.hypotheses[0].root_cause == "INSUFFICIENT_EVIDENCE"
    assert [s.command for s in val.validated_steps] == ["CMD_HEALTH_CHECK"]
    assert result.requires_human_review


def test_safety_6_contradictory_evidence_rejected():
    """Contradictory evidence case: physics-INVALID fault ranked first,
    fake evidence IDs, certainty language, invalid procedure ID."""
    crash, ri, physics = _pipeline("3")
    raw = json.dumps({
        "ranked_hypotheses": [
            {"fault_id": "EPS_SOLAR_UNDERVOLT", "rank": 1, "confidence": 0.99,
             "justification": "definitely EPS failure", "affected_component": "EPS",
             "causal_chain": []},
            {"fault_id": "OBC_WATCHDOG_OVERFLOW", "rank": 2, "confidence": 0.4,
             "justification": "OBC watchdog", "affected_component": "OBC",
             "causal_chain": []},
        ],
        "reasoning_summary": "It is definitely the battery.",
        "supporting_evidence_ids": ["EVID-NOT-EXISTENT"],
        "contradicting_evidence_ids": ["EVID-ALSO-FAKE"],
        "selected_procedure_ids": ["PROC-NOT-IN-LIB"],
        "uncertainty": "", "requires_human_review": False,
    })
    gr, final = _sentinel_from_raw(ri, physics, raw)
    assert not gr.is_valid
    types = {v.violation_type.value for v in gr.violations}
    assert "PHYSICS_OVERRIDE" in types
    assert "INVALID_PROCEDURE" in types
    assert "UNSUPPORTED_CERTAINTY" in types
    # Physics-invalid fault demoted below non-invalid candidates
    ranks = {h.fault_id: h.rank for h in final.ranked_hypotheses}
    assert ranks["OBC_WATCHDOG_OVERFLOW"] == 1
    assert ranks["EPS_SOLAR_UNDERVOLT"] == 2
    assert final.requires_human_review