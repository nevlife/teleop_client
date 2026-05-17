import math
import os
import logging
from time import monotonic
from typing import Optional

from ..state import StationState
from .base import Controller

logger = logging.getLogger(__name__)

# Headless SDL: drive pygame without any display/audio backend so it works on
# servers / launch units. Must be set BEFORE ``pygame.init()`` (M8).
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

try:
    import pygame
    _HAS_PYGAME = True
except ImportError:
    _HAS_PYGAME = False
    pygame = None  # type: ignore
    logger.warning('pygame not installed — joystick disabled')


# H2: how long since a successful poll before we force-zero the controls.
_FRESHNESS_DEADLINE_S = 0.2


def _safe_float(v: float) -> float:
    """M2: collapse NaN / Inf to 0.0 at the controller→state boundary."""
    if not math.isfinite(v):
        return 0.0
    return float(v)


class JoystickController(Controller):

    def __init__(self, state: StationState, cfg: dict):
        super().__init__(state)
        self.axis_speed   = cfg.get('axis_speed',   1)
        self.axis_steer   = cfg.get('axis_steer',   3)
        self.btn_estop    = cfg.get('btn_estop',    4)
        self.max_speed    = cfg.get('max_speed',    1.0)
        self.max_steer    = math.radians(cfg.get('max_steer_deg', 27.0))
        self.deadzone     = cfg.get('deadzone',     0.05)
        self.invert_speed = cfg.get('invert_speed', True)
        # M1: auto-fire E-stop on physical disconnect. Configurable.
        self.estop_on_disconnect = cfg.get('estop_on_disconnect', True)

        self._joystick: Optional[object] = None
        self._use_estop_btn = False
        # H2: track last successful poll so we can detect silent disconnects.
        self._last_successful_poll_ts: float = 0.0
        self._stale_zeroed: bool = False

    def name(self) -> str:
        return 'joystick'

    def _setup(self):
        # M7: subclasses no longer need to override start() to handle the
        # missing-pygame case — that's centralised in Controller._setup_guard.
        if not _HAS_PYGAME:
            raise RuntimeError('pygame not installed — joystick disabled')
        # M8: pygame.init() pulls in audio/video subsystems unnecessarily; we
        # only need joystick + event pump. Audio/video drivers are pinned to
        # "dummy" at module import (above) for safety on headless boxes.
        pygame.init()
        pygame.joystick.init()
        pygame.event.pump()

    def _teardown(self):
        if _HAS_PYGAME:
            pygame.quit()

    def poll(self) -> bool:
        for event in pygame.event.get():
            if event.type == pygame.JOYDEVICEADDED:
                if self._joystick is None:
                    self._connect(event.device_index)
            elif event.type == pygame.JOYDEVICEREMOVED:
                if self._joystick and event.instance_id == self._joystick.get_instance_id():
                    logger.warning('Joystick disconnected')
                    self._joystick = None
                    self.on_disconnect()
            # elif event.type == pygame.JOYBUTTONDOWN:
            #     # Joystick e-stop button disabled — toggled off pending
            #     # investigation of repeated False publishes seen on the server.
            #     if self._use_estop_btn and event.button == self.btn_estop:
            #         self._toggle_estop()

        now = monotonic()
        if self._joystick is None:
            # H2: still enforce stale-deadline path so callers see consistent
            # zeroing semantics even with no joystick.
            self._enforce_freshness_deadline(now)
            return False

        joy = self._joystick

        try:
            speed_raw = joy.get_axis(self.axis_speed)
            steer_raw = joy.get_axis(self.axis_steer)
        except Exception as e:
            # Read failed (e.g. device about to be removed). Trip freshness.
            logger.warning(f'joystick read failed: {e}')
            self._enforce_freshness_deadline(now)
            return self._stale_zeroed is False

        speed = self._apply_deadzone(speed_raw)
        if self.invert_speed:
            speed = -speed
        linear_x = speed * self.max_speed

        steer = self._apply_deadzone(steer_raw)
        steer_angle = -steer * self.max_steer

        # H1: clamp to configured limits.
        linear_x = max(-self.max_speed, min(self.max_speed, linear_x))
        steer_angle = max(-self.max_steer, min(self.max_steer, steer_angle))

        # M2: sanitize at the boundary.
        linear_x = _safe_float(linear_x)
        steer_angle = _safe_float(steer_angle)

        self.state.update_control(linear_x, steer_angle)

        self._last_successful_poll_ts = now
        self._stale_zeroed = False

        return True

    def _enforce_freshness_deadline(self, now: float) -> None:
        """H2: if we haven't had a fresh axis read in > 200ms, zero the controls."""
        if self._stale_zeroed:
            return
        # If we've never had a successful poll yet, leave state at its initial
        # (zero) value but mark stale so we don't keep doing it every tick.
        if self._last_successful_poll_ts == 0.0:
            self.state.update_control(0.0, 0.0)
            self._stale_zeroed = True
            return
        if (now - self._last_successful_poll_ts) > _FRESHNESS_DEADLINE_S:
            logger.warning(
                f'joystick poll stale > {_FRESHNESS_DEADLINE_S * 1000:.0f}ms — '
                f'forcing controls to zero'
            )
            self.state.update_control(0.0, 0.0)
            self._stale_zeroed = True

    def on_disconnect(self):
        super().on_disconnect()
        self._joystick = None
        self._stale_zeroed = True
        # Auto-E-stop on physical disconnect disabled — pending investigation
        # of repeated False publishes seen on the server. The controls are
        # still zeroed by the freshness-deadline path above.
        # if self.estop_on_disconnect and self._client is not None:
        #     try:
        #         self.state.estop = True
        #         self._client.send_estop(True, source='auto')
        #         logger.warning('Auto E-stop fired due to joystick disconnect')
        #     except Exception as e:
        #         logger.error(f'Auto E-stop on disconnect failed: {e}')

    def _connect(self, device_index: int):
        joy = pygame.joystick.Joystick(device_index)
        self._joystick = joy
        logger.info(f'Joystick connected: {joy.get_name()}')

        num_axes    = joy.get_numaxes()
        num_buttons = joy.get_numbuttons()

        speed_ok = self.axis_speed < num_axes
        steer_ok = self.axis_steer < num_axes

        # L3: refuse to operate if BOTH axes are out of range — silently
        # collapsing both to axis 0 produces phantom controls that may be wired
        # to a totally different physical axis (e.g. accelerator on a stick that
        # rests at -1).
        if not speed_ok and not steer_ok:
            raise RuntimeError(
                f'Both axis_speed={self.axis_speed} and axis_steer={self.axis_steer} '
                f'are out of range ({num_axes} axes on device). Refusing to operate.'
            )

        if not speed_ok:
            logger.error(
                f'axis_speed={self.axis_speed} out of range ({num_axes} axes) — clamped to 0'
            )
            self.axis_speed = 0
        if not steer_ok:
            logger.error(
                f'axis_steer={self.axis_steer} out of range ({num_axes} axes) — clamped to 0'
            )
            self.axis_steer = 0

        self._use_estop_btn = self.btn_estop < num_buttons
        if not self._use_estop_btn:
            logger.warning(f'btn_estop={self.btn_estop} out of range ({num_buttons} buttons) — disabled')

        # Reset freshness tracking on (re)connect.
        self._last_successful_poll_ts = monotonic()
        self._stale_zeroed = False

    def _apply_deadzone(self, value: float) -> float:
        if abs(value) < self.deadzone:
            return 0.0
        sign = 1 if value > 0 else -1
        return sign * (abs(value) - self.deadzone) / (1.0 - self.deadzone)

    def _toggle_estop(self):
        if self._client is None:
            return
        new_val = self.state.toggle_estop()
        self._client.send_estop(new_val, source='joystick')
        logger.info(f'Joystick e-stop → {new_val}')
