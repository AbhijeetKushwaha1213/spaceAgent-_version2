"""
SENTINEL — Bounded Sequence-Ordered Ingest Buffer (ingest/stream_buffer.py)

Phase 2, STEP 5. A small, deterministic, BOUNDED buffer that sits between the
mission decoder and the canonical telemetry layer. It is the single place that
holds not-yet-drained readings, and it enforces every ordering/memory policy the
streaming contract (§5–§12) requires:

  * per-APID duplicate detection (first arrival wins);
  * bounded out-of-order tolerance (a reorder window ``W``); a packet older than
    the window is rejected as too-late, never silently accepted as "latest";
  * bounded memory — a hard ``max_readings`` cap. When full the buffer REFUSES
    new readings and reports ``BUFFER_OVERFLOW`` (contract §12: refuse-and-report,
    NOT drop-oldest — dropping a safety-relevant "we stopped ingesting" fact would
    be unsafe);
  * a monotonic per-acceptance ingest index used later to derive the pipeline's
    relative-time offsets, WITHOUT inventing wall-clock.

Boundaries
----------
Pure Python + the CCSDS sequence arithmetic helper. Imports nothing from
detection/safety/agent and nothing from pydantic. It never fabricates a reading:
its only outputs are (a) accept/reject decisions with a stable reason code and
(b) the exact samples handed to it.

Safety spine
------------
Every rejection path drops the packet with a reason code and emits NO reading, so
a rejected/lost packet becomes "channel absent" downstream (→ UNKNOWN, Phase-1
gate stays closed). The buffer can never turn a fault into a NOMINAL value.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Sequence

from app.ingest.ccsds import SEQUENCE_COUNT_MODULUS, sequence_gap
from app.ingest.mission_decode import DecodedSample

# --- Default policy constants (documented [ASSUMPTION]s from the contract) ---

#: Default reorder window per APID (packets). A packet more than this many counts
#: behind the highest seen is rejected as too-late. Contract §7 [ASSUMPTION].
DEFAULT_REORDER_WINDOW = 64

#: Default hard cap on buffered readings. Contract §10/§11 [ASSUMPTION].
DEFAULT_MAX_READINGS = 4096


class OfferOutcome(str, Enum):
    """What the buffer did with an offered packet."""

    ACCEPTED_IN_ORDER = "ACCEPTED_IN_ORDER"
    ACCEPTED_REORDERED = "ACCEPTED_REORDERED"  # within the reorder window, behind highest
    DUPLICATE_SEQUENCE = "DUPLICATE_SEQUENCE"
    OUT_OF_ORDER_TOO_LATE = "OUT_OF_ORDER_TOO_LATE"
    BUFFER_OVERFLOW = "BUFFER_OVERFLOW"


@dataclass(frozen=True)
class OfferResult:
    """Outcome of one :meth:`SequenceOrderedBuffer.offer` call."""

    outcome: OfferOutcome
    #: Sequence counts skipped since the previous highest (a telemetry gap).
    #: Observability only — the buffer never fabricates the missing readings.
    gap: int = 0

    @property
    def accepted(self) -> bool:
        return self.outcome in (
            OfferOutcome.ACCEPTED_IN_ORDER,
            OfferOutcome.ACCEPTED_REORDERED,
        )


@dataclass
class PendingPacket:
    """One accepted packet's samples, tagged with its global acceptance index."""

    ingest_index: int
    apid: int
    sequence_count: int
    samples: List[DecodedSample]


@dataclass
class _ApidState:
    """Per-APID sequence-tracking state, bounded in size."""

    highest: Optional[int] = None
    recent_seqs: Deque[int] = field(default_factory=deque)
    recent_set: set = field(default_factory=set)


class SequenceOrderedBuffer:
    """Bounded, deterministic per-APID dedup + reorder buffer.

    Not thread-safe by design — one ingest session drives it from a single loop.
    """

    def __init__(
        self,
        *,
        reorder_window: int = DEFAULT_REORDER_WINDOW,
        max_readings: int = DEFAULT_MAX_READINGS,
    ) -> None:
        if reorder_window < 0:
            raise ValueError("reorder_window must be >= 0")
        if max_readings < 1:
            raise ValueError("max_readings must be >= 1")
        self.reorder_window = reorder_window
        self.max_readings = max_readings
        #: Bound on how many recent seqs to remember per APID for dedup. Kept a
        #: few multiples of the window so dedup is exact inside the window while
        #: memory stays bounded for an unbounded stream.
        self._recent_cap = max(4 * reorder_window, 256)

        self._apids: Dict[int, _ApidState] = {}
        self._pending: List[PendingPacket] = []
        self._buffered_readings = 0
        self._ingest_index = 0
        self._high_water = 0

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def buffered_readings(self) -> int:
        return self._buffered_readings

    @property
    def high_water(self) -> int:
        """Largest number of buffered readings seen at any point."""
        return self._high_water

    # ── core ─────────────────────────────────────────────────────────────────

    def offer(
        self, apid: int, sequence_count: int, samples: Sequence[DecodedSample]
    ) -> OfferResult:
        """Offer one packet's decoded samples. Deterministic; never raises.

        Returns an :class:`OfferResult`. On any non-accepted outcome the samples
        are discarded and nothing is buffered.
        """
        samples = list(samples)
        state = self._apids.get(apid)
        if state is None:
            state = _ApidState()
            self._apids[apid] = state

        # 1. Duplicate: seen recently for this APID → first arrival wins.
        if sequence_count in state.recent_set:
            return OfferResult(OfferOutcome.DUPLICATE_SEQUENCE)

        # 2. Ordering decision relative to the highest count seen for this APID.
        if state.highest is None:
            outcome = OfferOutcome.ACCEPTED_IN_ORDER
            gap = 0
        else:
            forward = sequence_gap(state.highest, sequence_count)   # ahead distance
            backward = sequence_gap(sequence_count, state.highest)  # behind distance
            if forward <= backward:
                # At or ahead of the highest → in-order; a forward>1 means counts
                # were skipped (a gap). gap counts the missing counts.
                outcome = OfferOutcome.ACCEPTED_IN_ORDER
                gap = forward - 1 if forward > 1 else 0
            elif backward <= self.reorder_window:
                # Behind the highest but inside the reorder window → accept as a
                # late-but-tolerable reorder; the highest does not move backward.
                outcome = OfferOutcome.ACCEPTED_REORDERED
                gap = 0
            else:
                # Behind beyond the window → stale. Rejected, never treated as
                # latest. (SEQUENCE_REGRESSION from the contract is reserved for a
                # dedicated counter-reset detector not built in Phase 2; a stale
                # count is reported here as OUT_OF_ORDER_TOO_LATE.)
                return OfferResult(OfferOutcome.OUT_OF_ORDER_TOO_LATE)

        # 3. Memory bound BEFORE mutating state: refuse-and-report on overflow.
        incoming = len(samples)
        if self._buffered_readings + incoming > self.max_readings:
            return OfferResult(OfferOutcome.BUFFER_OVERFLOW)

        # 4. Accept: record the packet and update per-APID tracking.
        self._pending.append(
            PendingPacket(
                ingest_index=self._ingest_index,
                apid=apid,
                sequence_count=sequence_count,
                samples=samples,
            )
        )
        self._ingest_index += 1
        self._buffered_readings += incoming
        self._high_water = max(self._high_water, self._buffered_readings)

        if outcome is OfferOutcome.ACCEPTED_IN_ORDER:
            state.highest = sequence_count
        self._remember_seq(state, sequence_count)

        return OfferResult(outcome, gap=gap)

    def drain(self) -> List[PendingPacket]:
        """Return all accepted packets in global acceptance order, and clear them.

        Acceptance order (``ingest_index``) is a stable, deterministic ordering.
        Downstream detection re-sorts findings by parsed time anyway (audit §7),
        so this order only needs to be deterministic, which it is.
        """
        drained = sorted(self._pending, key=lambda p: p.ingest_index)
        self._pending = []
        self._buffered_readings = 0
        return drained

    # ── internals ──────────────────────────────────────────────────────────

    def _remember_seq(self, state: _ApidState, seq: int) -> None:
        """Record ``seq`` for dedup, keeping the per-APID memory bounded."""
        state.recent_seqs.append(seq)
        state.recent_set.add(seq)
        while len(state.recent_seqs) > self._recent_cap:
            old = state.recent_seqs.popleft()
            # Only forget it if no duplicate copy remains in the deque.
            if old not in state.recent_seqs:
                state.recent_set.discard(old)
