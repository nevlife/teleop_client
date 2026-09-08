#include "teleop_client_v2/codec_probe.hpp"

#include <gst/gst.h>

#include <initializer_list>

namespace teleop_client_v2
{

namespace
{
CodecSupport probe(const char * name, std::initializer_list<const char *> factories)
{
  for (const char * factory_name : factories) {
    GstElementFactory * factory = gst_element_factory_find(factory_name);
    if (factory != nullptr) {
      gst_object_unref(factory);
      return CodecSupport{name, true, factory_name};
    }
  }
  return CodecSupport{name, false, {}};
}
}

std::vector<CodecSupport> probe_video_decoders()
{
  // Ordered to prefer zero-copy/hardware paths before software fallbacks.
  return {
    probe("AV1", {"nvav1dec", "vaav1dec", "dav1ddec", "av1dec"}),
    probe("VP9", {"nvvp9dec", "vavp9dec", "vp9dec", "avdec_vp9"}),
    probe("H264", {"nvh264dec", "vah264dec", "v4l2h264dec", "avdec_h264"}),
  };
}

}  // namespace teleop_client_v2
