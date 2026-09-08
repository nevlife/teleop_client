#include "teleop_client_v2/codec_probe.hpp"
#include "teleop_client_v2/signaling_client.hpp"

#include <gst/gst.h>

#include <QApplication>
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QGuiApplication>
#include <QLabel>
#include <QMainWindow>
#include <QScreen>
#include <QVBoxLayout>

#include <memory>
#include <vector>

int main(int argc, char ** argv)
{
  gst_init(&argc, &argv);
  QApplication app(argc, argv);
  QCoreApplication::setApplicationName("teleop-client-v2");

  QCommandLineParser parser;
  parser.addHelpOption();
  parser.addOption({"server", "Signaling WebSocket URL", "url", "ws://127.0.0.1:13437/ws"});
  parser.addOption({"robot", "Robot identifier", "id", "rover-01"});
  parser.addOption(QCommandLineOption("fullscreen", "Open one fullscreen window per monitor"));
  parser.process(app);

  QStringList codec_lines;
  for (const auto & codec : teleop_client_v2::probe_video_decoders()) {
    codec_lines << QString::fromStdString(
      codec.name + ": " + (codec.decode_available ? codec.selected_factory : "unavailable"));
  }

  teleop_client_v2::SignalingClient signaling(
    QUrl(parser.value("server")), parser.value("robot"));
  std::vector<std::unique_ptr<QMainWindow>> windows;
  const auto screens = QGuiApplication::screens();
  for (int index = 0; index < screens.size(); ++index) {
    auto window = std::make_unique<QMainWindow>();
    auto * central = new QWidget(window.get());
    auto * layout = new QVBoxLayout(central);
    auto * title = new QLabel(QString("TELEOP V2 — DISPLAY %1").arg(index + 1), central);
    auto * video = new QLabel("Waiting for WebRTC video track", central);
    auto * status = new QLabel("signaling disconnected", central);
    title->setStyleSheet("font-size: 24px; font-weight: 700");
    video->setAlignment(Qt::AlignCenter);
    video->setStyleSheet("background: #111; color: #aaa; font-size: 20px");
    video->setMinimumSize(640, 360);
    layout->addWidget(title);
    layout->addWidget(video, 1);
    layout->addWidget(new QLabel(codec_lines.join("\n"), central));
    layout->addWidget(status);
    window->setCentralWidget(central);
    window->setGeometry(screens[index]->availableGeometry());
    QObject::connect(
      &signaling, &teleop_client_v2::SignalingClient::status_changed,
      status, &QLabel::setText);
    if (parser.isSet("fullscreen")) {
      window->showFullScreen();
    } else {
      window->show();
    }
    windows.push_back(std::move(window));
  }

  signaling.connect_to_server();
  return app.exec();
}
