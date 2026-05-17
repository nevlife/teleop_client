"""Background send loop for teleop_client.

Handles the three periodic signals (heartbeat / teleop / ping) plus auxiliary
work (estop retry, RTT expiry).
"""
import asyncio
import time

from .client import TeleopClient
from .state import StationState


async def run_send_loop(
    client: TeleopClient,
    state: StationState,
    cfg: dict,
    stop_event: asyncio.Event | None = None,
):
    hb_interval   = 1.0 / cfg.get('heartbeat_rate', 5.0)
    tc_interval   = 1.0 / cfg.get('teleop_rate',    20.0)
    ping_interval = 1.0 / cfg.get('ping_rate', 1.0)

    last_hb   = 0.0
    last_tc   = 0.0
    last_ping = 0.0

    if stop_event is None:
        stop_event = asyncio.Event()

    try:
        while not stop_event.is_set():
            now = time.monotonic()

            if now - last_hb >= hb_interval:
                client.send_client_heartbeat()
                last_hb = now

            if now - last_tc >= tc_interval:
                linear_x, steer_angle = state.get_control()
                client.send_teleop(linear_x, steer_angle)
                last_tc = now

            if now - last_ping >= ping_interval:
                client.send_ping()
                last_ping = now

            # C2: keep retrying a pending E-stop publish each tick until cleared.
            client.retry_pending_estop()

            # H5: expire RTT from the send-loop tick (headless mode has no QTimer).
            client.expire_rtt_if_stale()

            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        pass
