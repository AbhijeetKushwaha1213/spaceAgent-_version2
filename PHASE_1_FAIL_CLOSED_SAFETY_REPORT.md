# PHASE 1 — FAIL-CLOSED SAFETY HARDENING REPORT

**Scope:** SENTINEL command-safety validator (backend deterministic gate) and the
frontend surfaces that report its verdict.
**Objective (single):** eliminate the **RED-01** flaw — *authorizing a
safety-critical recovery command when the telemetry that command's precondition
depends on is entirely absent from the crash dump* — by converting that case from
"UNKNOWN telemetry → potentially permissive" to "UNKNOWN safety-critical
precondition → **FAIL-CLOSED (block)**."
**Date:** 2026-08-26.
**Status:** Phase 1 changes implemented and verified within the boundaries stated
in §12. This report does **not** claim production-readiness; it states exactly
what was changed, why, and what was and was not verified.

---

## 0. THE FORMAL INVARIANT

> **NO SAFETY-CRITICAL COMMAND MAY BE AUTHORIZED WHEN ANY REQUIRED TELEMETRY OR
> PRECONDITION IS UNKNOWN.**

Precisely:

- A command carries a set of **required preconditions** (positive conditions that
  must hold) and **prohibited hazards** (conditions that must not hold).
- Each condition evaluates to one of three states against the supplied telemetry:
  `SATISFIED`, `VIOLATED`, or `UNKNOWN`, where **`UNKNOWN ⟺ the channel the
  condition depends on is entirely absent from the dump`** (as opposed to present
  but out-of-range, which is `VIOLATED`).
- If a **required precondition** is `UNKNOWN` **and** the command's severity is in
  `_FAIL_CLOSED_SEVERITIES` (`CRITICAL` or `HIGH`), the command is **BLOCKED**
  with reason code `MISSING_PRECONDITION`. Absent evidence is never treated as a
  satisfied precondition.
- Nothing the LLM reports about itself — risk label, confidence, or
  `requires_human_review` — can lift this block. The verdict is produced by the
  deterministic gate, downstream of the model, and overwrites the model's
  self-assessment.

The asymmetry (why this is *risk-aware* and not "block on every UNKNOWN") is
documented in §3.

---

## 1. THE RED-01 FLAW (BEFORE)

Before this change, the condition evaluator collapsed to a two-state view at the
decision boundary: a required precondition was treated as an obstacle only when it
could be positively shown to be *violated*. When the underlying channel was simply
**not present** in the dump, the evaluator did not raise a violation, and the
command flowed through to authorization as though the hazard were absent.

Concrete failure (now covered by clean-room Scenario B and adversarial tests):

- Command `CMD_SUN_ACQUISITION` requires `GYRO_DATA_VALID`.
- Telemetry dump contains `BATTERY_SOC` and `TEMP_C` but **no gyro channel at
  all**.
- The LLM proposes the command with `risk=LOW`, `confidence=0.99`,
  `requires_human_review=False`.
- **Old behavior:** authorized. The absence of gyro data was indistinguishable
  from "gyro fine."
- **New behavior:** BLOCKED, `MISSING_PRECONDITION`, plan emptied, human review
  forced — independent of every reassuring thing the LLM said.

This was a fail-*open* hazard: the most dangerous possible default for a recovery
system, because a truncated / lossy / partially-corrupted dump would silently
widen the set of "authorizable" commands.

---

## 2. THE FIX (AFTER) — WHERE AND HOW

The fix is implemented at the **smallest safety boundary**: the deterministic
condition gate itself, not the caller and not the LLM prompt.

- **Single live gate:** `evaluate_declared_conditions(step, ctx)` in
  `sentinel/backend/app/agent/safety.py`. It is the one place per-command
  declared conditions are evaluated. It is called by `validate_recovery_plan`,
  whose result is applied to the model output by `apply_validation_to_output`.
- **Tri-state evaluation:** condition evaluators in
  `sentinel/backend/app/validation/conditions.py` return
  `(ConditionState, supporting_context)`; `UNKNOWN` is returned precisely when the
  depended-on channel is absent.
- **The fail-closed rule (aggregate gate):** when a **required** condition returns
  `UNKNOWN`, the gate calls `_missing_precondition_violation(...)`. That helper
  returns `None` (no block) when the underlying condition's severity is **not** in
  `_FAIL_CLOSED_SEVERITIES`; otherwise it returns a `ConstraintViolation` with:
  - `code = MISSING_PRECONDITION`
  - a human-readable `reason` stating the required precondition is UNKNOWN because
    the telemetry it depends on is absent, "blocked fail-closed — absent evidence
    is not treated as a satisfied precondition,"
  - `supporting_context` enriched with `telemetry_state = "UNKNOWN"`,
    `reason_category = "UNKNOWN_TELEMETRY"`, and `underlying_condition_code`.
- **The same rule at the named-check body:** the shared per-condition path
  (`_check_single_condition`) applies the identical UNKNOWN→fail-closed logic, so
  a command routed through a named check cannot escape it.
- **Prohibited hazards are handled asymmetrically** (see §3): an `UNKNOWN`
  prohibited hazard stays permissive by design; only a `SATISFIED` (i.e.,
  positively present) hazard blocks.

No LLM-facing prompt text was relied on for safety. The model's proposed plan is
input to the gate, never an authority over it.

---

## 3. THE DETERMINISTIC POLICY — RISK-AWARE ASYMMETRIC FAIL-CLOSED

The instruction was explicit: **do not globally convert every UNKNOWN to
INVALID.** Doing so would over-block advisory/observation commands and make the
system refuse to act in exactly the degraded-telemetry situations it exists to
handle. The policy is therefore asymmetric and severity-gated:

| Condition role | Telemetry state | Command severity | Verdict | Reason code |
|---|---|---|---|---|
| Required precondition | `SATISFIED` | any | allow (this condition) | — |
| Required precondition | `VIOLATED` (present-but-bad) | any | **BLOCK** | condition's own code (e.g. `GYRO_HEALTH_PREREQUISITE`, `COMMS_LOCK_REBOOT`) |
| Required precondition | **`UNKNOWN` (channel absent)** | **CRITICAL / HIGH** | **BLOCK (fail-closed)** | **`MISSING_PRECONDITION`** (+ `reason_category=UNKNOWN_TELEMETRY`) |
| Required precondition | `UNKNOWN` (channel absent) | LOW / advisory | non-authorizing, not blocked | — (no emitter) |
| Prohibited hazard | `SATISFIED` (hazard present) | any | **BLOCK** | condition's own code |
| Prohibited hazard | `UNKNOWN` (channel absent) | any | stays permissive (by design) | — |

Rationale for the asymmetry:

- **Required precondition + UNKNOWN → block for safety-critical:** the command
  *asserts* it needs a guarantee; if we cannot see the evidence for that
  guarantee, we must not pretend it holds.
- **Prohibited hazard + UNKNOWN → permissive:** blocking here would mean "we can't
  see the hazard channel, therefore assume the hazard is present, therefore refuse
  everything" — which would brick recovery on any partial dump. The hazard must be
  *positively observed* to block.
- **Present-but-bad keeps its distinct, pre-existing code** so operators can tell
  "telemetry says NO" apart from "telemetry is missing."

`_FAIL_CLOSED_SEVERITIES = frozenset({BlockSeverity.CRITICAL, BlockSeverity.HIGH})`
(`safety.py:289`). The severity of a `MISSING_PRECONDITION` block is inherited
from the underlying required condition, so a LOW-risk advisory command with a
missing optional input is *not* escalated to a hard block.

---

## 4. REASON-CODE VOCABULARY (EMITTED vs RESERVED)

Defined in `safety.py:275–283`. Every block carries a machine-readable code so the
verdict is auditable and the frontend can render it without guessing.

**Emitted today:**

| Code | Meaning | How it surfaces |
|---|---|---|
| `MISSING_PRECONDITION` | required-precondition telemetry absent (UNKNOWN) on a CRITICAL/HIGH command | `violated_constraint`; `supporting_context.reason_category = UNKNOWN_TELEMETRY` |
| `UNKNOWN_TELEMETRY` | category tag for the above (not a standalone `violated_constraint`) | carried inside `supporting_context.reason_category` |
| `INVALID_TELEMETRY` | present-but-malformed telemetry | reported today via the condition's own code (e.g. `GYRO_HEALTH_PREREQUISITE`); the constant exists as the category name |
| `NOT_IN_REGISTRY` | command not on the deterministic whitelist (spec alias: `COMMAND_NOT_WHITELISTED`) | `violated_constraint` |

**Reserved (constant defined, no emitter yet — explicitly out of Phase-1 scope):**
`STALE_TELEMETRY`, `CONTRADICTORY_TELEMETRY`, `TOXIC_COMMAND_CONFLICT`,
`PHYSICS_REFUTED`. These are named so future phases have a stable vocabulary; this
report does **not** claim they are enforced.

---

## 5. THE GATE IS THE ONLY DOOR — BYPASS AUDIT (STEP 6)

Invariant checked: **no command reaches authorization without passing the
deterministic safety gate.**

- All command authorization flows through `validate_recovery_plan → (per step)
  evaluate_declared_conditions → apply_validation_to_output`. The registry
  whitelist check (`NOT_IN_REGISTRY`) and the per-command condition gate are both
  inside this path.
- `apply_validation_to_output` is what rewrites `recovery_plan`, `blocked_steps`,
  `safety_status`, and `requires_human_review` on the output object. The LLM's own
  `requires_human_review=False` cannot survive a block, because the gate's result
  overwrites it (clean-room Scenario B verifies this).
- Backend source in this path was **unchanged since the STEP 6 audit** in the
  prior session; the audit conclusion (single-door) therefore still holds for the
  code as shipped in this report.

---

## 6. FLAG — ADJACENT FAIL-OPEN HOLE FOUND AND CLOSED (`_eval_comms_lock`)

**This went slightly beyond the strict "UNKNOWN" scope and must be surfaced.**

While mapping the condition evaluators, `_eval_comms_lock` in `conditions.py` was
found to treat a **present-but-malformed** transponder-lock reading as if a lock
were confirmed in one branch — a *symmetric fail-open* hole distinct from the
absent-channel RED-01 case. A `None` / `NaN` / `"NaN"` / `""` value means "we
cannot confirm a lock," which for a lock *precondition* must be treated as
**not confirmed**.

Closure (present in this change set):

```python
support = {"transponder_lock": value}
if is_value_nan_or_missing(value):
    # Present but malformed (None / NaN / "NaN" / ""): cannot confirm a lock.
    return ConditionState.VIOLATED, support
if value in _NO_LOCK_VALUES:
    return ConditionState.VIOLATED, support
return ConditionState.SATISFIED, support
```

This makes "lock must be *positively* confirmed" hold for both the absent-channel
path (UNKNOWN → `MISSING_PRECONDITION` when required + critical) and the
malformed-value path (VIOLATED). It only ever **adds** a block; it cannot
authorize anything that was previously blocked.

---

## 7. BACKEND TEST UPDATES — AND WHY EACH IS INTENTIONALLY CORRECT

Per STEP 7: tests were updated **only** where the old expectation encoded the
now-fixed permissive-UNKNOWN behavior, and the change is documented here. No test
was disabled or weakened to make numbers pass. Three existing test files changed:

- **`tests/test_phase1_blocked_plans.py`** — previously asserted that a command
  with an absent required-precondition channel was *authorized* (encoding RED-01).
  Updated to assert the fail-closed outcome (`MISSING_PRECONDITION`, plan emptied
  / partially blocked, human review). This is the direct behavioral inversion the
  phase exists to produce.
- **`tests/test_phase16_llm_baseline.py`** — a baseline case whose fixture dump
  omitted a channel that a proposed critical command required; the old assertion
  expected authorization. Updated to expect the block, because the new verdict is
  the correct one for that fixture.
- **`tests/test_phase23_router_orchestrator.py`** — small assertion adjustment
  (10-line diff) where an end-to-end expectation flowed from the same
  now-blocked command; updated to match the corrected downstream state.

All three changes move expectations *toward stricter safety*, matching the
invariant in §0. None relax an existing safety assertion.

---

## 8. FLAG — PRODUCT-VISIBLE BEHAVIOR CHANGE (OBC_WATCHDOG SCENARIO)

**Operators/demo will see a different outcome; surfacing explicitly.**

The `OBC_WATCHDOG_OVERFLOW` scenario previously produced a clean 4-step recovery
ending in `CMD_OBC_CONTROLLED_REBOOT`. That reboot declares a required
`COMMS_LOCK_CONFIRMED` precondition. In the scenario's dump the transponder-lock
channel is **absent**, so under the new policy the reboot is **fail-closed
blocked** (`MISSING_PRECONDITION`) while the observation-only steps remain
authorized — i.e. `safety_status = PARTIALLY_BLOCKED` with **mandatory human
review**, instead of a clean authorization.

This is the intended, correct behavior (a reboot should not be auto-authorized
when the system cannot confirm the comms lock it depends on), but it is a visible
change to a previously "green" path and should not surprise anyone reviewing the
demo.

---

## 9. FRONTEND SAFETY VISIBILITY (STEP 9)

Guiding constraint: **do not create fake safety messages.** Backend correctness was
established first; the frontend only *reports* real emitted fields.

**9a. FLAG — fabricated always-"PASS" safety rows removed (real fake-safety
defect).** `PipelineDemoView.jsx` STAGE 8 previously hard-coded three static
`<li>` rows that always displayed `(Status: PASS)` with thresholds
(`SOC > 40%`, `< 0.5 deg/s`, `T > -20°C`) **regardless of the actual verdict** —
it would show green "PASS" even when the safety validator had blocked commands.
This is exactly the class of fabricated safety message the constraint forbids. It
was replaced with a data-driven block that renders the real `safety_status` and
the real `blocked_steps` (command, `violated_constraint`, `severity`,
`reason_category` tag such as `UNKNOWN_TELEMETRY`, and reason), or a truthful
"nothing was refused" / "not available" note.

**9b. GAP — `PARTIALLY_BLOCKED` was invisible on the pipeline overview.** The
pipeline state machine (`pipelineStateMachine.js`) refined the safety stage for
`BLOCKED` but not for `PARTIALLY_BLOCKED` — Phase 1's *most common* fail-closed
outcome — so a partial block rendered as a clean green completed stage. Added a
`PARTIALLY_BLOCKED` branch that marks the safety stage `blocked`, badges it
`PARTIALLY BLOCKED`, and derives its detail line **only** from real emitted fields
(count of blocked commands, the distinct constraint codes, and the authorized
count), ending in "Human review required."

**9c. Shared pure helper.** A single tested helper `deriveSafetyStageView(output)`
maps backend output to `{status, evaluated, blocked[], note}` with no fabricated
rows; the presentational component maps over it. When safety was not evaluated the
note says exactly that rather than implying a pass.

**Frontend verification:** `pipelineStateMachine.test.js` — **17 passed / 17
total** (fresh run), including a `PARTIALLY_BLOCKED` case asserting the safety
stage is visibly blocked (not green), the detail contains `MISSING_PRECONDITION`
and the authorized count, the recovery stage shows `HUMAN REVIEW`; plus five
`deriveSafetyStageView` cases (validated-no-blocks, partially-blocked,
fully-blocked, null/`NOT_VALIDATED`, and missing-`violated_constraint` fallback).

---

## 10. ADVERSARIAL TESTS (STEP 5)

`tests/test_phase1_failclosed_adversarial.py` — **19 passed, 4 skipped** (fresh
run). The 19 cover, among others: confident-LLM-with-missing-telemetry cannot buy
authorization; non-whitelisted command blocked even with perfect telemetry; mixed
plan isolates the unsafe step; telemetry withdrawn between diagnosis and
authorization is re-blocked (no cached trust); LOW-risk label cannot downgrade a
critical missing precondition.

The **4 skips are documented, not silent**: they correspond to the reserved codes
with no emitter yet (§4) — `STALE_TELEMETRY`, `CONTRADICTORY_TELEMETRY`,
`TOXIC_COMMAND_CONFLICT`, `PHYSICS_REFUTED` categories — which are explicitly out
of Phase-1 scope. They are written as skips so the intended future coverage is
visible rather than forgotten.

---

## 11. CLEAN-ROOM VERIFICATION (STEP 8)

`scripts/phase1_failclosed_verification.py` is a standalone proof independent of
the test suite. It never calls a real LLM; it constructs the *parsed* LLM output
directly (from cooperative to adversarial) and runs each through the **real** gate
(`validate_recovery_plan → apply_validation_to_output`). Fresh run: **5/5
scenarios PASS, exit 0.**

- **A** — cooperative LLM + complete telemetry → **authorized** (fail-closed does
  not over-block the good path).
- **B** — LOW-risk / 0.99-confidence / no-review LLM + missing gyro channel →
  **BLOCKED** `MISSING_PRECONDITION`, plan emptied, human review forced.
- **C** — adversarial LLM smuggling `CMD_SELF_DESTRUCT` (not whitelisted) →
  **BLOCKED** `NOT_IN_REGISTRY` even with perfect telemetry.
- **D** — mixed plan → **PARTIALLY_BLOCKED**: observation step authorized, unsafe
  step isolated and blocked.
- **E** — lock reading present at diagnosis but gone at authorization → validated
  then **re-BLOCKED**; the gate holds no cached verdict.

---

## 12. VERIFICATION EVIDENCE AND EXPLICIT BOUNDARIES

**Evidence (fresh this session):**

| Proof | Result |
|---|---|
| Full backend suite (`pytest -q`) | **1506 passed, 2627 subtests passed, 6 skipped** |
| — pre-existing failure | `test_phase3_contract.py::...::test_artifacts_are_not_stale` (1) |
| — environmental errors | `test_phase11_sovereign_llm` (2) + `test_phase12_evaluation` (2) local-LLM socket-bind |
| Adversarial suite | 19 passed, 4 documented skips |
| Clean-room script | 5/5, exit 0 |
| Frontend Jest | 17 passed / 17 total |

**The 1 failure and 4 errors are pre-existing / environmental and outside this
change set:**

- **Contract-artifact staleness** (`test_phase3_contract`): the committed
  JSON-Schema contract artifacts drift from the pydantic models under this local
  environment ("Run: `export_contracts.py`"). **`models.py` is not in this change
  set's diff**, so this drift is independent of the Phase-1 changes. Not
  regenerated here because contract export is out of Phase-1 scope and would touch
  unrelated artifacts.
- **phase11 / phase12 local-LLM errors:** these bind a local socket for a
  local-endpoint test; the sandbox denies `socket.bind`. Environmental, not a
  code defect introduced here.

**What was NOT verified (honest boundaries):**

- **No full browser E2E** of the `PARTIALLY_BLOCKED` safety stage was run. Reaching
  it live requires a running backend + LLM, and `@testing-library` is not
  installed in the frontend. Frontend verification is: fresh Babel compile of both
  edited files, plus 17 Jest tests that exercise the exact `PARTIALLY_BLOCKED` and
  `NOT_VALIDATED` data paths, plus the JSX being a direct map over the tested pure
  helper. This is unit-level proof of the data mapping, not a rendered-pixel proof.
- **Pre-existing dead reference left in place:** the `BLOCKED` branch in
  `pipelineStateMachine.js` reads `output?.safety_reason`, a field the backend does
  not emit; this predates Phase 1 and is out of scope. It is noted, not fixed, to
  avoid scope creep. (`safety_reason` appears in a *test fixture* for the legacy
  BLOCKED path, but is not part of the emitted contract.)

---

## 13. SECURITY / REGRESSION CONCLUSION AND PHASE BOUNDARY

**Net security assessment:** every change in this set either **adds a block**
(required-precondition UNKNOWN on a critical command; malformed comms-lock) or
**reports an existing block more truthfully** (frontend). The asymmetric policy
was chosen specifically so that the added strictness does not brick recovery on
partial dumps (prohibited-hazard UNKNOWN stays permissive; LOW/advisory missing
inputs are non-authorizing but not hard-blocked). No existing safety or security
control was removed or weakened; no test was disabled; the LLM was not granted any
new authority. The one fail-*open* hole found adjacent to RED-01
(`_eval_comms_lock`) was closed in the same direction.

**Residual / out-of-scope (explicitly NOT done — Phase boundary respected):**

- No Phase 2 work: no CCSDS streaming, no 3-axis physics, no LLM-router redesign.
- Reserved reason codes (`STALE_TELEMETRY`, `CONTRADICTORY_TELEMETRY`,
  `TOXIC_COMMAND_CONFLICT`, `PHYSICS_REFUTED`) are named but not enforced.
- Contract-artifact regeneration and the phase11/12 socket-bind environment are
  untouched.

**Claim discipline:** this report does not assert "fully verified," "production
ready," "zero issues," or "all tests pass." It asserts the specific, evidenced
result above: the RED-01 fail-open path is closed at the deterministic gate, the
fix is covered by adversarial + clean-room + unit proofs, and the remaining suite
failures are pre-existing/environmental and unrelated to this change set.

---

*Preceding artifact: `PHASE_1_PRECHANGE_SAFETY_MAP.md` (STEP 1 read-only map).
Companion proofs: `sentinel/backend/tests/test_phase1_failclosed_adversarial.py`,
`sentinel/backend/scripts/phase1_failclosed_verification.py`.*
