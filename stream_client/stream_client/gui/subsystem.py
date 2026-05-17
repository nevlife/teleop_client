"""Embeddable stream-client video + stats subsystem.

Extracted from ``MainWindow`` so the per-camera VideoWidgets, the
StatsPanel, the AU callback dispatch, and the per-camera metrics provider
can be reused inside a *different* QMainWindow (e.g. the unified
``teleop_ui`` shell) without instantiating the standalone window.

Standalone ``stream_client.gui.main_window.MainWindow`` is unchanged and
still works on its own. This module simply pulls the reusable pieces out
into one place.

Also implements the per-cam MetricsProvider protocol consumed by
``stream_client.send_loop.run_send_loop``.
"""
import logging
import time

from .stats_panel import StatsPanel
from .video_widget import VideoWidget

logger = logging.getLogger(__name__)


class StreamSubsystem:
    """Owns video widgets + stats panel + AU dispatch for one stream client.

    Args:
        client: ``StreamTcpClient`` instance (already configured but
            not yet started — caller invokes ``client.start(...)``
            before or after constructing this object; the AU callback
            is bound here in :meth:`start`).
        cfg: validated stream_client config dict.
    """

    def __init__(self, client, cfg: dict):
        self._client = client
        self._cfg = cfg

        gui_cfg = cfg.get('gui', {})
        font_family = gui_cfg.get(
            'font_family', "Consolas, 'Courier New', monospace"
        )
        panel_w = int(gui_cfg.get('panel_width', 340))
        fps_expected = float(cfg.get('fps_expected', 30.0))

        cameras = list(cfg.get('cameras') or [])
        if cameras:
            self._cameras = cameras
            self._single_mode = False
        else:
            cam_id = cfg.get('video', {}).get('camera_id', '')
            self._cameras = [cam_id]
            self._single_mode = True

        self.video_widgets: dict[str, VideoWidget] = {
            cam: VideoWidget(fps_expected=fps_expected, cam_id=cam)
            for cam in self._cameras
        }
        self.stats_panel = StatsPanel(
            panel_width=panel_w, font_family=font_family
        )
        self.stats_panel.set_cameras(self._cameras)

        self._last_veh_to_srv_ms: dict[str, float] = {c: 0.0 for c in self._cameras}
        self._last_idr_au_mono:   dict[str, float] = {c: 0.0 for c in self._cameras}
        self._last_au_mono:       dict[str, float] = {c: 0.0 for c in self._cameras}

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def cameras(self) -> list[str]:
        return list(self._cameras)

    @property
    def single_mode(self) -> bool:
        return self._single_mode

    # MetricsProvider protocol (per-camera) — consumed by stream send_loop.

    def cam_ids(self) -> list[str]:
        return list(self._cameras)

    def latency_p95_ms_last_1s(self, cam_id: str | None = None) -> float:
        if cam_id is None:
            cam_id = self._cameras[0]
        vw = self.video_widgets.get(cam_id)
        return vw.latency_p95_ms_last_1s() if vw else 0.0

    def freeze_ms_last_1s(self, cam_id: str | None = None) -> float:
        if cam_id is None:
            cam_id = self._cameras[0]
        vw = self.video_widgets.get(cam_id)
        return vw.freeze_ms_last_1s() if vw else 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        for vw in self.video_widgets.values():
            vw.start()
        self._client.set_au_callback(self._on_au)
        logger.info(
            'StreamSubsystem started (vehicle_id=%s, cameras=%s)',
            self._client.vehicle_id, self._cameras,
        )

    def stop(self) -> None:
        try:
            self._client.set_au_callback(None)
        except Exception:
            pass
        for vw in self.video_widgets.values():
            vw.stop()
        logger.info('StreamSubsystem stopped')

    # ------------------------------------------------------------------
    # Periodic refresh — call once per second from the owning window.
    # Returns (link_ok, idr_recent) so the owner can drive its own badges.
    # ------------------------------------------------------------------

    def refresh_stats(self) -> tuple[bool, bool]:
        now_mono = time.monotonic()
        vs_by_cam: dict[str, dict] = {}
        for cam, vw in self.video_widgets.items():
            vs = vw.snapshot()
            vs['put_latency_ms'] = float(self._last_veh_to_srv_ms.get(cam, 0.0))
            vs_by_cam[cam] = vs
        self.stats_panel.update_video_stats(vs_by_cam)
        self.stats_panel.update_client_stats(self._client.snapshot_stats())

        link_ok = any(
            (now_mono - t) < 2.0 for t in self._last_au_mono.values() if t > 0
        )
        idr_recent = any(
            (now_mono - t) < 5.0 for t in self._last_idr_au_mono.values() if t > 0
        )
        return link_ok, idr_recent

    # ------------------------------------------------------------------
    # AU dispatch (called on the Zenoh callback thread).
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
        widget = self.video_widgets.get(cam_id)
        if widget is None and self._single_mode and self.video_widgets:
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
