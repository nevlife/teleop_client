#pragma once

#include <string>
#include <vector>

namespace teleop_client_v2
{

struct CodecSupport
{
  std::string name;
  bool decode_available;
  std::string selected_factory;
};

std::vector<CodecSupport> probe_video_decoders();

}  // namespace teleop_client_v2
