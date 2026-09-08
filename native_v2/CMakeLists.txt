cmake_minimum_required(VERSION 3.20)
project(teleop_client_v2 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 6.4 REQUIRED COMPONENTS Widgets WebSockets)
find_package(PkgConfig REQUIRED)
find_package(Protobuf REQUIRED)

# The v2 wire contract lives in the teleop_contracts submodule; the C++
# classes are generated at build time rather than checked in, so the schema
# has exactly one source of truth.
set(TELEOP_PROTO_ROOT ${CMAKE_CURRENT_SOURCE_DIR}/../teleop_contracts/teleop_contracts/schema)
set(TELEOP_PROTO_DIR ${CMAKE_CURRENT_BINARY_DIR}/proto)
file(MAKE_DIRECTORY ${TELEOP_PROTO_DIR})
add_custom_command(
  OUTPUT ${TELEOP_PROTO_DIR}/teleop/v2/control.pb.cc
         ${TELEOP_PROTO_DIR}/teleop/v2/control.pb.h
  COMMAND protobuf::protoc
  ARGS --cpp_out=${TELEOP_PROTO_DIR} -I${TELEOP_PROTO_ROOT}
       ${TELEOP_PROTO_ROOT}/teleop/v2/control.proto
  DEPENDS ${TELEOP_PROTO_ROOT}/teleop/v2/control.proto protobuf::protoc
  COMMENT "Generating C++ for teleop/v2/control.proto"
  VERBATIM)
add_library(teleop_control_proto ${TELEOP_PROTO_DIR}/teleop/v2/control.pb.cc)
target_include_directories(teleop_control_proto PUBLIC ${TELEOP_PROTO_DIR})
target_link_libraries(teleop_control_proto PUBLIC protobuf::libprotobuf)
# Generated code is not ours to lint.
target_compile_options(teleop_control_proto PRIVATE -w)
pkg_check_modules(GSTREAMER REQUIRED IMPORTED_TARGET
  gstreamer-1.0>=1.24
  gstreamer-webrtc-1.0>=1.24
  gstreamer-sdp-1.0>=1.24
  gstreamer-app-1.0>=1.24
  gstreamer-video-1.0>=1.24)

# The input core is deliberately free of Qt and GStreamer so the motion
# limits can be unit-tested without a display or a media stack.
add_library(teleop_keyboard_input src/keyboard_input.cpp)
target_include_directories(teleop_keyboard_input PUBLIC include)
target_compile_options(teleop_keyboard_input PRIVATE -Wall -Wextra -Wpedantic)

# signaling_client.hpp must be listed: AUTOMOC only scans the files given to
# the target, and the Q_OBJECT class lives in include/ rather than next to its
# source, so without this the moc output is never generated and the link fails
# on the vtable and the signals.
add_executable(teleop-client-v2
  src/main.cpp
  src/codec_probe.cpp
  src/signaling_client.cpp
  src/video_view.cpp
  src/webrtc_session.cpp
  include/teleop_client_v2/signaling_client.hpp
  include/teleop_client_v2/video_view.hpp
  include/teleop_client_v2/webrtc_session.hpp)
target_include_directories(teleop-client-v2 PRIVATE include)
target_link_libraries(teleop-client-v2 PRIVATE
  teleop_keyboard_input
  teleop_control_proto
  Qt6::Widgets
  Qt6::WebSockets
  PkgConfig::GSTREAMER)
target_compile_options(teleop-client-v2 PRIVATE -Wall -Wextra -Wpedantic)

install(TARGETS teleop-client-v2 RUNTIME DESTINATION bin)

option(BUILD_TESTING "Build the unit tests" ON)
if(BUILD_TESTING)
  find_package(GTest)
  if(GTest_FOUND)
    enable_testing()
    add_executable(keyboard_input_test test/keyboard_input_test.cpp)
    target_link_libraries(keyboard_input_test PRIVATE
      teleop_keyboard_input GTest::gtest_main)
    target_compile_options(keyboard_input_test PRIVATE -Wall -Wextra -Wpedantic)
    add_test(NAME keyboard_input_test COMMAND keyboard_input_test)
  else()
    message(STATUS "GTest not found - unit tests skipped")
  endif()
endif()
