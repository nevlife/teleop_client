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
  include/teleop_client_v2/signaling_client.hpp)
target_include_directories(teleop-client-v2 PRIVATE include)
target_link_libraries(teleop-client-v2 PRIVATE
  teleop_keyboard_input
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
