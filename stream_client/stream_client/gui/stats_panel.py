"""Right-hand stats column for the TCP viewer.

Three cards (PIPELINE / LATENCY / FREEZE) replace the legacy four-card
PIPELINE/BANDWIDTH/RTX/RTT layout — RTX is gone (TCP has no NACK path),
RTT moves into the LATENCY card alongside the per-AU p95.

Multi-camera: when more than one camera id has been registered via
`set_cameras`, a combo box appears at the top of the panel and only the
selected camera's metrics are displayed. Stats for all cameras are kept
internally so switching does not lose context.
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame, QComboBox,
)

logger = logging.getLogger(__name__)

BG       = '#0d1117'
BG_CARD  = '#161b22'
BORDER   = '#21262d'
TEXT     = '#c9d1d9'
MUTED    = '#8b949e'


def _kv(key, val, color=''):
    style = f'color:{color};' if color else ''
    return (
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:baseline;line-height:1.8;gap:8px;">'
        f'<span style="color:{MUTED};white-space:nowrap;">{key}</span>'
        f'<span style="text-align:right;{style}">{val}</span>'
        f'</div>'
    )


class StatsPanel(QWidget):
    """3-card layout: PIPELINE / LATENCY / FREEZE.

    Stats per camera are stored in `_video_stats[cam_id]` and
    `_client_stats[cam_id]`. The panel always renders the currently
    selected camera (`_selected`). Single-camera mode shows the legacy
    cam-less view (no combo box).
    """

    CARDS = ('PIPELINE', 'LATENCY', 'FREEZE')

    def __init__(self, panel_width: int = 340, font_family: str = '', parent=None):
        super().__init__(parent)
        self.setFixedWidth(panel_width)
        css_font = f'font-family:{font_family};' if font_family else ''
        self.setStyleSheet(
            f'background:{BG}; color:{TEXT}; {css_font} font-size:12px;'
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Top: camera selector (hidden in single-camera mode).
        self._cam_combo = QComboBox()
        self._cam_combo.setStyleSheet(
            f'QComboBox {{ background:{BG_CARD}; color:{TEXT}; '
            f'border:1px solid {BORDER}; padding:4px 8px; }}'
        )
        self._cam_combo.currentTextChanged.connect(self._on_cam_changed)
        self._cam_combo.hide()
        outer.addWidget(self._cam_combo)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            f'QScrollArea {{ border: none; background: {BORDER}; }}'
            f'QScrollBar:vertical {{ width:4px; background:transparent; }}'
            f'QScrollBar::handle:vertical {{ background:{BORDER}; border-radius:2px; }}'
        )

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        self._cards: dict[str, QFrame] = {}
        for name in self.CARDS:
            card = self._make_card(name)
            self._layout.addWidget(card)
            self._cards[name] = card

        self._layout.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll)

        # Stats per cam_id. '' is the single-camera sentinel.
        self._video_stats: dict[str, dict] = {}
        self._client_stats: dict[str, dict] = {}
        self._cameras: list[str] = []
        self._selected: str = ''

    def _make_card(self, title: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f'background:{BG_CARD}; padding:8px 10px;')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(0)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f'font-size:10px;letter-spacing:1.5px;color:{MUTED};margin-bottom:6px;'
        )
        layout.addWidget(title_lbl)

        body = QLabel()
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setStyleSheet(f'color:{TEXT};')
        body.setObjectName(f'body_{title}')
        layout.addWidget(body)
        return frame

    def _body(self, name: str) -> QLabel:
        return self._cards[name].findChild(QLabel, f'body_{name}')

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    def set_cameras(self, cameras: list[str]) -> None:
        """Register the cam_ids this panel will display.

        Empty list (or a single '' sentinel) -> hide the combo and use
        the legacy single-camera view.
        """
        self._cameras = list(cameras)
        # block signals while we mutate the combo
        self._cam_combo.blockSignals(True)
        self._cam_combo.clear()
        if len(self._cameras) > 1:
            for c in self._cameras:
                self._cam_combo.addItem(c)
            self._cam_combo.show()
            self._selected = self._cameras[0]
            self._cam_combo.setCurrentText(self._selected)
        else:
            self._cam_combo.hide()
            self._selected = self._cameras[0] if self._cameras else ''
        self._cam_combo.blockSignals(False)
        self._refresh()

    def _on_cam_changed(self, cam_id: str) -> None:
        if cam_id:
            self._selected = cam_id
        self._refresh()

    # ------------------------------------------------------------------
    # update API (1 Hz from MainWindow)
    # ------------------------------------------------------------------

    def update_video_stats(self, stats_by_cam: dict) -> None:
        """Accepts either a flat dict (legacy single-cam) or a
        dict[cam_id, dict] (multi-cam)."""
        if stats_by_cam is None:
            stats_by_cam = {}
        if self._looks_per_cam(stats_by_cam):
            self._video_stats = dict(stats_by_cam)
        else:
            self._video_stats = {self._selected: dict(stats_by_cam)}
        self._refresh()

    def update_client_stats(self, stats_by_cam: dict) -> None:
        if stats_by_cam is None:
            stats_by_cam = {}
        if self._looks_per_cam(stats_by_cam):
            self._client_stats = dict(stats_by_cam)
        else:
            self._client_stats = {self._selected: dict(stats_by_cam)}
        self._refresh()

    @staticmethod
    def _looks_per_cam(d: dict) -> bool:
        """Heuristic: a per-cam dict has dict values; a flat stats dict
        has scalar values."""
        if not d:
            return False
        return all(isinstance(v, dict) for v in d.values())

    def _refresh(self) -> None:
        cam = self._selected
        vs = self._video_stats.get(cam, {})
        cs = self._client_stats.get(cam, {})

        # PIPELINE: per-AU counters + per-second throughput.
        decoded = vs.get('decoded_count', 0)
        fps = vs.get('fps', 0.0)
        frame_size_last = vs.get('frame_size_last', 0)
        frame_kb = (
            f'{frame_size_last / 1024:.1f} KB'
            if frame_size_last > 0 else 'N/A'
        )
        bitrate = vs.get('bitrate_mbps', 0.0)
        bitrate_hint = vs.get('bitrate_hint_kbps', 0)
        au_count = cs.get('au_count', 0)
        idr_count = cs.get('idr_count', 0)
        stale_drops = cs.get('stale_drop_count', 0)
        sanity_drops = cs.get('sanity_drop_count', 0)

        self._body('PIPELINE').setText(
            _kv('camera', cam or '<single>') +
            _kv('mode', 'tcp / AU') +
            _kv('rx AUs', str(au_count)) +
            _kv('IDR AUs', str(idr_count)) +
            _kv('decoded', str(decoded)) +
            _kv('fps', f'{fps:.1f}') +
            _kv('AU size', frame_kb) +
            _kv('bitrate', f'{bitrate:.2f} Mbps') +
            _kv('bitrate hint', f'{bitrate_hint} kbps' if bitrate_hint else 'N/A') +
            _kv('stale drop', str(stale_drops)) +
            _kv('sanity drop', str(sanity_drops))
        )

        # LATENCY: stale_ms aggregates (per-AU local now - vehicle_ts).
        lat_p95 = vs.get('latency_p95_ms', 0.0)
        lat_avg = vs.get('latency_avg_ms', 0.0)
        lat_max = vs.get('latency_max_ms', 0.0)
        # put-latency: server-side metric the bot puts in veh_to_srv_ms.
        # We surface the latest sample if available (decimal ms).
        put_lat = vs.get('put_latency_ms', None)
        put_lat_s = f'{put_lat:.1f} ms' if isinstance(put_lat, (int, float)) else 'N/A'

        self._body('LATENCY').setText(
            _kv('p95 (1s)', f'{lat_p95:.1f} ms') +
            _kv('avg (1s)', f'{lat_avg:.1f} ms') +
            _kv('max (1s)', f'{lat_max:.1f} ms') +
            _kv('put latency', put_lat_s)
        )

        # FREEZE: ms over the last 1s + cumulative event count.
        freeze_ms = vs.get('freeze_ms_last_1s', 0.0)
        freeze_evt = vs.get('freeze_event_count', 0)
        self._body('FREEZE').setText(
            _kv('freeze (1s)', f'{freeze_ms:.1f} ms') +
            _kv('events', str(freeze_evt))
        )
