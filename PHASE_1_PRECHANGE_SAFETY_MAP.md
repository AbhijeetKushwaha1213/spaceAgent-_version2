# PHASE 1 — Pre-Change Safety Map (STEP 1, read-only inspection)

**Objective of Phase 1:** eliminate RED-01 — the flaw whereby a safety-critical
recovery command can be authorized while its required telemetry is UNKNOWN
(missing / absent / not present in the dump). No code has been modified to
produce this document; it is the evidence base for STEP 2–3.

**Scope note.** This map records what the code does *today*, with exact
`file:line` anchors traced first-hand. Where the task's vocabulary (STALE,
CONTRADICTORY, "toxic conflict", "physics REFUTED → command block") does **not**
map onto an existing mechanism, that is stated explicitly rather than invented.

---

## 0. The authorization path, end to end (traced first-hand)

```
telemetry (crash dump dict)
  │
  ├─ app/validation/conditions.py
  │     extractors: get_battery_soc / get_gyro_rate /
  │                 get_transponder_lock / get_max_temperature
  │        → _eval_battery / _eval_gyro / _eval_comms_lock / _eval_thermal
  │        → ConditionState ∈ {SATISFIED, VIOLATED, UNKNOWN}      (conditions.py:89)
  │        → evaluate_condition(condition, ctx)                    (conditions.py:456)
  │
  ├─ app/agent/safety.py   ── THE SINGLE LIVE GATE ──
  │     validate_recovery_plan(sentinel_output, crash_dump_context) (safety.py:714)
  │        Check 1  CMD_ prefix                                     (safety.py:744)
  │        Check 2  registry membership (NOT_IN_REGISTRY)           (safety.py:758)
  │        Check 3  enabled? (COMMAND_DISABLED)                     (safety.py:774)
  │        Check 4  evaluate_declared_conditions(step, ctx)         (safety.py:792 → 517)
  │        Check 5  escalation (HIGH-risk → human review, NON-blocking) (safety.py:804)
  │     apply_validation_to_output(...)                             (safety.py:861)
  │        → empty recovery_plan + SafetyStatus.BLOCKED when all blocked
  │
  ├─ app/agent/agent.py   ── callers of the gate ──
  │     analyze_crash_dump()          gate at 1137 (`if not skip_safety:`)
  │     analyze_crash_dump_stream()   gate at ~2004 (ALWAYS runs; NO skip_safety param)
  │
  └─ app/main.py   ── the API ──
        /analyze SSE endpoints call analyze_crash_dump_stream(payload)
        at 767 / 844 / 925 — never pass skip_safety.
```

**The single deterministic constraint gate is
`safety.evaluate_declared_conditions` (safety.py:517), reached from
`validate_recovery_plan` Check 4 (safety.py:792).** The four named
`check_*` functions and the `_BLOCKING_CHECKS` list (safety.py:680) are **defined
but never iterated** by `validate_recovery_plan`; they survive only as
direct-call helpers for tests. Any fail-closed change must land in the live path,
not in `_BLOCKING_CHECKS`.

---

## 1. Current UNKNOWN behavior

**Policy, as documented in the code:** *UNKNOWN never blocks.*
- `command_registry.py:23-39` — "UNKNOWN never blocks… absence of evidence is
  treated as absence of the hazard."
- `conditions.py:13` — "Policy: UNKNOWN NEVER BLOCKS."

**Mechanically** (`safety.py:517 evaluate_declared_conditions`):
- For a **required** precondition: the step is blocked **only if** the predicate
  is `VIOLATED` (safety.py:540). `UNKNOWN` → not blocked.
- For a **prohibited** hazard: the step is blocked **only if** the hazard is
  `SATISFIED` (safety.py:544). The hazard is derived by inverting its positive
  counterpart (`conditions.py:424 _INVERT`), and `UNKNOWN` inverts to `UNKNOWN`,
  which is neither SATISFIED nor VIOLATED → not blocked.

**When is a predicate UNKNOWN?** Only when the channel is **entirely absent**
from the dump:
- `_eval_battery` → UNKNOWN iff `get_battery_soc` returns `None`
  (conditions.py:364-368). A present-but-NaN SoC keeps its last *usable* magnitude
  via `_latest_usable_number` (conditions.py:199-220), so a known-bad value is
  **not** erased to UNKNOWN.
- `_eval_gyro` → UNKNOWN iff `get_gyro_rate` returns `"NOT_FOUND"`
  (conditions.py:375-378). A present-but-NaN/None gyro → **VIOLATED** (blocks).
- `_eval_comms_lock` → UNKNOWN iff `get_transponder_lock` returns `"NOT_FOUND"`
  (conditions.py:385-388). A present "no-lock" value → **VIOLATED** (blocks).
- `_eval_thermal` → UNKNOWN iff `get_max_temperature` returns `None`
  (conditions.py:395-398).

**Net:** for the four safety predicates, `UNKNOWN ⟺ "the channel does not appear
anywhere in the dump."** A present-but-malformed reading already resolves to
VIOLATED for gyro/comms-lock, and to a retained known-bad magnitude for
battery/thermal. **The RED-01 gap is specifically: a command that declares a
dependency on a channel that is wholly absent is authorized as if the hazard were
absent.**

---

## 2. Which commands depend on telemetry

"Depends on telemetry" = the registry entry declares any
`required_preconditions` or `prohibited_conditions`. Derived from
`command_registry.py` (SECTION 3) and the derived sets in `safety.py:449-471`.

| Declared dependency | Predicate | Commands (representative) |
|---|---|---|
| Gyro rate valid | `GYRO_DATA_VALID` (required) | `CMD_ATTITUDE_REACQUISITION`, `CMD_REACTION_WHEEL_DESAT`, `CMD_REACTION_WHEEL_RESET`, `CMD_REACTION_WHEEL_SPEED_CHECK`, `CMD_SUN_ACQUISITION` |
| Comms lock confirmed | `COMMS_LOCK_CONFIRMED` (required) | `CMD_OBC_CONTROLLED_REBOOT`, `CMD_OBC_SOFT_RESET` |
| Battery ≥ floor | `BATTERY_BELOW_FLOOR` (prohibited, via `_TB`) | `CMD_ATTITUDE_REACQUISITION`, `CMD_ATTITUDE_RESET`, `CMD_REACTION_WHEEL_DESAT`, `CMD_REACTION_WHEEL_RESET`, `CMD_SUN_ACQUISITION`, `CMD_SOLAR_ARRAY_REDEPLOY`, `CMD_POWER_RESTORE`, `CMD_OBC_CONTROLLED_REBOOT`, `CMD_OBC_SOFT_RESET`, `CMD_HEATER_ENABLE`, `CMD_HEATER_ON`, `CMD_SAFE_MODE_EXIT` |
| Temp ≤ survival | `THERMAL_ABOVE_SURVIVAL` (prohibited, via `_T`/`_TB`) | the majority of actuating + read commands (all carrying `_T` or `_TB`) |

**Telemetry-INDEPENDENT (observation-only; both condition lists empty →
`CommandSpec.is_observation_only`, command_registry.py:130-138):**
all `CMD_VERIFY_*`, `CMD_HEALTH_CHECK`, `CMD_TELEMETRY_DUMP`, `CMD_TELEMETRY_CHECK`,
plus the thermal remedies / advisory commands that intentionally carry **no**
prohibition: `CMD_DISABLE_HEATER_ZONE`, `CMD_MONITOR_TEMPERATURE`,
`CMD_THERMAL_CHECK`, `CMD_THERMAL_MONITOR_CHECK`, `CMD_THERMAL_OVERRIDE_OFF`,
`CMD_HEATER_DISABLE`, `CMD_HEATER_OFF`, `CMD_BATTERY_HEATER_DISABLE`,
`CMD_POWER_SHED_NONESSENTIAL`, `CMD_SAFE_MODE_ENTRY`, `CMD_CONFIRM_COMMS_LOCK`,
`CMD_CONFIRM_GROUND_CONTACT`. These do not read as UNKNOWN because they ask no
question of telemetry — they must remain executable (they are the remedies).

---

## 3. Which commands are safety-critical

The consequence severity of each blocking condition is already classified in
`safety.py:234-247 _SEVERITY_BY_CODE`. This is the natural, non-invented spine for
a **risk-aware** fail-closed policy:

| Condition / code | Severity | Failure if executed on wrong/unknown state |
|---|---|---|
| `GYRO_HEALTH_PREREQUISITE` | **CRITICAL** | attitude actuation on bad rate data can tumble the vehicle |
| `COMMS_LOCK_REBOOT` | **CRITICAL** | OBC reboot without confirmed uplink can lose contact permanently |
| `BATTERY_FLOOR` | HIGH | deepens a power fault |
| `THERMAL_SURVIVAL` | HIGH | deepens a thermal fault |
| `INVALID_FORMAT`, `NOT_IN_REGISTRY`, `NOT_WHITELISTED` | CRITICAL | undefined behavior on the bus (already hard-blocked) |

**Safety-critical, telemetry-dependent commands** (the ones where UNKNOWN
telemetry is dangerous) are therefore exactly the **gyro-dependent** and
**comms-lock-dependent** commands (CRITICAL), followed by the **battery/thermal**
constrained set (HIGH). Per the task: CRITICAL/HIGH → UNKNOWN must BLOCK;
observation-only/advisory (LOW, no declared condition) → unaffected.

---

## 4. Where UNKNOWN can currently pass (RED-01 exposure points)

1. **`evaluate_declared_conditions` (safety.py:517-547)** — the single live gate.
   A required precondition that is UNKNOWN falls through the `is VIOLATED` test
   (540); a prohibited hazard that is UNKNOWN falls through the `is SATISFIED`
   test (544). **This is the one place the fix must land.**
2. `_check_single_condition` (safety.py:550-580) — same permissive logic, used by
   the four named `check_*` helpers. Not on the live path, but tests call it
   directly, so it must be fixed too for consistency.
3. The extractors returning the "absent" sentinel (`None` / `"NOT_FOUND"`) feed
   the UNKNOWN verdict (conditions.py). No change needed there — the verdict is
   correct; what changes is how the gate *treats* UNKNOWN for a declared
   dependency.

**Demonstrated live exposures (from existing tests that assert today's behavior):**
- `CMD_SUN_ACQUISITION` with **empty context** → `SafetyStatus.VALIDATED`
  (`test_phase1_blocked_plans.py:512 test_gyro_permissive_when_absent`). Attitude
  actuation authorized with **no gyro data at all**.
- `CMD_ATTITUDE_REACQUISITION` with **no SoC** → `VALIDATED`
  (`test_phase1_blocked_plans.py:499 test_battery_floor_permissive_when_absent`).

**Not a bypass (verified):**
- `skip_safety=True` exists (`agent.py:959,1137,1412`) but the API method
  `analyze_crash_dump_stream` has **no such parameter** and always validates;
  `app/main.py` never passes it. It is reachable only from ablation/eval tooling
  (`app/analytics/run_evaluation.py`), and when set it emits an explicit
  `NOT_VALIDATED` audit record stating "No safety claim is made"
  (agent.py:334-345). Not an authorization path in production.
- `router_orchestrator.py:165-166` also calls `validate_recovery_plan` +
  `apply_validation_to_output`; it is **not** imported by any `app/api/` route.

---

## 5. Existing safety invariants (must be preserved)

- **Registry is the single source of truth** — whitelist is *derived*
  (`safety.py:270 registry_by_subsystem`), proven by `conflicts.py:230
  check_whitelist_derived`. Do not hand-edit the whitelist.
- **Total rejection cannot masquerade as success** — all-blocked ⇒ empty
  `recovery_plan` + `SafetyStatus.BLOCKED` (safety.py:886-895), enforced by
  `SentinelOutput` invariant 6.
- **Blocked steps are structured data** (`SentinelOutput.blocked_steps` →
  `BlockedCommand`, safety.py:154-164), not prose.
- **LLM is non-authoritative** — `validate_recovery_plan` runs on the LLM's
  parsed output *after* the fact; the LLM never sees a bypass. Authority spine is
  fixed in `startup_report.py:36-38`.
- **Present-but-bad ≠ absent** — a known-bad magnitude is retained through a
  later NaN (`conditions.py:199-220`); a present NaN gyro/lock is already
  VIOLATED. The fix must not weaken these.
- **Deterministic & pure** — `safety.py` and `conditions.py` are pure Python, no
  I/O, no LLM. The fix must stay deterministic.
- **Thermal remedies are never blocked during a thermal event** — remedies
  declare no thermal prohibition (command_registry.py:417-445). The fix must not
  block a command that declares *no* dependency.

---

## 6. Existing tests covering this

| Test | File:line | Asserts today | Effect of fail-closed fix |
|---|---|---|---|
| `test_gyro_permissive_when_absent` | test_phase1_blocked_plans.py:512 | empty ctx → `VALIDATED` | **WILL FLIP** → must become BLOCKED (intended; RED-01) |
| `test_battery_floor_permissive_when_absent` | test_phase1_blocked_plans.py:499 | no SoC → `VALIDATED` | **WILL FLIP** → must become BLOCKED (intended; RED-01) |
| `test_gyro_prerequisite` | :503 | present NaN/None gyro → blocked `GYRO_HEALTH_PREREQUISITE` | unchanged (already VIOLATED) |
| `test_comms_lock_before_reboot` | :516 | present no-lock → blocked | unchanged |
| `test_comms_lock_satisfied_allows_reboot` | :525 | lock=1 → `VALIDATED` | unchanged (present & satisfied) |
| `test_battery_floor` | :494 | SoC 9.0 → blocked | unchanged |
| `test_thermal_survival` | :529 | temp 92 → blocked | unchanged |
| `test_observation_only_commands_are_never_blocked` | :535 | hostile ctx, obs-only cmds → none blocked | **must stay green** (no declared deps) |
| `test_documented_thermal_relaxations` | :554 | 6 cmds not thermally blocked | must stay green |
| `test_safety_1_valid_recovery_approved` | test_phase16_llm_baseline.py:186-189 | 4 steps VALIDATED, 0 blocked | **WILL FLIP (measured)** — step 2 `CMD_OBC_CONTROLLED_REBOOT` requires `COMMS_LOCK_CONFIRMED`=UNKNOWN (scenario 3 has no transponder channel) |
| `test_safety_2_invalid_command_blocked` | test_phase16_llm_baseline.py:215 | `validated_steps == [CMD_OBC_CONTROLLED_REBOOT]` | **WILL FLIP (measured)** — reboot's `COMMS_LOCK_CONFIRMED`=UNKNOWN → blocked; `validated_steps` becomes `[]` |
| happy-path agent plan | test_agent.py:143 | plan incl. `CMD_ATTITUDE_REACQUISITION` | **NOT COLLECTED** — `test_agent.py` is a `__main__`/`check()` script (0 pytest tests). Its `SAMPLE_CRASH_DUMP` yields gyro/battery/thermal all UNKNOWN, so it *would* flip if run directly, but it is **not** in the 1494-test suite |
| adversarial attitude block | test_phase25_adversarial_security.py:379-393 | `CMD_ATTITUDE_REACQUISITION` blocked | aligns with fail-closed (safe) |

**Measured condition states (scenario 3 / `SAMPLE_CRASH_DUMP`), run first-hand:**
- scenario 3 (`_pipeline("3")`): `battery_soc=90.0` (→ `BATTERY_BELOW_FLOOR`=VIOLATED/safe), `transponder=NOT_FOUND` (→ `COMMS_LOCK_CONFIRMED`=**UNKNOWN**), `max_temp=None` (→ `THERMAL_ABOVE_SURVIVAL`=**UNKNOWN**).
- `SAMPLE_CRASH_DUMP` (test_agent): `battery_soc=None`, `gyro_rate=NOT_FOUND`, `transponder=NOT_FOUND`, `max_temp=None` → **all UNKNOWN** (extractors do not walk its nested `pre_fault_telemetry.T_minus_*` shape).

**`tests/test_safety.py` is NOT a pytest module** — it defines `main()`/`check()`
under `if __name__ == "__main__"` (line 978) and has **zero** `test_`-prefixed
functions, so pytest collects nothing from it. Its permissive-UNKNOWN assertions
(its "TEST 25/26") only run on direct execution and are **not** part of the
collected suite.

**Collected-suite baseline (verified this session):** `pytest --collect-only`
reports **1494 tests collected**, consistent with the stated Phase 0 baseline of
1486 passed + 8 skipped. (Full-run pass count to be re-verified at STEP 7 in a
recon-disabled, CI-matching env per the known `.env` contamination.)

---

## 7. Exact files / functions requiring modification

**Primary (the live gate):**
- `app/agent/safety.py` → **`evaluate_declared_conditions` (line 517)**: when a
  declared **required** precondition evaluates to `UNKNOWN`, or a declared
  **prohibited** hazard's underlying quantity is `UNKNOWN`, BLOCK the step with a
  new machine-readable reason code — *for safety-critical (CRITICAL/HIGH)
  conditions*. This is the smallest change at the correct boundary.
- `app/agent/safety.py` → **`_check_single_condition` (line 550)**: mirror the
  same treatment so the direct-call helpers agree with the live gate.
- `app/agent/safety.py` → **`_SEVERITY_BY_CODE` (line 234)** and a new reason-code
  table: add the new codes (below) and their severities.

**Reason codes to add** (STEP 4), reusing existing style:
- `UNKNOWN_TELEMETRY` / `MISSING_PRECONDITION` — directly implementable now
  (absent channel for a declared safety-critical condition).
- `INVALID_TELEMETRY` — partially exists (present-NaN gyro/lock already VIOLATED);
  may be surfaced explicitly.
- `COMMAND_NOT_WHITELISTED` — already exists as `NOT_IN_REGISTRY` /
  `NOT_WHITELISTED` (safety.py:236-238); keep the existing code, document the
  alias.
- `STALE_TELEMETRY`, `CONTRADICTORY_TELEMETRY`, `TOXIC_COMMAND_CONFLICT`,
  `PHYSICS_REFUTED` — **no current mechanism** (see §8). STEP 2 must decide
  whether these are in-scope for Phase 1 or deferred; do not fabricate detectors
  that always no-op.

**Supporting:**
- `app/validation/conditions.py` — likely a small helper to distinguish "declared
  dependency is UNKNOWN" cleanly; extractors themselves need no change.
- Tests: update the two intended-flip tests (§6) **with documentation**; add the
  16 adversarial tests (STEP 5).

**Explicitly out of scope for the code change:** `command_registry.py` table
entries (no new commands), `conflicts.py` (build-time checker), physics.py,
reconciliation, RAG, prompts.

---

## 8. Backward-compat risks

1. **Intended flips (the fix itself).** Any plan validating a
   gyro/comms-lock/battery/thermal-dependent command **without** that channel in
   the dump flips `VALIDATED → BLOCKED`. **Measured** confirmed cases: the two
   `*_permissive_when_absent` tests (§6), plus phase16 `test_safety_1` and
   `test_safety_2` (both currently PASS; both flip because scenario 3 carries no
   transponder-lock channel → `COMMS_LOCK_CONFIRMED`=UNKNOWN on
   `CMD_OBC_CONTROLLED_REBOOT`). These are corrected, not regressed — each must be
   updated and documented per STEP 7.
2. **`test_agent.py` is a script, not a regression.** Confirmed
   `pytest --collect-only` → "no tests collected". Its plan would flip if run
   directly (all channels UNKNOWN), but it does not affect the 1494-test suite
   count. Note it in STEP 7 as a script whose behavior changes; do not silently
   leave it asserting the old 4-step expectation.

3. **⚠ PIVOTAL POLICY FORK — required-precondition vs prohibited-hazard UNKNOWN.**
   This is the single biggest design decision and it dictates blast radius:
   - **Required-precondition UNKNOWN** (enabling evidence absent) is scoped to
     exactly the **gyro-dependent** (5 cmds) and **comms-lock-dependent** (2 cmds)
     commands — CRITICAL severity. Blocking these on UNKNOWN is the *literal*
     RED-01 fix, surgical, small blast radius.
   - **Prohibited-hazard UNKNOWN** is the opposite: **nearly every actuating
     command prohibits `THERMAL_ABOVE_SURVIVAL`** (`_T`/`_TB`), and a max-temp
     channel is **absent in most dumps** (measured: scenario 3 and
     `SAMPLE_CRASH_DUMP` both have `max_temp=None`). If prohibited-hazard-UNKNOWN
     blocks at HIGH, **almost every recovery plan in almost every scenario newly
     blocks** — a massive, arguably over-broad change that would break most golden
     paths and likely force temperature telemetry to be added to every scenario.
   - The task text ("CRITICAL/HIGH → UNKNOWN → BLOCK") and the constraints
     ("smallest safe change", "do not break working functionality", "do NOT
     globally convert every UNKNOWN to INVALID") are in direct tension **only** on
     the prohibited-hazard-UNKNOWN case. STEP 2 must resolve this explicitly (see
     the STEP 2 recommendation accompanying this map).

3. **Scenario / demo / integration paths.** Preset scenarios, the demo cache
   (`data/demo_cache/*.json`), and end-to-end tests may propose telemetry-dependent
   commands. If a scenario's dump legitimately contains the channel, it stays
   green; if not, it correctly blocks. The demo cache is generated with an **empty
   context** (`generate_demo_cache.py:381 validate_recovery_plan(raw_output, {})`)
   — **this is the single highest-risk regression surface**: every
   telemetry-dependent command in a cached plan would newly block. Must be
   re-generated/re-checked in STEP 7, and the demo path re-run.
4. **`is_safe` / `safety_status` consumers.** More plans will carry `BLOCKED` /
   `PARTIALLY_BLOCKED`. Frontend (STEP 9) and any status assertion that assumed
   `VALIDATED` on partial dumps must read the real backend status.
5. **No mechanism yet for STALE / CONTRADICTORY / TOXIC / PHYSICS_REFUTED.**
   - *Staleness*: rows carry `relative_time_s` (`conditions.py:159 _time_rank`) but
     there is **no freshness threshold** — "stale" is not currently detectable.
   - *Contradiction*: no cross-channel contradiction detector exists.
   - *Toxic command-pair conflict*: **no runtime detector** anywhere in `app/`
     (`conflicts.py` is a build-time registry-consistency checker, not a
     command-pair blocker).
   - *Physics REFUTED → command block*: physics (`app/validation/physics.py`)
     validates **hypotheses** (verdicts VALID/INVALID/UNCERTAIN) and **never**
     calls `validate_recovery_plan` or blocks a command. There is no
     physics→command authorization edge today.
   Introducing any of these is **new capability**, not a hardening of an existing
   path. STEP 2 must scope them deliberately; the guaranteed-correct, minimal
   RED-01 fix is UNKNOWN(absent)-for-a-declared-critical-condition → BLOCK.

---

## STEP 1 conclusion

RED-01 is real, singular, and well-localized: the permissive treatment of
UNKNOWN in **`safety.evaluate_declared_conditions` (safety.py:517)**, fed by the
tri-state verdicts in `conditions.py`. The fix is a risk-aware, deterministic
change at that one gate (plus its `_check_single_condition` sibling), using the
existing `_SEVERITY_BY_CODE` classification to decide which UNKNOWNs must block.
It is reachable on every API path (no production bypass), the LLM cannot override
it, and the blast radius is a small, enumerable set of tests plus the
empty-context demo-cache generator. STEP 2 will define the explicit policy;
STEP 3 will implement it.
