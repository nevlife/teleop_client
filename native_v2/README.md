# Native teleop client v2

This is the clean native replacement for the old Python/Zenoh UI. It is not a
web client.

The current milestone provides:

- Qt 6 native windows, one per attached display
- runtime H.264, VP9 and AV1 decoder probing
- WebSocket signaling handshake and strict single-client pairing
- SDP/ICE signal hooks ready for the GStreamer `webrtcbin` pipeline

Build requirements are Qt 6 Widgets/WebSockets and GStreamer 1.24 development
packages including `gstreamer-webrtc-1.0`.

On Ubuntu 24.04 the development dependencies are installed with:

```bash
sudo apt install qt6-base-dev qt6-websockets-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
  libgstreamer-plugins-bad1.0-dev
```

```bash
cmake -S native_v2 -B build/native_v2
cmake --build build/native_v2 -j
./build/native_v2/teleop-client-v2 --server ws://SERVER:13437/ws --fullscreen
```

Actual video track rendering and the two data channels are the next milestone;
the placeholder windows intentionally do not pretend the media path is ready.
