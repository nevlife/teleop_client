#include "teleop_client_v2/signaling_client.hpp"

#include <QJsonDocument>
#include <QJsonObject>
#include <QtGlobal>

#include <utility>

namespace teleop_client_v2
{

SignalingClient::SignalingClient(QUrl url, QString robot_id, QObject * parent)
: QObject(parent), url_(std::move(url)), robot_id_(std::move(robot_id))
{
  connect(&socket_, &QWebSocket::connected, this, [this]() {
    emit status_changed("signaling connected");
    send_json({
      {"type", "hello"},
      {"protocol_version", 2},
      {"role", "client"},
      {"robot_id", robot_id_},
    });
  });
  connect(&socket_, &QWebSocket::disconnected, this, [this]() {
    emit status_changed("signaling disconnected — controls disabled");
  });
  connect(&socket_, &QWebSocket::textMessageReceived, this, &SignalingClient::on_text_message);
  const auto report_error = [this](QAbstractSocket::SocketError) {
      emit status_changed(socket_.errorString());
    };
#if QT_VERSION >= QT_VERSION_CHECK(6, 5, 0)
  connect(&socket_, &QWebSocket::errorOccurred, this, report_error);
#else
  connect(
    &socket_, QOverload<QAbstractSocket::SocketError>::of(&QWebSocket::error),
    this, report_error);
#endif
}

void SignalingClient::connect_to_server()
{
  emit status_changed(QString("connecting to %1").arg(url_.toString()));
  socket_.open(url_);
}

void SignalingClient::send_json(const QJsonObject & object)
{
  socket_.sendTextMessage(QJsonDocument(object).toJson(QJsonDocument::Compact));
}

void SignalingClient::send_offer(const QString & sdp)
{
  send_json({{"type", "offer"}, {"sdp", sdp}});
}

void SignalingClient::send_answer(const QString & sdp)
{
  send_json({{"type", "answer"}, {"sdp", sdp}});
}

void SignalingClient::send_ice(const QJsonObject & candidate)
{
  send_json({{"type", "ice"}, {"candidate", candidate}});
}

void SignalingClient::on_text_message(const QString & text)
{
  QJsonParseError parse_error;
  const auto document = QJsonDocument::fromJson(text.toUtf8(), &parse_error);
  if (parse_error.error != QJsonParseError::NoError || !document.isObject()) {
    emit status_changed("invalid signaling response");
    return;
  }
  const auto object = document.object();
  const auto type = object.value("type").toString();
  if (type == "hello_ack") {
    emit status_changed("waiting for rover");
  } else if (type == "peer_ready") {
    emit peer_ready(
      object.value("session_id").toString(),
      object.value("session_epoch").toString().toULongLong(),
      object.value("create_offer").toBool());
    emit status_changed("rover paired — WebRTC negotiation starting");
  } else if (type == "peer_left") {
    emit status_changed("rover disconnected — controls disabled");
  } else if (type == "offer") {
    emit offer_received(object.value("sdp").toString());
  } else if (type == "answer") {
    emit answer_received(object.value("sdp").toString());
  } else if (type == "ice") {
    emit ice_received(object.value("candidate").toObject());
  } else if (type == "error") {
    emit status_changed(QString("server error: %1").arg(object.value("code").toString()));
  }
}

}  // namespace teleop_client_v2
