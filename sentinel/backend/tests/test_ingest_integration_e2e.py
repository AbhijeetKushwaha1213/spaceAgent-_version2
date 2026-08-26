"""
SENTINEL — Phase 2 end-to-end ingestion integration (test_ingest_integration_e2e.py)

STEP 8. Proves the streaming ingestion layer connects to the EXISTING pipeline
without altering it, and — most importantly — that a telemetry-ingestion failure
never becomes healthy telemetry once it reaches the REAL, unmodified Phase-1
safety gate.

Two things are demonstrated:

  A. SAME CANONICAL OBJECTS. A window produced by streaming raw CCSDS bytes
     through the adapter is, after ``canonical_window(...)``, the same kind of
     canonical ``TelemetryEntry`` series the batch path yields — same parameters,
     same latest values — and every streamed reading carries status=UNKNOWN (the
     adapter never asserts NOMINAL).

  B. FAIL-CLOSED THROUGH THE REAL GATE. Raw synthetic binary telemetry is fed to
     the adapter, the resulting crash dump is handed to the ACTUAL
     ``validate_recovery_plan`` → ``apply_validation_to_output`` gate (imported
     from app.agent.safety, unchanged), and a safety-critical command that
     requires a live gyro is authorized ONLY when finite gyro telemetry is
     actually present. Every failure mode (disconnect, malformed value, truncation,
     garbage) leaves the gyro absent-or-unusable and the command BLOCKED.

Explicitly documented synthetic test packet definitions
-------------------------------------------------------
CCSDS Space Packet (ingest/ccsds.py), 6-octet big-endian primary header:
    version=0b000, type=TM(0), sec-hdr=0, APID=0x010, seq flags=UNSEGMENTED,
    packet sequence count as noted per packet, data length = len(data_field)-1.

Data field = SENTINEL_SYNTHETIC_TM_LAYOUT_V1 (ingest/mission_decode.py):
    octet 0            uint8   record count N
    per record:
      octet 0          uint8   channel-name length L
      octets 1..L      UTF-8   channel name (validated vs the REAL dictionary)
      octets L+1..L+8  >d      IEEE-754 float64 big-endian value

Channels used (all confirmed present in app.ingest.channel_dict):
    "Gyro_rate_degs"   unit deg/s   — gyro rate; GYRO_DATA_VALID precondition
    "SoC_pct"          unit %       — battery state of charge

Safety-critical command under test:
    CMD_SUN_ACQUISITION — required precondition GYRO_DATA_VALID (attitude
    actuation). On absent gyro the gate blocks MISSING_PRECONDITION; on a
    malformed (non-finite) gyro it blocks GYRO_HEALTH_PREREQUISITE.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.agent.safety import apply_validation_to_output, validate_recovery_plan  # noqa: E402
from app.api.adapters import canonical_window                                    # noqa: E402
from app.api.models import (                                                     # noqa: E402
    Hypothesis,
    RecoveryStep,
    RiskLevel,
    SafetyStatus,
    SentinelOutput,
    TelemetryEntry,
    TelemetryStatus,
)
from app.ingest.ccsds import PacketType, build_space_packet                      # noqa: E402
from app.ingest.mission_decode import build_synthetic_tm_payload                 # noqa: E402
from app.ingest.stream_adapter import IngestReason, StreamIngestAdapter          # noqa: E402

GYRO_CHANNEL = "Gyro_rate_degs"
SOC_CHANNEL = "SoC_pct"
GYRO_CMD = "CMD_SUN_ACQUISITION"  # requires GYRO_DATA_VALID (safety-critical)

# Reason/violation codes emitted by the REAL gate (asserted, not fabricated).
CODE_MISSING = "MISSING_PRECONDITION"          # required precondition telemetry absent
CODE_GYRO_INVALID = "GYRO_HEALTH_PREREQUISITE"  # required precondition present-but-malformed


# --- packet / stream builders (documented layout above) -------------------

def _packet(seq: int, measurements, apid: int = 0x010,
            ptype: PacketType = PacketType.TELEMETRY) -> bytes:
    return build_space_packet(
        apid=apid, sequence_count=seq,
        data_field=build_synthetic_tm_payload(measurements), packet_type=ptype,
    )


def _plan(command: str) -> SentinelOutput:
    """A minimal parsed-LLM output proposing exactly one command."""
    return SentinelOutput(
        hypotheses=[
            Hypothesis(rank=1, root_cause="ADCS_GYRO_SEU", affected_component="GYRO_A",
                       confidence=0.90, causal_chain=["SEU", "gyro anomaly"]),
            Hypothesis(rank=2, root_cause="ADCS_STAR_TRACKER_FAULT", affected_component="ST_A",
                       confidence=0.06, causal_chain=["ST degraded", "attitude drift"]),
            Hypothesis(rank=3, root_cause="OBC_WATCHDOG_OVERFLOW", affected_component="OBC",
                       confidence=0.04, causal_chain=["cpu high", "watchdog overflow"]),
        ],
        recovery_plan=[
            RecoveryStep(step=1, command=command, rationale="e2e ingestion test",
                         wait_seconds=10, verify="verify", risk=RiskLevel.MEDIUM),
        ],
        confidence=0.80,
        requires_human_review=False,
        reasoning_summary="Phase 2 end-to-end synthetic stream integration.",
    )


def _gate(crash_dump: dict):
    """Run the REAL deterministic safety gate against a crash dump."""
    output = _plan(GYRO_CMD)
    validation = validate_recovery_plan(output, crash_dump)
    final = apply_validation_to_output(output, validation)
    return final, validation


def _blocked_codes(validation) -> dict:
    return {b.original_step.command: b.violation_code for b in validation.blocked_steps}


def _authorized(validation) -> list:
    return [s.command for s in validation.validated_steps]


# =========================================================================
# A. The streamed window IS a canonical telemetry window
# =========================================================================

class TestStreamedWindowIsCanonical(unittest.TestCase):
    def setUp(self):
        stream = (
            _packet(0, [(GYRO_CHANNEL, 0.10), (SOC_CHANNEL, 90.0)])
            + _packet(1, [(GYRO_CHANNEL, 0.12)])
        )
        self.result = StreamIngestAdapter().ingest(stream)

    def test_adapter_entries_are_telemetry_entries(self):
        self.assertTrue(self.result.entries)
        self.assertTrue(all(isinstance(e, TelemetryEntry) for e in self.result.entries))

    def test_all_streamed_readings_are_unknown_never_nominal(self):
        for e in self.result.entries:
            self.assertIs(e.status, TelemetryStatus.UNKNOWN)

    def test_existing_canonical_window_reproduces_the_adapter_entries(self):
        # The pipeline's own function, unmodified, must see exactly the adapter's
        # readings: same (parameter, value) pairs — no lossy transform in between.
        window = canonical_window(self.result.crash_dump)
        got = sorted((e.parameter, e.value) for e in window)
        expected = sorted((e.parameter, e.value) for e in self.result.entries)
        self.assertEqual(got, expected)

    def test_parameters_match_the_real_dictionary_ids(self):
        params = {e.parameter for e in self.result.entries}
        # names round-trip to canonical dictionary ids (both are already canonical)
        self.assertIn(GYRO_CHANNEL, params)
        self.assertIn(SOC_CHANNEL, params)


# =========================================================================
# B. End-to-end fail-closed through the REAL Phase-1 safety gate
# =========================================================================

class TestEndToEndSyntheticStreamFailClosed(unittest.TestCase):
    """Raw bytes → adapter → crash dump → REAL safety gate. A safety-critical
    command is authorized ONLY when finite gyro telemetry is actually present."""

    def test_healthy_gyro_stream_authorizes_the_command(self):
        # A single valid TM packet carrying a finite gyro rate and battery SoC.
        stream = _packet(0, [(GYRO_CHANNEL, 0.10), (SOC_CHANNEL, 90.0)])
        crash_dump = StreamIngestAdapter().ingest(stream).crash_dump
        final, validation = _gate(crash_dump)
        self.assertIn(GYRO_CMD, _authorized(validation),
                      f"healthy gyro should authorize; blocked={_blocked_codes(validation)}")
        self.assertIs(validation.safety_status, SafetyStatus.VALIDATED)

    def test_disconnect_before_gyro_blocks_missing_precondition(self):
        # Transport delivers an unrelated channel (SoC) then drops. Gyro never
        # arrives → absent → the safety-critical command is blocked. Partial
        # telemetry does NOT rescue the missing required precondition.
        def source():
            yield _packet(0, [(SOC_CHANNEL, 90.0)])
            raise ConnectionError("link lost mid-stream")

        result = StreamIngestAdapter().ingest(source())
        self.assertEqual(result.stats.stream_ended_reason, IngestReason.STREAM_DISCONNECT.value)
        final, validation = _gate(result.crash_dump)
        self.assertNotIn(GYRO_CMD, _authorized(validation))
        self.assertEqual(_blocked_codes(validation).get(GYRO_CMD), CODE_MISSING)
        # the command was actually removed from the executable plan
        self.assertNotIn(GYRO_CMD, [s.command for s in final.recovery_plan])

    def test_malformed_gyro_value_blocks_and_never_authorizes(self):
        # Gyro packet carries a non-finite value; the adapter emits an EXPLICIT
        # unusable reading (value=None), never a fabricated number.
        stream = _packet(0, [(GYRO_CHANNEL, float("nan"))])
        result = StreamIngestAdapter().ingest(stream)
        self.assertEqual(result.stats.malformed_values, 1)
        self.assertEqual(result.entries[0].value, None)  # explicit unusable
        final, validation = _gate(result.crash_dump)
        self.assertNotIn(GYRO_CMD, _authorized(validation))
        self.assertEqual(_blocked_codes(validation).get(GYRO_CMD), CODE_GYRO_INVALID)

    def test_truncated_gyro_packet_blocks_missing_precondition(self):
        # A complete SoC packet, then a gyro packet with its trailing octets chopped.
        # The SoC reading flushes; the gyro reading is never emitted → gyro absent.
        good = _packet(0, [(SOC_CHANNEL, 90.0)])
        gyro = _packet(1, [(GYRO_CHANNEL, 0.10)])
        stream = good + gyro[:-3]
        result = StreamIngestAdapter().ingest(stream)
        self.assertEqual(result.stats.stream_ended_reason, IngestReason.TRUNCATED_PACKET.value)
        params = {e.parameter for e in result.entries}
        self.assertIn(SOC_CHANNEL, params)       # partial telemetry present
        self.assertNotIn(GYRO_CHANNEL, params)   # but the gyro is genuinely absent
        final, validation = _gate(result.crash_dump)
        self.assertNotIn(GYRO_CMD, _authorized(validation))
        self.assertEqual(_blocked_codes(validation).get(GYRO_CMD), CODE_MISSING)

    def test_garbage_stream_yields_no_readings_and_blocks(self):
        # Fewer than a full header of bytes: nothing is ever emitted as telemetry.
        result = StreamIngestAdapter().ingest(b"\xff\xff\xff\xff\xff")
        self.assertEqual(result.entries, [])
        final, validation = _gate(result.crash_dump)
        self.assertNotIn(GYRO_CMD, _authorized(validation))
        self.assertEqual(_blocked_codes(validation).get(GYRO_CMD), CODE_MISSING)

    def test_empty_crash_dump_baseline_also_blocks(self):
        # Control: with no telemetry at all the same command blocks the same way,
        # proving the streamed-failure verdict equals the "no telemetry" verdict —
        # a failure is treated exactly as absent, never as healthy.
        final, validation = _gate({"pre_fault_telemetry_window": []})
        self.assertEqual(_blocked_codes(validation).get(GYRO_CMD), CODE_MISSING)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
