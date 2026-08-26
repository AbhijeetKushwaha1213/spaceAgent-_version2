# PHASE 2 — Live Telemetry Ingestion Hardening — Report

**Status:** implemented and verified as an engineering layer, **not** flight‑ready and **not** wired to a live endpoint.
**Scope boundary:** upgrade SENTINEL from batch JSON telemetry to a real *streaming* ingestion front‑end **while preserving the existing deterministic analysis pipeline unchanged.** No redesign, no FDIR logic duplicated, no Phase‑1 change.
**Date:** 2026‑08‑26

> Reporting discipline for this document: no claim of "production ready", "flight‑ready", "fully verified", or "all tests pass" is made. Every capability below is tied to code and to an observable test/counter, and every limitation is stated explicitly (§9, §11, §12).

---

## 1. Executive summary

Phase 2 adds a streaming telemetry ingestion layer in four new modules under `sentinel/backend/app/ingest/` plus two new test files. Raw octets are deframed and parsed as **real CCSDS Space Packets**, decoded under an **explicitly synthetic** self‑describing mission layout, ordered/deduplicated through a **bounded buffer**, and emitted as the *exact* canonical `pre_fault_telemetry_window` crash‑dump shape the existing pipeline already consumes. The existing detection / reconciliation / physics / RAG / agent / safety code is untouched.

The core safety property is upheld and tested end‑to‑end through the **real, unmodified Phase‑1 safety gate**: a telemetry‑ingestion failure (malformed value, truncation, unsupported type, duplicate, too‑late, overflow, disconnect, garbage) is **never** turned into healthy telemetry — the affected channel becomes absent‑or‑unusable, resolves to UNKNOWN downstream, and the fail‑closed gate stays closed.

During integration testing this layer **caught and fixed a real fail‑closed defect in its own adapter** (§10) — a malformed value was leaking to the gate as a healthy reading. That is reported here rather than hidden, per the "every capability backed by evidence / don't look more real than you are" mandate.

**What is trustworthy today:** the CCSDS SPP primary‑header parser, the bounded ordering buffer, the fail‑closed representation of ingestion failures, and the data‑contract connection into the existing pipeline. **What is not:** transport, the mission packet layout, real timestamps, the CCSDS stack above/below SPP, and live runtime wiring (§11–§12).

---

## 2. What was built (component inventory)

| File | Role | New/changed |
|---|---|---|
| `app/ingest/ccsds.py` | CCSDS 133.0‑B Space Packet Protocol primary‑header parser + self‑delimiting deframer; test encoder `build_space_packet`. | new |
| `app/ingest/mission_decode.py` | **Synthetic** mission decoder for `SENTINEL_SYNTHETIC_TM_LAYOUT_V1`; validates channel *names* against the real dictionary; test encoder `build_synthetic_tm_payload`. | new |
| `app/ingest/stream_buffer.py` | Bounded, deterministic per‑APID dedup + reorder buffer; refuse‑and‑report overflow. | new |
| `app/ingest/stream_adapter.py` | The spine: bytes → CCSDS → telemetry filter → decode → buffer → canonical `TelemetryEntry` → crash‑dump dict. Reason‑coded stats/errors. | new (one fix, §10) |
| `tests/test_ingest_ccsds_streaming.py` | 50 tests incl. the 15 mandated failure modes, fail‑closed invariants, downstream integration. | new |
| `tests/test_ingest_integration_e2e.py` | 10 tests: raw synthetic bytes → adapter → **real** safety gate. | new |
| `PHASE_2_PRECHANGE_INGESTION_MAP.md`, `PHASE_2_STREAMING_CONTRACT.md` | STEP‑1 audit and STEP‑2 contract. | new |

No tracked/existing source or test files were modified. `git diff --stat app/agent/safety.py` is empty; Phase 1 is untouched.

---

## 3. Architecture & data flow (the pipeline seam)

```
Transport (in‑process bytes)              §1  NOT a real link — see §11 Q2
  → CCSDS deframing + primary‑header parse §2  app/ingest/ccsds.py           REAL
  → telemetry‑only filter                  §9  telecommand packets rejected
  → synthetic mission decode               §13 app/ingest/mission_decode.py  SYNTHETIC layout / real mechanics
  → bounded sequence buffer                §5  app/ingest/stream_buffer.py   REAL
  → CanonicalReading (TelemetryEntry)      →   the existing canonical type
  → crash‑dump‑shaped dict                 →   fed UNCHANGED to canonical_window()
        → existing detection → reconciliation → physics → RAG → agent → SAFETY GATE  (all Phase‑1, untouched)
```

The **single seam** is the crash dump: `crash_dump["pre_fault_telemetry_window"]` is a list of canonical entry dicts, exactly what a batch `CrashDumpRequest` produces. The adapter runs **no** detection, reconciliation, physics, RAG, LLM, or safety logic — no FDIR is duplicated. Provenance keys (`telemetry_source="ccsds_synthetic_stream"`, `ingest_layout`) are added and ignored by `canonical_window()`.

---

## 4. CCSDS parser — what is real (`ccsds.py`)

**Real and standards‑conformant (CCSDS 133.0‑B, Space Packet Protocol):** the 6‑octet big‑endian primary header — version (0b000), type (TM/TC), secondary‑header flag, 11‑bit APID, 2‑bit sequence flags, 14‑bit sequence count (wraps mod 16384), 16‑bit data length (= data‑field octets − 1). Framing is **self‑delimiting** by the length field (SPP has no sync marker), so on a structural fault the deframer **raises rather than resynchronizes** — guessing where the next packet starts could reinterpret arbitrary bytes as telemetry, which the safety spine forbids. `sequence_gap()` implements the modular wrap arithmetic. An operational `max_data_field_octets` cap makes `OVERSIZED_PACKET` reachable/testable and bounds allocation before reading data octets.

**Deliberately NOT here (and not claimed):** no transport; no secondary‑header **timecode** decode (those octets stay opaque inside `data_field` — reinterpreting an unverifiable mission time would be fabrication); no telecommand handling beyond *recognizing TC in order to reject it*; **no CCSDS stack above/below SPP** — no TM/TC transfer frames, no packet error control / CRC, no Reed‑Solomon/FEC, no randomization, no CFDP/COP‑1.

---

## 5. Mission decoder — synthetic, and why (`mission_decode.py`)

This module is **explicitly SYNTHETIC**. The repository has **no byte‑offset → channel map** for any real spacecraft (the channel dictionary maps *names* → specs, not *bytes* → channels). Fabricating a real‑looking mission layout would violate the project's core honesty rule.

So it decodes one **documented, self‑describing** layout, `SENTINEL_SYNTHETIC_TM_LAYOUT_V1`: `[uint8 record count]` then per record `[uint8 name length][UTF‑8 name][big‑endian float64 value]`. Because each record **carries its channel name**, the decoder never guesses a byte→channel mapping — it reads the name and validates it against the **real** dictionary via `get_channel`. Unknown names → `UNKNOWN_CHANNEL` and **no** reading. A known channel carrying a non‑finite value → an **explicit unusable sample** (`value=None`, `quality=MALFORMED_VALUE`) — never a fabricated number. The decode *mechanics* (IEEE‑754 big‑endian `struct.unpack`, UTF‑8 decode, dictionary validation) are real; only the *layout* is synthetic.

---

## 6. Bounded buffer — ordering & memory policy (`stream_buffer.py`)

Real, deterministic, and bounded:
- **Per‑APID dedup** — first arrival wins (bounded recent‑seq memory).
- **Bounded reorder window** (`DEFAULT_REORDER_WINDOW=64`, tunable) — a packet older than the window is `OUT_OF_ORDER_TOO_LATE`, never silently accepted as "latest"; the highest count never moves backward.
- **Hard memory cap** (`DEFAULT_MAX_READINGS=4096`, tunable) — on overflow the buffer **refuses and reports `BUFFER_OVERFLOW` *before* mutating state** (contract §12: refuse‑and‑report, **not** drop‑oldest — silently dropping the "we stopped ingesting" fact would be unsafe).
- **Deterministic drain** by monotonic acceptance index; gaps are counted for observability but the missing readings are **never** fabricated.

The two default sizes are the only assumptions here; both are documented and configurable.

---

## 7. Stream adapter & the fail‑closed spine (`stream_adapter.py`)

The adapter orchestrates the flow and converts `DecodedSample → TelemetryEntry`. It **never raises** out of `ingest()` for stream/packet faults — a CCSDS structural fault ends the stream and flushes what was validly received; any transport error becomes `STREAM_DISCONNECT`. Every rejection carries a stable reason code counted in `IngestStats` (the evidence base for this report).

**The spine (the whole point):**
- Ingested readings arrive `status = UNKNOWN` (the `TelemetryEntry` default). The adapter **never asserts NOMINAL** — only detection may classify.
- A rejected/lost packet produces **no reading** → the channel is **absent** downstream → UNKNOWN → gate stays closed.
- A malformed value produces an **explicit unusable reading** (`value=None`) that the pipeline recognizes as unusable (§10) → VIOLATED → gate stays closed.

---

## 8. Failure behavior — the 15 mandated cases

All 15 are covered by `TestStep6FifteenFailureModes` and pass:

| # | Case | Reason code / result |
|---|---|---|
| 01 | valid packet | reading emitted, `status=UNKNOWN` |
| 02 | truncated packet (short data) | `TRUNCATED_PACKET`, no reading |
| 03 | length declares more than present | `TRUNCATED_PACKET` |
| 04 | invalid header (bad version) | `INVALID_HEADER` |
| 05 | unsupported type (telecommand) | `UNSUPPORTED_TYPE`, dropped |
| 06 | duplicate sequence | `DUPLICATE_SEQUENCE`, first wins |
| 07 | out‑of‑order within window | accepted‑reordered |
| 08 | sequence regression beyond window | `OUT_OF_ORDER_TOO_LATE` |
| 09 | oversized (vs operational cap) | `OVERSIZED_PACKET` |
| 10 | burst | all accepted, bounded |
| 11 | empty payload | `EMPTY_PAYLOAD`, no reading |
| 12 | unknown channel | `UNKNOWN_CHANNEL`, no reading |
| 13 | malformed value (NaN) | explicit unusable `value=None`, `value_text="MISSING"`, counted `malformed_values` (§10) |
| 14 | stream disconnect | `STREAM_DISCONNECT`, flush valid prefix |
| 15 | buffer overflow | `BUFFER_OVERFLOW`, refuse‑and‑report |

Plus `TestFailClosedInvariant`: across a battery of malformed streams **no** emitted entry is `NOMINAL`; missing channels are simply absent (never fabricated); total garbage yields zero readings.

---

## 9. Testing & full‑suite evidence

**New tests:** `test_ingest_ccsds_streaming.py` (50) + `test_ingest_integration_e2e.py` (10) = **60 passed, 13 subtests passed** (run in isolation, 0.17s).

**Full backend suite** (`python3 -m pytest -q` from `sentinel/backend`), after the §10 fix:

```
1 failed, 1566 passed, 6 skipped, 1 warning, 4 errors, 2640 subtests passed in 37.50s
```

Delta vs the documented pre‑Phase‑2 baseline (`1 failed, 1506 passed, 6 skipped, 4 errors, 2627 subtests`): **+60 passed, +13 subtests, and nothing else moved.**

The remaining **1 failure + 4 errors are pre‑existing and environmental — proven, not asserted:**
- `test_phase3_contract::...test_artifacts_are_not_stale` — a repo‑hygiene chore (regenerate with `scripts/export_contracts.py`). Proven not mine: with **all** Phase‑2 files relocated out of the tree it fails identically.
- 4 `phase11`/`phase12` LocalMode errors — `PermissionError: [Errno 1] Operation not permitted` on `socket.bind` (the sandbox denies the listening socket the LLM stand‑in needs). Environmental, unrelated to ingestion.

**Phase‑1 fail‑closed regression, re‑run after the §10 fix:** `tests/test_phase1_failclosed_adversarial.py test_phase1_blocked_plans.py test_phase1_registry.py test_safety.py` → **110 passed, 4 skipped, 508 subtests passed.** Clean‑room `scripts/phase1_failclosed_verification.py` → **5/5 scenarios PASS.**

`evaluation_results.json` float jitter written by the phase12 suite was reverted (`git checkout --`); the working tree contains only the new Phase‑2 files.

I have **not** run this on flight hardware or a real link, and make no claim beyond the test evidence above.

---

## 10. A real fail‑closed defect — found and fixed

Integration testing surfaced a genuine hole **in this layer's own adapter**, not a mere test bug:

- `canonical_window_dicts` promotes a row's `value_text` into the `value` field for unusable rows, and the pipeline's `is_value_nan_or_missing()` recognizes only a fixed vocabulary of unusable tokens (`None`, `NaN`, `"NaN"`, `""`, and the model‑stamped `"MISSING"`).
- The adapter originally stamped unusable readings with `value_text="MALFORMED_VALUE"`. That string is **not** in that vocabulary, so a malformed gyro reading reached the safety gate as a **healthy** value and a gyro‑dependent command was **authorized** — the exact "ingestion failure treated as telemetry" failure this layer exists to prevent.
- **Fix:** `_drain_to_entries` now emits `value_text=None`, letting the `TelemetryEntry` model stamp the pipeline‑canonical `"MISSING"` token (recognized downstream → resolves to None → VIOLATED → `GYRO_HEALTH_PREREQUISITE` block). The malformed‑vs‑absent distinction is preserved where it belongs — in `IngestStats.malformed_values` and `IngestError` — not in a value the gate would misread.

Verified by `test_13_malformed_value` (asserts `value is None`, `value_text=="MISSING"`, `status=UNKNOWN`, `malformed_values==1`) and by `test_malformed_gyro_value_blocks_and_never_authorizes` (the real gate now returns `GYRO_HEALTH_PREREQUISITE`). This is the single most important result of Phase 2: the fail‑closed guarantee is now aligned with the pipeline's own unusable‑value convention as the single source of truth.

---

## 11. Honesty audit (STEP 9)

**Classification** — A=real · B=partially real · C=simulated/synthetic · D=hardcoded · E=demo‑only · F=placeholder.

| Component | Class | Basis |
|---|---|---|
| CCSDS SPP primary‑header parser | **A — real** | Exact, standards‑conformant, big‑endian, deterministic; SPP layer only. |
| Transport / byte source | **C — simulated** | No socket/serial/link; in‑process byte iterable. Intentional seam, not implemented. |
| Mission decoding | **C — synthetic layout, real mechanics** | `SENTINEL_SYNTHETIC_TM_LAYOUT_V1` is a documented test contract, not a real mission byte‑map; unpack + dictionary validation are real. |
| Bounded sequence buffer | **A — real** | Real dedup/reorder/overflow/memory policy; defaults are documented tunables. |
| Stream adapter & fail‑closed spine | **A — real** | Real orchestration; fail‑closed behavior proven through the real gate. |
| Ingestion → FDIR connection | **B — partially real** | Real & tested at the data‑contract + safety‑gate level; **not** wired to any live endpoint. |
| Inter‑packet timing / timestamps | **C — simulated** | `DEFAULT_SAMPLE_SPACING_S=1.0` synthetic spacing; secondary‑header timecode not decoded. |

**The 7 questions, answered plainly:**

1. **Is the CCSDS parser real?** **Yes (A).** The Space Packet Protocol primary‑header parse and self‑delimiting deframing are real and standards‑conformant. Caveat: **SPP layer only** — no transfer frames, no CRC/FEC, no secondary‑header timecode, no CFDP/COP‑1; TC is recognized only to be rejected.
2. **Is transport real?** **No (C).** There is no socket, serial line, ground‑station link, or file reader. The adapter consumes an in‑process byte iterable. It is transport‑*agnostic* by design, but no real transport is implemented.
3. **Is mission decoding real or synthetic?** **Synthetic (C).** The packet *layout* is a documented SENTINEL test contract, not any real spacecraft's byte map. The decode *mechanics* and the validation of channel names against the **real** dictionary are real — no channel is invented.
4. **Is buffering real?** **Yes (A).** Bounded memory (hard cap), per‑APID dedup, bounded reorder window, refuse‑and‑report overflow, deterministic drain. Only the default window/cap sizes are assumptions, and they are documented and configurable.
5. **Is the stream actually connected to FDIR?** **Partially (B).** It is connected at the **data‑contract** level and proven **end‑to‑end through the real, unmodified safety gate** in tests (raw bytes → adapter → `validate_recovery_plan`/`apply_validation_to_output`): a gyro‑dependent command is authorized only with finite gyro telemetry and blocked on every failure mode. **But no live FastAPI endpoint or runtime service invokes the adapter** — verified: the streaming modules are imported only by themselves and the two test files. It is not yet a runtime data source for the running pipeline.
6. **What remains simulated?** Transport; the mission packet layout; inter‑packet timing/timestamps; the CCSDS stack above/below SPP (transfer frames, FEC/CRC, timecode); and the live runtime wiring.
7. **What cannot yet be called flight‑ready?** The ingestion front‑end as a flight article: no real transport, no real mission byte‑map, no real time, no data‑link‑layer integrity (CRC/FEC), and no live endpoint. What *is* trustworthy today is the SPP primary‑header parser, the bounded buffer, and the tested fail‑closed guarantee that a dropout/malformation/overflow/disconnect never becomes healthy telemetry.

**Readiness is deliberately not scored as a single number** — that would inflate a layer whose transport and mission‑map are synthetic. The component table above is the honest breakdown.

---

## 12. What is NOT done, and the STOP boundary

**Not done (deferred, by design):** real transport (socket/serial/CCSDS Space Link); a real mission byte‑offset→channel map; secondary‑header timecode / real timestamps; CCSDS transfer frames + error control (CRC/Reed‑Solomon); and live wiring of `StreamIngestAdapter` into a FastAPI endpoint or ingest service.

**Explicitly out of Phase‑2 scope and NOT implemented** (per the standing instruction): 3‑axis attitude physics; thermal mesh; CCSDS **telecommand uplink**; orbital propagation; LLM router redesign; air‑gapped deployment.

**Phase‑1 boundary honored:** no Phase‑1 file was modified (`safety.py` diff empty); the fail‑closed battery and clean‑room verification pass unchanged after the §10 fix.

**STOP.** Phase 2 is complete as an engineering layer with the evidence above. No further capability is claimed, and no work beyond this scope was performed.
