# PHASE 2 — STREAMING TELEMETRY INGESTION CONTRACT (STEP 2)

**Status:** design contract. Grounded in (a) the read-only audit
`PHASE_2_PRECHANGE_INGESTION_MAP.md` and (b) the public CCSDS Space Packet
Protocol standard (CCSDS 133.0-B, "Space Packet Protocol"). Anything **not**
derivable from the repo or the public standard is tagged **[ASSUMPTION]** and is
confined to test-only material.

**Design rule (from the task):** the smallest *production-like* contract that
feeds the **existing** canonical interface unchanged, and that **preserves the
Phase-1 fail-closed invariant**: a telemetry-ingestion failure must never be
interpreted as healthy telemetry.

---

## 0. Terminating principle (safety spine)

> **Absence of a reading is UNKNOWN, never NOMINAL.**

Every failure mode in §6 resolves to one of exactly two safe outcomes:
1. **Reject the packet** (it never becomes a `TelemetryEntry`), or
2. **Emit an explicit unusable/absent reading** (`value=None`, status carries no
   NOMINAL claim).

Neither outcome can fabricate an in-range value. Because Phase-1 blocks
safety-critical commands whose required channel is absent/UNKNOWN
(`safety.py` fail-closed gate), a dropped or malformed packet automatically keeps
the downstream safety gate closed. **No ingestion path may invent a NOMINAL
reading to "fill a gap."**

---

## 1. Input transport

- **Contract:** the ingestion layer is transport-agnostic behind a single
  interface `TelemetryStreamSource` = *an iterable/async-iterable of raw byte
  chunks* (`Iterable[bytes]`). A chunk is an opaque run of octets; frame
  boundaries are recovered by the framer (§2), **not** assumed to align with
  chunk boundaries.
- **Concrete implementation in scope:** an in-process byte-stream source
  (`bytes` / `BytesIO` / iterator of `bytes`). This exercises **real** framing and
  parsing on **real** bytes.
- **[ASSUMPTION / OUT OF SCOPE] network transport:** a UDP/TCP socket source is a
  thin future adapter over the same interface. It is **not** implemented in Phase 2
  — (a) the task forbids connecting hardware/real systems, and (b) the sandbox
  denies `socket.bind` (documented: phase11/12 socket-bind errors), so a socket
  listener could not be honestly tested here. The abstraction is built so a socket
  source can be added later without touching the parser or decoder.

## 2. Packet / frame boundary

- **Contract:** frames are **CCSDS Space Packets**. The boundary is
  **self-delimiting** via the primary header's *Packet Data Length* field — there
  is no reliance on transport framing, newlines, or fixed record size.
- A CCSDS packet = **6-octet primary header + (Packet Data Length + 1) octets of
  data field**. Total length is computed from the header, so a byte stream is
  deframed deterministically: read 6 header octets → read `data_len + 1` more →
  that is one packet → repeat.
- A stream may carry back-to-back packets; the framer yields them in order and
  retains any trailing partial bytes for the next chunk.

## 3. CCSDS primary header (the real, parsed structure)

6 octets, big-endian, exactly per CCSDS 133.0-B:

| Field | Bits | Meaning |
|---|---|---|
| Packet Version Number | 3 | must be `0b000` for SPP v1 |
| Packet Type | 1 | 0 = telemetry (TM), 1 = telecommand (TC) |
| Secondary Header Flag | 1 | 1 = secondary header present |
| APID | 11 | Application Process ID (logical channel / source) |
| Sequence Flags | 2 | 01=first, 00=continuation, 10=last, 11=unsegmented |
| Packet Sequence Count | 14 | per-APID counter, **wraps modulo 16384** |
| Packet Data Length | 16 | (octet length of data field) − 1 |

**Sequence-count wrap (16384)** and **per-APID** counting are properties of the
standard and are honored explicitly (§4, §5).

## 4. Timestamp semantics

- The existing pipeline is built on **relative offsets** (`relative_time_s`,
  parsed from `"T-60s"` markers — audit §5). SENTINEL does **not** stamp readings
  with wall-clock on receipt, and Phase 2 will **not invent** one.
- **Contract:** the ingestion layer derives an **ingest-order index** per APID
  from the CCSDS Packet Sequence Count and maps it to the pipeline's relative-time
  model as a **monotonic, unit-documented offset**, *not* a fabricated absolute
  UTC. The mapping is explicit and reversible.
- **[ASSUMPTION]** any absolute epoch or CCSDS secondary-header timecode (CUC/CDS)
  is **not** decoded in Phase 2 (no repo evidence of a mission time format). If a
  secondary header is flagged, its bytes are preserved verbatim and **not**
  reinterpreted as telemetry.
- **Timestamp regression** (a sequence count that goes backwards beyond the
  dedup/reorder window, §6.8) is reported and the packet is handled by the
  out-of-order policy — never silently accepted as "latest."

## 5. Sequence-number handling

- Tracked **per APID**. The adapter maintains `last_seq[apid]` and the set/window
  of recently seen counts.
- **Expected next** = `(last_seq + 1) mod 16384`. A gap (`next` skipped) is
  **recorded as a telemetry gap** (observability, §15) but does not fabricate the
  missing readings — the affected channels simply have no reading for that step
  (→ UNKNOWN downstream, safe).

## 6. Duplicate detection

- A packet whose `(apid, seq_count)` is already present in the dedup window is a
  **duplicate**: dropped, counted, and logged. Deterministic — the *first* arrival
  wins; later identical counts are discarded.
- This is **new** capability: the existing `canonical_window` dedup is by
  `(parameter, timestamp, value)` (audit §8) and has no notion of sequence; Phase 2
  adds sequence-level dedup **before** the canonical layer.

## 7. Out-of-order handling

- Bounded reordering inside a **reorder window** of `W` packets per APID
  ([ASSUMPTION] default `W=64`, configurable). A packet whose count is within `W`
  behind the highest seen is inserted in order; a packet **older than `W`** (or a
  regression beyond the window) is **rejected as too-late** (counted/logged), never
  reordered arbitrarily.
- Deterministic tie-break: strictly by `(apid, seq_count)` with wrap handled via
  modular distance.

## 8. Malformed packet behavior

Deterministic rejection, **never** reinterpretation:
- header < 6 octets available → **truncated**, reject.
- Packet Version Number ≠ 0 → **invalid header**, reject.
- declared length exceeds `MAX_PACKET_OCTETS` (§11) → **oversized**, reject.
- fewer data octets present than the header declares → **truncated**, reject.
- **No malformed byte run is ever mapped to a telemetry value** (task STEP 3
  requirement). A rejected packet produces a typed `IngestError`, not a reading.

## 9. Unsupported packet behavior

- **Packet Type = TC (telecommand)** → **not ingested as telemetry** (Phase 2 is
  telemetry-only; TC uplink is explicitly a later phase). Counted as
  `unsupported`, dropped.
- **APID not in the mission map** ([ASSUMPTION] test map, §13) → the packet is
  structurally valid but the payload cannot be decoded to channels; recorded as
  `unknown_apid`, produces **no** reading (→ UNKNOWN downstream), never a guess.

## 10. Buffer / ring-buffer behavior

- A **bounded ring buffer** per ingestion session holds decoded-but-not-yet-drained
  `TelemetryEntry` items and the per-APID reorder windows.
- **Bounded memory:** hard cap `MAX_BUFFERED_READINGS` ([ASSUMPTION] default
  `4096`). The buffer never grows unbounded.
- Drain assembles a canonical `pre_fault_telemetry_window` list handed to the
  **existing** `canonical_window()` (audit §3, §6) — the ring buffer is *upstream*
  of the canonical layer and does not duplicate it.

## 11. Maximum packet / window size

- `MAX_PACKET_OCTETS` ([ASSUMPTION] default `65542` = 6 header + 65536 max data
  field; the 16-bit length field caps the data field at 65536 octets — a property
  of the standard). A declared length above this is rejected as oversized (§8).
- `MAX_BUFFERED_READINGS` and reorder window `W` as above.

## 12. Backpressure behavior

- The source is **pull-based** (the adapter reads at its own pace), so a slow
  consumer cannot force unbounded buffering. When the ring buffer is full, the
  adapter applies a **deterministic policy: block-then-drop-oldest is NOT used for
  telemetry** — instead new packets are **refused with a `buffer_overflow`
  IngestError and counted**, so an overflow is a *reported, safe* condition (no
  reading fabricated, no silent loss of the safety-relevant "we stopped ingesting"
  fact). [ASSUMPTION] this refuse-and-report policy is the conservative choice for
  a safety system; a drop-oldest ring is a documented alternative not chosen here.

## 13. Telemetry normalization

- **Two explicitly separated stages** (task STEP 3):
  1. **CCSDS parsing** (`ccsds.py`) — structure/header/length only → typed
     `SpacePacket`. Mission-agnostic. **Real.**
  2. **Mission telemetry decoding** (`mission_decode.py`) — maps
     `(APID, data field bytes)` → channel name + numeric value. **[ASSUMPTION —
     SYNTHETIC]**: the repo has no byte-layout → channel map (audit §12), so the
     Phase-2 decoder uses an **explicitly documented test packet layout** and
     resolves channel names through the **real** channel dictionary
     (`app.ingest.get_channel` / `is_known_channel`). Unknown channels are **not**
     invented (mirrors `adapters._enrich_from_channel_dictionary`, audit §3).
- The decoder's output is the **existing** `TelemetryEntry` (canonical reading);
  no new canonical type is introduced.

## 14. Timeout behavior

- **Stream idle/disconnect timeout** ([ASSUMPTION] configurable, default off in
  the in-process source): if the source yields no bytes for `T_idle`, the session
  is closed with a `stream_disconnect` condition. A disconnect **flushes only what
  was validly received** and marks the session ended — it **does not** emit
  trailing NOMINAL readings and **does not** treat a partial trailing packet as
  complete (that partial is a truncation, §8).

## 15. Error reporting

- Every rejection/anomaly is a typed `IngestError` / counter with a **stable
  machine-readable reason code**, mirroring Phase-1's reason-code discipline
  (`safety.py` codes). Proposed codes:
  `TRUNCATED_PACKET`, `INVALID_HEADER`, `INVALID_LENGTH`, `OVERSIZED_PACKET`,
  `UNSUPPORTED_TYPE`, `UNKNOWN_APID`, `DUPLICATE_SEQUENCE`, `OUT_OF_ORDER_TOO_LATE`,
  `SEQUENCE_REGRESSION`, `BUFFER_OVERFLOW`, `EMPTY_PAYLOAD`, `UNKNOWN_CHANNEL`,
  `MALFORMED_VALUE`, `STREAM_DISCONNECT`.
- Errors are **aggregated into an ingest report** (counts per code + samples) so a
  run is auditable, never a black box (mirrors `adapters.coverage_report`).

## 16. Observability requirements

- Per-session `IngestStats`: packets received / accepted / rejected (by code),
  duplicates dropped, out-of-order reordered vs too-late, gaps detected per APID,
  buffer high-water-mark, readings emitted, unknown-APID/channel counts.
- These stats are the **evidence** required by the honesty audit (STEP 9) and the
  final report (STEP 10). No capability is claimed without a counter or test
  proving it.

---

## Downstream contract (unchanged)

The adapter's **only** output to the rest of SENTINEL is a crash-dump-shaped dict
whose `pre_fault_telemetry_window` is a list of canonical `TelemetryEntry` — i.e.
exactly what `canonical_window()` / `run_detection_on_crash_dump()` /
`analyze_crash_dump_stream()` already consume (audit §3, §6). **No detection,
reconciliation, physics, RAG, LLM, or safety code is modified.** Ingestion is a new
*upstream* producer, not a change to any downstream consumer.

---

## Explicit non-goals (Phase boundary)

Not in Phase 2: CCSDS **telecommand uplink**, secondary-header timecode decoding,
3-axis physics, thermal mesh, orbital propagation, LLM-router redesign, air-gapped
deployment, real network sockets, real spacecraft/hardware connection.
