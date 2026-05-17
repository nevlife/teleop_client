"""AU-based H.265 GStreamer pipeline builder.

Wire-spec §4/§5: each Zenoh sample's au_payload is one Access Unit, H.265
Annex-B, byte-stream-formatted. Pipeline shape:

    appsrc(do-timestamp=true, is-live=true, caps=h265 byte-stream/AU)
      -> h265parse alignment=au
      -> nvh265dec
      -> cudadownload
      -> videoconvert
      -> appsink(drop=true, max-buffers=1)

There is no RTP layer (no depayloader), no jitter buffer, no FEC, no
PLI/retransmit path — TCP is reliable + in-order so they are meaningless.
The appsink config is the second line of defense behind §8's stale-AU
drop: even if a stale buffer slipped past the wire-side filter,
downstream backpressure won't accumulate.

NVDEC is mandatory; missing nvh265dec or cudadownload -> RuntimeError.
"""
import logging

import gi

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

logger = logging.getLogger(__name__)

MIN_GSTREAMER_VERSION = (1, 24)
_LOGGED_VERSION = False

# h265parse advertises both forms; we hand-feed AU/byte-stream from the
# wire so the caps lock matches the actual payload.
APPSRC_CAPS = 'video/x-h265,stream-format=byte-stream,alignment=au'


def init_gstreamer() -> None:
    global _LOGGED_VERSION
    Gst.init(None)
    version = Gst.version()
    if version[:2] < MIN_GSTREAMER_VERSION:
        required = '.'.join(str(part) for part in MIN_GSTREAMER_VERSION)
        current = '.'.join(str(part) for part in version[:3])
        raise RuntimeError(f'GStreamer {required}+ is required, found {current}')
    if not _LOGGED_VERSION:
        logger.info('Using %s', Gst.version_string())
        _LOGGED_VERSION = True


def _has_element(name: str) -> bool:
    return Gst.ElementFactory.find(name) is not None


def _require_nvdec() -> None:
    missing = [n for n in ('nvh265dec', 'cudadownload') if not _has_element(n)]
    if missing:
        raise RuntimeError(
            'NVDEC required (no SW fallback). Missing GStreamer element(s): '
            + ', '.join(missing)
            + '. Install gstreamer1.0-plugins-bad with NVIDIA NVDEC support.'
        )


def create_au_pipeline(
    output_format: str = 'RGB',
    appsink_sync: bool = False,
    cam_id: str = '',
) -> tuple[Gst.Pipeline, Gst.Element, Gst.Element, str]:
    """Build the AU-fed H.265 decode pipeline. Returns:
        (pipeline, appsrc, appsink, description)

    `output_format` selects the post-videoconvert raw format. 'RGB' is what
    the Qt VideoWidget expects. The headless viewer wants 'BGR' for cv2 or
    can use the bypass pipeline below.

    `cam_id` is purely for log/element-name disambiguation when multiple
    pipelines coexist; the wire payload is identical.
    """
    init_gstreamer()
    _require_nvdec()

    src_name = f'src_{cam_id}' if cam_id else 'src'
    sink_name = f'sink_{cam_id}' if cam_id else 'sink'

    pipeline_str = (
        f'appsrc name={src_name} is-live=true do-timestamp=true format=time '
        f'caps="{APPSRC_CAPS}" ! '
        f'h265parse config-interval=-1 ! '
        f'nvh265dec ! '
        f'cudadownload ! '
        f'videoconvert ! '
        f'video/x-raw,format={output_format} ! '
        f'appsink name={sink_name} emit-signals=true '
        f'sync={"true" if appsink_sync else "false"} '
        f'max-buffers=1 drop=true'
    )
    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except GLib.Error as exc:
        raise RuntimeError(
            f'Failed to construct AU pipeline: {exc.message}\n  {pipeline_str}'
        )
    appsrc = pipeline.get_by_name(src_name)
    appsink = pipeline.get_by_name(sink_name)
    if appsrc is None or appsink is None:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError('Failed to resolve appsrc / appsink in AU pipeline')

    # 1 MB cushion (one IDR + a few P-AUs). block=False -> drop on
    # overflow instead of stalling the Zenoh callback thread.
    appsrc.set_property('max-bytes', 1024 * 1024)
    appsrc.set_property('block', False)

    description = 'NVDEC AU pipeline (nvh265dec + cudadownload)'
    if cam_id:
        description += f' [cam={cam_id}]'
    logger.info(
        'Built %s, output=%s, sync=%s', description, output_format, appsink_sync
    )
    return pipeline, appsrc, appsink, description


def create_au_autosink_pipeline() -> tuple[Gst.Pipeline, Gst.Element, str]:
    """Headless viewer variant: autovideosink instead of appsink.

    Used by viewer_h265_tcp.py when --gst-sink is selected so the decoded
    frames go straight to a GStreamer-managed window — no Qt or cv2
    needed. Returns (pipeline, appsrc, description).
    """
    init_gstreamer()
    _require_nvdec()

    pipeline_str = (
        f'appsrc name=src is-live=true do-timestamp=true format=time '
        f'caps="{APPSRC_CAPS}" ! '
        f'h265parse config-interval=-1 ! '
        f'nvh265dec ! '
        f'cudadownload ! '
        f'videoconvert ! '
        f'autovideosink sync=false'
    )
    try:
        pipeline = Gst.parse_launch(pipeline_str)
    except GLib.Error as exc:
        raise RuntimeError(
            f'Failed to construct AU autosink pipeline: {exc.message}\n  {pipeline_str}'
        )
    appsrc = pipeline.get_by_name('src')
    if appsrc is None:
        pipeline.set_state(Gst.State.NULL)
        raise RuntimeError('Failed to resolve appsrc in AU autosink pipeline')
    appsrc.set_property('max-bytes', 1024 * 1024)
    appsrc.set_property('block', False)
    description = 'NVDEC AU pipeline + autovideosink'
    logger.info('Built %s', description)
    return pipeline, appsrc, description


class PipelineBundle:
    """Wrapper around N independent AU pipelines keyed by cam_id.

    Each entry stores the pipeline, the appsrc (push-buffer target) and
    the appsink (decoded-sample source). Callers register a per-cam
    `on_new_sample` callback before `start()`.
    """

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def add(
        self,
        cam_id: str,
        output_format: str = 'RGB',
        appsink_sync: bool = False,
    ) -> tuple[Gst.Pipeline, Gst.Element, Gst.Element]:
        if cam_id in self._items:
            raise ValueError(f'cam {cam_id!r} already added to bundle')
        pipeline, appsrc, appsink, _ = create_au_pipeline(
            output_format=output_format,
            appsink_sync=appsink_sync,
            cam_id=cam_id,
        )
        self._items[cam_id] = dict(
            pipeline=pipeline, appsrc=appsrc, appsink=appsink,
        )
        return pipeline, appsrc, appsink

    def appsrc(self, cam_id: str) -> Gst.Element | None:
        item = self._items.get(cam_id)
        return None if item is None else item['appsrc']

    def cam_ids(self) -> list[str]:
        return list(self._items.keys())

    def start_all(self) -> None:
        for item in self._items.values():
            item['pipeline'].set_state(Gst.State.PLAYING)

    def stop_all(self) -> None:
        for item in self._items.values():
            try:
                item['pipeline'].set_state(Gst.State.NULL)
            except Exception:
                pass
        self._items.clear()
