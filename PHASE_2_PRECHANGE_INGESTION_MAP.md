# PHASE 2 — PRE-CHANGE TELEMETRY INGESTION AUDIT (STEP 1, READ-ONLY)

**Scope:** map how telemetry currently enters SENTINEL and reaches analysis, as
evidence for the Phase 2 streaming-ingestion design. **Nothing was implemented in
this step.** Every claim below cites `file:line` in
`sentinel/backend/` at the current `main` HEAD.

**One-line finding:** telemetry ingress today is **batch JSON only**
(`CrashDumpRequest`). The only "streaming" in the system is **Server-Sent-Events
on the *output* side** (analysis trace → frontend). There is **no** socket / UDP /
TCP / WebSocket transport and **no** CCSDS code anywhere. The single convergence
point every downstream consumer reads is `canonical_window()` in
`app/api/adapters.py`.

---

## 1. Current telemetry entry points

All ingress is HTTP, and every telemetry-bearing route takes a **batch**
`CrashDumpRequest` body (or a JSON blob in a query param). None ingests a stream.

| Route | Location | Intake | Notes |
|---|---|---|---|
| `POST /detect`, `/api/detect` | `main.py:632-634` | `CrashDumpRequest` | sync, LLM-free; → `sanitize_telemetry_payload_data` → `run_detection_on_crash_dump` (`main.py:650-654`) |
| `POST /v1/detect` | `main.py:319` | `CrashDumpRequest` | `response_model=AnomalyReport` |
| `POST /analyze`, `/api/analyze` | `main.py:819-821` | `CrashDumpRequest` | returns `StreamingResponse` SSE (`main.py:865`) — **output** stream |
| `POST /v1/analyze` | `main.py:868` | `CrashDumpRequest` | SSE (`main.py:977`) |
| `GET /api/analyze` | `main.py:679-680` | `preset` / `payload` **query string** | `urllib.parse.unquote` → `json.loads` → `sanitize_telemetry_payload_data` (`main.py:687-695`) |
| `POST /v1/reconciliation` | `main.py:550` | crash-dump shaped | |
| `POST /v1/physics` | `main.py:387` | | |

Agent-side ingestion entry: `agent.analyze_crash_dump_stream(data)`
(`main.py:767`), which canonicalizes **once** at ingestion (`agent.py:1560`).
`agent.py:1542` emits a cosmetic SSE status string
`"Connecting to Sentinel FDIR telemetry stream..."` — **a display label, not a
real connection.**

---

## 2. Request / API models used for telemetry

- **`CrashDumpRequest`** (`models.py:755`) — batch intake, `model_config
  extra="allow"` (`models.py:820-821`, forwards `hardware_state`,
  `operating_context`, etc.). Telemetry fields:
  - `pre_fault_telemetry_window: Optional[List[TelemetryEntry]]` (`models.py:791`)
    — the canonical field.
  - `pre_fault_telemetry: Optional[List[Dict]]` (`models.py:802`, **deprecated**)
    — legacy bounds-only shape.
  - `event_log`, `telecommand_context`, `fault_register`, `safe_mode_trigger`,
    `incident_id`, `scenario_id`, `fault_type`.
- **`TelemetryEntry`** (`models.py:644`) — the CANONICAL single reading:
  `timestamp: str` (relative marker, `models.py:658`), `relative_time_s:
  Optional[float]` (`:664`), `value: Optional[float]` (`:670`), `value_text`
  (`:674`), `unit`, `status: TelemetryStatus` (default `UNKNOWN`, `:685`),
  `anomalous`, `nominal_min/max`, `baseline_mean/std`.
- **`TelemetryStatus`** (`models.py:578`) — closed enum
  (`NOMINAL/WARNING/ANOMALOUS/CRITICAL/NOMINAL_CONTEXT/LABELLED_ANOMALY/UNKNOWN`);
  `normalize()` (`:614`) coerces aliases; **UNKNOWN is never treated as NOMINAL**
  (`:611-612`, `is_nominal` `:638`).
- **`TelecommandContext`** (`models.py:545`) — `execution_timestamp: datetime`
  (`:559`) is the **only absolute ISO-8601 timestamp** in the telemetry intake.

---

## 3. `app/api/adapters.py` and canonical normalization

`canonical_window(crash_dump)` (`adapters.py:206`) is documented as **"THE ONLY
PLACE the two historical telemetry shapes are reconciled"** (`adapters.py:4`). It:

- merges `pre_fault_telemetry_window` (timing+status) and `pre_fault_telemetry`
  (bounds) into one `list[TelemetryEntry]` (`adapters.py:228-262`);
- dedups/merges via `_merge_key = (parameter, timestamp, value-identity)`
  (`adapters.py:57-75`), filling gaps rather than letting first-read win
  (`:246-262`);
- enriches missing bounds/units from the Phase-5 channel dictionary
  (`_enrich_from_channel_dictionary`, `:179-203`) — **only for declared channels;
  unknown channels are left untouched** (`:184-192`), explicitly "nothing is
  invented" (`:40`);
- **"Never raises on malformed input"** (`:222`); non-dict rows and empty-parameter
  rows are skipped (`:233-237`, `_to_entry` `:128-132`).

Sibling helpers: `canonical_window_dicts` (`:272`), `with_canonical_window`
(`:294`), `canonical_channels` (`:315`), `coverage_report` (`:324`, auditable
merge). `app/ingest/` (`ingest/__init__.py`, `channel_dict.py` ~60 KB,
`esa_mapping.py`) is the **Phase-5 channel dictionary** (name→spec, hard_limits,
`is_known_channel`, ESA name mapping) — it is a *metadata dictionary*, **not** a
byte-to-channel decoder.

---

## 4. Current telemetry validation and sanitization

Two independent layers, both **content**-level (not transport/structure):

1. **Pydantic validation** at the API boundary (`TelemetryEntry` / `CrashDumpRequest`):
   - `coerce_status` (`models.py:713`) → `TelemetryStatus.normalize`.
   - `coerce_value` (`models.py:719-741`) — maps NaN/Inf/non-numeric/empty to
     `None` (unusable) rather than rejecting the payload.
   - `preserve_unusable_reading` (`models.py:743-748`) — guarantees `value_text`
     so a dropout stays visible ("MISSING").
2. **Injection sanitizer** `sanitize_telemetry_payload_data`
   (`security/sanitization.py:99-112`) — recursively strips unknown keys
   (`sanitize_input_keys`) and neutralizes prompt-injection strings
   (`sanitize_prompt_injection`). **This is anti-injection hardening, not a
   structural validator**: it does not check packet framing, lengths, or
   sequencing (none exist to check).

There is **no** length check, CRC/checksum, header validation, or schema-version
gate on any *binary/framed* input, because input is JSON today.

---

## 5. Where timestamps are created / validated

- Telemetry timestamps are **relative markers** (`"T-120.5s"`, `"T+0.000s"`),
  parsed by `_parse_seconds` (`adapters.py:87-109`) and
  `temporal.parse_offset_seconds` into `relative_time_s`. Unparseable → `None`,
  **never 0.0** (`adapters.py:90-93`), so an unparseable offset does not collapse
  a window to one instant.
- The **only absolute** timestamp is `TelecommandContext.execution_timestamp`
  (`models.py:559`), validated by pydantic's `datetime`.
- **No per-reading wall-clock / monotonic timestamp, no timestamp-regression
  check, no clock at all** — timestamps arrive in the payload; SENTINEL does not
  stamp readings on receipt.

---

## 6. Where telemetry reaches anomaly detection

`run_detection_on_crash_dump` (`fusion.py:251-261`) → `extract_readings`
(`fusion.py:117-132`, which **delegates to `adapters.canonical_window_dicts()`**)
→ `run_detection(readings, channel_summaries)`. Pipeline order (`fusion.py:4`,
`main.py:643`): **hard limits → discrete states → statistical → temporal →
fusion.** So detection consumes the *same* canonical window the adapter produces —
confirming `canonical_window()` is the single interface a stream adapter must feed.

---

## 7. Is telemetry ordering assumed?

**No, for correctness of the final report** — but temporal detection depends on
parsed time:

- Detection imposes a **total deterministic sort** on findings
  (`_sort_key` "severity desc, channel, detector, offset, id" `fusion.py:94`;
  `all_anomalies.sort` `:217`; `sorted(items, key=_sort_key)` `:302`;
  `findings.sort` `:305`). `_earliest_offset` (`:309-322`) picks the earliest by
  **parsed** seconds, not by list position.
- The canonical window preserves "first-seen order" (window entries, then
  legacy — `adapters.py:264`) only as a **stable presentation order**.
- **Caveat:** temporal/rate detectors rely on `relative_time_s`; a missing or
  unparseable offset degrades temporal analysis (offset `None`). There is no
  explicit "sort readings by time before temporal analysis" step beyond parsing,
  so **out-of-order-in-time input is tolerated for limit/statistical detection but
  is a latent assumption for rate/trend detection** — a design input for Phase 2
  buffering/ordering.

---

## 8. Are duplicate readings handled?

Only inside the merge: `canonical_window` collapses rows with identical
`_merge_key = (parameter, timestamp, value-identity)` (`adapters.py:57-75`,
`239-262`); a NaN and an absent sample stay distinct (`:73-75`). This was added to
fix an idempotency bug (re-merging a canonical dump double-counted a NaN,
`adapters.py:60-71`). **There is no sequence-number dedup (no sequence numbers
exist) and no cross-request/temporal dedup.**

---

## 9. Can malformed readings enter the analysis pipeline?

- **Structurally malformed** rows (non-dict; empty `parameter`) are **dropped**
  before becoming entries (`adapters.py:233-237`, `_to_entry` `:128-132`).
- **Value-malformed** readings (NaN/Inf/non-numeric/empty) are **not rejected**;
  they become **unusable entries** (`value=None`, original kept in `value_text`,
  `models.py:719-748`) so a dropout is explicitly visible and never silently
  nominal. `limits.py:467` — "Never raises on malformed input."
- Net: a bad value cannot crash ingestion and cannot masquerade as NOMINAL, but it
  **does enter** as an explicit unusable/UNKNOWN reading (by design). No framed
  packet exists to be "malformed" at the byte level yet.

---

## 10. Existing tests covering telemetry ingestion

By content (all pass except the known pre-existing `test_phase3_contract`
staleness and env-gated cases):

- `tests/test_phase2_detection.py` — detection + canonical window / `extract_readings`.
- `tests/test_phase3_contract.py` — `TelemetryEntry` / `CrashDumpRequest` contract.
- `tests/test_phase5_channel_dict.py` — channel dictionary (`app.ingest`).
- `tests/test_phase14_security.py` — `sanitize_telemetry_payload_data`.
- `tests/test_phase15_evidence_pipeline.py`, `test_phase16_llm_baseline.py`,
  `test_phase17_evidence_rag_safety.py`, `test_phase26/30`, `test_phase4_audit.py`,
  `test_phase8_physics.py` — consume the adapter.
- `tests/test_streaming.py` — **SSE HTTP contract only** (`/api/health`,
  `/api/scenarios`, `POST /api/analyze` event-stream) — not transport ingestion.

**No test exercises a binary or streaming *ingress* transport** (none exists).
Total backend test files: **60**.

---

## 11. Existing WebSocket / UDP / TCP / SSE / queue / buffer / streaming infra

- **SSE (output only):** `StreamingResponse(..., media_type="text/event-stream")`
  at `main.py:816, 865, 977`. Streams the analysis **trace** to the browser. The
  per-entry `yield` of telemetry in `GET /api/analyze` (`main.py:705-764`) is a
  re-emission of the *already-ingested* canonical window for display, paced with
  `await asyncio.sleep(0.05)` — **cosmetic, not an ingress stream.**
- **WebSocket / UDP / TCP / raw socket / datagram / `asyncio.start_server` /
  ring-buffer / explicit backpressure: NONE.** Grep for
  `socket.socket|.bind(|.recv(|.accept(|@app.websocket|start_server|create_datagram`
  across `app/` returns nothing.
- `test_streaming.py` name refers to the SSE contract.

---

## 12. Existing CCSDS-related code / constants / models / dependencies

**NONE.** Grep for `ccsds|space packet|apid|primary header|spp|packet sequence`
across `app/` and `tests/` returns nothing. No dependency in the project declares
CCSDS support. **A CCSDS Space Packet Protocol parser (STEP 3) would be entirely
net-new**, and there is **no existing byte-offset → telemetry-channel map** —
`channel_dict.py` maps channel *names* to specs, not payload bytes to channels.

---

## IMPLICATIONS FOR THE PHASE 2 DESIGN (recorded, not yet implemented)

These are consequences of the evidence above; they constrain STEPs 2–8. Anything
not grounded in the repo is flagged as an **ASSUMPTION**.

1. **Smallest safe seam = feed the existing canonical interface.** A stream adapter
   should terminate in `TelemetryEntry` objects assembled into a
   `pre_fault_telemetry_window` list / crash-dump-shaped dict, then hand off to the
   **unchanged** `canonical_window()` → `run_detection_on_crash_dump` /
   `analyze_crash_dump_stream` path. No FDIR logic should be duplicated in ingestion.
2. **CCSDS parsing MUST be separated from mission telemetry decoding.** The repo
   has no packet-layout → channel map, so byte-level mission decoding can only be a
   **documented, synthetic, test-only** mapping (ASSUMPTION territory). CCSDS
   primary-header parsing itself is a real, standardized structure and can be
   implemented against the public SPP spec.
3. **Fail-closed continuity with Phase 1 (critical).** Phase 1 blocks
   safety-critical commands when a required channel is **absent/UNKNOWN**. Therefore
   a stream **dropout, disconnect, malformed packet, or overflow must map to
   "channel absent / no reading"**, never to a fabricated NOMINAL reading. This is
   the single most important safety constraint for the ingestion layer.
4. **Timestamps:** the pipeline expects relative offsets (`relative_time_s`). A
   real stream carries sequence counters and (often) absolute time; STEP 2 must
   define how CCSDS sequence/time maps onto the existing relative-offset model
   **without inventing wall-clock semantics the pipeline doesn't have.**
5. **Ordering/dedup:** detection re-sorts and the adapter dedups by
   `(parameter, timestamp, value)`, but there is **no sequence-based** dedup or
   reordering. STEP 5 buffering must add deterministic sequence-number dedup and
   out-of-order handling *before* the canonical layer, since the canonical layer
   was not designed for it.

---

*Read-only. No source modified in STEP 1. Companion to Phase 1's
`PHASE_1_PRECHANGE_SAFETY_MAP.md`. Next: STEP 2 — define the streaming contract
from this evidence.*
