# stream_client

NEV teleop video client — **TCP-only, AU-framed** H.265 path.

Wire contract: [`/home/nev/teleop/TCP_WIRE_SPEC.md`](../TCP_WIRE_SPEC.md).
This package shares only that spec with `stream_bot` / `stream_server`;
no Python imports cross those package boundaries.

Difference vs the original `stream_client`: **AU framed (one Zenoh PUT =
one frame), no RTX / PLI / FEC, stale-AU drop at the wire**. The pipeline
is a flat `appsrc -> h265parse -> nvh265dec -> cudadownload -> videoconvert
-> appsink` — no `rtph265depay`, no `rtpjitterbuffer`, no `rtpulpfecdec`,
no UDP locator.

## Structure

```
main.py                         GUI entry point (PySide6 + send_loop)
viewer_h265_tcp.py              headless single-window viewer (autovideosink / cv2)
config.yaml                     TCP-only config
stream_client/
├── client.py                   StreamTcpClient (Zenoh, nev/stream_tcp/{vid}/...)
├── config.py                   config validation
├── gstreamer_tcp.py            AU pipeline builder (NVDEC only)
├── send_loop.py                stream_heartbeat 5 Hz + video_feedback 1 Hz
└── gui/
    ├── main_window.py          video + stats window
    ├── video_widget.py         appsrc + decode + Qt paint, metrics provider
    └── stats_panel.py          PIPELINE / LATENCY / FREEZE
test/
└── test_au_parsing.py          28B header round-trip + flags.is_idr + static checks
```

## Zenoh topics (`nev/stream_tcp/{vehicle_id}/...`)

| pub/sub | suffix              | rate / type                 | source       |
|---------|---------------------|-----------------------------|--------------|
| sub     | `camera[/{cam_id}]` | AU + 28B relay header (§5)  | server -> us |
| pub     | `stream_heartbeat`  | 5 Hz, `{"ts":...}` (§6.c)   | us -> server |
| pub     | `video_feedback`    | 1 Hz, p95+freeze (§6.b)     | us -> server |

All publishers run **RELIABLE + BLOCK** per spec §3.

## Stale-AU drop (spec §8)

For every received sample we compute `stale_ms = (now - vehicle_ts) * 1000`.
If `stale_ms > stale_threshold_ms` (default 150) **and** `flags.is_idr == 0`,
the AU is dropped without touching the decoder. IDR AUs are always pushed
(state recovery is worth more than freshness). The 28B header sanity
bounds from spec §13 are also enforced before drop logic runs.

## Run

GUI (PySide6 stats panel + on-screen video):

```bash
python3 main.py --config config.yaml
# or override locator
python3 main.py --server-tcp-locator tcp/192.168.0.10:7457
# multi-camera
python3 main.py --camera-id front
```

Headless single-window viewer (no PySide6 needed):

```bash
# autovideosink (lowest copy overhead, default)
python3 viewer_h265_tcp.py --config config.yaml

# OpenCV imshow (requires `pip install opencv-python numpy`)
python3 viewer_h265_tcp.py --config config.yaml --cv-sink
```

## Tests

From the project root:

```bash
python3 -m unittest test.test_au_parsing -v
```

The test module also performs static checks: any reintroduction of
`rtph265depay`, `rtpjitterbuffer`, `rtpulpfecdec`, `rtx_request`, or the
legacy `nev/stream/` (non-TCP) topic prefix will fail the build.

## Dependencies

- `eclipse-zenoh`, `PyYAML`, `PySide6`, `PyGObject`
- GStreamer 1.24+ with `nvh265dec`, `cudadownload`, `h265parse`,
  `appsrc`, `appsink`, `videoconvert`, optionally `autovideosink`
- NVDEC required — no SW fallback. Missing `nvh265dec` /
  `cudadownload` raises `RuntimeError` at startup.
