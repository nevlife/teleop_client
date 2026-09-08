#include "teleop_client_v2/codec_probe.hpp"
#include "teleop_client_v2/keyboard_input.hpp"
#include "teleop_client_v2/signaling_client.hpp"

#include <gst/gst.h>

#include <QApplication>
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QElapsedTimer>
#include <QEvent>
#include <QGuiApplication>
#include <QKeyEvent>
#include <QLabel>
#include <QMainWindow>
#include <QObject>
#include <QScreen>
#include <QTimer>
#include <QVBoxLayout>
#include <QWindow>

#include <memory>
#include <vector>

namespace
{

/// Rate at which the keyboard state is turned into a motion command. The
/// rover's ControlGuard watchdog is 250 ms, so commands must leave well
/// inside that.
constexpr int kInputTickMs = 20;

/// Maps a Qt key onto a control intent. Returns false for anything unbound.
bool action_for_key(int key, teleop_client_v2::DriveAction & action)
{
  switch (key) {
    case Qt::Key_W: action = teleop_client_v2::DriveAction::kForward; return true;
    case Qt::Key_S: action = teleop_client_v2::DriveAction::kBackward; return true;
    case Qt::Key_A: action = teleop_client_v2::DriveAction::kLeft; return true;
    case Qt::Key_D: action = teleop_client_v2::DriveAction::kRight; return true;
    case Qt::Key_Space: action = teleop_client_v2::DriveAction::kDeadman; return true;
    default: return false;
  }
}

/// Feeds Qt key transitions into the input core.
///
/// Installed on the QApplication rather than on a window so the control keys
/// work regardless of which child widget holds focus. Bound keys are consumed:
/// without that, Space would activate whichever button has focus.
///
/// Auto-repeat is dropped -- the core tracks held keys itself, and the repeat
/// stream carries a spurious release before each press.
class KeyboardBridge : public QObject
{
public:
  KeyboardBridge(teleop_client_v2::KeyboardInput & input, QObject * parent)
  : QObject(parent), input_(input) {}

protected:
  bool eventFilter(QObject * object, QEvent * event) override
  {
    const auto type = event->type();
    if (type != QEvent::KeyPress && type != QEvent::KeyRelease) {
      return QObject::eventFilter(object, event);
    }
    auto * key_event = static_cast<QKeyEvent *>(event);
    if (key_event->isAutoRepeat()) {
      return QObject::eventFilter(object, event);
    }
    teleop_client_v2::DriveAction action{};
    if (!action_for_key(key_event->key(), action)) {
      return QObject::eventFilter(object, event);
    }
    input_.set_action(action, type == QEvent::KeyPress);
    return true;
  }

private:
  teleop_client_v2::KeyboardInput & input_;
};

QString describe(const teleop_client_v2::DriveCommand & command)
{
  if (!command.deadman) {
    return QStringLiteral("HOLD — press and hold SPACE to drive (W/S, A/D)");
  }
  return QStringLiteral("DRIVE  linear %1 m/s   angular %2 rad/s")
         .arg(command.linear_mps, 0, 'f', 2)
         .arg(command.angular_rps, 0, 'f', 2);
}

}  // namespace

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

  teleop_client_v2::KeyboardInput input;
  auto * bridge = new KeyboardBridge(input, &app);
  app.installEventFilter(bridge);
  // Window focus is the keyboard's "connected" signal, and every transition
  // clears the held keys -- Qt sends no key-up for keys that were down when a
  // window lost focus.
  QObject::connect(
    &app, &QGuiApplication::focusWindowChanged,
    &app, [&input](QWindow * window) {input.set_focused(window != nullptr);});

  std::vector<std::unique_ptr<QMainWindow>> windows;
  std::vector<QLabel *> drive_labels;
  const auto screens = QGuiApplication::screens();
  for (int index = 0; index < screens.size(); ++index) {
    auto window = std::make_unique<QMainWindow>();
    auto * central = new QWidget(window.get());
    auto * layout = new QVBoxLayout(central);
    auto * title = new QLabel(QString("TELEOP V2 — DISPLAY %1").arg(index + 1), central);
    auto * video = new QLabel("Waiting for WebRTC video track", central);
    auto * drive = new QLabel(describe({}), central);
    auto * status = new QLabel("signaling disconnected", central);
    title->setStyleSheet("font-size: 24px; font-weight: 700");
    video->setAlignment(Qt::AlignCenter);
    video->setStyleSheet("background: #111; color: #aaa; font-size: 20px");
    video->setMinimumSize(640, 360);
    drive->setStyleSheet("font-size: 16px; font-family: monospace");
    layout->addWidget(title);
    layout->addWidget(video, 1);
    layout->addWidget(new QLabel(codec_lines.join("\n"), central));
    layout->addWidget(drive);
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
    drive_labels.push_back(drive);
    windows.push_back(std::move(window));
  }

  // The motion command is only displayed for now; wiring it to the control
  // data channel is the next milestone.
  QElapsedTimer clock;
  clock.start();
  QTimer input_timer;
  QObject::connect(
    &input_timer, &QTimer::timeout, &app, [&input, &drive_labels, &clock]() {
      const double dt = static_cast<double>(clock.restart()) / 1000.0;
      const auto command = input.poll(dt);
      const auto text = describe(command);
      for (auto * label : drive_labels) {
        label->setText(text);
      }
    });
  input_timer.start(kInputTickMs);

  signaling.connect_to_server();
  return app.exec();
}
