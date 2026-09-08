#pragma once

#include <QObject>
#include <QJsonObject>
#include <QUrl>
#include <QWebSocket>

#include <cstdint>
#include <string>

namespace teleop_client_v2
{

class SignalingClient : public QObject
{
  Q_OBJECT

public:
  SignalingClient(QUrl url, QString robot_id, QObject * parent = nullptr);
  void connect_to_server();
  void send_offer(const QString & sdp);
  void send_answer(const QString & sdp);
  void send_ice(const QJsonObject & candidate);

signals:
  void status_changed(const QString & status);
  void peer_ready(const QString & session_id, std::uint64_t epoch, bool create_offer);
  void offer_received(const QString & sdp);
  void answer_received(const QString & sdp);
  void ice_received(const QJsonObject & candidate);

private:
  void send_json(const QJsonObject & object);
  void on_text_message(const QString & text);

  QUrl url_;
  QString robot_id_;
  QWebSocket socket_;
};

}  // namespace teleop_client_v2
