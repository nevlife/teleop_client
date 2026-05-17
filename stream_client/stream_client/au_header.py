"""28B relay header (spec §5) + §13 sanity helpers.

Pure-Python, no zenoh / gstreamer dependency — kept separate so unit tests
can import it without the runtime deps installed.

Layout (little-endian, no struct padding):

    Offset  Size  Type    Field           Meaning
    0       8     f64     vehicle_ts      bot system_clock seconds (epoch)
    8       4    f32      encode_ms       encoder turnaround (ms)
    12      1     u8      flags           bit0=is_idr, other bits reserved 0
    13      3     bytes   padding         0x00 0x00 0x00
    16      8     f64     server_rx_ts    server system_clock seconds
    24      4    f32      veh_to_srv_ms   (server_rx_ts - vehicle_ts)*1000
    28      N     bytes   au_payload      H.265 Annex-B byte-stream, AU-aligned
"""
import struct

RELAY_HEADER_FMT = '<dfB3xdf'
RELAY_HEADER_SIZE = struct.calcsize(RELAY_HEADER_FMT)
assert RELAY_HEADER_SIZE == 28, f'header size mismatch: {RELAY_HEADER_SIZE}'

FLAG_IS_IDR = 0x01

# Spec §13 sanity bounds.
VEHICLE_TS_MIN = 1_000_000.0           # ~1970-01-12, anything earlier is bogus
VEHICLE_TS_FUTURE_SLACK_S = 10.0       # > now + 10s -> bogus
AU_PAYLOAD_MIN = 16                    # at least one short NAL
AU_PAYLOAD_MAX = 1 * 1024 * 1024       # 1 MB hard cap


def parse_relay_header(raw: bytes):
    """Return (vehicle_ts, encode_ms, flags, server_rx_ts, veh_to_srv_ms, au).

    Raises struct.error if the buffer is too short.
    """
    vehicle_ts, encode_ms, flags, server_rx_ts, veh_to_srv_ms = (
        struct.unpack_from(RELAY_HEADER_FMT, raw, 0)
    )
    au = raw[RELAY_HEADER_SIZE:]
    return vehicle_ts, encode_ms, flags, server_rx_ts, veh_to_srv_ms, au


def is_idr(flags: int) -> bool:
    return bool(flags & FLAG_IS_IDR)


def header_sanity_ok(vehicle_ts: float, au_len: int, now: float) -> bool:
    """Spec §13. False -> caller should drop the sample with a metric bump."""
    if not (VEHICLE_TS_MIN < vehicle_ts < (now + VEHICLE_TS_FUTURE_SLACK_S)):
        return False
    if au_len < AU_PAYLOAD_MIN or au_len > AU_PAYLOAD_MAX:
        return False
    return True
