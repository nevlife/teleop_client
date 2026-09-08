# teleop_client

Operator-side native client. Qt 6 windows, one per attached display, receiving
video over WebRTC from [`teleop_rover`](https://github.com/nevlife/teleop_rover)
and sending control over a WebRTC data channel. Pairing is brokered by
[`teleop_server`](https://github.com/nevlife/teleop_server).

This is not a web client. Zenoh is not a dependency and is not part of the
client runtime.

## Build

Ubuntu 24.04:

```bash
sudo apt install cmake ninja-build pkg-config \
  qt6-base-dev qt6-websockets-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  libgstreamer-plugins-bad1.0-dev \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav
```

`libgstreamer-plugins-bad1.0-dev` supplies the `gstreamer-webrtc-1.0`
pkg-config module, without which the CMake configure step fails.

```bash
git clone --recurse-submodules https://github.com/nevlife/teleop_client.git
cd teleop_client
cmake -S native_v2 -B build/native_v2 -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build/native_v2
```

## Test

```bash
ctest --test-dir build/native_v2 --output-on-failure
```

Tests need `libgtest-dev`; without it the test target is skipped and the client
still builds.

## Run

```bash
./build/native_v2/teleop-client-v2 --server ws://SERVER:13437/ws --robot rover-01
```

| Option | Default | Meaning |
|---|---|---|
| `--server` | `ws://127.0.0.1:13437/ws` | Signaling WebSocket URL |
| `--robot` | `rover-01` | Robot identifier to pair with |
| `--fullscreen` | off | One fullscreen window per monitor |

## Keyboard control

The window must have focus for any command to be produced.

| Key | Action |
|---|---|
| `Space` | Deadman — commands are produced **only while it is held** |
| `W` / `S` | Forward / reverse |
| `A` / `D` | Yaw left / right |

A key is binary, unlike an analog stick, so the target implied by the held keys
is approached at the configured acceleration limits rather than stepped to
directly. Deceleration is deliberately faster than acceleration.

Motion ramps to a stop whenever:

- the deadman is released;
- the window loses focus (every held key is dropped, because Qt delivers no
  key-up for keys that were down at that moment);
- the window regains focus (keys held at that moment are not inherited, so an
  operator cannot drive by alt-tabbing in with the deadman pressed).

Bound keys are consumed by the application event filter, so `Space` never
activates whichever button holds focus.

The limits live in `KeyboardConfig` (`native_v2/include/teleop_client_v2/keyboard_input.hpp`).
E-stop is not bound to the keyboard.

## Layout

```
teleop_client/
├── native_v2/
│   ├── include/teleop_client_v2/
│   │   ├── codec_probe.hpp
│   │   ├── keyboard_input.hpp     # Qt-free input core
│   │   └── signaling_client.hpp
│   ├── src/
│   │   ├── codec_probe.cpp        # runtime H.264 / VP9 / AV1 decoder probe
│   │   ├── keyboard_input.cpp
│   │   ├── main.cpp               # windows, Qt key bridge, input tick
│   │   └── signaling_client.cpp   # WebSocket signaling + SDP/ICE hooks
│   └── test/keyboard_input_test.cpp
└── teleop_contracts/              # git submodule: v2 wire contract
```

## Status

Implemented: per-display Qt windows, decoder probing, the signaling handshake
with strict single-client pairing, SDP/ICE hooks, and the keyboard input core.

Not yet implemented: the `webrtcbin` pipeline that renders the video track, and
the data channels that carry `MotionCommand` and telemetry. The motion command
is currently displayed in the window rather than transmitted — the placeholder
does not pretend the media or control path is ready.
