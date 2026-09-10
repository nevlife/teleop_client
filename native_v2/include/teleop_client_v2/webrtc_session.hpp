#pragma once

#include <gst/gst.h>

#define GST_USE_UNSTABLE_API
#include <gst/webrtc/webrtc.h>

#include <QByteArray>
#include <QImage>
#include <QJsonObject>
#include <QObject>
#include <QString>

namespace teleop_client_v2
{

/// The operator half of the WebRTC session.
///
/// The rover is always the offerer, so this side only ever answers. Media
/// arrives as an RTP pad on `webrtcbin`, is auto-plugged through decodebin,
/// and leaves as RGB frames on `frame_ready`.
class WebRtcSession : public QObject
{
  Q_OBJECT

public:
  explicit WebRtcSession(QObject * parent = nullptr);
  ~WebRtcSession() override;

  /// Relay credentials from `hello_ack`. Must be set before `handle_offer`
  /// for the relay to be usable.
  void set_turn(const QString & url, const QString & username, const QString & credential);

  /// Restrict ICE to relay candidates. Mirrors the rover's
  /// `transport.force_relay`; both peers must agree or negotiation stalls.
  void set_force_relay(bool force_relay);

  /// Build the pipeline, apply the remote offer and answer it.
  void handle_offer(const QString & sdp);

  /// Add a remote candidate. Ignored before the session exists.
  void handle_ice(const QJsonObject & candidate);

  /// Release the pipeline. Safe to call when there is none.
  void stop();

  /// Send one frame on the rover's control channel. Returns false when the
  /// channel is not open yet, so the caller can tell "not connected" from
  /// "sent".
  bool send_control(const QByteArray & payload);

  [[nodiscard]] bool control_open() const;

signals:
  void answer_ready(const QString & sdp);
  /// The rover opened the control channel; commands may now be sent.
  void control_ready();
  void ice_ready(const QJsonObject & candidate);
  void status_changed(const QString & status);
  void frame_ready(const QImage & frame);

private:
  friend struct WebRtcCallbacks;

  bool build_pipeline();

  GstElement * pipeline_{nullptr};
  GstElement * webrtc_{nullptr};
  /// Created by the rover, which is the offerer. Owned by webrtcbin.
  GstWebRTCDataChannel * control_{nullptr};
  QString turn_url_;
  QString turn_username_;
  QString turn_credential_;
  bool force_relay_{true};
};

}  // namespace teleop_client_v2
