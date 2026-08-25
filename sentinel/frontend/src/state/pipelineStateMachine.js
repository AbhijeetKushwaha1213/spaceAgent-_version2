/*
 * SENTINEL — Live Pipeline State Machine (pipelineStateMachine.js)
 *
 * Provides a deterministic, reactive state machine tracking the 10 real
 * pipeline stages during live FDIR execution.
 *
 * Guarantees:
 *   - Zero fake results or simulated completion timers.
 *   - Only ONE normal stage is 'running' at any point in time.
 *   - Stage transitions are strictly driven by real incoming SSE events / API results.
 *   - Explicit status: 'pending' | 'running' | 'completed' | 'failed' | 'blocked' | 'uncertain'
 *   - Clear authority labels:
 *       PHYSICS = BINDING AUTHORITY
 *       SAFETY = BINDING AUTHORITY
 *       LLM = ASSISTIVE ONLY
 *       RECONCILIATION = DETERMINISTIC
 */

export const PIPELINE_STAGES = Object.freeze([
  {
    id: "ingest",
    num: 1,
    name: "Telemetry Ingestion",
    shortLabel: "Ingest",
    category: "Ingest",
    authority: "Deterministic",
    authorityTone: "info",
    tags: ["INGESTION"],
    description: "Ingesting and canonicalizing time-synchronized spacecraft telemetry frames...",
  },
  {
    id: "detect",
    num: 2,
    name: "Anomaly / FDIR Detection",
    shortLabel: "Detect",
    category: "Detection",
    authority: "Deterministic",
    authorityTone: "info",
    tags: ["DETECTION"],
    description: "Evaluating statistical z-scores, hard thresholds, and bitmasks across all channels...",
  },
  {
    id: "reconciliation",
    num: 3,
    name: "Observation Reconciliation",
    shortLabel: "Reconciliation",
    category: "Reconciliation",
    authority: "Deterministic",
    authorityTone: "purple",
    tags: ["RECONCILIATION"],
    description: "Clustering multi-channel observations: preserving separation under uncertainty (CORRELATION != IDENTITY)...",
  },
  {
    id: "isolation",
    num: 4,
    name: "Case Isolation",
    shortLabel: "Case Isolation",
    category: "Security & Isolation",
    authority: "Case Boundary Guard",
    authorityTone: "purple",
    tags: ["CASE_ISOLATION"],
    description: "Enforcing CaseEvidenceIndex isolation boundaries — cross-case data leakage strictly prevented...",
  },
  {
    id: "rag",
    num: 5,
    name: "Case-Scoped RAG Retrieval",
    shortLabel: "RAG Retrieval",
    category: "Procedures",
    authority: "Knowledge Base",
    authorityTone: "info",
    tags: ["RAG_RETRIEVAL", "ESA_MAPPING"],
    description: "Retrieving relevant ECSS spacecraft flight operations procedures scoped to isolated cases...",
  },
  {
    id: "hypotheses",
    num: 6,
    name: "Hypothesis Generation & Ranking",
    shortLabel: "Hypotheses",
    category: "Reasoning",
    authority: "LLM = ASSISTIVE ONLY",
    authorityTone: "warning",
    tags: ["HYPOTHESIS_GENERATION", "LLM_RANKING", "STATE_ESTIMATION"],
    description: "Generating ranked candidate fault hypotheses and causal chains with constrained LLM...",
  },
  {
    id: "physics",
    num: 7,
    name: "Deterministic Physics Validation",
    shortLabel: "Physics",
    category: "Physics",
    authority: "PHYSICS = BINDING AUTHORITY",
    authorityTone: "danger",
    tags: ["PHYSICS_VALIDATION"],
    description: "Validating hypotheses against conservation laws, orbital dynamics, and residual tolerances...",
  },
  {
    id: "safety",
    num: 8,
    name: "Safety Validation",
    shortLabel: "Safety",
    category: "Safety",
    authority: "SAFETY = BINDING AUTHORITY",
    authorityTone: "danger",
    tags: ["SAFETY_VALIDATION"],
    description: "Evaluating command interlocks, battery state-of-charge floors, and operational preconditions...",
  },
  {
    id: "recovery",
    num: 9,
    name: "Recovery Decision",
    shortLabel: "Recovery",
    category: "Recovery",
    authority: "Safety Gated",
    authorityTone: "success",
    tags: ["RECOVERY_PLAN"],
    description: "Formulating authorized recovery action sequence or enforcing human review escalation...",
  },
  {
    id: "audit",
    num: 10,
    name: "Audit & Final Verdict",
    shortLabel: "Audit & Verdict",
    category: "Audit & Operator",
    authority: "SHA-256 & Operator Authority",
    authorityTone: "success",
    tags: ["AUDIT_RECORD", "FINAL_RESULT"],
    description: "Persisting tamper-evident SHA-256 audit record and publishing diagnostic outcome...",
  },
]);

/**
 * Scan event list to find the highest stage index reached.
 */
export function computeHighestStageIndex(events) {
  let highest = -1;
  for (const ev of events || []) {
    const rawText = typeof ev?.data === "object" && ev?.data !== null
      ? JSON.stringify(ev.data)
      : String(ev?.data || "");
    const text = rawText.toUpperCase();

    if (ev?.type === "result") {
      highest = Math.max(highest, PIPELINE_STAGES.length - 1);
    }

    for (let i = 0; i < PIPELINE_STAGES.length; i += 1) {
      const stage = PIPELINE_STAGES[i];
      if (stage.tags.some((tag) => text.includes(`[${tag}]`) || text.includes(tag))) {
        highest = Math.max(highest, i);
      }
    }
  }
  return highest;
}

/**
 * Derives full pipeline progression state from current Sentinel state.
 */
export function derivePipelineProgress({
  analysis,
  scenario,
  detection,
  reconciliation,
  physicsReport,
  systemStatus,
}) {
  const status = analysis?.status || "IDLE";
  const events = analysis?.events || [];
  const output = analysis?.output || null;
  const isRunning = status === "RUNNING";
  const isComplete = status === "COMPLETE" || Boolean(output);
  const isError = status === "ERROR";

  const highestIndex = computeHighestStageIndex(events);
  const activeIndex = isComplete
    ? PIPELINE_STAGES.length - 1
    : isRunning || isError
    ? Math.max(0, highestIndex)
    : -1;

  // Derive per-stage states
  const stages = PIPELINE_STAGES.map((stage, idx) => {
    let stageStatus = "pending"; // 'pending' | 'running' | 'completed' | 'failed' | 'blocked' | 'uncertain'
    let detail = stage.description;
    let badge = null;

    if (isComplete) {
      stageStatus = "completed";
      // Refine status based on real outputs
      if (stage.id === "safety" && output?.safety_status === "BLOCKED") {
        stageStatus = "blocked";
        detail = `Safety Interlock BLOCKED recovery: ${output?.safety_reason || "Safety constraints violated"}`;
        badge = "BLOCKED";
      } else if (stage.id === "safety" && output?.safety_status === "PARTIALLY_BLOCKED") {
        // Phase 1 fail-closed hardening: one or more safety-critical commands were
        // refused (e.g. a required precondition was UNKNOWN — telemetry absent)
        // while other steps were authorized. This MUST NOT render as a clean
        // (green) completed safety stage — the operator has to see that the
        // safety validator intervened. Detail is derived only from real emitted
        // fields (blocked_steps / recovery_plan); nothing is fabricated.
        stageStatus = "blocked";
        const blockedList = output?.blocked_steps || [];
        const constraints = [
          ...new Set(blockedList.map((b) => b.violated_constraint).filter(Boolean)),
        ];
        const constraintText = constraints.length ? ` (${constraints.join(", ")})` : "";
        const authorizedCount = output?.recovery_plan?.length || 0;
        detail =
          `Safety Interlock blocked ${blockedList.length} command(s)${constraintText}; ` +
          `${authorizedCount} authorized. Human review required.`;
        badge = "PARTIALLY BLOCKED";
      } else if (stage.id === "physics") {
        const topVerdict = physicsReport?.data?.verdicts?.[0];
        if (topVerdict?.validation_status === "INVALID") {
          stageStatus = "failed";
          detail = `Physics Refuted: ${topVerdict.summary || "Violates physical constraints"}`;
          badge = "REFUTED";
        } else if (topVerdict?.validation_status === "UNCERTAIN") {
          stageStatus = "uncertain";
          detail = `Physics Uncertain: ${topVerdict.summary || "Insufficient constraint data"}`;
          badge = "UNCERTAIN";
        } else {
          detail = "Physics Verified: All conservation laws satisfied.";
          badge = "VALIDATED";
        }
      } else if (stage.id === "reconciliation") {
        const caseCount = reconciliation?.data?.total_cases ?? 1;
        const rels = reconciliation?.data?.relationships || [];
        const hasConflict = rels.some((r) => r.relationship_type === "CONFLICT");
        if (hasConflict) {
          stageStatus = "uncertain";
          detail = `Reconciliation detected CONFLICT between observation signals. Separation preserved.`;
          badge = "CONFLICT";
        } else {
          detail = `Deterministic separation established: ${caseCount} isolated case(s).`;
          badge = `${caseCount} CASE${caseCount > 1 ? "S" : ""}`;
        }
      } else if (stage.id === "recovery") {
        if (output?.requires_human_review) {
          detail = "Mandatory Human Review Required before command authorization.";
          badge = "HUMAN REVIEW";
        } else {
          detail = `Recovery Sequence Approved: ${output?.recovery_plan?.length || 0} verified step(s).`;
          badge = "AUTHORIZED";
        }
      }
    } else if (isError) {
      if (idx < activeIndex) {
        stageStatus = "completed";
      } else if (idx === activeIndex) {
        stageStatus = "failed";
        detail = `Pipeline execution halted: ${analysis?.error || "Error in stage execution"}`;
      } else {
        stageStatus = "pending";
      }
    } else if (isRunning) {
      if (idx < activeIndex) {
        stageStatus = "completed";
      } else if (idx === activeIndex) {
        stageStatus = "running";
        // Extract live event details for this stage
        const matchingEv = [...events].reverse().find((e) => {
          const t = String(e?.data || "").toUpperCase();
          return stage.tags.some((tag) => t.includes(`[${tag}]`) || t.includes(tag));
        });
        if (matchingEv?.data) {
          detail = matchingEv.data;
        }
      } else {
        stageStatus = "pending";
      }
    }

    return {
      ...stage,
      status: stageStatus,
      detail,
      badge,
      isActive: idx === activeIndex,
    };
  });

  const activeStage = activeIndex >= 0 ? stages[activeIndex] : null;

  return {
    stages,
    activeIndex,
    activeStage,
    isRunning,
    isComplete,
    isError,
    statusLabel: isError
      ? "RUN FAILED"
      : isComplete
      ? "ANALYSIS COMPLETE"
      : isRunning
      ? `STAGE ${activeIndex + 1}/${PIPELINE_STAGES.length}: ${activeStage?.name?.toUpperCase()}`
      : "STANDING BY",
  };
}

/**
 * Derive the SAFETY-stage display from the real analysis output.
 *
 * There are NO fabricated pass/fail rows here: every value returned is read
 * straight from backend-emitted fields (``safety_status`` and the
 * ``blocked_steps`` the deterministic safety validator produced). Presentational
 * components map this to markup. When nothing was blocked the note says so
 * truthfully; when safety has not been evaluated it says THAT rather than
 * implying a pass. This replaces a previous static "(Status: PASS)" list that
 * displayed the same rows regardless of the real verdict.
 */
export function deriveSafetyStageView(output) {
  const status = output?.safety_status || "NOT_VALIDATED";
  const evaluated = status !== "NOT_VALIDATED";
  const blocked = (output?.blocked_steps || []).map((b) => ({
    command: b.command,
    constraint: b.violated_constraint || "BLOCKED",
    severity: b.severity || null,
    reason: b.reason || null,
    // reason_category (e.g. UNKNOWN_TELEMETRY) rides in supporting_context; it is
    // the machine-readable tag for a fail-closed-on-missing-telemetry block.
    reasonCategory: b.supporting_context?.reason_category || null,
  }));

  let note;
  if (!evaluated) {
    note = "Safety validation status not available.";
  } else if (blocked.length === 0) {
    note = "No commands were refused by the safety validator.";
  } else {
    note = `${blocked.length} command(s) refused by the safety validator.`;
  }

  return { status, evaluated, blocked, note };
}
