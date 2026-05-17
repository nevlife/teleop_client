"""Main window for stream_client.

Layout (single-camera, legacy):
    [ video ] | [ StatsPanel ]

Layout (multi-camera):
    [ video front | video rear | video left | ... ] | [ StatsPanel ]

The MainWindow constructs one VideoWidget per cam_id plus a single
StatsPanel that lets the user pick which camera's metrics to display. A
1 Hz QTimer refreshes the panel from each widget's snapshot() and the
client's per-camera counters.
"""
import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
)

from .stats_panel import StatsPanel
from .video_widget import VideoWidget

logger = logging.getLogger(__name__)

BG       = '#0d1117'
BORDER   = '#21262d'
BORDER2  = '#30363d'
TEXT     = '#c9d1d9'
MUTED    = '#8b949e'
GREEN    = '#3fb950'
RED      = '#f85149'
YELLOW   = '#d29922'


class Badge(QLabel):

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._base_text = text
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(20)
        self.setMinimumWidth(36)
        self.set_state('off')

    def set_state(self, state, text=None):
        if text:
            self.setText(text)
        else:
            self.setText(self._base_text)
        colors = {
            'ok':    (GREEN, GREEN),
            'warn':  (YELLOW, YELLOW),
            'error': (RED, RED),
            'off':   (MUTED, BORDER2),
        }
        fg, border = colors.get(state, colors['off'])
        self.setStyleSheet(
            f'padding:1px 8px; border:1px solid {border}; border-radius:3px;'
            f'font-size:11px; color:{fg}; background:transparent;'
        )


class MainWindow(QMainWindow):
    """N-camera video grid + stats. AU callback wiring happens in `start()`."""

    def __init__(self, client, cfg: dict):
        super().__init__()
        self._client = client
        self._cfg = cfg

        gui_cfg = cfg.get('gui', {})
        font_family = gui_cfg.get('font_family', "Consolas, 'Courier New', monospace")
        min_w = int(gui_cfg.get('window_min_width', 1280))
        min_h = int(gui_cfg.get('window_min_height', 720))
        panel_w = int(gui_cfg.get('panel_width', 340))

        # Resolve the cameras list. `cameras: [...]` takes precedence; an
        # empty list falls back to legacy single-camera mode and uses the
        # configured camera_id (which may itself be '' = default key).
        cameras = list(cfg.get('cameras') or [])
        if cameras:
            self._cameras = cameras
            self._single_mode = False
        else:
            cam_id = cfg.get('video', {}).get('camera_id', '')
            self._cameras = [cam_id]  # may be ['']
            self._single_mode = True

        self.setWindowTitle('NEV Stream Client (TCP / AU)')
        self.setMinimumSize(min_w, min_h)
        self.setStyleSheet(
            f'QMainWindow {{ background:{BG}; }}'
            f'QWidget {{ color:{TEXT}; font-family:{font_family}; font-size:12px; }}'
        )

        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        topbar = QWidget()
        topbar.setFixedHeight(36)
        topbar.setStyleSheet(f'background:{BG}; border-bottom:1px solid {BORDER};')
        tb_layout = QHBoxLayout(topbar)
        tb_layout.setContentsMargins(12, 0, 12, 0)

        title = QLabel('NEV STREAM TCP')
        title.setStyleSheet(
            f'font-size:13px; font-weight:bold; letter-spacing:1px; color:{TEXT};'
        )
        tb_layout.addWidget(title)
        tb_layout.addStretch()

        self._badge_link = Badge('LINK')
        self._badge_idr = Badge('IDR')
        for b in (self._badge_link, self._badge_idr):
            tb_layout.addWidget(b)

        self._clock = QLabel('--:--:--')
        self._clock.setStyleSheet(f'font-size:11px; color:{MUTED}; margin-left:8px;')
        tb_layout.addWidget(self._clock)

        main_layout.addWidget(topbar)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Video grid: one VideoWidget per camera, laid out horizontally.
        fps_expected = float(cfg.get('fps_expected', 30.0))
        video_grid = QWidget()
        grid_layout = QHBoxLayout(video_grid)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(1)

        self.video_widgets: dict[str, VideoWidget] = {}
        for cam in self._cameras:
            # Caption only when we have a real (non-empty) cam_id.
            vw = VideoWidget(fps_expected=fps_expected, cam_id=cam)
            self.video_widgets[cam] = vw
            grid_layout.addWidget(vw, stretch=1)

        content_layout.addWidget(video_grid, stretch=1)

        # Legacy alias: callers (e.g. send_loop metrics_provider) used to
        # read `.video_widget`. Keep it pointing at the first widget so
        # the heartbeat path still works without breaking imports.
        first_cam = self._cameras[0]
        self.video_widget = self.video_widgets[first_cam]

        separator = QWidget()
        separator.setFixedWidth(1)
        separator.setStyleSheet(f'background:{BORDER};')
        content_layout.addWidget(separator)

        self.stats_panel = StatsPanel(
            panel_width=panel_w, font_family=font_family
        )
        # Only register a combo when there's > 1 real camera id.
        if not self._single_mode:
            self.stats_panel.set_cameras(self._cameras)
        else:
            # single-camera: pass [cam_id] so the panel knows which key
            # to index into when it gets per-cam stats from the client.
            self.stats_panel.set_cameras(self._cameras)
        content_layout.addWidget(self.stats_panel, stretch=0)

        main_layout.addWidget(content, stretch=1)
        self.setCentralWidget(central)

        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)

        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(1000)

        # rolling latest server-side veh_to_srv_ms per camera for the
        # "put latency" surface. Updated from the AU callback.
        self._last_veh_to_srv_ms: dict[str, float] = {c: 0.0 for c in self._cameras}
        self._last_idr_au_mono: dict[str, float] = {c: 0.0 for c in self._cameras}
        self._last_au_mono: dict[str, float] = {c: 0.0 for c in self._cameras}

    @property
    def cameras(self) -> list[str]:
        return list(self._cameras)

    # ------------------------------------------------------------------
    # MetricsProvider impl (per-camera; consumed by send_loop)
    # ------------------------------------------------------------------

    def cam_ids(self) -> list[str]:
        return list(self._cameras)

    def latency_p95_ms_last_1s(self, cam_id: str | None = None) -> float:
        if cam_id is None:
            # legacy single-cam: use the first widget
            cam_id = self._cameras[0]
        vw = self.video_widgets.get(cam_id)
        return vw.latency_p95_ms_last_1s() if vw else 0.0

    def freeze_ms_last_1s(self, cam_id: str | None = None) -> float:
        if cam_id is None:
            cam_id = self._cameras[0]
        vw = self.video_widgets.get(cam_id)
        return vw.freeze_ms_last_1s() if vw else 0.0

    def start(self) -> None:
        for vw in self.video_widgets.values():
            vw.start()
        # Wire client -> per-cam widget. The client invokes this on its
        # Zenoh callback thread.
        self._client.set_au_callback(self._on_au)
        logger.info(
            'MainWindow started (vehicle_id=%s, cameras=%s)',
            self._client.vehicle_id,
            self._cameras,
        )

    def stop(self) -> None:
        self._clock_timer.stop()
        self._stats_timer.stop()
        try:
            self._client.set_au_callback(None)
        except Exception:
            pass
        for vw in self.video_widgets.values():
            vw.stop()
        logger.info('MainWindow stopped')

    # ------------------------------------------------------------------
    # AU dispatch (Zenoh thread)
    # ------------------------------------------------------------------

    def _on_au(
        self,
        cam_id: str,
        au: bytes,
        vehicle_ts: float,
        encode_ms: float,
        flags: int,
        server_rx_ts: float,
        veh_to_srv_ms: float,
        stale_ms: float,
    ) -> None:
        now_mono = time.monotonic()
        # In single-camera legacy mode the client may emit cam_id='' even
        # if our key is ''.
        widget = self.video_widgets.get(cam_id)
        if widget is None and self._single_mode and self.video_widgets:
            # fall back to the lone widget
            cam_id, widget = next(iter(self.video_widgets.items()))

        if widget is None:
            return

        self._last_veh_to_srv_ms[cam_id] = float(veh_to_srv_ms)
        self._last_au_mono[cam_id] = now_mono
        if flags & 0x01:
            self._last_idr_au_mono[cam_id] = now_mono

        widget.on_au(
            au, vehicle_ts, encode_ms, flags, server_rx_ts,
            veh_to_srv_ms, stale_ms,
        )

    # ------------------------------------------------------------------
    # periodic UI refresh (Qt main thread)
    # ------------------------------------------------------------------

    def _update_clock(self) -> None:
        self._clock.setText(time.strftime('%H:%M:%S'))

    def _update_stats(self) -> None:
        # Build per-cam video stats dict.
        vs_by_cam: dict[str, dict] = {}
        for cam, vw in self.video_widgets.items():
            vs = vw.snapshot()
            vs['put_latency_ms'] = float(self._last_veh_to_srv_ms.get(cam, 0.0))
            vs_by_cam[cam] = vs
        self.stats_panel.update_video_stats(vs_by_cam)
        self.stats_panel.update_client_stats(self._client.snapshot_stats())

        now_mono = time.monotonic()
        # Aggregate: link OK if any cam has produced an AU in 2 s; IDR
        # recent if any cam has had an IDR in 5 s.
        link_ok = any(
            (now_mono - t) < 2.0 for t in self._last_au_mono.values() if t > 0
        )
        idr_recent = any(
            (now_mono - t) < 5.0 for t in self._last_idr_au_mono.values() if t > 0
        )
        self._badge_link.set_state('ok' if link_ok else 'off')
        self._badge_idr.set_state('ok' if idr_recent else 'off')

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
