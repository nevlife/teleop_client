"""Tests for the 28B relay header (spec §5) and static contract checks.

Run from the project root:

    python3 -m unittest test.test_au_parsing -v

The static checks (test_no_legacy_strings, test_no_rtp_legacy_topic) guard
that we never accidentally re-introduce the UDP/RTX/PLI/FEC machinery that
was the whole point of stream_client existing.
"""
import os
import re
import struct
import unittest
from pathlib import Path

from stream_client.au_header import (
    FLAG_IS_IDR,
    RELAY_HEADER_FMT,
    RELAY_HEADER_SIZE,
    header_sanity_ok,
    is_idr,
    parse_relay_header,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestRelayHeader(unittest.TestCase):

    def test_header_size_is_28(self):
        self.assertEqual(RELAY_HEADER_SIZE, 28)
        self.assertEqual(RELAY_HEADER_FMT, '<dfB3xdf')

    def test_round_trip_non_idr(self):
        vehicle_ts = 1700000000.123456
        encode_ms = 4.25
        flags = 0  # non-IDR
        server_rx_ts = 1700000000.223456
        veh_to_srv_ms = 100.0
        au_payload = b'\x00\x00\x00\x01\x42\x01' + b'\xaa' * 200

        raw = (
            struct.pack(
                RELAY_HEADER_FMT,
                vehicle_ts, encode_ms, flags, server_rx_ts, veh_to_srv_ms,
            )
            + au_payload
        )
        self.assertEqual(len(raw), RELAY_HEADER_SIZE + len(au_payload))

        (vts, enc, fl, srx, vts_ms, au) = parse_relay_header(raw)
        self.assertAlmostEqual(vts, vehicle_ts, places=9)
        self.assertAlmostEqual(enc, encode_ms, places=3)
        self.assertEqual(fl, flags)
        self.assertAlmostEqual(srx, server_rx_ts, places=9)
        self.assertAlmostEqual(vts_ms, veh_to_srv_ms, places=3)
        self.assertEqual(au, au_payload)
        self.assertFalse(is_idr(fl))

    def test_round_trip_idr(self):
        vehicle_ts = 1700000123.456
        encode_ms = 6.0
        flags = FLAG_IS_IDR
        server_rx_ts = 1700000123.500
        veh_to_srv_ms = 44.0
        au_payload = b'\x00\x00\x00\x01\x40\x01' + b'\xbb' * 5000

        raw = (
            struct.pack(
                RELAY_HEADER_FMT,
                vehicle_ts, encode_ms, flags, server_rx_ts, veh_to_srv_ms,
            )
            + au_payload
        )
        (vts, enc, fl, srx, vts_ms, au) = parse_relay_header(raw)
        self.assertEqual(fl & FLAG_IS_IDR, FLAG_IS_IDR)
        self.assertTrue(is_idr(fl))
        self.assertEqual(au, au_payload)

    def test_padding_bytes_are_zero(self):
        # The 3 pad bytes after `flags` must be zero on the wire. Pack
        # then re-parse to confirm struct '<dfB3xdf' skips them — i.e.
        # the trailing fields land at offset 16, not 13.
        raw = struct.pack(RELAY_HEADER_FMT, 1.0, 2.0, 0xFF, 3.0, 4.0)
        self.assertEqual(len(raw), 28)
        # bytes 13,14,15 are the padding region.
        self.assertEqual(raw[13:16], b'\x00\x00\x00')
        # byte 12 carries flags as 0xFF — confirms layout.
        self.assertEqual(raw[12], 0xFF)

    def test_flag_bit0_extraction(self):
        # Only bit0 must be interpreted as is_idr. Set bit0 + a few
        # nuisance bits — is_idr() should still return True; other bits
        # must be preserved by struct round-trip.
        flags_in = 0b1010_0101  # bit0=1, plus other bits
        raw = struct.pack(RELAY_HEADER_FMT, 1.0, 2.0, flags_in, 3.0, 4.0)
        (_, _, fl, _, _) = struct.unpack_from(RELAY_HEADER_FMT, raw, 0)
        self.assertEqual(fl, flags_in)
        self.assertTrue(is_idr(fl))

        flags_in2 = 0b1010_0100  # bit0=0
        raw2 = struct.pack(RELAY_HEADER_FMT, 1.0, 2.0, flags_in2, 3.0, 4.0)
        (_, _, fl2, _, _) = struct.unpack_from(RELAY_HEADER_FMT, raw2, 0)
        self.assertFalse(is_idr(fl2))


class TestHeaderSanity(unittest.TestCase):
    """Spec §13."""

    def test_negative_vehicle_ts(self):
        self.assertFalse(header_sanity_ok(-1.0, 1000, now=1_700_000_000.0))

    def test_too_old_vehicle_ts(self):
        # vehicle_ts < 1e6 -> bogus
        self.assertFalse(header_sanity_ok(0.5, 1000, now=1_700_000_000.0))

    def test_too_far_future_vehicle_ts(self):
        # > now + 10s -> bogus
        now = 1_700_000_000.0
        self.assertFalse(header_sanity_ok(now + 11.0, 1000, now=now))

    def test_au_too_small(self):
        self.assertFalse(header_sanity_ok(1_700_000_000.0, 4, now=1_700_000_000.0))

    def test_au_too_large(self):
        self.assertFalse(
            header_sanity_ok(1_700_000_000.0, 2 * 1024 * 1024, now=1_700_000_000.0)
        )

    def test_typical_idr_ok(self):
        self.assertTrue(
            header_sanity_ok(1_700_000_000.0, 80_000, now=1_700_000_000.05)
        )


# ---------------------------------------------------------------------
# Static repo-shape checks: enforce that the TCP-only codebase never
# accidentally grows back the UDP/RTP/RTX/PLI/FEC machinery.
# ---------------------------------------------------------------------

LEGACY_STRINGS = (
    'rtph265depay',
    'rtpjitterbuffer',
    'rtpulpfecdec',
    'rtx_request',
)

LEGACY_TOPIC_PATTERN = re.compile(r'\bnev/stream/(?!_tcp/)')


def _scan_py_files():
    """Yield (path, text) for all .py files in the project (excluding
    __pycache__ and the test file itself)."""
    here = Path(__file__).resolve()
    for path in PROJECT_ROOT.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        if path.resolve() == here:
            continue
        try:
            yield path, path.read_text(encoding='utf-8')
        except Exception:
            continue


class TestNoLegacyStrings(unittest.TestCase):

    def test_no_rtp_or_rtx_machinery(self):
        offenders: list[str] = []
        for path, text in _scan_py_files():
            rel = os.path.relpath(path, PROJECT_ROOT)
            for needle in LEGACY_STRINGS:
                if needle in text:
                    offenders.append(f'{rel} contains {needle!r}')
        self.assertFalse(
            offenders,
            'legacy RTP/RTX/FEC strings present: ' + ', '.join(offenders),
        )

    def test_no_legacy_topic_prefix(self):
        offenders: list[str] = []
        for path, text in _scan_py_files():
            rel = os.path.relpath(path, PROJECT_ROOT)
            if LEGACY_TOPIC_PATTERN.search(text):
                offenders.append(rel)
        self.assertFalse(
            offenders,
            'legacy "nev/stream/" (non-TCP) topic present: '
            + ', '.join(offenders),
        )


class TestMultiCameraRouting(unittest.TestCase):
    """Sanity check that the per-cam callback wiring fans samples out by
    cam_id without re-importing zenoh."""

    def test_callback_bound_to_cam_id(self):
        # Pure unit test: emulate the closure that
        # StreamTcpClient._make_sub_callback returns. We don't import
        # zenoh here (lighter dependency surface for the unit test).
        seen: list[tuple] = []

        def make_cb(cam_id):
            def _cb(sample):
                seen.append((cam_id, sample))
            return _cb

        cb_front = make_cb('front')
        cb_rear = make_cb('rear')
        cb_front('SAMPLE_A')
        cb_rear('SAMPLE_B')
        cb_front('SAMPLE_C')

        self.assertEqual(
            seen,
            [('front', 'SAMPLE_A'), ('rear', 'SAMPLE_B'), ('front', 'SAMPLE_C')],
        )

    def test_camera_key_format(self):
        """Spec §1: per-cam topic is
        nev/stream_tcp/{vid}/camera/{cam_id}; legacy single-cam is
        nev/stream_tcp/{vid}/camera (no trailing slash)."""
        vid = '0'
        cam = 'front'
        per_cam_key = f'nev/stream_tcp/{vid}/camera/{cam}'
        legacy_key = f'nev/stream_tcp/{vid}/camera'
        self.assertEqual(per_cam_key, 'nev/stream_tcp/0/camera/front')
        self.assertEqual(legacy_key, 'nev/stream_tcp/0/camera')
        self.assertTrue(per_cam_key.startswith(legacy_key + '/'))


if __name__ == '__main__':
    unittest.main()
