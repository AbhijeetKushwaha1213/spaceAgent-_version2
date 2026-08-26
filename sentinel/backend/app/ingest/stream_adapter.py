"""
SENTINEL — Streaming Telemetry Ingest Adapter (ingest/stream_adapter.py)

Phase 2, STEP 4. The spine that turns a raw byte stream into exactly the input
the EXISTING SENTINEL pipeline already consumes — and nothing more:

    Transport (bytes)                     §1  in-process byte source
      → CCSDS deframing + parsing         §2  ingest/ccsds.py            (REAL)
      → telemetry-only filter             §9  drop telecommand packets
      → synthetic mission decoding        §13 ingest/mission_decode.py   (SYNTHETIC)
      → bounded sequence buffer           §5  ingest/stream_buffer.py
      → CanonicalReading (TelemetryEntry) §13 the existing canonical type
      → crash-dump-shaped dict            →   fed UNCHANGED to canonical_window()

The adapter DOES NOT run detection, reconciliation, physics, RAG, the LLM, or the
safety validator. It produces the same ``pre_fault_telemetry_window`` list a batch
``CrashDumpRequest`` would, so every downstream consumer is untouched (contract
"Downstream contract (unchanged)"). No FDIR logic is duplicated here.

Safety spine (the whole point)
------------------------------
A telemetry-ingestion failure is NEVER interpreted as healthy telemetry:
  * malformed / unsupported / duplicate / too-late / overflow / disconnect →
    the packet produces NO reading and is counted with a stable reason code;
  * a known channel with a non-finite value → an EXPLICIT unusable reading
    (``value=None``), never a fabricated number;
  * ingested readings arrive with ``status = UNKNOWN`` (the ``TelemetryEntry``
    default) — the adapter never asserts NOMINAL; only detection may classify.
Because a lost/rejected packet becomes "channel absent → UNKNOWN downstream", the
Phase-1 fail-closed gate stays closed exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from app.api.models import TelemetryEntry, TelemetryStatus
from app.ingest.ccsds import (
    CcsdsErrorCode,
    CcsdsParseError,
    MAX_DATA_FIELD_OCTETS,
    SpacePacket,
    iter_space_packets,
)
from app.ingest.mission_decode import (
    DecodeErrorCode,
    SampleQuality,
    decode_data_field,
)
from app.ingest.stream_buffer import (
    DEFAULT_MAX_READINGS,
    DEFAULT_REORDER_WINDOW,
    OfferOutcome,
    SequenceOrderedBuffer,
)

STREAM_ADAPTER_VERSION = "1.0.0"

#: [ASSUMPTION] Synthetic inter-packet spacing in seconds used to place readings
#: on the pipeline's relative-time axis. Neither the synthetic payload nor the
#: CCSDS sequence count carries real inter-sample time, so this is a DOCUMENTED
#: synthetic spacing — not wall-clock and not fault-relative truth. The newest
#: accepted packet is placed at T+0.000s and earlier packets at negative offsets.
DEFAULT_SAMPLE_SPACING_S = 1.0


class IngestReason(str, Enum):
    """The complete reason-code vocabulary (contract §15).

    Mirroring the Phase-1 discipline of separating EMITTED from RESERVED codes, so
    the report can state exactly which paths are exercised by code + tests.
    """

    # -- emitted in Phase 2 --
    TRUNCATED_PACKET = "TRUNCATED_PACKET"
    INVALID_HEADER = "INVALID_HEADER"
    OVERSIZED_PACKET = "OVERSIZED_PACKET"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    EMPTY_PAYLOAD = "EMPTY_PAYLOAD"
    UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"
    TRUNCATED_RECORD = "TRUNCATED_RECORD"
    MALFORMED_VALUE = "MALFORMED_VALUE"
    DUPLICATE_SEQUENCE = "DUPLICATE_SEQUENCE"
    OUT_OF_ORDER_TOO_LATE = "OUT_OF_ORDER_TOO_LATE"
    BUFFER_OVERFLOW = "BUFFER_OVERFLOW"
    STREAM_DISCONNECT = "STREAM_DISCONNECT"

    # -- reserved (defined for the contract, deliberately NOT emitted in Phase 2) --
    #: The synthetic layout is self-describing (name travels in the packet), so no
    #: APID→channel table exists to miss against. Reserved for a future mission map.
    UNKNOWN_APID = "UNKNOWN_APID"
    #: A dedicated counter-reset detector is out of Phase-2 scope; a stale count is
    #: reported as OUT_OF_ORDER_TOO_LATE instead.
    SEQUENCE_REGRESSION = "SEQUENCE_REGRESSION"


_CCSDS_REASON = {
    CcsdsErrorCode.TRUNCATED_HEADER: IngestReason.TRUNCATED_PACKET,
    CcsdsErrorCode.TRUNCATED_DATA: IngestReason.TRUNCATED_PACKET,
    CcsdsErrorCode.INVALID_VERSION: IngestReason.INVALID_HEADER,
    CcsdsErrorCode.OVERSIZED_PACKET: IngestReason.OVERSIZED_PACKET,
}

_DECODE_REASON = {
    DecodeErrorCode.EMPTY_PAYLOAD: IngestReason.EMPTY_PAYLOAD,
    DecodeErrorCode.TRUNCATED_RECORD: IngestReason.TRUNCATED_RECORD,
    DecodeErrorCode.UNKNOWN_CHANNEL: IngestReason.UNKNOWN_CHANNEL,
}

_OFFER_REASON = {
    OfferOutcome.DUPLICATE_SEQUENCE: IngestReason.DUPLICATE_SEQUENCE,
    OfferOutcome.OUT_OF_ORDER_TOO_LATE: IngestReason.OUT_OF_ORDER_TOO_LATE,
    OfferOutcome.BUFFER_OVERFLOW: IngestReason.BUFFER_OVERFLOW,
}


@dataclass(frozen=True)
class IngestError:
    """One rejected/anomalous event, with a stable reason code (contract §15)."""

    reason: IngestReason
    detail: str
    apid: Optional[int] = None
    sequence_count: Optional[int] = None


@dataclass
class IngestStats:
    """Per-session counters — the evidence the honesty audit (STEP 9) requires.

    No capability is claimed without a counter or test proving it (contract §16).
    """

    packets_received: int = 0
    packets_accepted: int = 0
    telecommand_dropped: int = 0
    duplicates: int = 0
    reordered: int = 0
    too_late: int = 0
    overflow: int = 0
    gaps_detected: int = 0
    readings_emitted: int = 0
    malformed_values: int = 0
    unknown_channels: int = 0
    buffer_high_water: int = 0
    stream_ended_reason: Optional[str] = None
    rejected_by_reason: Dict[str, int] = field(default_factory=dict)

    def _count(self, reason: IngestReason) -> None:
        self.rejected_by_reason[reason.value] = self.rejected_by_reason.get(reason.value, 0) + 1

    def as_dict(self) -> Dict[str, Any]:
        return {
            "packets_received": self.packets_received,
            "packets_accepted": self.packets_accepted,
            "telecommand_dropped": self.telecommand_dropped,
            "duplicates": self.duplicates,
            "reordered": self.reordered,
            "too_late": self.too_late,
            "overflow": self.overflow,
            "gaps_detected": self.gaps_detected,
            "readings_emitted": self.readings_emitted,
            "malformed_values": self.malformed_values,
            "unknown_channels": self.unknown_channels,
            "buffer_high_water": self.buffer_high_water,
            "stream_ended_reason": self.stream_ended_reason,
            "rejected_by_reason": dict(self.rejected_by_reason),
        }


@dataclass
class IngestResult:
    """The adapter's full output: the pipeline-ready dict plus observability."""

    #: Crash-dump-shaped dict for the EXISTING pipeline. Its
    #: ``pre_fault_telemetry_window`` is a list of canonical entry dicts.
    crash_dump: Dict[str, Any]
    #: The same window as validated ``TelemetryEntry`` objects (canonical readings).
    entries: List[TelemetryEntry]
    stats: IngestStats
    errors: List[IngestError]


class StreamIngestAdapter:
    """Drives one ingest session over a byte source. Deterministic; never raises
    out of :meth:`ingest` for stream/packet faults — they become reason-coded
    stats/errors and a flush of whatever was validly received."""

    def __init__(
        self,
        *,
        reorder_window: int = DEFAULT_REORDER_WINDOW,
        max_readings: int = DEFAULT_MAX_READINGS,
        sample_spacing_s: float = DEFAULT_SAMPLE_SPACING_S,
        max_data_field_octets: int = MAX_DATA_FIELD_OCTETS,
        max_error_samples: int = 256,
    ) -> None:
        self.reorder_window = reorder_window
        self.max_readings = max_readings
        self.sample_spacing_s = sample_spacing_s
        self.max_data_field_octets = max_data_field_octets
        self.max_error_samples = max_error_samples

    def ingest(self, source: "bytes | bytearray | Iterable[bytes]") -> IngestResult:
        """Consume ``source`` fully and return the pipeline-ready result."""
        stats = IngestStats()
        errors: List[IngestError] = []
        buffer = SequenceOrderedBuffer(
            reorder_window=self.reorder_window, max_readings=self.max_readings
        )

        def record(err: IngestError) -> None:
            stats._count(err.reason)
            if len(errors) < self.max_error_samples:
                errors.append(err)

        packets = iter_space_packets(
            source, max_data_field_octets=self.max_data_field_octets
        )
        try:
            for packet in packets:
                self._handle_packet(packet, buffer, stats, record)
        except CcsdsParseError as exc:
            # A structural fault ends the stream deterministically (SPP has no sync
            # marker to resync on — see ccsds.iter_space_packets). Whatever was
            # validly received before it is still flushed below.
            reason = _CCSDS_REASON.get(exc.code, IngestReason.TRUNCATED_PACKET)
            record(IngestError(reason, f"stream stopped: {exc}"))
            stats.stream_ended_reason = reason.value
        except Exception as exc:  # noqa: BLE001 — any source error = disconnect
            # The transport itself failed mid-stream. This is a DISCONNECT, not
            # healthy telemetry: flush what we have, emit no fabricated readings.
            record(IngestError(IngestReason.STREAM_DISCONNECT, f"source error: {exc!r}"))
            stats.stream_ended_reason = IngestReason.STREAM_DISCONNECT.value

        entries = self._drain_to_entries(buffer, stats)
        stats.buffer_high_water = buffer.high_water
        stats.readings_emitted = len(entries)

        crash_dump: Dict[str, Any] = {
            "scenario_id": None,
            "fault_type": None,
            # Provenance so an audit can see this window came from the streaming
            # ingest layer, not a hand-authored batch. Extra keys are allowed and
            # ignored by canonical_window().
            "telemetry_source": "ccsds_synthetic_stream",
            "ingest_layout": "SENTINEL_SYNTHETIC_TM_LAYOUT_V1",
            "pre_fault_telemetry_window": [e.model_dump(mode="json") for e in entries],
        }
        return IngestResult(crash_dump=crash_dump, entries=entries, stats=stats, errors=errors)

    def ingest_to_crash_dump(
        self, source: "bytes | bytearray | Iterable[bytes]"
    ) -> Dict[str, Any]:
        """Convenience: just the crash-dump dict for the existing pipeline."""
        return self.ingest(source).crash_dump

    # ── internals ──────────────────────────────────────────────────────────

    def _handle_packet(self, packet: SpacePacket, buffer, stats, record) -> None:
        stats.packets_received += 1

        # §9 unsupported: telecommand is not ingested as telemetry in Phase 2.
        if not packet.is_telemetry:
            stats.telecommand_dropped += 1
            record(
                IngestError(
                    IngestReason.UNSUPPORTED_TYPE,
                    "telecommand packet not ingested as telemetry",
                    apid=packet.apid,
                    sequence_count=packet.sequence_count,
                )
            )
            return

        decoded = decode_data_field(packet.apid, packet.data_field)
        for derr in decoded.errors:
            reason = _DECODE_REASON.get(derr.code, IngestReason.MALFORMED_VALUE)
            if reason is IngestReason.UNKNOWN_CHANNEL:
                stats.unknown_channels += 1
            record(
                IngestError(reason, derr.detail, apid=packet.apid, sequence_count=packet.sequence_count)
            )

        # A packet whose payload yielded no samples at all produces no reading.
        if not decoded.samples:
            return

        result = buffer.offer(packet.apid, packet.sequence_count, decoded.samples)
        if result.accepted:
            stats.packets_accepted += 1
            if result.outcome is OfferOutcome.ACCEPTED_REORDERED:
                stats.reordered += 1
            if result.gap:
                stats.gaps_detected += result.gap
            # Count malformed-value samples that were accepted as explicit-unusable.
            for s in decoded.samples:
                if s.quality is SampleQuality.MALFORMED_VALUE:
                    stats.malformed_values += 1
                    record(
                        IngestError(
                            IngestReason.MALFORMED_VALUE,
                            f"non-finite value on known channel '{s.channel_id}'",
                            apid=packet.apid,
                            sequence_count=packet.sequence_count,
                        )
                    )
        else:
            reason = _OFFER_REASON[result.outcome]
            if result.outcome is OfferOutcome.DUPLICATE_SEQUENCE:
                stats.duplicates += 1
            elif result.outcome is OfferOutcome.OUT_OF_ORDER_TOO_LATE:
                stats.too_late += 1
            elif result.outcome is OfferOutcome.BUFFER_OVERFLOW:
                stats.overflow += 1
            record(
                IngestError(reason, result.outcome.value, apid=packet.apid, sequence_count=packet.sequence_count)
            )

    def _drain_to_entries(self, buffer, stats) -> List[TelemetryEntry]:
        """Turn accepted packets into canonical ``TelemetryEntry`` objects.

        Timestamp policy (§4, [ASSUMPTION] spacing): the newest accepted packet is
        placed at T+0.000s and each earlier packet at ``-(steps) * spacing_s``.
        This is a documented synthetic spacing, not wall-clock; it exists only to
        put readings on the pipeline's existing relative-time axis.
        """
        pending = buffer.drain()
        if not pending:
            return []

        max_index = max(p.ingest_index for p in pending)
        entries: List[TelemetryEntry] = []
        for p in pending:
            offset = -(max_index - p.ingest_index) * self.sample_spacing_s
            timestamp = f"T{offset:+.3f}s"
            for s in p.samples:
                usable = s.quality is SampleQuality.USABLE and s.value is not None
                entries.append(
                    TelemetryEntry(
                        timestamp=timestamp,
                        parameter=s.channel_id,
                        relative_time_s=offset,
                        value=s.value if usable else None,
                        # An unusable reading carries NO value_text of our own: the
                        # TelemetryEntry model stamps the pipeline's canonical
                        # "MISSING" token, which the downstream validity/limit
                        # checks (app.validation.conditions) recognize as unusable.
                        # A custom label like "MALFORMED_VALUE" is NOT recognized by
                        # those checks and would leak an unusable reading through as
                        # a healthy value — the exact "ingestion failure treated as
                        # telemetry" hole this layer exists to prevent. The
                        # malformed-vs-absent distinction is preserved in IngestStats
                        # (malformed_values) and IngestError, not in value_text.
                        value_text=None,
                        unit=s.unit,
                        status=TelemetryStatus.UNKNOWN,
                    )
                )
        return entries
