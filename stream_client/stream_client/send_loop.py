"""Telemetry coroutine: stream_heartbeat (5 Hz) + video_feedback (1 Hz).

Spec §6.b / §6.c. Both feedback values are sourced from a per-camera
metrics provider — the loop is just the periodic publisher; the widget
owns the measurement window.

The provider object must expose one of two shapes:

    (a) Per-camera (preferred for multi-cam):

        .cam_ids() -> list[str]
        .latency_p95_ms_last_1s(cam_id) -> float
        .freeze_ms_last_1s(cam_id) -> float

    (b) Legacy single-cam fallback:

        .latency_p95_ms_last_1s() -> float
        .freeze_ms_last_1s() -> float

Pass None for `metrics_provider` to publish zeros (useful for the
headless viewer if it doesn't track freeze).
"""
import asyncio
import inspect
import logging
import time
from typing import Protocol

logger = logging.getLogger(__name__)


class MetricsProvider(Protocol):
    def latency_p95_ms_last_1s(self) -> float: ...
    def freeze_ms_last_1s(self) -> float: ...


def _provider_supports_per_cam(provider) -> bool:
    if provider is None:
        return False
    if not hasattr(provider, 'cam_ids'):
        return False
    fn = getattr(provider, 'latency_p95_ms_last_1s', None)
    if fn is None:
        return False
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    # Accepts 1 explicit positional arg (cam_id) — bound method excludes self.
    return any(
        p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
        for p in sig.parameters.values()
    )


async def run_send_loop(
    client,
    cfg: dict,
    *,
    metrics_provider=None,
    stop_event: asyncio.Event | None = None,
) -> None:
    hb_interval = 1.0 / cfg.get('stream_heartbeat_rate', 5.0)
    fb_interval = 1.0  # spec §6.b: 1 Hz

    last_hb = 0.0
    last_fb = 0.0

    if stop_event is None:
        stop_event = asyncio.Event()

    per_cam = _provider_supports_per_cam(metrics_provider)

    try:
        while not stop_event.is_set():
            now = time.monotonic()

            if now - last_hb >= hb_interval:
                try:
                    client.send_stream_heartbeat()
                except Exception as e:
                    logger.debug('heartbeat publish error: %s', e)
                last_hb = now

            if now - last_fb >= fb_interval:
                if per_cam:
                    try:
                        cams = list(metrics_provider.cam_ids())
                    except Exception:
                        cams = []
                    for cam in cams:
                        latency_p95 = 0.0
                        freeze_ms = 0.0
                        try:
                            latency_p95 = float(
                                metrics_provider.latency_p95_ms_last_1s(cam)
                            )
                        except Exception:
                            latency_p95 = 0.0
                        try:
                            freeze_ms = float(
                                metrics_provider.freeze_ms_last_1s(cam)
                            )
                        except Exception:
                            freeze_ms = 0.0
                        try:
                            client.send_video_feedback(
                                latency_p95, freeze_ms, camera_id=cam,
                            )
                        except Exception as e:
                            logger.debug(
                                'video_feedback publish error cam=%s: %s', cam, e,
                            )
                else:
                    latency_p95 = 0.0
                    freeze_ms = 0.0
                    if metrics_provider is not None:
                        try:
                            latency_p95 = float(
                                metrics_provider.latency_p95_ms_last_1s()
                            )
                        except Exception:
                            latency_p95 = 0.0
                        try:
                            freeze_ms = float(metrics_provider.freeze_ms_last_1s())
                        except Exception:
                            freeze_ms = 0.0
                    try:
                        client.send_video_feedback(latency_p95, freeze_ms)
                    except Exception as e:
                        logger.debug('video_feedback publish error: %s', e)
                last_fb = now

            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        pass
