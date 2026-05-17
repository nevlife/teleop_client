"""Qt VideoWidget: AU push -> NVDEC -> QImage paint.

This widget owns the GStreamer pipeline and computes the per-AU metrics
that feed both the on-screen stats panel and the 1 Hz video_feedback
publish (see send_loop.py).

Threading:
  - StreamTcpClient invokes `on_au` on its Zenoh callback thread. We
    push the buffer into appsrc there.
  - nvh265dec calls `_on_decoded_sample` on a GStreamer streaming
    thread; we emit a Qt signal so the QImage construction + setPixmap
    happen on the Qt main thread.
"""
import bisect
import logging
import threading
import time
from collections import deque

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from stream_client.gstreamer_tcp import create_au_pipeline

logger = logging.getLogger(__name__)


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    if pct <= 0:
        return s[0]
    if pct >= 100:
        return s[-1]
    # Nearest-rank — fine for short windows (1 s @ 30 fps = 30 samples).
    k = max(0, min(len(s) - 1, int(round(pct / 100.0 * (len(s) - 1)))))
    return s[k]


class VideoWidget(QWidget):
    """AU -> NVDEC -> QImage. Provides metrics_provider methods for send_loop.

    One instance per camera. `cam_id` is used for log lines, pipeline
    element naming and the on-screen label.
    """

    frame_ready = Signal(bytes, int, int)

    def __init__(
        self,
        fps_expected: float = 30.0,
        cam_id: str = '',
        parent=None,
    ):
        super().__init__(parent)
        self._cam_id = cam_id or ''
        self._pipeline: Gst.Pipeline | None = None
        self._appsrc: Gst.Element | None = None
        self._appsink: Gst.Element | None = None
        self._running = False

        self._fps_expected = max(1.0, float(fps_expected))
        # Spec §6.b: freeze when frame-interval > 1.5/fps.
        self._freeze_gap_s = 1.5 / self._fps_expected

        self._lock = threading.Lock()
        # (monotonic_ts, stale_ms) — 1 s window for latency p95.
        self._stale_samples: deque[tuple[float, float]] = deque()
        # (monotonic_ts, gap_s) — 1 s window for freeze accumulator.
        self._freeze_samples: deque[tuple[float, float]] = deque()
        self._last_decoded_mono = 0.0

        # Counters surfaced to the panel.
        self._decoded_count = 0
        self._frame_size_last = 0
        self._bitrate_hint_kbps = 0  # not populated yet (no inbound from server)
        self._freeze_event_count = 0

        # rolling 1 s bytes window for live bitrate estimate
        self._bytes_window: deque[tuple[float, int]] = deque()

        # Optional camera-name caption above the video (only shown when a
        # cam_id is supplied — single-camera mode keeps the legacy
        # caption-less layout).
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._caption: QLabel | None = None
        if self._cam_id:
            self._caption = QLabel(self._cam_id.upper())
            self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._caption.setStyleSheet(
                'background:#161b22; color:#c9d1d9;'
                'font-size:11px; letter-spacing:1.5px; padding:2px;'
                'border-bottom:1px solid #21262d;'
            )
            self._caption.setFixedHeight(20)
            layout.addWidget(self._caption)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet('background-color: #1a1a2e;')
        self._label.setMinimumSize(320, 240)

        layout.addWidget(self._label, stretch=1)

        self.frame_ready.connect(self._update_frame)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._pipeline, self._appsrc, self._appsink, desc = create_au_pipeline(
            output_format='RGB',
            cam_id=self._cam_id,
        )
        self._appsink.set_property('emit-signals', True)
        self._appsink.connect('new-sample', self._on_decoded_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
        logger.info(
            'VideoWidget started (%s) cam=%s',
            desc, self._cam_id or '<single>',
        )

    def stop(self) -> None:
        self._running = False
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._appsrc = None
        self._appsink = None

    # ------------------------------------------------------------------
    # AU ingress (called from StreamTcpClient Zenoh thread)
    # ------------------------------------------------------------------

    def on_au(
        self,
        au: bytes,
        vehicle_ts: float,
        encode_ms: float,
        flags: int,
        server_rx_ts: float,
        veh_to_srv_ms: float,
        stale_ms: float,
    ) -> None:
        if not self._running or self._appsrc is None:
            return
        now_mono = time.monotonic()
        with self._lock:
            self._stale_samples.append((now_mono, stale_ms))
            self._bytes_window.append((now_mono, len(au)))
            self._prune_locked(now_mono)
            self._frame_size_last = len(au)

        buf = Gst.Buffer.new_wrapped(au)
        # appsrc do-timestamp=true assigns PTS for us; we don't try to set
        # buf.pts from vehicle_ts because the two clocks are unrelated.
        try:
            self._appsrc.emit('push-buffer', buf)
        except Exception as e:
            logger.warning('push-buffer failed: %s', e)

    # ------------------------------------------------------------------
    # decoder callback (GStreamer streaming thread)
    # ------------------------------------------------------------------

    def _on_decoded_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit('pull-sample')
        if not isinstance(sample, Gst.Sample):
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct_ = caps.get_structure(0)
        width = struct_.get_value('width')
        height = struct_.get_value('height')

        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(map_info.data)
        finally:
            buf.unmap(map_info)

        now_mono = time.monotonic()
        with self._lock:
            last = self._last_decoded_mono
            gap = (now_mono - last) if last > 0 else 0.0
            if last > 0 and gap > self._freeze_gap_s:
                # Record only the overage above expected interval.
                over = gap - (1.0 / self._fps_expected)
                self._freeze_samples.append((now_mono, max(0.0, over)))
                self._freeze_event_count += 1
            self._last_decoded_mono = now_mono
            self._decoded_count += 1
            self._prune_locked(now_mono)

        self.frame_ready.emit(data, width, height)
        return Gst.FlowReturn.OK

    # ------------------------------------------------------------------
    # Qt paint (main thread)
    # ------------------------------------------------------------------

    def _update_frame(self, data: bytes, width: int, height: int) -> None:
        img = QImage(data, width, height, width * 3, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(img)
        scaled = pixmap.scaled(
            self._label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self._label.setPixmap(scaled)

    # ------------------------------------------------------------------
    # rolling-window utilities + MetricsProvider impl
    # ------------------------------------------------------------------

    def _prune_locked(self, now_mono: float) -> None:
        """Drop samples older than 1 s. Caller holds self._lock."""
        cutoff = now_mono - 1.0
        while self._stale_samples and self._stale_samples[0][0] < cutoff:
            self._stale_samples.popleft()
        while self._freeze_samples and self._freeze_samples[0][0] < cutoff:
            self._freeze_samples.popleft()
        while self._bytes_window and self._bytes_window[0][0] < cutoff:
            self._bytes_window.popleft()

    def latency_p95_ms_last_1s(self) -> float:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            vals = [s for _, s in self._stale_samples]
        return _percentile(vals, 95.0)

    def freeze_ms_last_1s(self) -> float:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            total_s = sum(g for _, g in self._freeze_samples)
        return total_s * 1000.0

    def bitrate_mbps_last_1s(self) -> float:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            total_bytes = sum(b for _, b in self._bytes_window)
        return total_bytes * 8.0 / 1_000_000.0

    def fps_last_1s(self) -> float:
        # Approximation: count AU-decode samples in the freeze window's
        # parent (we don't keep a separate decoded-times deque; use
        # _stale_samples since each AU produces a stale sample when it
        # arrives, and the decode rate tracks AU rate one-for-one once
        # past the initial buffer).
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            return float(len(self._stale_samples))

    @property
    def cam_id(self) -> str:
        return self._cam_id

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            stale = [s for _, s in self._stale_samples]
            bytes_total = sum(b for _, b in self._bytes_window)
            freeze_total = sum(g for _, g in self._freeze_samples)
            fps = float(len(self._stale_samples))
            frame_size_last = self._frame_size_last
            decoded = self._decoded_count
            freeze_evt = self._freeze_event_count
        return {
            'latency_p95_ms': _percentile(stale, 95.0),
            'latency_avg_ms': (sum(stale) / len(stale)) if stale else 0.0,
            'latency_max_ms': max(stale) if stale else 0.0,
            'freeze_ms_last_1s': freeze_total * 1000.0,
            'freeze_event_count': freeze_evt,
            'bitrate_mbps': bytes_total * 8.0 / 1_000_000.0,
            'fps': fps,
            'frame_size_last': frame_size_last,
            'decoded_count': decoded,
            'bitrate_hint_kbps': self._bitrate_hint_kbps,
        }
