#!/usr/bin/env python3
"""Unified operator-side entry point: teleop + stream in one Qt process.

Hosts a single ``QApplication`` and a single window. The teleop side's
``MainWindow`` is reused as the shell (it already owns the topbar,
command bar, E-stop banner, and readiness indicator); the stream side's
per-camera ``VideoWidget`` grid and ``StatsPanel`` are embedded into its
content slot via :class:`stream_client.gui.subsystem.StreamSubsystem`.

Both clients open their own Zenoh sessions on the same router. Both
send-loops run as coroutines on a shared background asyncio loop.

The standalone entry points (``teleop_client/main.py`` and
``stream_client/main.py``) are preserved for solo runs.
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from teleop_client.client import TeleopClient
from teleop_client.config import load_config as load_teleop_config
from teleop_client.controller import create_controller
from teleop_client.gui.main_window import MainWindow as TeleopMainWindow
from teleop_client.send_loop import run_send_loop as run_teleop_send_loop
from teleop_client.state import StationState

from stream_client.client import StreamTcpClient
from stream_client.config import load_config as load_stream_config
from stream_client.gui.subsystem import StreamSubsystem
from stream_client.send_loop import run_send_loop as run_stream_send_loop


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('teleop_ui')


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='NEV unified teleop UI (control + telemetry + video)'
    )
    p.add_argument('--teleop-config', default='teleop_client/config.yaml',
                   help='Path to teleop_client config.yaml.')
    p.add_argument('--stream-config', default='stream_client/config.yaml',
                   help='Path to stream_client config.yaml.')
    p.add_argument('--server-tcp-locator', default=None,
                   help='Override server_tcp_locator for BOTH clients.')
    p.add_argument('--camera-id', default=None,
                   help='Override stream video.camera_id.')
    p.add_argument('--cameras', default=None,
                   help='Override stream cameras list, comma-separated. '
                        'Empty string = single-camera mode.')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='DEBUG-level logs.')
    return p


def main() -> None:
    args = _build_argparser().parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ---- Load both configs ----------------------------------------------
    teleop_cfg = load_teleop_config(args.teleop_config, {
        'server_tcp_locator': args.server_tcp_locator,
    })
    stream_cfg = load_stream_config(args.stream_config, {
        'server_tcp_locator': args.server_tcp_locator,
    })
    if args.camera_id is not None:
        stream_cfg['video']['camera_id'] = args.camera_id
    if args.cameras is not None:
        cams = [c.strip() for c in args.cameras.split(',') if c.strip()]
        stream_cfg['cameras'] = cams

    vehicle_id = teleop_cfg['vehicle_id']
    if stream_cfg.get('vehicle_id') and stream_cfg['vehicle_id'] != vehicle_id:
        logger.warning(
            'vehicle_id mismatch: teleop=%r stream=%r — using teleop value.',
            vehicle_id, stream_cfg['vehicle_id'],
        )
        stream_cfg['vehicle_id'] = vehicle_id

    teleop_locator = teleop_cfg.get('server_tcp_locator', '')
    stream_locator = stream_cfg['server_tcp_locator']
    stale_ms = int(stream_cfg['stale_threshold_ms'])

    # ---- Qt application FIRST -------------------------------------------
    # QApplication must exist before any QWidget is constructed. The stream
    # subsystem builds VideoWidget/StatsPanel in its __init__, so create the
    # app up front (everything else can come after).
    app = QApplication(sys.argv)

    # ---- Start both clients (each opens its own Zenoh session) ----------
    state = StationState()
    teleop_client = TeleopClient()
    teleop_client.start(teleop_locator, vehicle_id)

    stream_client = StreamTcpClient(stale_threshold_ms=stale_ms)
    stream_client.start(
        locator=stream_locator,
        vehicle_id=vehicle_id,
        camera_id=stream_cfg['video']['camera_id'],
        cameras=list(stream_cfg.get('cameras') or []),
    )

    # ---- Build the stream subsystem (video widgets + stats panel) -------
    # Constructed BEFORE the teleop MainWindow so we can hand its widgets
    # to the window constructor for embedding.
    stream_subsys = StreamSubsystem(stream_client, stream_cfg)

    # ---- Controller (joystick) + shared asyncio loop --------------------
    loop = asyncio.new_event_loop()
    teleop_stop = asyncio.Event()
    stream_stop = asyncio.Event()
    controller = create_controller(state, teleop_cfg)
    controller.setup(teleop_client, loop)

    done_event = threading.Event()

    async def _async_run():
        logger.info(
            'send-loops started -> teleop: %s, stream: %s',
            teleop_locator or 'auto-discovery',
            stream_locator or 'auto-discovery',
        )
        try:
            await asyncio.gather(
                run_teleop_send_loop(teleop_client, state, teleop_cfg,
                                     stop_event=teleop_stop),
                run_stream_send_loop(stream_client, stream_cfg,
                                     metrics_provider=stream_subsys,
                                     stop_event=stream_stop),
            )
        finally:
            done_event.set()

    send_thread = threading.Thread(
        target=loop.run_until_complete, args=(_async_run(),), daemon=True,
    )

    ctrl_thread = threading.Thread(target=controller.start, daemon=True)

    # ---- Build the unified window (uses widgets from stream_subsys) -----
    window = TeleopMainWindow(
        teleop_client.session, teleop_cfg,
        client=teleop_client, loop=loop,
        video_widgets=stream_subsys.video_widgets,
        stats_panel=stream_subsys.stats_panel,
        title='NEV Teleop UI',
    )

    stream_subsys.start()
    window.start()
    window.show()
    send_thread.start()
    ctrl_thread.start()

    # 1Hz timer to refresh the stream stats panel (the standalone stream
    # MainWindow used to do this; here the unified window owns the tick).
    stream_stats_timer = QTimer()
    stream_stats_timer.timeout.connect(lambda: stream_subsys.refresh_stats())
    stream_stats_timer.start(1000)

    # SIGINT → graceful Qt quit; 200 ms tick keeps Qt awake to receive it.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    sig_tick = QTimer()
    sig_tick.timeout.connect(lambda: None)
    sig_tick.start(200)

    logger.info('Unified teleop UI started')
    app.exec()

    # ---- Cleanup --------------------------------------------------------
    # Shutdown order mirrors the standalone teleop_client.main.main:
    #   stop UI side first → drain loops → close zenoh sessions → exit.
    logger.info('Shutting down...')
    stream_stats_timer.stop()
    window.stop()
    stream_subsys.stop()
    controller.stop()

    loop.call_soon_threadsafe(teleop_stop.set)
    loop.call_soon_threadsafe(stream_stop.set)
    send_thread.join(timeout=2.0)
    loop.call_soon_threadsafe(loop.stop)
    send_thread.join(timeout=2.0)
    done_event.wait(timeout=1.0)
    try:
        loop.close()
    except Exception as e:
        logger.warning(f'event loop close error: {e}')

    teleop_client.stop()
    stream_client.stop()
    ctrl_thread.join(timeout=1.0)
    logger.info('Shutdown complete')
    # Zenoh native worker threads can keep the interpreter alive past
    # daemon-thread teardown; force-exit so Ctrl+C lands in <1s.
    os._exit(0)


if __name__ == '__main__':
    main()
