"""
SENTINEL — Streaming Telemetry Ingestion tests (test_ingest_ccsds_streaming.py)

Phase 2 (LIVE TELEMETRY INGESTION HARDENING), STEPs 6 & 7. Exercises the new
ingestion layer end to end:

    ingest/ccsds.py          real CCSDS Space Packet Protocol primary-header parser
    ingest/mission_decode.py SYNTHETIC self-describing mission decoder
    ingest/stream_buffer.py  bounded, deterministic dedup + reorder buffer
    ingest/stream_adapter.py the spine that terminates in canonical TelemetryEntry

Run:
    python3 -m unittest tests.test_ingest_ccsds_streaming -v

Grouped by the guarantee under test:

  1. CCSDS PARSING        the primary header is parsed exactly, big-endian
  2. CCSDS REJECTION      malformed octets are rejected deterministically
  3. MISSION DECODE       the synthetic layout round-trips; unknown/bad handled
  4. SEQUENCE BUFFER      dedup, ordering, reorder window, bounded memory
  5. STEP-6 FAILURE MODES the 15 mandated cases, each with its packet definition
  6. FAIL-CLOSED          no failure path ever yields a NOMINAL reading
  7. DOWNSTREAM           the output feeds the EXISTING canonical interface unchanged

The synthetic test packet layout used throughout (SENTINEL_SYNTHETIC_TM_LAYOUT_V1):
    data field octet 0        uint8   record count N (1..255)
    per record:
      octet 0                 uint8   channel-name length L (1..255)
      octets 1..L             UTF-8   channel name (validated vs the real dictionary)
      octets L+1..L+8         >d      IEEE-754 float64, big-endian
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.api.adapters import canonical_window                       # noqa: E402
from app.api.models import TelemetryStatus                          # noqa: E402
from app.ingest.ccsds import (                                      # noqa: E402
    MAX_DATA_FIELD_OCTETS,
    PRIMARY_HEADER_OCTETS,
    SEQUENCE_COUNT_MODULUS,
    CcsdsErrorCode,
    CcsdsParseError,
    PacketType,
    SequenceFlags,
    build_space_packet,
    iter_space_packets,
    parse_primary_header,
    parse_space_packet,
    sequence_gap,
)
from app.ingest.channel_dict import channel_ids, get_channel        # noqa: E402
from app.ingest.mission_decode import (                             # noqa: E402
    DecodeErrorCode,
    SampleQuality,
    build_synthetic_tm_payload,
    decode_data_field,
)
from app.ingest.stream_adapter import (                             # noqa: E402
    IngestReason,
    StreamIngestAdapter,
)
from app.ingest.stream_buffer import (                              # noqa: E402
    OfferOutcome,
    SequenceOrderedBuffer,
)


# --- shared test fixtures -------------------------------------------------

def _real_channels(n: int = 3) -> list[str]:
    """Return ``n`` channel ids that the REAL dictionary declares."""
    ids = list(channel_ids())
    assert len(ids) >= n, "channel dictionary unexpectedly small"
    return ids[:n]


def _tm(apid: int, seq: int, measurements, ptype: PacketType = PacketType.TELEMETRY) -> bytes:
    """Build one synthetic telemetry Space Packet."""
    return build_space_packet(
        apid=apid,
        sequence_count=seq,
        data_field=build_synthetic_tm_payload(measurements),
        packet_type=ptype,
    )


# =========================================================================
# 1. CCSDS PARSING — the real, standardized primary header
# =========================================================================

class TestCcsdsPrimaryHeaderParsing(unittest.TestCase):
    def test_round_trip_extracts_every_field(self):
        raw = build_space_packet(apid=0x2AB, sequence_count=1234, data_field=b"\x01\x02\x03")
        pkt = parse_space_packet(raw)
        h = pkt.primary_header
        self.assertEqual(h.version, 0)
        self.assertEqual(h.apid, 0x2AB)
        self.assertEqual(h.sequence_count, 1234)
        self.assertIs(h.packet_type, PacketType.TELEMETRY)
        self.assertTrue(pkt.is_telemetry)
        self.assertEqual(pkt.data_field, b"\x01\x02\x03")
        self.assertEqual(h.total_octets, len(raw))
        self.assertEqual(h.data_field_octets, 3)

    def test_header_is_big_endian(self):
        # APID 1 in the low 11 bits of the first 16-bit word => octet0=0x00, octet1=0x01
        raw = build_space_packet(apid=1, sequence_count=0, data_field=b"Z")
        self.assertEqual(raw[0], 0x00)
        self.assertEqual(raw[1], 0x01)

    def test_telecommand_type_is_recognised_not_hidden(self):
        raw = _tm(5, 0, [(_real_channels()[0], 1.0)], ptype=PacketType.TELECOMMAND)
        pkt = parse_space_packet(raw)
        self.assertIs(pkt.primary_header.packet_type, PacketType.TELECOMMAND)
        self.assertFalse(pkt.is_telemetry)

    def test_sequence_flags_preserved(self):
        raw = build_space_packet(
            apid=1, sequence_count=0, data_field=b"Z", sequence_flags=SequenceFlags.FIRST
        )
        self.assertIs(parse_primary_header(raw).sequence_flags, SequenceFlags.FIRST)

    def test_sequence_gap_wraps_modulo_16384(self):
        self.assertEqual(sequence_gap(SEQUENCE_COUNT_MODULUS - 1, 0), 1)  # wrap
        self.assertEqual(sequence_gap(5, 5), 0)                           # duplicate
        self.assertEqual(sequence_gap(5, 9), 4)                           # gap
        self.assertEqual(sequence_gap(5, 4), SEQUENCE_COUNT_MODULUS - 1)  # behind

    def test_deframe_multiple_packets_across_ragged_chunks(self):
        a = _tm(1, 0, [(_real_channels()[0], 1.0)])
        b = _tm(2, 0, [(_real_channels()[1], 2.0)])
        stream = a + b
        chunks = [stream[:3], stream[3:7], stream[7:]]  # boundaries misaligned
        got = list(iter_space_packets(chunks))
        self.assertEqual([p.apid for p in got], [1, 2])


# =========================================================================
# 2. CCSDS REJECTION — deterministic, never reinterpreted as telemetry
# =========================================================================

class TestCcsdsMalformedRejection(unittest.TestCase):
    def test_truncated_header(self):
        with self.assertRaises(CcsdsParseError) as ctx:
            parse_primary_header(b"\x00\x00\x00")
        self.assertIs(ctx.exception.code, CcsdsErrorCode.TRUNCATED_HEADER)

    def test_invalid_version(self):
        raw = bytearray(build_space_packet(apid=1, sequence_count=0, data_field=b"Z"))
        raw[0] = 0xE0  # version bits = 0b111
        with self.assertRaises(CcsdsParseError) as ctx:
            parse_primary_header(bytes(raw))
        self.assertIs(ctx.exception.code, CcsdsErrorCode.INVALID_VERSION)

    def test_truncated_data(self):
        raw = build_space_packet(apid=1, sequence_count=0, data_field=b"ABCD")
        with self.assertRaises(CcsdsParseError) as ctx:
            parse_space_packet(raw[:-1])
        self.assertIs(ctx.exception.code, CcsdsErrorCode.TRUNCATED_DATA)

    def test_oversized_against_operational_cap(self):
        raw = build_space_packet(apid=1, sequence_count=0, data_field=b"X" * 40)
        with self.assertRaises(CcsdsParseError) as ctx:
            parse_space_packet(raw, max_data_field_octets=16)
        self.assertIs(ctx.exception.code, CcsdsErrorCode.OVERSIZED_PACKET)

    def test_default_cap_equals_theoretical_field_maximum(self):
        # Documents that with the default cap the oversized branch is unreachable
        # by construction: the 16-bit length field cannot encode a larger field.
        self.assertEqual(MAX_DATA_FIELD_OCTETS, 1 << 16)

    def test_trailing_partial_packet_is_truncation_not_a_packet(self):
        a = _tm(1, 0, [(_real_channels()[0], 1.0)])
        b = _tm(1, 1, [(_real_channels()[0], 2.0)])
        with self.assertRaises(CcsdsParseError) as ctx:
            list(iter_space_packets(a + b[:-2]))
        self.assertIs(ctx.exception.code, CcsdsErrorCode.TRUNCATED_DATA)

    def test_parser_never_emits_a_reading_on_rejection(self):
        # A rejected packet raises; it does not silently yield a SpacePacket.
        with self.assertRaises(CcsdsParseError):
            parse_space_packet(b"\xff\xff\xff\xff\xff\xff\xff")


# =========================================================================
# 3. MISSION DECODE — synthetic, validated against the REAL dictionary
# =========================================================================

class TestSyntheticMissionDecode(unittest.TestCase):
    def test_round_trip_known_channels(self):
        c0, c1, _ = _real_channels()
        res = decode_data_field(100, build_synthetic_tm_payload([(c0, 3.5), (c1, 42.0)]))
        self.assertEqual(res.errors, [])
        self.assertEqual(len(res.samples), 2)
        self.assertEqual(res.samples[0].channel_id, get_channel(c0).channel_id)
        self.assertTrue(res.samples[0].is_usable)
        self.assertEqual(res.samples[0].value, 3.5)

    def test_unknown_channel_produces_no_sample(self):
        res = decode_data_field(1, build_synthetic_tm_payload([("not_a_real_channel_xyz", 1.0)]))
        self.assertEqual(res.samples, [])
        self.assertEqual(len(res.errors), 1)
        self.assertIs(res.errors[0].code, DecodeErrorCode.UNKNOWN_CHANNEL)

    def test_nonfinite_value_on_known_channel_is_explicit_unusable(self):
        c0 = _real_channels()[0]
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                res = decode_data_field(1, build_synthetic_tm_payload([(c0, bad)]))
                self.assertEqual(len(res.samples), 1)
                s = res.samples[0]
                self.assertIsNone(s.value)
                self.assertIs(s.quality, SampleQuality.MALFORMED_VALUE)
                self.assertFalse(s.is_usable)

    def test_empty_payload_record_count_zero(self):
        res = decode_data_field(1, bytes([0]))
        self.assertEqual(res.samples, [])
        self.assertIs(res.errors[0].code, DecodeErrorCode.EMPTY_PAYLOAD)

    def test_truncated_record_keeps_valid_prefix(self):
        c0, c1, _ = _real_channels()
        good = build_synthetic_tm_payload([(c0, 1.0), (c1, 2.0)])
        res = decode_data_field(1, good[:-3])  # chop the 2nd record's value
        self.assertEqual(len(res.samples), 1)
        self.assertTrue(any(e.code is DecodeErrorCode.TRUNCATED_RECORD for e in res.errors))

    def test_alias_and_case_resolve_to_canonical_id(self):
        c0 = _real_channels()[0]
        res = decode_data_field(1, build_synthetic_tm_payload([(c0.upper(), 9.0)]))
        self.assertTrue(res.samples)
        self.assertEqual(res.samples[0].channel_id, get_channel(c0).channel_id)

    def test_decoder_never_raises_on_garbage(self):
        for garbage in (b"", b"\x05\xff\xff", bytes(range(20)), b"\x01\x03abc"):
            with self.subTest(garbage=garbage):
                res = decode_data_field(1, garbage)
                self.assertIsNotNone(res)  # returned a result, did not raise


# =========================================================================
# 4. SEQUENCE BUFFER — dedup, ordering, bounded memory
# =========================================================================

def _sample(channel: str, value=1.0):
    # Build a real DecodedSample via the decoder so tests use the real type.
    return decode_data_field(1, build_synthetic_tm_payload([(channel, value)])).samples[0]


class TestSequenceBuffer(unittest.TestCase):
    def setUp(self):
        self.c0 = _real_channels()[0]

    def test_in_order_accepts_and_counts_gap(self):
        buf = SequenceOrderedBuffer()
        self.assertIs(buf.offer(1, 0, [_sample(self.c0)]).outcome, OfferOutcome.ACCEPTED_IN_ORDER)
        r = buf.offer(1, 3, [_sample(self.c0)])  # skipped 1,2
        self.assertIs(r.outcome, OfferOutcome.ACCEPTED_IN_ORDER)
        self.assertEqual(r.gap, 2)

    def test_duplicate_sequence_dropped(self):
        buf = SequenceOrderedBuffer()
        buf.offer(1, 7, [_sample(self.c0)])
        self.assertIs(buf.offer(1, 7, [_sample(self.c0)]).outcome, OfferOutcome.DUPLICATE_SEQUENCE)

    def test_reorder_within_window_accepted(self):
        buf = SequenceOrderedBuffer(reorder_window=8)
        buf.offer(1, 10, [_sample(self.c0)])
        r = buf.offer(1, 6, [_sample(self.c0)])  # 4 behind, within window
        self.assertIs(r.outcome, OfferOutcome.ACCEPTED_REORDERED)

    def test_out_of_order_beyond_window_rejected(self):
        buf = SequenceOrderedBuffer(reorder_window=2)
        buf.offer(1, 20, [_sample(self.c0)])
        r = buf.offer(1, 5, [_sample(self.c0)])  # 15 behind, beyond window
        self.assertIs(r.outcome, OfferOutcome.OUT_OF_ORDER_TOO_LATE)

    def test_per_apid_sequences_are_independent(self):
        buf = SequenceOrderedBuffer()
        self.assertTrue(buf.offer(1, 5, [_sample(self.c0)]).accepted)
        # same count on a DIFFERENT apid is not a duplicate
        self.assertTrue(buf.offer(2, 5, [_sample(self.c0)]).accepted)

    def test_overflow_refuses_and_reports_not_drop_oldest(self):
        buf = SequenceOrderedBuffer(max_readings=2)
        self.assertTrue(buf.offer(1, 0, [_sample(self.c0)]).accepted)
        self.assertTrue(buf.offer(1, 1, [_sample(self.c0)]).accepted)
        r = buf.offer(1, 2, [_sample(self.c0)])  # would exceed cap
        self.assertIs(r.outcome, OfferOutcome.BUFFER_OVERFLOW)
        # the earlier readings are retained (NOT dropped to make room)
        self.assertEqual(buf.buffered_readings, 2)

    def test_high_water_is_tracked(self):
        buf = SequenceOrderedBuffer()
        buf.offer(1, 0, [_sample(self.c0), _sample(self.c0)])
        self.assertEqual(buf.high_water, 2)
        buf.drain()
        self.assertEqual(buf.high_water, 2)  # high-water is not reset by drain

    def test_drain_returns_acceptance_order_and_clears(self):
        buf = SequenceOrderedBuffer()
        buf.offer(1, 0, [_sample(self.c0)])
        buf.offer(1, 1, [_sample(self.c0)])
        drained = buf.drain()
        self.assertEqual([p.ingest_index for p in drained], [0, 1])
        self.assertEqual(buf.drain(), [])  # emptied

    def test_wrap_boundary_is_in_order(self):
        buf = SequenceOrderedBuffer()
        buf.offer(1, SEQUENCE_COUNT_MODULUS - 1, [_sample(self.c0)])
        r = buf.offer(1, 0, [_sample(self.c0)])  # wraps to next
        self.assertIs(r.outcome, OfferOutcome.ACCEPTED_IN_ORDER)
        self.assertEqual(r.gap, 0)


# =========================================================================
# 5. STEP-6 FAILURE MODES — the 15 mandated cases, each documented
# =========================================================================

class TestStep6FifteenFailureModes(unittest.TestCase):
    """One test per STEP-6 case. Each builds an explicitly-defined packet/stream
    and asserts the deterministic outcome. The invariant across all of them:
    a failure NEVER becomes a healthy reading."""

    def setUp(self):
        self.c0, self.c1, self.c2 = _real_channels()

    def test_01_valid(self):
        res = StreamIngestAdapter().ingest(_tm(100, 0, [(self.c0, 1.0), (self.c1, 2.0)]))
        self.assertEqual(res.stats.packets_accepted, 1)
        self.assertEqual(res.stats.readings_emitted, 2)

    def test_02_truncated_packet(self):
        good = _tm(1, 0, [(self.c0, 1.0)])
        res = StreamIngestAdapter().ingest(good + _tm(1, 1, [(self.c1, 2.0)])[:-2])
        self.assertEqual(res.stats.stream_ended_reason, IngestReason.TRUNCATED_PACKET.value)
        self.assertEqual(res.stats.readings_emitted, 1)  # valid prefix flushed

    def test_03_invalid_length_declares_more_than_present(self):
        # A single packet whose declared length exceeds the bytes present: the
        # deframer holds it as an incomplete trailing packet -> truncation.
        raw = bytearray(_tm(1, 0, [(self.c0, 1.0)]))
        raw[5] = raw[5] + 50  # inflate the data-length field
        res = StreamIngestAdapter().ingest(bytes(raw))
        self.assertEqual(res.stats.stream_ended_reason, IngestReason.TRUNCATED_PACKET.value)
        self.assertEqual(res.stats.readings_emitted, 0)

    def test_04_invalid_header(self):
        raw = bytearray(_tm(1, 0, [(self.c0, 1.0)]))
        raw[0] = 0xE0  # illegal version
        res = StreamIngestAdapter().ingest(bytes(raw))
        self.assertEqual(res.stats.stream_ended_reason, IngestReason.INVALID_HEADER.value)
        self.assertEqual(res.stats.readings_emitted, 0)

    def test_05_unsupported_type_telecommand(self):
        res = StreamIngestAdapter().ingest(_tm(9, 0, [(self.c0, 1.0)], ptype=PacketType.TELECOMMAND))
        self.assertEqual(res.stats.telecommand_dropped, 1)
        self.assertEqual(res.stats.readings_emitted, 0)
        self.assertEqual(res.stats.rejected_by_reason.get(IngestReason.UNSUPPORTED_TYPE.value), 1)

    def test_06_duplicate(self):
        res = StreamIngestAdapter().ingest(_tm(7, 5, [(self.c0, 1.0)]) + _tm(7, 5, [(self.c0, 9.9)]))
        self.assertEqual(res.stats.duplicates, 1)
        self.assertEqual(res.stats.readings_emitted, 1)

    def test_07_out_of_order(self):
        stream = _tm(3, 0, [(self.c0, 1.0)]) + _tm(3, 20, [(self.c0, 2.0)]) + _tm(3, 5, [(self.c0, 3.0)])
        res = StreamIngestAdapter(reorder_window=2).ingest(stream)
        self.assertEqual(res.stats.too_late, 1)

    def test_08_sequence_regression_beyond_window(self):
        # SENTINEL has no wall-clock, so a "timestamp regression" is modeled as a
        # sequence count going far backwards: rejected, never accepted as latest.
        stream = _tm(4, 500, [(self.c0, 1.0)]) + _tm(4, 3, [(self.c0, 2.0)])
        res = StreamIngestAdapter(reorder_window=8).ingest(stream)
        self.assertEqual(res.stats.too_late, 1)
        self.assertEqual(res.stats.readings_emitted, 1)  # only the first accepted

    def test_09_oversized(self):
        good = _tm(1, 0, [(self.c0, 1.0)])
        big = build_space_packet(apid=1, sequence_count=1, data_field=b"Y" * 300)
        res = StreamIngestAdapter(max_data_field_octets=64).ingest(good + big)
        self.assertEqual(res.stats.stream_ended_reason, IngestReason.OVERSIZED_PACKET.value)
        self.assertEqual(res.stats.readings_emitted, 1)

    def test_10_burst(self):
        stream = b"".join(_tm(1, i, [(self.c0, float(i))]) for i in range(200))
        res = StreamIngestAdapter().ingest(stream)
        self.assertEqual(res.stats.packets_accepted, 200)
        self.assertEqual(res.stats.readings_emitted, 200)

    def test_11_empty_payload(self):
        # A valid packet whose data field declares zero records.
        raw = build_space_packet(apid=1, sequence_count=0, data_field=bytes([0]))
        res = StreamIngestAdapter().ingest(raw)
        self.assertEqual(res.stats.readings_emitted, 0)
        self.assertEqual(res.stats.rejected_by_reason.get(IngestReason.EMPTY_PAYLOAD.value), 1)

    def test_12_unknown_channel(self):
        res = StreamIngestAdapter().ingest(_tm(1, 0, [("totally_unknown_channel", 1.0)]))
        self.assertEqual(res.stats.unknown_channels, 1)
        self.assertEqual(res.stats.readings_emitted, 0)

    def test_13_malformed_value(self):
        res = StreamIngestAdapter().ingest(_tm(1, 0, [(self.c0, float("nan"))]))
        self.assertEqual(res.stats.readings_emitted, 1)
        e = res.entries[0]
        self.assertIsNone(e.value)
        # The canonical unusable token the whole pipeline recognizes — NOT a custom
        # label. A custom value_text would leak past the downstream validity checks.
        self.assertEqual(e.value_text, "MISSING")
        self.assertIs(e.status, TelemetryStatus.UNKNOWN)
        # the malformed-vs-absent distinction is preserved in the stats/error stream
        self.assertEqual(res.stats.malformed_values, 1)
        self.assertEqual(res.stats.rejected_by_reason.get(IngestReason.MALFORMED_VALUE.value), 1)

    def test_14_stream_disconnect(self):
        def source():
            yield _tm(2, 0, [(self.c0, 1.0)])
            raise ConnectionError("link lost")
        res = StreamIngestAdapter().ingest(source())
        self.assertEqual(res.stats.stream_ended_reason, IngestReason.STREAM_DISCONNECT.value)
        self.assertEqual(res.stats.readings_emitted, 1)  # what was received is flushed

    def test_15_buffer_overflow(self):
        stream = b"".join(_tm(1, i, [(self.c0, float(i))]) for i in range(10))
        res = StreamIngestAdapter(max_readings=4).ingest(stream)
        self.assertEqual(res.stats.readings_emitted, 4)  # capped
        self.assertGreaterEqual(res.stats.overflow, 1)
        self.assertGreaterEqual(res.stats.rejected_by_reason.get(IngestReason.BUFFER_OVERFLOW.value, 0), 1)


# =========================================================================
# 6. FAIL-CLOSED — no failure path ever yields a NOMINAL reading
# =========================================================================

class TestFailClosedInvariant(unittest.TestCase):
    """The Phase-1 spine, preserved: a telemetry-ingestion failure must never be
    interpreted as healthy telemetry."""

    def setUp(self):
        self.c0 = _real_channels()[0]

    def test_no_emitted_reading_is_ever_nominal(self):
        # Throw a battery of malformed / degenerate streams at the adapter and
        # assert nothing it emits claims NOMINAL — the ingest layer never asserts
        # health; only detection may classify.
        c0 = self.c0
        cases = [
            _tm(1, 0, [(c0, float("nan"))]),                 # malformed value
            _tm(1, 0, [("unknown_xyz", 1.0)]),               # unknown channel
            build_space_packet(apid=1, sequence_count=0, data_field=bytes([0])),  # empty
            _tm(9, 0, [(c0, 1.0)], ptype=PacketType.TELECOMMAND),                  # TC
            _tm(1, 0, [(c0, 1.0)]) + _tm(1, 0, [(c0, 2.0)]),  # duplicate
            b"\xff\xff\xff\xff\xff\xff\xff\xff",              # garbage bytes
        ]
        for i, stream in enumerate(cases):
            with self.subTest(case=i):
                res = StreamIngestAdapter().ingest(stream)
                for e in res.entries:
                    self.assertIsNot(e.status, TelemetryStatus.NOMINAL)
                    self.assertNotEqual(e.status, TelemetryStatus.NOMINAL)

    def test_missing_channel_simply_absent_never_fabricated(self):
        # A stream carrying only c0 must not invent any reading for another channel.
        res = StreamIngestAdapter().ingest(_tm(1, 0, [(self.c0, 1.0)]))
        channels = {e.parameter for e in res.entries}
        self.assertEqual(channels, {get_channel(self.c0).channel_id})

    def test_total_failure_yields_zero_readings_not_a_default(self):
        # A wholly malformed stream yields NO readings — not a fabricated default.
        res = StreamIngestAdapter().ingest(b"\x00\x01\x02")  # < 6 octets, truncated header
        self.assertEqual(res.entries, [])
        self.assertEqual(res.stats.readings_emitted, 0)


# =========================================================================
# 7. DOWNSTREAM — output feeds the EXISTING canonical interface unchanged
# =========================================================================

class TestDownstreamIntegration(unittest.TestCase):
    def setUp(self):
        self.c0, self.c1, _ = _real_channels()

    def test_output_is_consumed_by_existing_canonical_window(self):
        stream = _tm(100, 0, [(self.c0, 1.0), (self.c1, 10.0)]) + _tm(100, 1, [(self.c0, 2.0)])
        res = StreamIngestAdapter().ingest(stream)
        window = canonical_window(res.crash_dump)  # the EXISTING function, unmodified
        self.assertEqual(len(window), 3)
        self.assertEqual({e.parameter for e in window},
                         {get_channel(self.c0).channel_id, get_channel(self.c1).channel_id})

    def test_relative_time_places_newest_at_zero(self):
        stream = _tm(1, 0, [(self.c0, 1.0)]) + _tm(1, 1, [(self.c0, 2.0)]) + _tm(1, 2, [(self.c0, 3.0)])
        res = StreamIngestAdapter(sample_spacing_s=1.0).ingest(stream)
        offsets = sorted(e.relative_time_s for e in res.entries)
        self.assertEqual(offsets, [-2.0, -1.0, 0.0])

    def test_detection_consumes_stream_output_without_error(self):
        try:
            from app.detection.fusion import run_detection_on_crash_dump
        except ImportError:
            self.skipTest("detection package not importable in this environment")
        stream = _tm(100, 0, [(self.c0, 1.0), (self.c1, 10.0)])
        res = StreamIngestAdapter().ingest(stream)
        report = run_detection_on_crash_dump(res.crash_dump)  # must not raise
        self.assertIsNotNone(report)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
