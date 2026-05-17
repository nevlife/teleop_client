"""Config loader for stream_client.

Validates the keys this process actually reads. The TCP-only client has a
much smaller surface than the original stream_client: no FEC / RTX / PLI /
jitterbuffer / UDP locator knobs.

Spec: /home/nev/teleop/TCP_WIRE_SPEC.md
"""
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _validate_config(cfg: dict) -> None:
    vid = cfg.get('vehicle_id')
    if vid is None:
        raise ValueError('vehicle_id is required (set it in config.yaml)')
    cfg['vehicle_id'] = str(vid)
    if '/' in cfg['vehicle_id'] or not cfg['vehicle_id']:
        raise ValueError(
            f'vehicle_id must be non-empty and contain no "/", got {vid!r}'
        )

    locator = cfg.get('server_tcp_locator', '')
    if locator is None:
        locator = ''
    if not isinstance(locator, str):
        raise ValueError(
            f'server_tcp_locator must be a string, got {locator!r}'
        )
    cfg['server_tcp_locator'] = locator

    sh = cfg.get('stream_heartbeat_rate', 5.0)
    if not isinstance(sh, (int, float)) or isinstance(sh, bool) or sh <= 0:
        raise ValueError(
            f'stream_heartbeat_rate must be a positive number, got {sh!r}'
        )
    cfg['stream_heartbeat_rate'] = float(sh)

    stale_ms = cfg.get('stale_threshold_ms', 150)
    if (
        not isinstance(stale_ms, (int, float))
        or isinstance(stale_ms, bool)
        or stale_ms < 0
    ):
        raise ValueError(
            f'stale_threshold_ms must be a non-negative number, got {stale_ms!r}'
        )
    cfg['stale_threshold_ms'] = int(stale_ms)

    fps = cfg.get('fps_expected', 30.0)
    if not isinstance(fps, (int, float)) or isinstance(fps, bool) or fps <= 0:
        raise ValueError(
            f'fps_expected must be a positive number, got {fps!r}'
        )
    cfg['fps_expected'] = float(fps)

    video = cfg.get('video', {})
    if not isinstance(video, dict):
        raise ValueError(f'video must be a mapping, got {video!r}')
    cam_id = video.get('camera_id', '')
    if cam_id is None:
        cam_id = ''
    if not isinstance(cam_id, str):
        raise ValueError(
            f'video.camera_id must be a string (use "" for single camera), '
            f'got {cam_id!r}'
        )
    if '/' in cam_id:
        raise ValueError(
            f'video.camera_id must not contain "/", got {cam_id!r}'
        )
    video['camera_id'] = cam_id

    decoder_pref = video.get('decoder_preference')
    if decoder_pref is not None:
        if not isinstance(decoder_pref, list) or not all(
            isinstance(x, str) and x for x in decoder_pref
        ):
            raise ValueError(
                f'video.decoder_preference must be a list of non-empty strings, '
                f'got {decoder_pref!r}'
            )
    cfg['video'] = video

    # Multi-camera list. Top-level `cameras: [...]` takes precedence over
    # legacy `video.camera_id`. Empty list -> single-camera mode (legacy).
    cameras = cfg.get('cameras', [])
    if cameras is None:
        cameras = []
    if not isinstance(cameras, list):
        raise ValueError(
            f'cameras must be a list of strings, got {cameras!r}'
        )
    norm: list[str] = []
    for c in cameras:
        if not isinstance(c, str) or not c:
            raise ValueError(
                f'cameras entries must be non-empty strings, got {c!r}'
            )
        if '/' in c:
            raise ValueError(
                f'cameras entry must not contain "/", got {c!r}'
            )
        norm.append(c)
    cfg['cameras'] = norm

    gui = cfg.get('gui', {})
    if not isinstance(gui, dict):
        raise ValueError(f'gui must be a mapping, got {gui!r}')
    for key, default in (
        ('window_min_width', 1280),
        ('window_min_height', 720),
        ('panel_width', 340),
    ):
        v = gui.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ValueError(f'gui.{key} must be a positive int, got {v!r}')
        gui[key] = v
    ff = gui.get('font_family', "Consolas, 'Courier New', monospace")
    if not isinstance(ff, str) or not ff:
        raise ValueError(f'gui.font_family must be a non-empty string, got {ff!r}')
    gui['font_family'] = ff
    cfg['gui'] = gui


def load_config(path: str, overrides: dict | None = None) -> dict:
    cfg: dict = {}
    p = Path(path)
    if p.exists():
        cfg = yaml.safe_load(p.read_text()) or {}
    else:
        logger.warning(f'Config file not found: {path}')
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                cfg[k] = v
    _validate_config(cfg)
    return cfg
