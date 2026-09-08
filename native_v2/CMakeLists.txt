cmake_minimum_required(VERSION 3.20)
project(teleop_client_v2 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Widgets WebSockets)
find_package(PkgConfig REQUIRED)
pkg_check_modules(GSTREAMER REQUIRED IMPORTED_TARGET
  gstreamer-1.0>=1.24
  gstreamer-webrtc-1.0>=1.24
  gstreamer-sdp-1.0>=1.24)

# signaling_client.hpp must be listed: AUTOMOC only scans the files given to
# the target, and the Q_OBJECT class lives in include/ rather than next to its
# source, so without this the moc output is never generated and the link fails
# on the vtable and the signals.
add_executable(teleop-client-v2
  src/main.cpp
  src/codec_probe.cpp
  src/signaling_client.cpp
  include/teleop_client_v2/signaling_client.hpp)
target_include_directories(teleop-client-v2 PRIVATE include)
target_link_libraries(teleop-client-v2 PRIVATE
  Qt6::Widgets
  Qt6::WebSockets
  PkgConfig::GSTREAMER)
target_compile_options(teleop-client-v2 PRIVATE -Wall -Wextra -Wpedantic)

install(TARGETS teleop-client-v2 RUNTIME DESTINATION bin)
