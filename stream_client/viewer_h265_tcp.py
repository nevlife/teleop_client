#!/usr/bin/env python3
"""Headless H.265 AU viewer.

Single-window debugging entry point. Reuses StreamTcpClient (same Zenoh
wiring + 28B header parse + stale-AU drop) and only swaps the sink:

  --gst-sink   (default) NVDEC + autovideosink — no Python frame copy
  --cv-sink              NVDEC -> appsink BGR -> cv2.imshow

Use --gst-sink when you just want to see the picture with minimum latency.
Use --cv-sink when you need a frame-buffer hook (e.g. snapshot to disk).

Examples:

    python3 viewer_h265_tcp.py --config config.yaml
    python3 viewer_h265_tcp.py --server-tcp-locator tcp/192.168.0.10:7457
    python3 viewer_h265_tcp.py --camera-id front --cv-sink

Always pass --vehicle-id when not using a config file.
"""
import argparse
import logging
import signal
import sys
import threading
import time

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from stream_client.client import StreamTcpClient
from stream_client.config import load_config
from stream_client.gstreamer_tcp import (
    create_au_autosink_pipeline,
    create_au_pipeline,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('viewer_h265_tcp')


def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='NEV H.265 TCP/AU viewer')
    p.add_argument('--config', default='config.yaml')
    p.add_argument('--server-tcp-locator', default=None)
    p.add_argument('--vehicle-id', default=None)
    p.add_argument('--camera-id', default=None)
    p.add_argument('--stale-threshold-ms', type=int, default=None)
    sink = p.add_mutually_exclusive_group()
    sink.add_argument('--gst-sink', dest='sink', action='store_const',
                      const='gst', help='autovideosink (default)')
    sink.add_argument('--cv-sink', dest='sink', action='store_const',
                      const='cv', help='OpenCV cv2.imshow')
    p.set_defaults(sink='gst')
    return p.parse_args()


def _run_gst_sink(client: StreamTcpClient) -> None:
    """autovideosink path: appsrc -> ... -> autovideosink, GLib main loop."""
    pipeline, appsrc, desc = create_au_autosink_pipeline()
    logger.info('Using %s', desc)

    def on_au(cam_id, au, vehicle_ts, encode_ms, flags, server_rx_ts,
              veh_to_srv_ms, stale_ms):
        buf = Gst.Buffer.new_wrapped(au)
        try:
            appsrc.emit('push-buffer', buf)
        except Exception as e:
            logger.warning('push-buffer: %s', e)

    client.set_au_callback(on_au)
    pipeline.set_state(Gst.State.PLAYING)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()

    def _on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            logger.error('GStreamer error: %s', err.message)
            loop.quit()
        return True

    bus.add_signal_watch()
    bus.connect('message', _on_msg)
    signal.signal(signal.SIGINT, lambda *_: loop.quit())

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        client.set_au_callback(None)


def _run_cv_sink(client: StreamTcpClient) -> None:
    """cv2 path: appsrc -> ... -> appsink BGR -> imshow."""
    try:
        import cv2  # local import so gst-sink users don't need cv2
        import numpy as np
    except ImportError as e:
        raise SystemExit(
            f'--cv-sink requires opencv-python + numpy: {e}'
        )

    pipeline, appsrc, appsink, desc = create_au_pipeline(output_format='BGR')
    logger.info('Using %s (cv2.imshow)', desc)

    latest: dict = {'frame': None, 'ts': 0.0}
    latest_lock = threading.Lock()

    def on_new_sample(sink):
        sample = sink.emit('pull-sample')
        if not isinstance(sample, Gst.Sample):
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        s = caps.get_structure(0)
        w = s.get_value('width')
        h = s.get_value('height')
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = np.frombuffer(mi.data, dtype=np.uint8).reshape(h, w, 3).copy()
        finally:
            buf.unmap(mi)
        with latest_lock:
            latest['frame'] = data
            latest['ts'] = time.time()
        return Gst.FlowReturn.OK

    appsink.set_property('emit-signals', True)
    appsink.connect('new-sample', on_new_sample)

    def on_au(cam_id, au, vehicle_ts, encode_ms, flags, server_rx_ts,
              veh_to_srv_ms, stale_ms):
        buf = Gst.Buffer.new_wrapped(au)
        try:
            appsrc.emit('push-buffer', buf)
        except Exception as e:
            logger.warning('push-buffer: %s', e)

    client.set_au_callback(on_au)
    pipeline.set_state(Gst.State.PLAYING)

    win = 'NEV stream_client viewer'
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    stop = {'flag': False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    try:
        while not stop['flag']:
            with latest_lock:
                frame = latest['frame']
            if frame is not None:
                cv2.imshow(win, frame)
            key = cv2.waitKey(10) & 0xFF
            if key in (ord('q'), 27):  # q / ESC
                break
    finally:
        cv2.destroyAllWindows()
        pipeline.set_state(Gst.State.NULL)
        client.set_au_callback(None)


def main() -> None:
    args = _build_args()

    overrides = {}
    if args.server_tcp_locator is not None:
        overrides['server_tcp_locator'] = args.server_tcp_locator
    if args.vehicle_id is not None:
        overrides['vehicle_id'] = args.vehicle_id
    if args.stale_threshold_ms is not None:
        overrides['stale_threshold_ms'] = args.stale_threshold_ms

    try:
        cfg = load_config(args.config, overrides)
    except FileNotFoundError:
        cfg = load_config('/dev/null', overrides)

    if args.camera_id is not None:
        cfg['video']['camera_id'] = args.camera_id

    Gst.init(None)

    client = StreamTcpClient(stale_threshold_ms=int(cfg['stale_threshold_ms']))
    # Headless viewer only renders one camera; pick the first from
    # cameras=[...] if present, else fall back to video.camera_id.
    cameras_cfg = list(cfg.get('cameras') or [])
    viewer_cam = cameras_cfg[0] if cameras_cfg else cfg['video']['camera_id']
    client.start(
        locator=cfg['server_tcp_locator'],
        vehicle_id=cfg['vehicle_id'],
        camera_id=viewer_cam,
    )

    try:
        if args.sink == 'cv':
            _run_cv_sink(client)
        else:
            _run_gst_sink(client)
    finally:
        client.stop()
        logger.info('viewer exit')
        sys.exit(0)


if __name__ == '__main__':
    main()
