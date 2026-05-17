#!/usr/bin/env python3
"""Headless (no-GUI) entry point for teleop_client.

For environments that do not need a GUI (e.g. a handheld controller box),
this only handles joystick -> vehicle publishing. main.py is the full
version that also includes the GUI.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import threading

# Make the vendored ``teleop_contracts`` submodule importable — same shim
# as main.py. The submodule lives at ./teleop_contracts in this repo.
_CONTRACTS_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "teleop_contracts")
)
if _CONTRACTS_ROOT not in sys.path:
    sys.path.insert(0, _CONTRACTS_ROOT)

from teleop_client.config import load_config
from teleop_client.state import StationState
from teleop_client.client import TeleopClient
from teleop_client.controller import create_controller
from teleop_client.send_loop import run_send_loop

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('controller')


def main():
    parser = argparse.ArgumentParser(description='NEV Controller (headless)')
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--server-tcp-locator', default=None)
    args = parser.parse_args()

    cfg = load_config(args.config, {'server_tcp_locator': args.server_tcp_locator})
    locator = cfg.get('server_tcp_locator', '')
    vehicle_id = cfg['vehicle_id']

    state = StationState()
    client = TeleopClient()
    client.start(locator, vehicle_id)

    loop = asyncio.new_event_loop()
    async_stop_event = asyncio.Event()
    controller = create_controller(state, cfg)
    controller.setup(client, loop)

    done_event = threading.Event()
    # C1: single async-safe event that signals shutdown intent. The signal
    # handler ONLY sets this event; it must not touch the controller or the
    # zenoh client from signal context.
    shutdown_event = threading.Event()

    async def async_run():
        logger.info(f'Controller started → server: {locator or "auto-discovery"}')
        try:
            await run_send_loop(client, state, cfg, stop_event=async_stop_event)
        finally:
            done_event.set()

    send_thread = threading.Thread(
        target=loop.run_until_complete, args=(async_run(),), daemon=True)
    send_thread.start()

    # C1: run the controller poll loop on a background thread so the main
    # thread can block on the shutdown event. This eliminates the race between
    # the SIGINT handler and the controller blocking inside pygame.
    ctrl_thread = threading.Thread(target=controller.start, daemon=True)
    ctrl_thread.start()

    def shutdown(*_):
        # Signal-safe: only set the event. No calls into controller/client.
        shutdown_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        # Wait for a signal (set by the handler above). Using a poll wait lets
        # KeyboardInterrupt propagate on platforms where the handler does not
        # interrupt the wait.
        while not shutdown_event.wait(timeout=1.0):
            pass
    except KeyboardInterrupt:
        shutdown_event.set()

    logger.info('Shutting down...')
    # Stop controller poll loop first so it stops writing to state.
    controller.stop()
    # Then drain the asyncio send loop in the same ordered pattern as main.py
    # (see C4) — signal stop, join the thread, stop the loop, join again.
    loop.call_soon_threadsafe(async_stop_event.set)
    send_thread.join(timeout=2.0)
    loop.call_soon_threadsafe(loop.stop)
    send_thread.join(timeout=2.0)
    done_event.wait(timeout=1.0)
    try:
        loop.close()
    except Exception as e:
        logger.warning(f'event loop close error: {e}')
    # Now safe to close the zenoh session (no more publishes in flight).
    client.stop()
    # Best-effort join the controller thread (daemon, so we don't block forever).
    ctrl_thread.join(timeout=1.0)
    logger.info('Shutdown complete')
    # zenoh's native worker threads can keep the interpreter alive even after
    # daemon threads are released; force exit so Ctrl+C terminates in <1s.
    os._exit(0)


if __name__ == '__main__':
    main()
