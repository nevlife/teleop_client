"""Zenoh client for the TCP-only video path.

Wire spec: /home/nev/teleop/TCP_WIRE_SPEC.md

Topics under ``nev/stream_tcp/{vehicle_id}/``:

    sub  camera[/{cam}]    server -> client AU + 28B relay header (§5)
    pub  stream_heartbeat  client -> server, 5 Hz (§6.c)
    pub  video_feedback    client -> server, 1 Hz (§6.b)

No video_ctl (server -> bot), no retransmit topic (does not exist on TCP
path), no PLI / FEC.

This module owns the Zenoh session and topic wiring. AU dispatch (parse
28B header -> stale-AU drop -> appsrc push) is delegated to a callback
the caller registers via `on_au` so the same client can drive either the
Qt VideoWidget or the headless viewer.

Multi-camera:
    StreamTcpClient supports either a single-camera key
    (`nev/stream_tcp/{vid}/camera`, when cameras=[] and camera_id="") or a
    list of explicit camera ids each subscribed on
    `nev/stream_tcp/{vid}/camera/{cam}`. The AU callback receives the
    cam_id as its first argument so the dispatcher knows which pipeline /
    widget to push into.
"""
import json
import logging
import struct
import threading
import time
from typing import Callable, Optional

import zenoh

from .au_header import (
    FLAG_IS_IDR,
    RELAY_HEADER_FMT,
    RELAY_HEADER_SIZE,
    header_sanity_ok,
    is_idr,
    parse_relay_header,
)

logger = logging.getLogger(__name__)

# Re-export for callers that historically imported these from client.py.
__all__ = (
    'StreamTcpClient',
    'AuCallback',
    'FLAG_IS_IDR',
    'RELAY_HEADER_FMT',
    'RELAY_HEADER_SIZE',
    'header_sanity_ok',
    'is_idr',
    'parse_relay_header',
)

# Sentinel cam_id reported by the single-camera (legacy) topic.
SINGLE_CAM_SENTINEL = ''

# Per-suffix Zenoh QoS. Spec §3: every publisher uses RELIABLE+BLOCK.
_PUB_QOS = dict(
    reliability=zenoh.Reliability.RELIABLE,
    congestion_control=zenoh.CongestionControl.BLOCK,
)


AuCallback = Callable[
    [str, bytes, float, float, int, float, float, float], None
]
"""Signature: (cam_id, au_bytes, vehicle_ts, encode_ms, flags,
              server_rx_ts, veh_to_srv_ms, stale_ms).

cam_id is '' for the legacy single-camera topic."""


class _CamStats:
    """Per-camera rolling counters. Single-writer (the Zenoh sub thread for
    that key) plus reader threads through `_lock`."""

    __slots__ = (
        'stale_drop_count', 'sanity_drop_count',
        'au_count', 'idr_count', 'byte_count',
    )

    def __init__(self) -> None:
        self.stale_drop_count = 0
        self.sanity_drop_count = 0
        self.au_count = 0
        self.idr_count = 0
        self.byte_count = 0


class StreamTcpClient:
    """Zenoh session manager + camera-topic dispatcher for TCP path.

    Lifecycle:
        c = StreamTcpClient(stale_threshold_ms=150)
        c.set_au_callback(my_cb)
        # multi-cam:
        c.start(locator='tcp/host:7457', vehicle_id='0',
                cameras=['front', 'rear', 'left'])
        # or single-cam (legacy):
        c.start(locator='tcp/host:7457', vehicle_id='0', camera_id='')
        ...
        c.stop()
    """

    def __init__(self, stale_threshold_ms: int = 150):
        self._session: zenoh.Session | None = None
        self._vehicle_id: str = ''
        self._cameras: list[str] = []
        # legacy: when single-camera (sentinel='') is used, _cameras=[].
        self._single_mode: bool = False

        self._stale_threshold_s: float = max(0.0, float(stale_threshold_ms) / 1000.0)
        self._stale_threshold_ms: float = float(stale_threshold_ms)

        self._cam_subs: dict[str, zenoh.Subscriber] = {}
        self._pubs: dict[str, zenoh.Publisher] = {}

        self._au_cb: AuCallback | None = None

        # Per-camera counters. Locked for cross-thread reads.
        self._lock = threading.Lock()
        self._stats: dict[str, _CamStats] = {}

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------

    def _key(self, suffix: str) -> str:
        return f'nev/stream_tcp/{self._vehicle_id}/{suffix}'

    def _camera_key(self, cam_id: str) -> str:
        if cam_id:
            return f'nev/stream_tcp/{self._vehicle_id}/camera/{cam_id}'
        return f'nev/stream_tcp/{self._vehicle_id}/camera'

    def start(
        self,
        locator: str,
        vehicle_id: str,
        camera_id: str = '',
        cameras: Optional[list[str]] = None,
    ) -> None:
        if not vehicle_id:
            raise ValueError('vehicle_id is required')
        self._vehicle_id = vehicle_id

        cam_list = list(cameras) if cameras else []
        if cam_list:
            self._cameras = cam_list
            self._single_mode = False
        else:
            # legacy single-camera mode (cam_id may be "" for default key)
            self._cameras = []
            self._single_mode = True

        conf = zenoh.Config()
        if locator:
            conf.insert_json5('connect/endpoints', json.dumps([locator]))
        self._session = zenoh.open(conf)

        try:
            for suffix in ('stream_heartbeat', 'video_feedback'):
                self._pubs[suffix] = self._session.declare_publisher(
                    self._key(suffix), **_PUB_QOS
                )

            if self._single_mode:
                cam = camera_id or SINGLE_CAM_SENTINEL
                self._stats[cam] = _CamStats()
                cam_key = self._camera_key(cam)
                self._cam_subs[cam] = self._session.declare_subscriber(
                    cam_key, self._make_sub_callback(cam)
                )
                logger.info(
                    'StreamTcpClient started -> %s (vehicle_id=%s, '
                    'camera=%s, stale_threshold=%.0fms) [single-mode]',
                    locator or 'auto-discovery',
                    vehicle_id,
                    cam or '<single>',
                    self._stale_threshold_ms,
                )
            else:
                for cam in self._cameras:
                    self._stats[cam] = _CamStats()
                    cam_key = self._camera_key(cam)
                    self._cam_subs[cam] = self._session.declare_subscriber(
                        cam_key, self._make_sub_callback(cam)
                    )
                logger.info(
                    'StreamTcpClient started -> %s (vehicle_id=%s, '
                    'cameras=%s, stale_threshold=%.0fms) [multi-mode]',
                    locator or 'auto-discovery',
                    vehicle_id,
                    self._cameras,
                    self._stale_threshold_ms,
                )
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        for cam, sub in self._cam_subs.items():
            try:
                sub.undeclare()
            except Exception:
                pass
        self._cam_subs.clear()
        for suffix, pub in self._pubs.items():
            try:
                pub.undeclare()
            except Exception as e:
                logger.warning('undeclare publisher [%s]: %s', self._key(suffix), e)
        self._pubs.clear()
        try:
            if self._session is not None:
                self._session.close()
        finally:
            self._session = None

    # ------------------------------------------------------------------
    # publishers (control plane)
    # ------------------------------------------------------------------

    def _publish(self, suffix: str, payload: dict) -> None:
        pub = self._pubs.get(suffix)
        if pub is None:
            return
        try:
            pub.put(json.dumps(payload))
        except Exception as e:
            logger.warning('zenoh put [%s]: %s', self._key(suffix), e)

    def send_stream_heartbeat(self) -> None:
        """Spec §6.c: 5 Hz {ts: epoch_seconds}."""
        self._publish('stream_heartbeat', {'ts': time.time()})

    def send_video_feedback(
        self,
        latency_p95_ms: float,
        freeze_ms_last_1s: float,
        camera_id: str | None = None,
    ) -> None:
        """Spec §6.b: 1 Hz feedback for server-side ABR.

        camera_id is included in the JSON whenever it is a non-empty
        string. Pass the explicit cam id in multi-camera mode.
        """
        msg = {
            'ts': time.time(),
            'latency_p95_ms': round(float(latency_p95_ms), 2),
            'freeze_ms_last_1s': round(float(freeze_ms_last_1s), 2),
        }
        if camera_id:
            msg['camera_id'] = camera_id
        self._publish('video_feedback', msg)

    # ------------------------------------------------------------------
    # camera subscriber
    # ------------------------------------------------------------------

    def set_au_callback(self, cb: AuCallback | None) -> None:
        self._au_cb = cb

    def _make_sub_callback(self, cam_id: str):
        """Bind a Zenoh subscriber callback to a specific cam_id."""

        def _cb(sample) -> None:
            self._on_camera_sample(cam_id, sample)

        return _cb

    def _on_camera_sample(self, cam_id: str, sample) -> None:
        try:
            raw = bytes(sample.payload)
        except Exception as e:
            logger.warning('payload read error: %s', e)
            return
        if len(raw) <= RELAY_HEADER_SIZE:
            return
        try:
            vehicle_ts, encode_ms, flags, server_rx_ts, veh_to_srv_ms, au = (
                parse_relay_header(raw)
            )
        except struct.error as e:
            logger.warning('header parse error: %s', e)
            return

        now = time.time()
        cs = self._stats.get(cam_id)
        if cs is None:
            # subscriber raced ahead of registration; create on demand.
            with self._lock:
                cs = self._stats.setdefault(cam_id, _CamStats())

        if not header_sanity_ok(vehicle_ts, len(au), now):
            with self._lock:
                cs.sanity_drop_count += 1
            return

        stale_ms = (now - vehicle_ts) * 1000.0
        idr = is_idr(flags)

        # Spec §8: drop only non-IDR past threshold. IDR is always decoded.
        if (
            stale_ms > self._stale_threshold_ms
            and not idr
        ):
            with self._lock:
                cs.stale_drop_count += 1
                drop_count = cs.stale_drop_count
            if drop_count <= 5 or drop_count % 100 == 0:
                logger.debug(
                    'stale-AU drop cam=%s #%d age=%.1fms thr=%.0fms is_idr=0',
                    cam_id or '<single>', drop_count, stale_ms,
                    self._stale_threshold_ms,
                )
            return

        with self._lock:
            cs.au_count += 1
            if idr:
                cs.idr_count += 1
            cs.byte_count += len(au)

        cb = self._au_cb
        if cb is None:
            return
        try:
            cb(cam_id, au, vehicle_ts, encode_ms, flags, server_rx_ts,
               veh_to_srv_ms, stale_ms)
        except Exception as e:
            logger.warning('AU callback error: %s', e)

    # ------------------------------------------------------------------
    # stats read-out
    # ------------------------------------------------------------------

    def snapshot_stats(self) -> dict:
        """Return per-camera counters as a dict[cam_id, dict].

        Backward compat: when running in single-camera mode the only entry
        is keyed by '' (or the configured camera_id) and the per-camera
        sub-dict has the same field names as before.
        """
        out: dict[str, dict] = {}
        with self._lock:
            for cam, cs in self._stats.items():
                out[cam] = {
                    'au_count': cs.au_count,
                    'idr_count': cs.idr_count,
                    'byte_count': cs.byte_count,
                    'stale_drop_count': cs.stale_drop_count,
                    'sanity_drop_count': cs.sanity_drop_count,
                }
        return out

    @property
    def stale_threshold_ms(self) -> float:
        return self._stale_threshold_ms

    @property
    def cameras(self) -> list[str]:
        """Explicit cam ids when in multi-camera mode. Empty in single
        mode (use camera_id instead).
        """
        return list(self._cameras)

    @property
    def camera_id(self) -> str:
        """Legacy single-camera id (sentinel '' for the default topic).

        In multi-camera mode this returns '' as well — use `cameras`.
        """
        if self._single_mode:
            # the lone stats key is the configured camera id
            if self._stats:
                return next(iter(self._stats.keys()))
        return ''

    @property
    def vehicle_id(self) -> str:
        return self._vehicle_id

    @property
    def active_cam_ids(self) -> list[str]:
        """The keys clients should iterate when refreshing per-cam UIs.

        In multi mode -> explicit cameras list; in single mode -> [cam] of
        the single subscription (which may be '').
        """
        if self._cameras:
            return list(self._cameras)
        return list(self._stats.keys())
