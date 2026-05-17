"""GUI main window for teleop_client.

Simplified version of the original nev_teleop_client/gui/main_window.py with
all VideoWidget / video-stats code removed; only the vehicle controls (MODE
buttons, E-STOP) and the vehicle telemetry (TelemetryPanel) remain.

Shares no imports with stream_client, so either side can be launched alone.
"""
import asyncio
import logging
import threading
import time

import zenoh
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QFrame,
)

from .telemetry_panel import TelemetryPanel

logger = logging.getLogger(__name__)

BG       = '#0d1117'
BG_CARD  = '#161b22'
BORDER   = '#21262d'
BORDER2  = '#30363d'
TEXT     = '#c9d1d9'
MUTED    = '#8b949e'
GREEN    = '#3fb950'
RED      = '#f85149'
YELLOW   = '#d29922'
ORANGE   = '#f0883e'
BLUE     = '#58a6ff'
FONT     = "Consolas, 'Courier New', monospace"

# M5: telemetry freshness window. Outside this, the E-stop toggle reads stale
# local-intent state instead of bot truth, so we disable it.
_STALE_TELEMETRY_S = 3.0
# Feature ②: telemetry-staleness threshold beyond which a PENDING/CONFIRMED
# banner escalates to STALE_NOACK (bot may not actually have stopped).
_ESTOP_NOACK_STALE_S = 2.0
# Feature ③: readiness aggregator's "fresh telemetry" threshold (matches the
# 2s contract used elsewhere; intentionally tighter than _STALE_TELEMETRY_S
# which is the e-stop button's stricter lockout).
_READINESS_FRESH_S = 2.0
# RTT considered "slow" enough to demote to HOLD.
_READINESS_SLOW_RTT_MS = 500.0

SRC_LABEL = {
    'gui':      'Source: GUI',
    'joystick': 'Source: Joystick',
    'auto':     'Source: Auto (controller disconnected)',
    '':         'Source: —',
}


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


class EstopBanner(QFrame):
    """Feature ②: top-of-content E-stop state-machine banner.

    States: NORMAL / PENDING / CONFIRMED / RETRY / CLEAR_PENDING / STALE_NOACK.

    Owns its visual state only; the MainWindow tells it the latest inputs and
    it picks the state itself. Visual-only (no sound per spec).
    """

    # State constants kept as plain strings so callers/tests can compare easily.
    NORMAL         = 'NORMAL'
    PENDING        = 'PENDING'
    CONFIRMED      = 'CONFIRMED'
    RETRY          = 'RETRY'
    CLEAR_PENDING  = 'CLEAR_PENDING'
    STALE_NOACK    = 'STALE_NOACK'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(64)
        self._state = self.NORMAL
        # Pulse phase for STALE_NOACK; flips between two backgrounds.
        self._pulse_on = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(16)

        # Left text block: state + sub-source.
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self._main_lbl = QLabel('ARMED')
        self._main_lbl.setStyleSheet(
            f'font-size:22px; font-weight:bold; letter-spacing:2px; color:{TEXT};'
        )
        self._sub_lbl = QLabel('')
        self._sub_lbl.setStyleSheet(f'font-size:11px; color:{MUTED};')
        text_box.addWidget(self._main_lbl)
        text_box.addWidget(self._sub_lbl)
        layout.addLayout(text_box, stretch=1)

        # Right text block: confirmation timestamp / retry count etc.
        self._aux_lbl = QLabel('')
        self._aux_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._aux_lbl.setStyleSheet(f'font-size:12px; color:{MUTED};')
        layout.addWidget(self._aux_lbl, stretch=0)

        # Initial appearance — thin grey ARMED strip per spec.
        self._apply_style(self.NORMAL)

    def _apply_style(self, state: str, pulse_on: bool = False) -> None:
        # Border colors / backgrounds per state.
        if state == self.NORMAL:
            self.setFixedHeight(22)
            self.setStyleSheet(
                f'EstopBanner {{ background:{BG_CARD}; '
                f'border-bottom:1px solid {BORDER}; }}'
            )
            self._main_lbl.setStyleSheet(
                f'font-size:10px; letter-spacing:2px; color:{MUTED};'
            )
            self._sub_lbl.setVisible(False)
            self._aux_lbl.setVisible(False)
            return

        # Non-NORMAL states are tall and prominent.
        self.setFixedHeight(64)
        self._sub_lbl.setVisible(True)
        self._aux_lbl.setVisible(True)

        if state == self.PENDING or state == self.CLEAR_PENDING:
            bg, fg, border = 'rgba(210,153,34,0.18)', YELLOW, YELLOW
        elif state == self.CONFIRMED:
            bg, fg, border = RED, '#ffffff', RED
        elif state == self.RETRY:
            bg, fg, border = 'rgba(240,136,62,0.20)', ORANGE, ORANGE
        elif state == self.STALE_NOACK:
            if pulse_on:
                bg = RED
                fg = '#ffffff'
            else:
                bg = 'rgba(248,81,73,0.25)'
                fg = RED
            border = RED
        else:
            bg, fg, border = BG_CARD, TEXT, BORDER

        self.setStyleSheet(
            f'EstopBanner {{ background:{bg}; '
            f'border-top:1px solid {border}; border-bottom:2px solid {border}; }}'
        )
        self._main_lbl.setStyleSheet(
            f'font-size:24px; font-weight:bold; letter-spacing:2px; color:{fg};'
        )
        self._sub_lbl.setStyleSheet(f'font-size:11px; color:{fg};')
        self._aux_lbl.setStyleSheet(f'font-size:12px; color:{fg};')

    def render(
        self,
        state: str,
        *,
        source: str = '',
        elapsed_s: float = 0.0,
        retry_count: int = 0,
        confirmed_at: float = 0.0,
        stale_age_s: float = 0.0,
    ) -> None:
        """Apply a new state with the relevant detail fields."""
        if state != self._state:
            # State transitioned — reset pulse phase.
            self._pulse_on = False
            self._state = state

        # Pulse only for STALE_NOACK; toggled by render() being called at 10Hz.
        if state == self.STALE_NOACK:
            self._pulse_on = not self._pulse_on
        self._apply_style(state, pulse_on=self._pulse_on)

        if state == self.NORMAL:
            self._main_lbl.setText('ARMED')
            return

        src_text = SRC_LABEL.get(source, SRC_LABEL[''])
        if state == self.PENDING:
            self._main_lbl.setText(f'E-STOP sending… {elapsed_s:.1f}s')
            self._sub_lbl.setText(src_text)
            self._aux_lbl.setText('Waiting for command to land…')
        elif state == self.CONFIRMED:
            self._main_lbl.setText('■ E-STOP ACTIVE')
            self._sub_lbl.setText(src_text)
            if confirmed_at > 0:
                ts = time.strftime('%H:%M:%S', time.localtime(confirmed_at))
                self._aux_lbl.setText(f'Confirmed: {ts}')
            else:
                self._aux_lbl.setText('Confirmed')
        elif state == self.RETRY:
            self._main_lbl.setText(f'E-STOP retrying… {retry_count} failures')
            self._sub_lbl.setText(src_text)
            self._aux_lbl.setText('zenoh publish failed — retrying')
        elif state == self.CLEAR_PENDING:
            self._main_lbl.setText(f'E-STOP releasing… {elapsed_s:.1f}s')
            self._sub_lbl.setText(src_text)
            self._aux_lbl.setText('Waiting for release confirmation…')
        elif state == self.STALE_NOACK:
            self._main_lbl.setText(f'E-STOP unconfirmed — telemetry lost {stale_age_s:.1f}s')
            self._sub_lbl.setText(src_text)
            self._aux_lbl.setText('Bot state unknown — assuming worst case')


class ReadinessIndicator(QFrame):
    """Feature ③: aggregate READY / HOLD / UNSAFE indicator.

    Lives in the command bar, top-left. Reads from the same inputs the topbar
    badges already use; collapses them into one large at-a-glance widget. The
    tooltip lists every sub-condition that's failing.
    """

    READY  = 'READY'
    HOLD   = 'HOLD'
    UNSAFE = 'UNSAFE'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(200, 64)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)

        self._main_lbl = QLabel('—')
        self._main_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._main_lbl)

        self._sub_lbl = QLabel('Initializing')
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setStyleSheet(f'font-size:10px; color:{MUTED};')
        layout.addWidget(self._sub_lbl)

        self._state = self.HOLD
        self._apply_style(self.HOLD)

    def _apply_style(self, state: str) -> None:
        if state == self.READY:
            bg = 'rgba(63,185,80,0.14)'
            fg = GREEN
            border = GREEN
        elif state == self.UNSAFE:
            bg = 'rgba(248,81,73,0.18)'
            fg = RED
            border = RED
        else:  # HOLD or unknown
            bg = 'rgba(210,153,34,0.14)'
            fg = YELLOW
            border = YELLOW
        self.setStyleSheet(
            f'ReadinessIndicator {{ background:{bg}; border:2px solid {border}; '
            f'border-radius:4px; }}'
        )
        self._main_lbl.setStyleSheet(
            f'font-size:22px; font-weight:bold; letter-spacing:3px; color:{fg};'
        )

    def render(self, state: str, reasons: list, summary: str = '') -> None:
        if state != self._state:
            self._state = state
        self._apply_style(state)
        self._main_lbl.setText(state)
        self._sub_lbl.setText(summary or ('OK' if state == self.READY else 'Check required'))
        if reasons:
            self.setToolTip('\n'.join('• ' + r for r in reasons))
        else:
            self.setToolTip('All conditions met')


class MainWindow(QMainWindow):
    """Standalone window for the teleop side.

    Args:
        session: Zenoh session owned by TeleopClient (used for telemetry sub).
        cfg: validated teleop_client config.
        client: ``TeleopClient`` instance. The mode/E-STOP buttons publish
            through it directly. If None, only telemetry display is available.
        loop: asyncio loop in which TeleopClient is running. When provided,
            GUI mode/E-STOP clicks dispatch work onto this loop (M9) so the
            GUI thread does not block. When None, falls back to a background
            ``threading.Thread``.
    """

    # Feature ②: emitted by the client's estop observer (which may be invoked
    # from arbitrary threads). Using a Signal hops to the GUI thread safely.
    estop_intent_signaled = Signal(bool, str)

    def __init__(
        self,
        session: zenoh.Session,
        cfg: dict,
        client=None,
        loop=None,
        video_widgets: dict | None = None,
        stats_panel: QWidget | None = None,
        title: str | None = None,
    ):
        super().__init__()
        self._session = session
        self._cfg = cfg
        self._client = client
        self._loop = loop
        # When ``video_widgets`` is supplied the content slot hosts the
        # stream-client video grid in place of the legacy placeholder. The
        # caller (e.g. teleop_ui) owns the widgets and their lifecycle.
        self._embedded_video_widgets = video_widgets
        self._embedded_stats_panel = stats_panel
        self._last_state = {}
        self._last_telemetry_ts = 0.0  # M5: monotonic timestamp of last telemetry msg
        # M5: local intent reconciled against telemetry when fresh.
        self._estop_local_intent = False

        # Feature ②: e-stop state-machine inputs.
        # ``_estop_intent`` is what the user/auto last commanded.
        # ``_estop_intent_ts`` (monotonic) drives the elapsed-time display in
        # PENDING / CLEAR_PENDING / STALE_NOACK.
        # ``_estop_source`` is one of 'gui' / 'joystick' / 'auto' / ''.
        # ``_estop_confirmed_at`` (wall-clock) is the moment telemetry first
        # showed is_estop matching intent — for the "Confirmed:" timestamp.
        # ``_estop_pending_retry_count`` increments while client.estop_pending.
        self._estop_intent = False
        self._estop_intent_ts = 0.0
        self._estop_source = ''
        self._estop_confirmed_at = 0.0
        self._estop_pending_retry_count = 0
        # Tracks last seen client.estop_pending to count retry events.
        self._prev_estop_pending = False

        self.setWindowTitle(title or 'NEV Teleop Client')
        self.setMinimumSize(720, 720)
        self.setStyleSheet(
            f'QMainWindow {{ background:{BG}; }}'
            f'QWidget {{ color:{TEXT}; font-family:{FONT}; font-size:12px; }}'
        )

        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- topbar ----
        topbar = QWidget()
        topbar.setFixedHeight(36)
        topbar.setStyleSheet(f'background:{BG}; border-bottom:1px solid {BORDER};')
        tb_layout = QHBoxLayout(topbar)
        tb_layout.setContentsMargins(12, 0, 12, 0)

        title = QLabel('NEV TELEOP')
        title.setStyleSheet(
            f'font-size:13px; font-weight:bold; letter-spacing:1px; color:{TEXT};'
        )
        tb_layout.addWidget(title)
        tb_layout.addStretch()

        # Feature ③: badges are now smaller and live next to the readiness
        # indicator in the cmdbar (relocated below). The topbar keeps only
        # title + clock.

        self._clock = QLabel('--:--:--')
        self._clock.setStyleSheet(f'font-size:11px; color:{MUTED}; margin-left:8px;')
        tb_layout.addWidget(self._clock)

        main_layout.addWidget(topbar)

        # ---- command bar (MODE / E-STOP) ----
        self._cmdbar = QWidget()
        self._cmdbar.setFixedHeight(80)
        self._cmdbar.setStyleSheet(f'background:{BG}; border-bottom:1px solid {BORDER};')
        cb_layout = QHBoxLayout(self._cmdbar)
        cb_layout.setContentsMargins(12, 6, 12, 6)
        cb_layout.setSpacing(12)

        # Feature ③: top-left readiness indicator (large, aggregate).
        self._readiness = ReadinessIndicator()
        cb_layout.addWidget(self._readiness)

        # Feature ③: keep the 5 diagnostic badges next to the aggregate. They
        # are slightly smaller (compact_badge) so the readiness indicator
        # remains the focal point.
        badge_box = QVBoxLayout()
        badge_box.setSpacing(2)
        badge_row1 = QHBoxLayout()
        badge_row1.setSpacing(4)
        badge_row2 = QHBoxLayout()
        badge_row2.setSpacing(4)

        def _compact_badge(name):
            b = Badge(name)
            b.setFixedHeight(18)
            b.setMinimumWidth(32)
            return b

        self._badge_veh = _compact_badge('VEH')
        self._badge_stas = _compact_badge('STAS')
        self._badge_joy = _compact_badge('JOY')
        self._badge_rem = _compact_badge('REM')
        # H4: session-health surface. Goes red when zenoh reconnect is in
        # progress / has failed.
        self._badge_link = _compact_badge('LINK')
        badge_row1.addWidget(self._badge_veh)
        badge_row1.addWidget(self._badge_stas)
        badge_row1.addWidget(self._badge_joy)
        badge_row2.addWidget(self._badge_rem)
        badge_row2.addWidget(self._badge_link)
        badge_row1.addStretch()
        badge_row2.addStretch()
        badge_box.addLayout(badge_row1)
        badge_box.addLayout(badge_row2)
        cb_layout.addLayout(badge_box)

        mode_label = QLabel('MODE')
        mode_label.setStyleSheet(f'color:{MUTED}; font-size:11px; margin-right:4px;')
        cb_layout.addWidget(mode_label)

        self._mode_buttons = {}
        for mode_val, mode_name in [(-1, 'IDLE'), (0, 'CTRL'), (1, 'NAV'), (2, 'REMOTE')]:
            btn = QPushButton(mode_name)
            btn.setStyleSheet(self._mode_btn_style(False))
            btn.clicked.connect(lambda checked, m=mode_val: self._on_mode_click(m))
            cb_layout.addWidget(btn)
            self._mode_buttons[mode_val] = btn

        cb_layout.addStretch()

        self._estop_btn = QPushButton('■ E-STOP')
        self._estop_btn.setStyleSheet(
            f'color:{RED}; border:1px solid {RED}; padding:3px 18px;'
            f'font-size:12px; font-weight:bold; background:transparent; border-radius:3px;'
        )
        self._estop_btn.clicked.connect(self._on_estop_click)
        cb_layout.addWidget(self._estop_btn)

        main_layout.addWidget(self._cmdbar)

        # Feature ②: prominent E-stop banner directly under the cmdbar.
        self._estop_banner = EstopBanner()
        main_layout.addWidget(self._estop_banner)

        # ---- body: vehicle telemetry panel only ----
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Content slot: either the embedded stream video grid (unified UI)
        # or the legacy "stream lives elsewhere" placeholder (standalone).
        if self._embedded_video_widgets:
            video_grid = QWidget()
            grid_layout = QHBoxLayout(video_grid)
            grid_layout.setContentsMargins(0, 0, 0, 0)
            grid_layout.setSpacing(1)
            for vw in self._embedded_video_widgets.values():
                grid_layout.addWidget(vw, stretch=1)
            content_layout.addWidget(video_grid, stretch=1)
        else:
            placeholder = QLabel('VIDEO is hosted by stream_client (separate process)')
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet(
                f'color:{MUTED}; background:#0a0d12; padding:16px; font-size:13px;'
            )
            content_layout.addWidget(placeholder, stretch=1)

        separator = QWidget()
        separator.setFixedWidth(1)
        separator.setStyleSheet(f'background:{BORDER};')
        content_layout.addWidget(separator)

        # Right column: stream stats panel (if embedded) on top, telemetry
        # panel below. In standalone mode the stats panel is absent and the
        # telemetry panel takes the whole right column.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        if self._embedded_stats_panel is not None:
            right_layout.addWidget(self._embedded_stats_panel, stretch=0)
            sep_h = QWidget()
            sep_h.setFixedHeight(1)
            sep_h.setStyleSheet(f'background:{BORDER};')
            right_layout.addWidget(sep_h)
        self.telemetry_panel = TelemetryPanel()
        right_layout.addWidget(self.telemetry_panel, stretch=1)
        content_layout.addWidget(right, stretch=0)

        main_layout.addWidget(content, stretch=1)
        self.setCentralWidget(central)

        # ---- timers ----
        self._clock_timer = QTimer()
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)

        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(1000)

        # H5: drive RTT expiry from a Qt timer in GUI mode.
        self._rtt_expire_timer = QTimer()
        self._rtt_expire_timer.timeout.connect(self._expire_rtt)
        self._rtt_expire_timer.start(1000)

        # Feature ②/③: 10Hz tick for banner-elapsed/pulse and readiness aggregate.
        self._fast_timer = QTimer()
        self._fast_timer.timeout.connect(self._tick_fast)
        self._fast_timer.start(100)

        # M4: telemetry_updated now carries a parsed dict, not a JSON string.
        self.telemetry_panel.telemetry_updated.connect(self._on_telemetry)

        # Feature ②: hook into the client's estop publisher to learn about
        # joystick / auto-on-disconnect entry points immediately (not via
        # telemetry, which can lag or never arrive on a bad link).
        self.estop_intent_signaled.connect(self._on_estop_intent)
        if self._client is not None:
            # Observer may be invoked from arbitrary threads — emit the signal
            # to hop to the GUI thread.
            self._client.set_estop_observer(
                lambda activate, source: self.estop_intent_signaled.emit(activate, source)
            )

    def start(self):
        vehicle_id = self._cfg['vehicle_id']
        self.telemetry_panel.start(self._session, vehicle_id)
        logger.info(f'Teleop MainWindow started (vehicle_id={vehicle_id})')

    def stop(self):
        self._clock_timer.stop()
        self._stats_timer.stop()
        self._rtt_expire_timer.stop()
        self._fast_timer.stop()
        if self._client is not None:
            # Drop the observer so a late publish during teardown doesn't try
            # to emit into a destroyed Qt object.
            try:
                self._client.set_estop_observer(None)
            except Exception:
                pass
        self.telemetry_panel.stop()
        logger.info('Teleop MainWindow stopped')

    def _update_clock(self):
        self._clock.setText(time.strftime('%H:%M:%S'))

    def _update_stats(self):
        if self._client:
            self.telemetry_panel.update_rtt(self._client.rtt_cli_bot_ms)
            # H4: surface session health on the LINK badge.
            if self._client.session_unhealthy or self._client.estop_pending:
                self._badge_link.set_state('error')
            else:
                self._badge_link.set_state('ok')

    def _expire_rtt(self):
        if self._client:
            self._client.expire_rtt_if_stale()

    def _on_telemetry(self, s: dict):
        """M4: receives a parsed telemetry dict (parsed once in the bg callback)."""
        self._last_state = s
        self._last_telemetry_ts = time.monotonic()

        vid = self._cfg['vehicle_id']
        veh = s.get('vehicles', {}).get(vid, {})

        robot_age = veh.get('robot_age', -1)
        if robot_age < 0:
            self._badge_veh.set_state('off')
        elif robot_age < 2:
            self._badge_veh.set_state('ok')
        else:
            self._badge_veh.set_state('error', f'VEH {robot_age:.0f}s')

        self._badge_stas.set_state('ok' if s.get('station_connected', False) else 'error')
        ctrl = s.get('control', {})
        self._badge_joy.set_state('ok' if ctrl.get('joystick_connected', False) else 'off')
        self._badge_rem.set_state('ok' if veh.get('remote_enabled', False) else 'off')

        active_mode = veh.get('mux', {}).get('requested_mode', -1)
        station_on = s.get('station_connected', False)
        for mode_val, btn in self._mode_buttons.items():
            is_active = (mode_val == active_mode)
            btn.setStyleSheet(self._mode_btn_style(is_active, not station_on))

        estop_active = ctrl.get('estop', False) or veh.get('estop', {}).get('is_estop', False)
        # M5: reconcile local intent with fresh telemetry.
        self._estop_local_intent = estop_active

        # Feature ②: capture wall-clock moment of first telemetry-confirmed
        # transition INTO is_estop=true while we have intent. Only fires once
        # per intent edge so the displayed "Confirmed:" timestamp doesn't drift.
        if estop_active and self._estop_intent and self._estop_confirmed_at == 0.0:
            self._estop_confirmed_at = time.time()
        elif not estop_active and not self._estop_intent:
            # Fully cleared (both sides agree off) → reset confirmation marker.
            self._estop_confirmed_at = 0.0

        if estop_active:
            self._estop_btn.setText('■ RELEASE')
            self._estop_btn.setStyleSheet(
                f'color:#fff; border:1px solid {RED}; padding:3px 18px;'
                f'font-size:12px; font-weight:bold; background:{RED}; border-radius:3px;'
            )
        else:
            self._estop_btn.setText('■ E-STOP')
            self._estop_btn.setStyleSheet(
                f'color:{RED}; border:1px solid {RED}; padding:3px 18px;'
                f'font-size:12px; font-weight:bold; background:transparent; border-radius:3px;'
            )

    def _submit_to_loop(self, coro_factory):
        """M9: dispatch a publish off the GUI thread.

        ``coro_factory`` is a zero-arg callable returning the coroutine to run
        (so we can construct it inside the loop thread to avoid "coroutine was
        never awaited" warnings on the cross-thread path).
        """
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(
                    asyncio.create_task, coro_factory()
                )
                return
            except RuntimeError as e:
                logger.warning(f'submit-to-loop failed, falling back to thread: {e}')
        # Fallback: run the (synchronous wrapper of the) work on a daemon thread.
        threading.Thread(
            target=lambda: self._run_sync_wrapper(coro_factory),
            daemon=True,
        ).start()

    @staticmethod
    def _run_sync_wrapper(coro_factory):
        # The publish helpers are synchronous; the coro_factory just wraps a
        # sync call. Running it directly is safe.
        try:
            asyncio.run(coro_factory())
        except Exception as e:
            logger.warning(f'background publish error: {e}')

    def _on_mode_click(self, mode: int):
        if not self._client:
            return

        async def _do_send():
            self._client.send_cmd_mode(mode)

        self._submit_to_loop(_do_send)

    def _on_estop_click(self):
        if not self._client:
            return
        # M5: refuse the click if telemetry is stale; we don't know the bot's
        # truth, so toggling local intent could send the wrong direction.
        if self._telemetry_is_stale():
            logger.warning(
                f'E-stop click ignored: telemetry stale > {_STALE_TELEMETRY_S}s'
            )
            return
        ctrl = self._last_state.get('control', {})
        active = not ctrl.get('estop', False)
        self._estop_local_intent = active

        async def _do_send():
            self._client.send_estop(active, source='gui')

        self._submit_to_loop(_do_send)

    def _telemetry_is_stale(self) -> bool:
        if self._last_telemetry_ts == 0.0:
            return True
        return (time.monotonic() - self._last_telemetry_ts) > _STALE_TELEMETRY_S

    # ── Feature ② : E-stop banner state machine ────────────────────────────
    def _on_estop_intent(self, activate: bool, source: str) -> None:
        """Called on the GUI thread the instant ANY entry point fires E-stop.

        Hooked up to ``TeleopClient.send_estop``'s observer via a Signal so
        joystick/auto-on-disconnect paths surface the banner without waiting
        for telemetry to come back.
        """
        # If intent changed, reset the elapsed-time clock and confirmation.
        if activate != self._estop_intent:
            self._estop_intent_ts = time.monotonic()
            self._estop_confirmed_at = 0.0
            self._estop_pending_retry_count = 0
        self._estop_intent = activate
        self._estop_source = source or self._estop_source or 'gui'
        # Render once immediately for responsiveness; the 10Hz timer takes
        # over after that.
        self._tick_fast()

    def _telemetry_age_s(self) -> float:
        if self._last_telemetry_ts == 0.0:
            return float('inf')
        return time.monotonic() - self._last_telemetry_ts

    def _compute_estop_state(self) -> str:
        """Pick a banner state from (intent, telemetry, client.estop_pending)."""
        telemetry_bot_estop = False
        try:
            ctrl = self._last_state.get('control', {})
            veh = self._last_state.get('vehicles', {}).get(self._cfg['vehicle_id'], {})
            telemetry_bot_estop = (
                ctrl.get('estop', False) or veh.get('estop', {}).get('is_estop', False)
            )
        except Exception:
            pass

        tele_age = self._telemetry_age_s()
        pending = bool(self._client and self._client.estop_pending)

        # Stale-noack escalation: we intended an estop, telemetry hasn't
        # updated, bot status is unknown — assume worst.
        if self._estop_intent and tele_age > _ESTOP_NOACK_STALE_S:
            return EstopBanner.STALE_NOACK

        # zenoh publish is stuck → operator must know the bot may not have
        # received the command yet.
        if pending:
            return EstopBanner.RETRY

        if self._estop_intent and telemetry_bot_estop:
            return EstopBanner.CONFIRMED
        if self._estop_intent and not telemetry_bot_estop:
            return EstopBanner.PENDING
        if not self._estop_intent and telemetry_bot_estop:
            # Operator/auto cleared it locally but bot still shows estop.
            return EstopBanner.CLEAR_PENDING
        return EstopBanner.NORMAL

    def _tick_fast(self) -> None:
        """10Hz: update banner elapsed/pulse + readiness aggregator."""
        # ── Banner ─────────────────────────────────────────────────────────
        state = self._compute_estop_state()

        # Track retry-count edges from the client's estop_pending boolean.
        if self._client is not None:
            cur_pending = self._client.estop_pending
            if cur_pending and not self._prev_estop_pending:
                self._estop_pending_retry_count += 1
            self._prev_estop_pending = cur_pending

        elapsed = 0.0
        if self._estop_intent_ts > 0.0:
            elapsed = time.monotonic() - self._estop_intent_ts

        self._estop_banner.render(
            state,
            source=self._estop_source,
            elapsed_s=elapsed,
            retry_count=self._estop_pending_retry_count,
            confirmed_at=self._estop_confirmed_at,
            stale_age_s=self._telemetry_age_s(),
        )

        # ── Readiness aggregate ───────────────────────────────────────────
        self._update_readiness()

    # ── Feature ③ : READY / HOLD / UNSAFE aggregate ────────────────────────
    def _update_readiness(self) -> None:
        s = self._last_state
        vid = self._cfg.get('vehicle_id', '')
        veh = s.get('vehicles', {}).get(vid, {}) if s else {}
        ctrl = s.get('control', {}) if s else {}

        tele_age = self._telemetry_age_s()
        tele_fresh = tele_age <= _READINESS_FRESH_S
        station = bool(s.get('station_connected', False)) if s else False
        joystick = bool(ctrl.get('joystick_connected', False))
        estop_active = bool(
            ctrl.get('estop', False) or veh.get('estop', {}).get('is_estop', False)
        )
        mux = veh.get('mux', {})
        teleop_active = bool(mux.get('teleop_active', False))
        requested_mode = mux.get('requested_mode', -1)

        rtt = self._client.rtt_cli_bot_ms if self._client else 0.0
        # rtt == 0 means "unknown" per spec ("RTT<500ms or unknown"). Treat as OK.
        link_ok = (rtt == 0.0) or (rtt < _READINESS_SLOW_RTT_MS)

        alerts = s.get('alerts', []) if s else []
        has_error_alert = any(a.get('level') == 'error' for a in alerts)

        # ── UNSAFE conditions (any one triggers) ──────────────────────────
        unsafe_reasons = []
        if estop_active:
            unsafe_reasons.append('E-STOP active')
        if not tele_fresh:
            if tele_age == float('inf'):
                unsafe_reasons.append('No telemetry received')
            else:
                unsafe_reasons.append(f'Telemetry {tele_age:.1f}s stale')
        if has_error_alert:
            unsafe_reasons.append('Error-level alert raised')

        if unsafe_reasons:
            self._readiness.render(
                ReadinessIndicator.UNSAFE,
                unsafe_reasons,
                summary=unsafe_reasons[0],
            )
            return

        # ── HOLD conditions (only checked once not UNSAFE) ────────────────
        hold_reasons = []
        if not station:
            hold_reasons.append('Station disconnected')
        if not joystick:
            hold_reasons.append('Joystick disconnected')
        if not link_ok:
            hold_reasons.append(f'Link slow (RTT={rtt:.0f}ms)')
        if requested_mode != 2:
            hold_reasons.append('Mode != REMOTE — input ignored')

        if hold_reasons:
            self._readiness.render(
                ReadinessIndicator.HOLD,
                hold_reasons,
                summary=hold_reasons[0],
            )
            return

        # ── READY (additional must-haves) ─────────────────────────────────
        # Spec: mux.teleop_active true (operator's input is reaching wheels).
        if not teleop_active:
            self._readiness.render(
                ReadinessIndicator.HOLD,
                ['teleop_active=false (input not reaching wheels)'],
                summary='Input not reaching wheels',
            )
            return

        self._readiness.render(
            ReadinessIndicator.READY,
            [],
            summary='Ready to drive',
        )

    def _mode_btn_style(self, active=False, disabled=False):
        if active:
            return (
                f'background:rgba(88,166,255,0.12); color:#fff; border:1px solid {BLUE};'
                f'font-size:11px; padding:3px 10px; border-radius:3px;'
            )
        opacity = 'opacity:0.4;' if disabled else ''
        return (
            f'background:transparent; color:{MUTED}; border:1px solid {BORDER2};'
            f'font-size:11px; padding:3px 10px; border-radius:3px; {opacity}'
        )

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
