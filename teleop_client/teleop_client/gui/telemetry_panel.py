"""Vehicle telemetry panel for teleop_client.

Trimmed-down version of the original nev_teleop_client/gui/telemetry_panel.py:
the video-related cards (VIDEO PIPELINE / VIDEO BANDWIDTH / RTT) have been
removed and only the control-side cards (vehicle / joystick / MUX / E-STOP /
RESOURCES / NET INTERFACES / DISK / ALERTS / TWIST) are kept.

Fully independent from stream_client's video_panel.py.

NOTE (L5): this panel is only constructed by ``main.py`` (GUI mode). The
headless ``controller_main.py`` entry point does NOT subscribe to telemetry —
operators using headless mode cannot see vehicle telemetry on the box itself.
"""
import json
import logging

import zenoh
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame,
)

from teleop_contracts import (
    IncompatibleSchemaError,
    parse_envelope,
    TOPIC_TELEMETRY,
    key_for,
)

logger = logging.getLogger(__name__)

MODE_NAMES   = {-1: 'IDLE', 0: 'CTRL', 1: 'NAV', 2: 'REMOTE'}
SRC_NAMES    = {-1: 'NONE', 0: 'NAV',  1: 'TELEOP'}
NS_CODES     = {0: 'OK', 1: 'HB-DELAY', 2: 'SOCK-ERR'}
BRIDGE_FLAGS = {0: 'OK', 1: 'SRV-CMD', 2: 'SOCK-ERR', 3: 'HB-TIMEOUT', 4: 'CTRL-TIMEOUT'}
MUX_FLAGS    = {0: 'OK', 1: 'NAV+NO-TELEOP'}

BG       = '#0d1117'
BG_CARD  = '#161b22'
BORDER   = '#21262d'
TEXT     = '#c9d1d9'
MUTED    = '#8b949e'
GREEN    = '#3fb950'
RED      = '#f85149'
YELLOW   = '#d29922'
BLUE     = '#58a6ff'
FONT     = "Consolas, 'Courier New', monospace"


def _sgn(v):
    return f'{v:+.2f}'


def _fmt_gb(b):
    return f'{b / 1073741824:.1f}'


def _fmt_rate(bps):
    if bps < 1024:
        return f'{bps:.0f}B/s'
    if bps < 1048576:
        return f'{bps / 1024:.1f}K/s'
    return f'{bps / 1048576:.1f}M/s'


def _text_cls(val, warn_at, error_at):
    if val >= error_at:
        return RED
    if val >= warn_at:
        return YELLOW
    return ''


def _dot_html(on, color=GREEN):
    c = color if on else BORDER
    return (
        f'<span style="display:inline-block;width:7px;height:7px;'
        f'border-radius:50%;background:{c};vertical-align:middle;margin-right:4px;"></span>'
    )


def _kv(key, val, color=''):
    style = f'color:{color};' if color else ''
    return (
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;line-height:1.8;gap:8px;">'
        f'<span style="color:{MUTED};white-space:nowrap;">{key}</span>'
        f'<span style="text-align:right;{style}">{val}</span>'
        f'</div>'
    )


def _bar(pct):
    w = max(0, min(100, pct or 0))
    color = RED if w > 90 else YELLOW if w > 70 else BLUE
    return (
        f'<div style="width:100%;height:3px;background:{BORDER};border-radius:2px;margin:2px 0 6px;overflow:hidden;">'
        f'<div style="height:100%;width:{w}%;background:{color};border-radius:2px;"></div>'
        f'</div>'
    )


class TelemetryPanel(QWidget):
    """Bundle of vehicle telemetry cards (control side).

    Video-side cards live separately in stream_client/gui/video_panel.py.
    """

    # M4: emit the parsed dict so consumers don't re-parse the JSON.
    telemetry_updated = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sub = None
        self._vehicle_id: str = ''
        self.setFixedWidth(340)

        self.setStyleSheet(
            f'background:{BG}; color:{TEXT}; font-family:{FONT}; font-size:12px;'
        )

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

        self._cards = {}
        # Video cards (VIDEO PIPELINE / BANDWIDTH / RTT) are owned by stream_client.
        # Only vehicle / network / system cards are kept here.
        # AUTHORITY is on top: at-a-glance answer to "is my input reaching the robot?".
        # ALERTS sits above MUX (alarms take priority over CPU stats).
        for name in ['AUTHORITY', 'ALERTS', 'MUX', 'NETWORK', 'TWIST', 'E-STOP',
                     'JOYSTICK', 'RESOURCES', 'NET INTERFACES', 'DISK']:
            card = self._make_card(name)
            self._layout.addWidget(card)
            self._cards[name] = card

        self._layout.addStretch()
        scroll.setWidget(container)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._rtt_cli_bot_ms = 0.0
        # M3: throttle parse-error warnings on telemetry too.
        self._last_parse_warn_ts = 0.0

        self.telemetry_updated.connect(self._refresh)

    def _make_card(self, title):
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

    def _body(self, name) -> QLabel:
        return self._cards[name].findChild(QLabel, f'body_{name}')

    def start(self, session: zenoh.Session, vehicle_id: str):
        if not vehicle_id:
            raise ValueError('vehicle_id is required')
        self._vehicle_id = vehicle_id
        # Telemetry broadcast is also scoped under the teleop prefix.
        key = key_for(vehicle_id, TOPIC_TELEMETRY)
        self._sub = session.declare_subscriber(key, self._on_telemetry)
        self._body('ALERTS').setText('<span style="color:#3fb950;">Panel OK</span>')
        logger.info(f'TelemetryPanel started (sub={key})')

    def stop(self):
        if self._sub:
            self._sub.undeclare()
            self._sub = None

    def _on_telemetry(self, sample):
        """M4: parse JSON ONCE in the background callback and emit the dict."""
        try:
            _v, s = parse_envelope(bytes(sample.payload))
        except IncompatibleSchemaError as e:
            import time as _time
            now = _time.monotonic()
            if now - self._last_parse_warn_ts >= 5.0:
                logger.warning(f'Telemetry drop: {e}')
                self._last_parse_warn_ts = now
            return
        except Exception as e:
            # M3: rate-limit noisy parse failures to once per 5s.
            import time as _time
            now = _time.monotonic()
            if now - self._last_parse_warn_ts >= 5.0:
                logger.warning(f'Telemetry parse error: {e}')
                self._last_parse_warn_ts = now
            return
        self.telemetry_updated.emit(s)

    def update_rtt(self, rtt_ms: float):
        self._rtt_cli_bot_ms = rtt_ms

    def _refresh(self, s: dict):
        try:
            vid = self._vehicle_id
            veh = s.get('vehicles', {}).get(vid, {})
            self._render_authority(veh.get('mux', {}), veh.get('twist', {}))
            self._render_alerts(s.get('alerts', []))
            self._render_mux(veh.get('mux', {}))
            self._render_network(veh.get('network', {}))
            self._render_twist(veh.get('twist', {}))
            self._render_estop(veh.get('estop', {}), s.get('control', {}))
            self._render_joy(s.get('control', {}), s.get('station_connected', False))
            self._render_resources(veh.get('resources', {}), veh.get('gpu_list', []))
            self._render_netifaces(veh.get('net_interfaces', []), veh.get('resources', {}))
            self._render_disk(veh.get('disk_partitions', []))
        except Exception as e:
            logger.error(f'_refresh error: {e}', exc_info=True)

    # ── AUTHORITY ─────────────────────────────────────────────────────────
    # Tolerances for treating ``twist.final`` as equal to a candidate source.
    # Picked at 0.05 m/s and 0.05 rad/s per spec — coarse enough to tolerate
    # bridge rounding/quantization, tight enough that a real override (e.g.
    # safety layer clamping a 0.4 m/s teleop command to 0.1 m/s) shows up.
    _AUTH_LX_TOL = 0.05
    _AUTH_AZ_TOL = 0.05

    def _render_authority(self, mx, tv):
        final_active = mx.get('final_active', False)
        final_lx = tv.get('final_lx', 0.0)
        final_az = tv.get('final_az', 0.0)
        teleop_lx = tv.get('teleop_lx', 0.0)
        teleop_az = tv.get('teleop_az', 0.0)
        nav_lx = tv.get('nav_lx', 0.0)
        nav_az = tv.get('nav_az', 0.0)

        near_zero = (abs(final_lx) < 1e-6 and abs(final_az) < 1e-6)
        match_teleop = (abs(final_lx - teleop_lx) <= self._AUTH_LX_TOL and
                        abs(final_az - teleop_az) <= self._AUTH_AZ_TOL)
        match_nav = (abs(final_lx - nav_lx) <= self._AUTH_LX_TOL and
                     abs(final_az - nav_az) <= self._AUTH_AZ_TOL)

        # Decision order matches the spec exactly.
        if not final_active or near_zero:
            label, color, sub = 'STOPPED', MUTED, ''
        elif match_teleop:
            label, color, sub = 'OPERATOR', GREEN, ''
        elif match_nav:
            label, color, sub = 'AUTONOMOUS', YELLOW, ''
        else:
            # Override / clamp / safety-layer-modified. Show what's "expected"
            # against the teleop intent so the operator can see how much their
            # command was reduced.
            d_lx = final_lx - teleop_lx
            d_az = final_az - teleop_az
            label = 'OVERRIDDEN'
            color = '#f0883e'  # orange
            sub = (f'<div style="font-size:11px;color:{MUTED};line-height:1.6;'
                   f'margin-top:2px;">Δlx={d_lx:+.2f}, Δaz={d_az:+.2f}</div>')

        # Background tint follows the level for at-a-glance distinction.
        if color == GREEN:
            bg_tint = 'background:rgba(63,185,80,0.08);border-left:3px solid ' + GREEN + ';'
        elif color == YELLOW:
            bg_tint = 'background:rgba(210,153,34,0.08);border-left:3px solid ' + YELLOW + ';'
        elif color == '#f0883e':
            bg_tint = 'background:rgba(240,136,62,0.08);border-left:3px solid #f0883e;'
        else:
            bg_tint = f'background:{BG_CARD};border-left:3px solid {BORDER};'
        self._cards['AUTHORITY'].setStyleSheet(bg_tint + 'padding:8px 10px;')

        body_html = (
            f'<div style="font-size:18px;font-weight:bold;letter-spacing:1px;'
            f'color:{color};line-height:1.4;">{label}</div>'
            + sub
        )
        self._body('AUTHORITY').setText(body_html)

    def _render_mux(self, mx):
        mode = mx.get('requested_mode', -1)
        mode_cls = BLUE if mode == 2 else MUTED if mode == -1 else ''
        self._body('MUX').setText(
            _kv('mode', MODE_NAMES.get(mode, str(mode)), mode_cls) +
            _kv('src', SRC_NAMES.get(mx.get('active_source', -1), '?')) +
            _kv('remote', _dot_html(mx.get('remote_enabled', False)) +
                ('YES' if mx.get('remote_enabled') else 'NO')) +
            _kv('nav', _dot_html(mx.get('nav_active', False)) +
                ('ON' if mx.get('nav_active') else 'OFF')) +
            _kv('teleop', _dot_html(mx.get('teleop_active', False)) +
                ('ON' if mx.get('teleop_active') else 'OFF')) +
            _kv('final', _dot_html(mx.get('final_active', False)) +
                ('ON' if mx.get('final_active') else 'OFF'))
        )

    def _render_network(self, ns):
        connected = ns.get('connected', False)
        st_cls = GREEN if connected else RED

        tele_d = ns.get('tele_delay_ms', 0)
        sep = f'<div style="line-height:1.8;color:{MUTED};">─────</div>'

        self._body('NETWORK').setText(
            _kv('status', _dot_html(connected, st_cls) +
                (NS_CODES.get(ns.get('status_code', 2), '?')), st_cls) +
            sep +
            f'<div style="line-height:1.6;color:{MUTED};font-weight:bold;">TELEMETRY</div>' +
            _kv('tele rx', f'{ns.get("bw_telemetry", 0):.2f} Mbps' if ns.get('bw_telemetry', 0) > 0 else '—') +
            _kv('tele delay', f'{tele_d:.1f} ms' if tele_d > 0 else '—') +
            sep +
            f'<div style="line-height:1.6;color:{MUTED};font-weight:bold;">RTT</div>' +
            _kv('cli↔bot', f'{self._rtt_cli_bot_ms:.1f} ms'
                if self._rtt_cli_bot_ms > 0 else '—')
        )

    def _render_twist(self, tv):
        def row(lx, az):
            return f'{_sgn(lx)} m/s / {_sgn(az)} rad/s'
        self._body('TWIST').setText(
            _kv('nav', row(tv.get('nav_lx', 0), tv.get('nav_az', 0))) +
            _kv('teleop', row(tv.get('teleop_lx', 0), tv.get('teleop_az', 0))) +
            _kv('final', row(tv.get('final_lx', 0), tv.get('final_az', 0)))
        )

    def _render_estop(self, es, ctrl):
        active = es.get('is_estop', False) or ctrl.get('estop', False)
        if active:
            self._cards['E-STOP'].setStyleSheet(
                f'background:rgba(248,81,73,0.07);border-left:2px solid {RED};padding:8px 10px;')
            status = f'<span style="color:{RED};">&#9888; E-STOP ACTIVE</span>'
        else:
            self._cards['E-STOP'].setStyleSheet(f'background:{BG_CARD};padding:8px 10px;')
            status = f'<span style="color:{GREEN};">NORMAL</span>'

        bf = es.get('bridge_flag', 0)
        mf = es.get('mux_flag', 0)
        self._body('E-STOP').setText(
            _kv('status', status) +
            _kv('bridge', BRIDGE_FLAGS.get(bf, str(bf)), RED if bf != 0 else '') +
            _kv('mux', MUX_FLAGS.get(mf, str(mf)), YELLOW if mf != 0 else '')
        )

    def _render_joy(self, ctrl, station_connected):
        joy_con = ctrl.get('joystick_connected', False)
        joy_cls = GREEN if joy_con else RED
        sta_cls = GREEN if station_connected else RED

        self._body('JOYSTICK').setText(
            _kv('station',
                _dot_html(station_connected, sta_cls) +
                ('CONNECTED' if station_connected else 'OFFLINE'),
                sta_cls) +
            _kv('joystick',
                _dot_html(joy_con, joy_cls) +
                ('CONNECTED' if joy_con else 'DISCONNECTED'),
                joy_cls) +
            _kv('cmd',
                f'{ctrl.get("linear_x", 0):.3f} m/s / '
                f'{ctrl.get("steer_angle_deg", 0):.1f} deg / '
                f'{ctrl.get("angular_z", 0):.4f} rad/s')
        )

    def _render_resources(self, r, gpu_list):
        cpu = r.get('cpu_usage', 0)
        cpu_cls = _text_cls(cpu, 70, 90)
        temp = r.get('cpu_temp', 0)
        temp_cls = _text_cls(temp, 60, 80)
        load = r.get('cpu_load', 0)

        html = _kv('CPU',
            f'<span style="color:{cpu_cls or TEXT};">{cpu:.1f}%</span>'
            f'&ensp;<span style="color:{temp_cls or TEXT};">{temp:.1f}C</span>'
            f'&ensp;load {load:.2f}') + _bar(cpu)

        ram_used = r.get('ram_used', 0)
        ram_total = r.get('ram_total', 0)
        ram_pct = (ram_used / ram_total * 100) if ram_total > 0 else 0
        html += _kv('RAM', f'{ram_used} / {ram_total} MB') + _bar(ram_pct)

        if gpu_list:
            for i, g in enumerate(gpu_list):
                if not g:
                    continue
                usage = g.get('gpu_usage', 0)
                gtemp = g.get('gpu_temp', 0)
                power = g.get('gpu_power', 0)
                mem_u = g.get('gpu_mem_used', 0)
                mem_t = g.get('gpu_mem_total', 0)
                g_cls = _text_cls(usage, 70, 90)
                gt_cls = _text_cls(gtemp, 60, 80)
                html += _kv(f'GPU{i}',
                    f'<span style="color:{g_cls or TEXT};">{usage:.1f}%</span>'
                    f'&ensp;<span style="color:{gt_cls or TEXT};">{gtemp:.0f}C</span>'
                    f'&ensp;{power:.0f}W') + _bar(usage)
                html += _kv('', f'mem {int(mem_u)} / {int(mem_t)} MB')

        self._body('RESOURCES').setText(html)

    def _render_netifaces(self, ifaces, resources):
        if not ifaces:
            self._body('NET INTERFACES').setText(f'<span style="color:{MUTED};">no data</span>')
            return

        html = ''
        total = resources.get('net_total_ifaces', 0)
        active = resources.get('net_active_ifaces', 0)
        if total > 0:
            html += _kv('total', f'{active} up / {total}')

        for iface in ifaces:
            if not iface or not iface.get('name'):
                continue
            is_up = iface.get('is_up', False)
            up_cls = GREEN if is_up else RED
            spd = f'{iface.get("speed_mbps", 0)}M' if iface.get('speed_mbps', 0) > 0 else '—'
            html += _kv(iface['name'],
                _dot_html(is_up, up_cls) +
                f'<span style="color:{up_cls};">{"UP" if is_up else "DOWN"}</span>'
                f'&ensp;{spd}')
            html += _kv('',
                f'&#8595;{_fmt_rate(iface.get("in_bps", 0))}'
                f'&ensp;&#8593;{_fmt_rate(iface.get("out_bps", 0))}')

        self._body('NET INTERFACES').setText(html)

    def _render_disk(self, partitions):
        if not partitions:
            self._body('DISK').setText(f'<span style="color:{MUTED};">no data</span>')
            return
        html = ''
        for p in partitions:
            if not p or not p.get('mountpoint'):
                continue
            pct = p.get('percent', 0)
            cls = _text_cls(pct, 70, 90)
            html += _kv(p['mountpoint'],
                f'{_fmt_gb(p.get("used_bytes", 0))} / {_fmt_gb(p.get("total_bytes", 1))} GB'
                f'  <span style="color:{cls or TEXT};">{pct:.0f}%</span>') + _bar(pct)
        self._body('DISK').setText(html)

    def _render_alerts(self, alerts):
        if not alerts:
            self._body('ALERTS').setText(f'<span style="color:{MUTED};">—</span>')
            return
        html = ''
        for a in alerts:
            lvl = a.get('level', 'ok')
            color = RED if lvl == 'error' else YELLOW if lvl == 'warn' else TEXT
            weight = 'font-weight:bold;' if lvl == 'error' else ''
            html += (
                f'<div style="padding:2px 0;line-height:1.6;color:{color};{weight}">'
                f'&#9650; {a.get("message", "")}</div>'
            )
        self._body('ALERTS').setText(html)
