"""
SENTINEL — CCSDS Space Packet Protocol Parser (ingest/ccsds.py)

Phase 2. A NARROW, PURE parser for the CCSDS Space Packet Protocol primary
header (CCSDS 133.0-B). It does exactly one job: turn raw octets into a typed,
validated ``SpacePacket`` — or reject them deterministically. It knows nothing
about spacecraft channels, faults, physics, or safety, and imports nothing from
those layers. Mission-specific interpretation of the data field lives in a
separate module (``ingest/mission_decode.py``); this file must never guess what
the payload bytes *mean*.

What is REAL here
-----------------
The 6-octet primary header is a fixed, public, standardized structure. Parsing
it — version / type / secondary-header flag / APID / sequence flags / sequence
count / data length — is real and exact, big-endian per the standard.

What is deliberately NOT here
-----------------------------
  * No transport. This module parses ``bytes``; it opens no socket and reads no
    file (mirrors the discipline in ``ingest/esa_mapping.py``: "nothing loads
    files or guesses").
  * No mission semantics. The data field is preserved verbatim as opaque octets.
  * No secondary-header timecode decoding. If the secondary-header flag is set,
    those octets stay inside ``data_field`` untouched — reinterpreting them as a
    mission time we cannot verify would be fabrication.
  * No telecommand handling. ``PacketType.TELECOMMAND`` is recognised so it can
    be *rejected* upstream as non-telemetry; TC uplink is a later phase.

Safety spine (shared with Phase 1)
----------------------------------
A malformed run of bytes is NEVER silently reinterpreted as telemetry. It is
rejected with a stable machine-readable ``CcsdsErrorCode``. Absence or corruption
becomes an error, never a fabricated reading — so a downstream required channel
stays UNKNOWN and the Phase-1 fail-closed gate stays closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable, Iterator

CCSDS_PARSER_VERSION = "1.0.0"

# --- Fixed structural constants from CCSDS 133.0-B (Space Packet Protocol) ---

#: The primary header is always exactly 6 octets.
PRIMARY_HEADER_OCTETS = 6

#: Space Packet Protocol version 1 is encoded as 0b000 in the version field.
SPP_VERSION_1 = 0

#: The Packet Data Length field is 16 bits and encodes (data_field_octets - 1),
#: so the data field spans 1..65536 octets. A value of 0 means a 1-octet field.
MAX_DATA_FIELD_OCTETS = 65536

#: Largest legal whole packet: 6-octet header + maximum data field.
MAX_PACKET_OCTETS = PRIMARY_HEADER_OCTETS + MAX_DATA_FIELD_OCTETS  # 65542

#: The Packet Sequence Count is 14 bits and wraps modulo this value.
SEQUENCE_COUNT_MODULUS = 1 << 14  # 16384


class PacketType(IntEnum):
    """Primary-header Packet Type bit."""

    TELEMETRY = 0
    TELECOMMAND = 1


class SequenceFlags(IntEnum):
    """Primary-header Sequence Flags (2 bits)."""

    CONTINUATION = 0b00
    FIRST = 0b01
    LAST = 0b10
    UNSEGMENTED = 0b11


class CcsdsErrorCode(str, Enum):
    """Stable, machine-readable reasons a byte run is rejected.

    Mirrors the reason-code discipline of the Phase-1 safety gate: every rejection
    carries a code an operator/test can assert on, and no rejection path emits a
    telemetry value.
    """

    TRUNCATED_HEADER = "TRUNCATED_HEADER"      # fewer than 6 octets for the header
    INVALID_VERSION = "INVALID_VERSION"        # packet version number != 0
    OVERSIZED_PACKET = "OVERSIZED_PACKET"      # declared data field exceeds the max
    TRUNCATED_DATA = "TRUNCATED_DATA"          # fewer data octets than the header declares


class CcsdsParseError(Exception):
    """Raised when octets cannot be parsed into a valid Space Packet.

    Deterministic: the same bytes always raise the same ``code`` at the same
    ``offset``. Carries no payload interpretation — only the structural fault.
    """

    def __init__(self, code: CcsdsErrorCode, detail: str, *, offset: int = 0) -> None:
        self.code = code
        self.detail = detail
        self.offset = offset
        super().__init__(f"{code.value} at offset {offset}: {detail}")


@dataclass(frozen=True)
class PrimaryHeader:
    """The parsed 6-octet CCSDS primary header. Immutable."""

    version: int
    packet_type: PacketType
    secondary_header_flag: bool
    apid: int
    sequence_flags: SequenceFlags
    sequence_count: int
    #: Raw Packet Data Length field value = (data_field_octets - 1).
    data_length_field: int

    @property
    def data_field_octets(self) -> int:
        """Number of octets in the data field (always >= 1)."""
        return self.data_length_field + 1

    @property
    def total_octets(self) -> int:
        """Whole-packet length in octets: header + data field."""
        return PRIMARY_HEADER_OCTETS + self.data_field_octets

    @property
    def is_telemetry(self) -> bool:
        return self.packet_type is PacketType.TELEMETRY


@dataclass(frozen=True)
class SpacePacket:
    """A structurally valid Space Packet: parsed header + opaque data field.

    ``data_field`` is preserved verbatim. This type carries no mission meaning;
    decoding it into telemetry channels is a separate, clearly-labelled step.
    """

    primary_header: PrimaryHeader
    data_field: bytes

    # Convenience pass-throughs so callers need not reach into the header.
    @property
    def apid(self) -> int:
        return self.primary_header.apid

    @property
    def sequence_count(self) -> int:
        return self.primary_header.sequence_count

    @property
    def is_telemetry(self) -> bool:
        return self.primary_header.is_telemetry

    @property
    def total_octets(self) -> int:
        return self.primary_header.total_octets


def parse_primary_header(
    raw: bytes, *, max_data_field_octets: int = MAX_DATA_FIELD_OCTETS
) -> PrimaryHeader:
    """Parse and validate the 6-octet primary header from the start of ``raw``.

    Reads only the first 6 octets; any further octets are ignored here (the data
    field is handled by :func:`parse_space_packet`). Raises :class:`CcsdsParseError`
    with a specific code on any structural fault — never returns a partial/guessed
    header.

    ``max_data_field_octets`` is the OPERATIONAL cap enforced as ``OVERSIZED_PACKET``.
    Note that the 16-bit Packet Data Length field can itself only encode a data
    field up to :data:`MAX_DATA_FIELD_OCTETS` (65536) octets, so with the default
    cap the oversized branch is unreachable by construction — a larger field cannot
    be expressed. Configuring a smaller operational cap (e.g. a deployment MTU or
    ring-buffer limit) is what makes an oversized rejection meaningful and testable.
    """
    if raw is None or len(raw) < PRIMARY_HEADER_OCTETS:
        raise CcsdsParseError(
            CcsdsErrorCode.TRUNCATED_HEADER,
            f"need {PRIMARY_HEADER_OCTETS} header octets, got {0 if raw is None else len(raw)}",
        )

    word0 = (raw[0] << 8) | raw[1]      # version | type | sec_hdr | apid
    word1 = (raw[2] << 8) | raw[3]      # seq_flags | seq_count
    word2 = (raw[4] << 8) | raw[5]      # data length field

    version = (word0 >> 13) & 0b111
    if version != SPP_VERSION_1:
        raise CcsdsParseError(
            CcsdsErrorCode.INVALID_VERSION,
            f"packet version number {version} != {SPP_VERSION_1}",
        )

    packet_type = PacketType((word0 >> 12) & 0b1)
    secondary_header_flag = bool((word0 >> 11) & 0b1)
    apid = word0 & 0x7FF                # low 11 bits

    sequence_flags = SequenceFlags((word1 >> 14) & 0b11)
    sequence_count = word1 & 0x3FFF     # low 14 bits

    data_length_field = word2           # 0..65535 → 1..65536 data octets

    # Enforce the operational cap. This also bounds memory before we ever attempt
    # to read the data octets, so a crafted length cannot force a large allocation.
    if data_length_field + 1 > max_data_field_octets:
        raise CcsdsParseError(
            CcsdsErrorCode.OVERSIZED_PACKET,
            f"declared data field {data_length_field + 1} octets exceeds cap "
            f"{max_data_field_octets}",
        )

    return PrimaryHeader(
        version=version,
        packet_type=packet_type,
        secondary_header_flag=secondary_header_flag,
        apid=apid,
        sequence_flags=sequence_flags,
        sequence_count=sequence_count,
        data_length_field=data_length_field,
    )


def parse_space_packet(
    raw: bytes, *, max_data_field_octets: int = MAX_DATA_FIELD_OCTETS
) -> SpacePacket:
    """Parse exactly one Space Packet from the START of ``raw``.

    ``raw`` must contain at least the whole packet (header + declared data field);
    any trailing octets beyond ``total_octets`` are ignored here — deframing a
    continuous stream is :func:`iter_space_packets`' job. Raises
    :class:`CcsdsParseError` on truncation or a structural fault.
    """
    header = parse_primary_header(raw, max_data_field_octets=max_data_field_octets)
    total = header.total_octets
    if len(raw) < total:
        raise CcsdsParseError(
            CcsdsErrorCode.TRUNCATED_DATA,
            f"packet declares {total} octets, only {len(raw)} present",
        )
    data_field = bytes(raw[PRIMARY_HEADER_OCTETS:total])
    return SpacePacket(primary_header=header, data_field=data_field)


def iter_space_packets(
    source: "bytes | bytearray | Iterable[bytes]",
    *,
    max_data_field_octets: int = MAX_DATA_FIELD_OCTETS,
) -> Iterator[SpacePacket]:
    """Deframe a byte stream into Space Packets using the length field.

    Accepts a single ``bytes`` blob or an iterable of byte chunks (chunk
    boundaries need not align with packet boundaries). Packets are yielded in
    stream order.

    Framing is self-delimiting: the primary header's data-length field gives each
    packet's exact size, so no sync marker is needed. The Space Packet Protocol
    has NO sync marker, which means a corrupt length is not recoverable by
    resynchronising — so on a structural fault at the current position this
    generator raises :class:`CcsdsParseError` rather than guessing where the next
    packet begins. The caller (the stream adapter) decides how to record and end
    the stream; silently skipping ahead would risk reinterpreting arbitrary bytes
    as telemetry, which the safety spine forbids.

    Trailing bytes that form an incomplete packet at end-of-input are left
    unparsed and reported by the caller as a truncation — they are never emitted
    as a partial packet.
    """
    if isinstance(source, (bytes, bytearray)):
        chunks: Iterable[bytes] = (bytes(source),)
    else:
        chunks = source

    buffer = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)

        # Emit every whole packet currently in the buffer.
        while len(buffer) >= PRIMARY_HEADER_OCTETS:
            header = parse_primary_header(
                buffer, max_data_field_octets=max_data_field_octets
            )  # may raise INVALID_VERSION/OVERSIZED
            total = header.total_octets
            if len(buffer) < total:
                break  # wait for more chunks; partial packet retained
            packet = parse_space_packet(
                buffer[:total], max_data_field_octets=max_data_field_octets
            )
            yield packet
            del buffer[:total]

    # Leftover octets: either empty (clean) or an incomplete trailing packet.
    if buffer:
        raise CcsdsParseError(
            CcsdsErrorCode.TRUNCATED_DATA,
            f"stream ended with {len(buffer)} trailing octets not forming a whole packet",
            offset=0,
        )


def build_space_packet(
    *,
    apid: int,
    sequence_count: int,
    data_field: bytes,
    packet_type: PacketType = PacketType.TELEMETRY,
    secondary_header_flag: bool = False,
    sequence_flags: SequenceFlags = SequenceFlags.UNSEGMENTED,
) -> bytes:
    """Encode a valid Space Packet to octets. TEST / SYNTHETIC-STREAM HELPER.

    This is the inverse of the parser, used only to construct explicitly-defined
    synthetic packets for tests and the end-to-end synthetic stream (STEP 8). It
    is NOT a flight encoder and performs no mission encoding — ``data_field`` is
    written verbatim. Kept beside the parser so the byte layout has exactly one
    source of truth.
    """
    if not (0 <= apid <= 0x7FF):
        raise ValueError(f"apid {apid} out of 11-bit range")
    if not (0 <= sequence_count < SEQUENCE_COUNT_MODULUS):
        raise ValueError(f"sequence_count {sequence_count} out of 14-bit range")
    if len(data_field) < 1:
        raise ValueError("data field must contain at least 1 octet")
    if len(data_field) > MAX_DATA_FIELD_OCTETS:
        raise ValueError(f"data field {len(data_field)} exceeds max {MAX_DATA_FIELD_OCTETS}")

    word0 = (
        (SPP_VERSION_1 & 0b111) << 13
        | (int(packet_type) & 0b1) << 12
        | (1 if secondary_header_flag else 0) << 11
        | (apid & 0x7FF)
    )
    word1 = ((int(sequence_flags) & 0b11) << 14) | (sequence_count & 0x3FFF)
    word2 = (len(data_field) - 1) & 0xFFFF

    header = bytes(
        [
            (word0 >> 8) & 0xFF, word0 & 0xFF,
            (word1 >> 8) & 0xFF, word1 & 0xFF,
            (word2 >> 8) & 0xFF, word2 & 0xFF,
        ]
    )
    return header + bytes(data_field)


def sequence_gap(previous: int, current: int) -> int:
    """Forward distance from ``previous`` to ``current`` sequence count, with wrap.

    Returns the number of steps forward (modulo 16384). 1 means ``current`` is the
    expected next count; 0 means a duplicate; >1 means a gap; a large value close
    to the modulus indicates ``current`` is actually behind ``previous`` (an
    out-of-order / regressed count). Pure arithmetic; policy lives in the adapter.
    """
    return (current - previous) % SEQUENCE_COUNT_MODULUS
