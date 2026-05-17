"""Config loader for teleop_client.

Validates only the control/telemetry-side keys (video-side validation lives in
stream_client/config.py). Video-only toggles such as transport_mode and
stale_threshold_ms are not handled here.
"""
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# M6: known top-level keys for teleop_client. Unknown keys are warned about
# (typo guard) but not rejected — forward-compat with future additions.
_KNOWN_TOP_LEVEL_KEYS = {
    'server_tcp_locator',
    'vehicle_id',
    'heartbeat_rate',
    'teleop_rate',
    'ping_rate',
    'controller_type',
    'joystick',
}

_KNOWN_JOYSTICK_KEYS = {
    'axis_speed', 'axis_steer', 'btn_estop',
    'max_speed', 'max_steer_deg', 'deadzone',
    'invert_speed', 'estop_on_disconnect',
}


def _validate_config(cfg: dict) -> None:
    for key in ('heartbeat_rate', 'teleop_rate'):
        val = cfg.get(key)
        if val is not None:
            if not isinstance(val, (int, float)) or val <= 0:
                raise ValueError(f'{key} must be a positive number, got {val!r}')

    vid = cfg.get('vehicle_id')
    if vid is None:
        raise ValueError('vehicle_id is required (set it in config.yaml)')
    cfg['vehicle_id'] = str(vid)
    if '/' in cfg['vehicle_id'] or not cfg['vehicle_id']:
        raise ValueError(f'vehicle_id must be non-empty and contain no "/", got {vid!r}')

    joy = cfg.get('joystick', {})
    if isinstance(joy, dict):
        for key in ('max_speed', 'max_steer_deg', 'deadzone'):
            val = joy.get(key)
            if val is not None:
                if not isinstance(val, (int, float)) or val < 0:
                    raise ValueError(f'joystick.{key} must be a non-negative number, got {val!r}')

        for key in ('axis_speed', 'axis_steer', 'btn_estop'):
            val = joy.get(key)
            if val is not None:
                if not isinstance(val, int) or val < 0:
                    raise ValueError(f'joystick.{key} must be a non-negative integer, got {val!r}')

        unknown_joy = set(joy.keys()) - _KNOWN_JOYSTICK_KEYS
        if unknown_joy:
            logger.warning(
                f'Unknown joystick config keys: {sorted(unknown_joy)} '
                f'(known: {sorted(_KNOWN_JOYSTICK_KEYS)})'
            )

    unknown = set(cfg.keys()) - _KNOWN_TOP_LEVEL_KEYS
    if unknown:
        logger.warning(
            f'Unknown teleop_client config keys: {sorted(unknown)} '
            f'(known: {sorted(_KNOWN_TOP_LEVEL_KEYS)})'
        )


def load_config(path: str, overrides: dict) -> dict:
    cfg = {}
    p = Path(path)
    if p.exists():
        cfg = yaml.safe_load(p.read_text()) or {}
    else:
        logger.warning(f'Config file not found: {path}')
    # NOTE: an empty-string override value here is treated as "use the value
    # already in cfg" (see L1 in main.py). Only non-None overrides apply.
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    _validate_config(cfg)
    return cfg
