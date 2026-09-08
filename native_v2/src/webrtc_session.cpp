#include "teleop_client_v2/webrtc_session.hpp"

#include <gst/app/gstappsink.h>
#include <gst/sdp/sdp.h>
#include <gst/video/video.h>

#define GST_USE_UNSTABLE_API
#include <gst/webrtc/webrtc.h>

#include <QJsonObject>
#include <QUrl>

namespace teleop_client_v2
{
namespace
{

/// webrtcbin takes one `turn://user:pass@host:port` string, while the server
/// sends `turn:host:port?transport=udp` with the credentials alongside.
QString turn_server_uri(
  const QString & url, const QString & username, const QString & credential)
{
  QString rest;
  bool secure = false;
  if (url.startsWith("turns:")) {
    rest = url.mid(6);
    secure = true;
  } else if (url.startsWith("turn:")) {
    rest = url.mid(5);
  } else {
    return {};
  }
  const auto query = rest.indexOf('?');
  const QString host_port = query < 0 ? rest : rest.left(query);
  // Escaping keeps a future password containing '@' or ':' from splitting
  // the URI in the wrong place.
  const QString user = QUrl::toPercentEncoding(username);
  const QString pass = QUrl::toPercentEncoding(credential);
  return (secure ? QStringLiteral("turns://") : QStringLiteral("turn://")) +
         user + ":" + pass + "@" + host_port;
}

}  // namespace

/// Free functions with C linkage semantics for the GStreamer signals. Kept in
/// a friend struct so they can reach the session's private members without
/// widening the public interface.
struct WebRtcCallbacks
{
  static void on_answer_created(GstPromise * promise, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    const GstStructure * reply = gst_promise_get_reply(promise);
    GstWebRTCSessionDescription * answer = nullptr;
    gst_structure_get(reply, "answer", GST_TYPE_WEBRTC_SESSION_DESCRIPTION, &answer, nullptr);
    gst_promise_unref(promise);
    if (answer == nullptr) {
      emit self->status_changed("webrtcbin produced no answer");
      return;
    }

    GstPromise * local = gst_promise_new();
    g_signal_emit_by_name(self->webrtc_, "set-local-description", answer, local);
    gst_promise_interrupt(local);
    gst_promise_unref(local);

    gchar * text = gst_sdp_message_as_text(answer->sdp);
    emit self->answer_ready(QString::fromUtf8(text));
    g_free(text);
    gst_webrtc_session_description_free(answer);
  }

  static void on_remote_description_set(GstPromise * promise, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    gst_promise_unref(promise);
    GstPromise * answer = gst_promise_new_with_change_func(on_answer_created, self, nullptr);
    g_signal_emit_by_name(self->webrtc_, "create-answer", nullptr, answer);
  }

  static void on_ice_candidate(
    GstElement *, guint mline_index, gchar * candidate, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    QJsonObject object;
    object["candidate"] = QString::fromUtf8(candidate != nullptr ? candidate : "");
    object["sdpMLineIndex"] = static_cast<int>(mline_index);
    emit self->ice_ready(object);
  }

  static GstFlowReturn on_new_sample(GstAppSink * sink, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    GstSample * sample = gst_app_sink_pull_sample(sink);
    if (sample == nullptr) {
      return GST_FLOW_OK;
    }
    GstCaps * caps = gst_sample_get_caps(sample);
    GstBuffer * buffer = gst_sample_get_buffer(sample);
    GstVideoInfo info;
    GstMapInfo map;
    if (caps != nullptr && buffer != nullptr && gst_video_info_from_caps(&info, caps) &&
      gst_buffer_map(buffer, &map, GST_MAP_READ))
    {
      // GST_VIDEO_INFO_PLANE_STRIDE, not width * 3: GStreamer aligns rows.
      const QImage borrowed(
        map.data, GST_VIDEO_INFO_WIDTH(&info), GST_VIDEO_INFO_HEIGHT(&info),
        GST_VIDEO_INFO_PLANE_STRIDE(&info, 0), QImage::Format_RGB888);
      // Deep copy before emitting. This runs on a streaming thread and the
      // connection to the UI is queued, so the buffer is unmapped and may be
      // recycled long before the slot runs; a QImage over borrowed memory
      // copies shallowly and would leave the widget reading freed pixels.
      emit self->frame_ready(borrowed.copy());
      gst_buffer_unmap(buffer, &map);
    }
    gst_sample_unref(sample);
    return GST_FLOW_OK;
  }

  /// decodebin has produced raw video: convert to RGB and hand it to the UI.
  static void on_decoded_pad(GstElement *, GstPad * pad, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    if (gst_pad_get_current_caps(pad) == nullptr && gst_pad_has_current_caps(pad) == FALSE) {
      return;
    }

    GstElement * convert = gst_element_factory_make("videoconvert", nullptr);
    GstElement * sink = gst_element_factory_make("appsink", nullptr);
    if (convert == nullptr || sink == nullptr) {
      emit self->status_changed("videoconvert or appsink is missing");
      return;
    }

    GstCaps * caps = gst_caps_new_simple(
      "video/x-raw", "format", G_TYPE_STRING, "RGB", nullptr);
    g_object_set(
      sink, "emit-signals", FALSE, "sync", FALSE, "max-buffers", 1, "drop", TRUE,
      "caps", caps, nullptr);
    gst_caps_unref(caps);

    GstAppSinkCallbacks callbacks{};
    callbacks.new_sample = on_new_sample;
    gst_app_sink_set_callbacks(GST_APP_SINK(sink), &callbacks, self, nullptr);

    gst_bin_add_many(GST_BIN(self->pipeline_), convert, sink, nullptr);
    gst_element_sync_state_with_parent(convert);
    gst_element_sync_state_with_parent(sink);
    gst_element_link(convert, sink);

    GstPad * target = gst_element_get_static_pad(convert, "sink");
    gst_pad_link(pad, target);
    gst_object_unref(target);
    emit self->status_changed("video track decoding");
  }

  /// webrtcbin has produced an RTP pad: auto-plug depayloader and decoder.
  static void on_incoming_stream(GstElement *, GstPad * pad, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    if (GST_PAD_DIRECTION(pad) != GST_PAD_SRC) {
      return;
    }
    GstElement * decode = gst_element_factory_make("decodebin", nullptr);
    if (decode == nullptr) {
      emit self->status_changed("decodebin is missing");
      return;
    }
    g_signal_connect(decode, "pad-added", G_CALLBACK(on_decoded_pad), self);
    gst_bin_add(GST_BIN(self->pipeline_), decode);
    gst_element_sync_state_with_parent(decode);
    GstPad * target = gst_element_get_static_pad(decode, "sink");
    gst_pad_link(pad, target);
    gst_object_unref(target);
  }

  /// The rover creates the control channel, so this side receives it.
  static void on_data_channel(
    GstElement *, GstWebRTCDataChannel * channel, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    gchar * label = nullptr;
    g_object_get(channel, "label", &label, nullptr);
    const QString name = QString::fromUtf8(label != nullptr ? label : "");
    g_free(label);
    if (name != "control") {
      emit self->status_changed(QString("ignoring data channel '%1'").arg(name));
      return;
    }
    self->control_ = channel;
    emit self->status_changed("control channel open");
    emit self->control_ready();
  }

  static void on_bus_message(GstBus *, GstMessage * message, gpointer user_data)
  {
    auto * self = static_cast<WebRtcSession *>(user_data);
    if (GST_MESSAGE_TYPE(message) != GST_MESSAGE_ERROR) {
      return;
    }
    GError * error = nullptr;
    gchar * debug = nullptr;
    gst_message_parse_error(message, &error, &debug);
    emit self->status_changed(QString("pipeline error: %1").arg(error->message));
    g_error_free(error);
    g_free(debug);
  }
};

WebRtcSession::WebRtcSession(QObject * parent)
: QObject(parent)
{
}

WebRtcSession::~WebRtcSession()
{
  stop();
}

void WebRtcSession::set_turn(
  const QString & url, const QString & username, const QString & credential)
{
  turn_url_ = url;
  turn_username_ = username;
  turn_credential_ = credential;
}

void WebRtcSession::set_force_relay(bool force_relay)
{
  force_relay_ = force_relay;
}

bool WebRtcSession::build_pipeline()
{
  stop();

  pipeline_ = gst_pipeline_new("teleop-receive");
  webrtc_ = gst_element_factory_make("webrtcbin", "webrtc");
  if (pipeline_ == nullptr || webrtc_ == nullptr) {
    emit status_changed("webrtcbin is missing from this GStreamer build");
    return false;
  }
  g_object_set(webrtc_, "bundle-policy", GST_WEBRTC_BUNDLE_POLICY_MAX_BUNDLE, nullptr);
  g_object_set(webrtc_, "latency", 0, nullptr);
  gst_bin_add(GST_BIN(pipeline_), webrtc_);

  if (!turn_url_.isEmpty()) {
    const auto uri = turn_server_uri(turn_url_, turn_username_, turn_credential_);
    if (uri.isEmpty()) {
      emit status_changed("TURN url from the server is not usable");
    } else {
      g_object_set(webrtc_, "turn-server", uri.toUtf8().constData(), nullptr);
    }
  }
  if (force_relay_) {
    g_object_set(
      webrtc_, "ice-transport-policy", GST_WEBRTC_ICE_TRANSPORT_POLICY_RELAY, nullptr);
  }

  g_signal_connect(webrtc_, "pad-added", G_CALLBACK(WebRtcCallbacks::on_incoming_stream), this);
  g_signal_connect(
    webrtc_, "on-ice-candidate", G_CALLBACK(WebRtcCallbacks::on_ice_candidate), this);
  g_signal_connect(
    webrtc_, "on-data-channel", G_CALLBACK(WebRtcCallbacks::on_data_channel), this);

  GstBus * bus = gst_element_get_bus(pipeline_);
  gst_bus_add_signal_watch(bus);
  g_signal_connect(bus, "message", G_CALLBACK(WebRtcCallbacks::on_bus_message), this);
  gst_object_unref(bus);

  if (gst_element_set_state(pipeline_, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
    emit status_changed("receive pipeline will not start");
    return false;
  }
  return true;
}

void WebRtcSession::handle_offer(const QString & sdp)
{
  if (!build_pipeline()) {
    return;
  }

  GstSDPMessage * message = nullptr;
  if (gst_sdp_message_new_from_text(sdp.toUtf8().constData(), &message) != GST_SDP_OK) {
    emit status_changed("offer is not parseable SDP");
    return;
  }
  GstWebRTCSessionDescription * offer =
    gst_webrtc_session_description_new(GST_WEBRTC_SDP_TYPE_OFFER, message);
  // The answer is created from the completion callback rather than inline:
  // webrtcbin must have finished applying the remote description first.
  GstPromise * promise = gst_promise_new_with_change_func(
    WebRtcCallbacks::on_remote_description_set, this, nullptr);
  g_signal_emit_by_name(webrtc_, "set-remote-description", offer, promise);
  gst_webrtc_session_description_free(offer);
  emit status_changed("offer received — answering");
}

void WebRtcSession::handle_ice(const QJsonObject & candidate)
{
  if (webrtc_ == nullptr) {
    return;
  }
  const auto text = candidate.value("candidate").toString();
  const auto index = candidate.value("sdpMLineIndex").toInt();
  g_signal_emit_by_name(
    webrtc_, "add-ice-candidate", static_cast<guint>(index), text.toUtf8().constData());
}

bool WebRtcSession::control_open() const
{
  if (control_ == nullptr) {
    return false;
  }
  GstWebRTCDataChannelState state = GST_WEBRTC_DATA_CHANNEL_STATE_CONNECTING;
  g_object_get(control_, "ready-state", &state, nullptr);
  return state == GST_WEBRTC_DATA_CHANNEL_STATE_OPEN;
}

bool WebRtcSession::send_control(const QByteArray & payload)
{
  if (!control_open()) {
    return false;
  }
  GBytes * bytes = g_bytes_new(payload.constData(), static_cast<gsize>(payload.size()));
  g_signal_emit_by_name(control_, "send-data", bytes);
  g_bytes_unref(bytes);
  return true;
}

void WebRtcSession::stop()
{
  if (pipeline_ == nullptr) {
    return;
  }
  gst_element_set_state(pipeline_, GST_STATE_NULL);
  gst_object_unref(pipeline_);
  pipeline_ = nullptr;
  webrtc_ = nullptr;
  // Owned by webrtcbin, which the pipeline has just disposed.
  control_ = nullptr;
}

}  // namespace teleop_client_v2
