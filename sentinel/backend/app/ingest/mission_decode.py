"""
SENTINEL — Synthetic Mission Telemetry Decoder (ingest/mission_decode.py)

Phase 2, STEP 3 (second half). This module turns the OPAQUE data field of a
CCSDS Space Packet (produced by ``ingest/ccsds.py``) into named, numeric
measurements. It is deliberately and explicitly **SYNTHETIC**.

WHY SYNTHETIC — read this before trusting any output
----------------------------------------------------
The Phase-1 audit and the Phase-2 pre-change map established that this repository
has **no byte-offset → telemetry-channel map** for any real spacecraft: the
channel dictionary (``app.ingest.channel_dict``) maps channel *names* to specs,
not payload *bytes* to channels. Fabricating a real-looking mission packet layout
and presenting it as authentic would be exactly the kind of "make it look more
real than it is" that this project forbids.

So this decoder does NOT claim to decode any real mission's packets. It decodes a
single, fully documented, **self-describing test layout**
(``SENTINEL_SYNTHETIC_TM_LAYOUT_V1``) in which each measurement carries its own
channel *name* as text. Because the name travels inside the packet, the decoder
never has to guess a byte→channel mapping — it reads the name and then validates
it against the **real** channel dictionary (``get_channel``). Names the dictionary
does not know are reported as ``UNKNOWN_CHANNEL`` and produce **no** reading; the
decoder invents nothing.

Boundaries (kept identical to ccsds.py)
---------------------------------------
  * Imports only the channel dictionary from ``app.ingest`` — nothing from
    detection, safety, physics, RAG, or the agent. Decoding must never touch FDIR.
  * Produces a plain, typed intermediate (:class:`DecodedSample` /
    :class:`MissionDecodeResult`), NOT a pydantic ``TelemetryEntry``. The stream
    adapter (STEP 4) performs the ``DecodedSample → TelemetryEntry`` conversion,
    so this module carries no dependency on the API models and cannot create an
    import cycle.

Safety spine (shared with Phase 1 and ccsds.py)
-----------------------------------------------
No malformed or unknown record is ever turned into an in-range NOMINAL value:
  * empty payload / truncated record / unknown channel → recorded error, NO sample;
  * a known channel carrying a non-finite value → an EXPLICIT UNUSABLE sample
    (``value=None``), never a fabricated number.
Either way the affected channel ends up absent-or-unusable downstream, so the
Phase-1 fail-closed gate stays closed.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple

from app.ingest.channel_dict import get_channel

MISSION_DECODE_VERSION = "1.0.0"

#: The layout this module understands. The name is intentionally explicit that it
#: is a SENTINEL test contract, not a mission specification.
SYNTHETIC_LAYOUT_ID = "SENTINEL_SYNTHETIC_TM_LAYOUT_V1"

# --- Synthetic layout constants (see module docstring / build helper below) ---

_RECORD_COUNT_OCTETS = 1     # uint8 leading record count
_NAME_LEN_OCTETS = 1         # uint8 per-record name length
_VALUE_OCTETS = 8            # IEEE-754 float64, big-endian ('>d')
_VALUE_STRUCT = ">d"         # big-endian double, matching CCSDS byte order
_MAX_UINT8 = 0xFF


class SampleQuality(str, Enum):
    """Why a decoded sample is or is not usable.

    ``USABLE`` — a known channel with a finite numeric value.
    ``MALFORMED_VALUE`` — a known channel whose encoded value was non-finite
    (NaN/Inf). The sample is kept with ``value=None`` so the dropout stays visible
    downstream; it is never treated as a valid reading.
    """

    USABLE = "USABLE"
    MALFORMED_VALUE = "MALFORMED_VALUE"


class DecodeErrorCode(str, Enum):
    """Stable reasons a record produced no sample. Aligns with the streaming
    contract's reason-code vocabulary (§15)."""

    EMPTY_PAYLOAD = "EMPTY_PAYLOAD"        # declared record count is zero
    TRUNCATED_RECORD = "TRUNCATED_RECORD"  # not enough octets for a declared record
    UNKNOWN_CHANNEL = "UNKNOWN_CHANNEL"    # name not in the channel dictionary


@dataclass(frozen=True)
class DecodedSample:
    """One decoded measurement for a KNOWN channel.

    ``value`` is ``None`` exactly when ``quality is MALFORMED_VALUE``. ``channel_id``
    is the dictionary's canonical id (aliases are resolved); ``raw_name`` is what
    the packet actually carried, kept for audit.
    """

    channel_id: str
    raw_name: str
    value: Optional[float]
    unit: Optional[str]
    quality: SampleQuality = SampleQuality.USABLE

    @property
    def is_usable(self) -> bool:
        return self.quality is SampleQuality.USABLE and self.value is not None


@dataclass(frozen=True)
class DecodeError:
    """One record (or the whole field) that produced no sample."""

    code: DecodeErrorCode
    detail: str
    raw_name: Optional[str] = None
    record_index: Optional[int] = None


@dataclass(frozen=True)
class MissionDecodeResult:
    """Everything decoding one data field produced. Never raises to the caller;
    all faults are captured as :class:`DecodeError` entries so a run is auditable."""

    apid: int
    layout_id: str
    samples: List[DecodedSample] = field(default_factory=list)
    errors: List[DecodeError] = field(default_factory=list)
    #: Record count the payload declared in its first octet.
    declared_records: int = 0
    #: Octets actually consumed while decoding (<= len(data_field)).
    bytes_consumed: int = 0

    @property
    def usable_samples(self) -> List[DecodedSample]:
        return [s for s in self.samples if s.is_usable]


def decode_data_field(apid: int, data_field: bytes) -> MissionDecodeResult:
    """Decode one CCSDS data field under the synthetic layout. Never raises.

    The layout is self-describing (each record carries its channel name), so the
    ``apid`` is recorded for provenance/observability but is NOT used to select a
    channel — there is deliberately no APID→channel table to fabricate.

    Faults are deterministic and localized: a truncated record stops decoding at
    that point (subsequent octets cannot be trusted to be record-aligned), and an
    unknown channel or non-finite value affects only that record.
    """
    samples: List[DecodedSample] = []
    errors: List[DecodeError] = []

    if data_field is None or len(data_field) < _RECORD_COUNT_OCTETS:
        errors.append(
            DecodeError(DecodeErrorCode.EMPTY_PAYLOAD, "data field carries no record count")
        )
        return MissionDecodeResult(
            apid=apid, layout_id=SYNTHETIC_LAYOUT_ID, samples=samples, errors=errors,
            declared_records=0, bytes_consumed=0,
        )

    declared = data_field[0]
    offset = _RECORD_COUNT_OCTETS

    if declared == 0:
        errors.append(
            DecodeError(DecodeErrorCode.EMPTY_PAYLOAD, "record count is zero")
        )
        return MissionDecodeResult(
            apid=apid, layout_id=SYNTHETIC_LAYOUT_ID, samples=samples, errors=errors,
            declared_records=0, bytes_consumed=offset,
        )

    for index in range(declared):
        # name length
        if offset + _NAME_LEN_OCTETS > len(data_field):
            errors.append(
                DecodeError(
                    DecodeErrorCode.TRUNCATED_RECORD,
                    f"record {index}: no name-length octet",
                    record_index=index,
                )
            )
            break
        name_len = data_field[offset]
        offset += _NAME_LEN_OCTETS

        # name bytes
        if name_len == 0 or offset + name_len > len(data_field):
            errors.append(
                DecodeError(
                    DecodeErrorCode.TRUNCATED_RECORD,
                    f"record {index}: name length {name_len} exceeds remaining octets",
                    record_index=index,
                )
            )
            break
        raw_name = data_field[offset : offset + name_len].decode("utf-8", errors="replace")
        offset += name_len

        # value bytes
        if offset + _VALUE_OCTETS > len(data_field):
            errors.append(
                DecodeError(
                    DecodeErrorCode.TRUNCATED_RECORD,
                    f"record {index}: value truncated for channel '{raw_name}'",
                    raw_name=raw_name,
                    record_index=index,
                )
            )
            break
        (value,) = struct.unpack(_VALUE_STRUCT, data_field[offset : offset + _VALUE_OCTETS])
        offset += _VALUE_OCTETS

        # resolve against the REAL dictionary; invent nothing
        channel = get_channel(raw_name)
        if channel is None:
            errors.append(
                DecodeError(
                    DecodeErrorCode.UNKNOWN_CHANNEL,
                    f"record {index}: '{raw_name}' is not in the channel dictionary",
                    raw_name=raw_name,
                    record_index=index,
                )
            )
            continue

        if not math.isfinite(value):
            # Known channel, unusable value: keep an EXPLICIT unusable sample so the
            # dropout is visible downstream; never fabricate a number.
            samples.append(
                DecodedSample(
                    channel_id=channel.channel_id,
                    raw_name=raw_name,
                    value=None,
                    unit=channel.unit,
                    quality=SampleQuality.MALFORMED_VALUE,
                )
            )
            continue

        samples.append(
            DecodedSample(
                channel_id=channel.channel_id,
                raw_name=raw_name,
                value=float(value),
                unit=channel.unit,
                quality=SampleQuality.USABLE,
            )
        )

    return MissionDecodeResult(
        apid=apid,
        layout_id=SYNTHETIC_LAYOUT_ID,
        samples=samples,
        errors=errors,
        declared_records=declared,
        bytes_consumed=offset,
    )


def build_synthetic_tm_payload(measurements: Sequence[Tuple[str, float]]) -> bytes:
    """Encode measurements into a ``SENTINEL_SYNTHETIC_TM_LAYOUT_V1`` data field.

    TEST / SYNTHETIC-STREAM HELPER — the inverse of :func:`decode_data_field`,
    kept beside it so the layout has exactly one source of truth. Not a flight
    encoder. A non-finite value is encoded verbatim (so tests can build the
    malformed-value case); channel names are NOT validated here — that is the
    decoder's job against the real dictionary.

    Layout produced::

        octet 0            uint8   record count N (1..255)
        per record:
          octet 0          uint8   channel-name length L (1..255)
          octets 1..L      UTF-8   channel name
          octets L+1..L+8  >d      IEEE-754 float64, big-endian
    """
    if not measurements:
        raise ValueError("at least one measurement is required")
    if len(measurements) > _MAX_UINT8:
        raise ValueError(f"record count {len(measurements)} exceeds {_MAX_UINT8}")

    out = bytearray([len(measurements) & _MAX_UINT8])
    for name, value in measurements:
        name_bytes = name.encode("utf-8")
        if not (1 <= len(name_bytes) <= _MAX_UINT8):
            raise ValueError(f"channel name '{name}' encodes to {len(name_bytes)} octets (need 1..255)")
        out.append(len(name_bytes))
        out.extend(name_bytes)
        out.extend(struct.pack(_VALUE_STRUCT, float(value)))
    return bytes(out)
