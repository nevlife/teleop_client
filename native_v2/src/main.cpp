#include "teleop_client_v2/codec_probe.hpp"
#include "teleop_client_v2/keyboard_input.hpp"
#include "teleop_client_v2/signaling_client.hpp"
#include "teleop_client_v2/video_view.hpp"
#include "teleop_client_v2/webrtc_session.hpp"

#include <gst/gst.h>

#include "teleop/v2/control.pb.h"

#include <QApplication>
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QElapsedTimer>
#include <QEvent>
#include <QGuiApplication>
#include <QKeyEvent>
#include <QLabel>
#include <QMetaType>
#include <QMainWindow>
#include <QObject>
#include <QScreen>
#include <QTimer>
#include <QVBoxLayout>
#include <QWindow>

#include <chrono>
#include <cstdio>
#include <memory>
#include <vector>

namespace
{

/// Rate at which the keyboard state is turned into a motion command. The
/// rover's ControlGuard watchdog is 250 ms, so commands must leave well
/// inside that.
constexpr int kInputTickMs = 20;

/// How long the rover may honour a command. Must not exceed the rover's
/// watchdog, so a command stuck in a queue expires before the guard would
/// already have given up on the link.
constexpr int kCommandValidForMs = 200;

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

/// The link state leads, because the command is computed locally whether or
/// not anything receives it. Showing a speed first made a client with no
/// rover behind it read as though the vehicle were moving.
QString describe(const teleop_client_v2::DriveCommand & command, bool control_linked)
{
  if (!control_linked) {
    return QStringLiteral("NO CONTROL LINK — commands are not reaching the rover");
  }
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
  teleop_client_v2::WebRtcSession session;

  // Both signaling and media report through the same line on stdout, so a
  // headless run says what happened without a window to read.
  const auto log = [](const QString & line) {
      std::fprintf(stdout, "teleop-client: %s\n", qUtf8Printable(line));
      std::fflush(stdout);
    };
  QObject::connect(
    &signaling, &teleop_client_v2::SignalingClient::status_changed, &app, log);
  QObject::connect(
    &session, &teleop_client_v2::WebRtcSession::status_changed, &app, log);

  // Session identity from pairing. Every MotionCommand carries it, and the
  // rover's ControlGuard rejects anything from another session or epoch.
  auto session_id = std::make_shared<QString>();
  auto session_epoch = std::make_shared<quint64>(0);
  auto sequence = std::make_shared<quint64>(0);
  QObject::connect(
    &signaling, &teleop_client_v2::SignalingClient::peer_ready, &app,
    [session_id, session_epoch, sequence](const QString & id, std::uint64_t epoch, bool) {
      *session_id = id;
      *session_epoch = epoch;
      // Sequence numbers must increase within a session; a new session starts
      // over, which the new epoch makes unambiguous.
      *sequence = 0;
    });

  // The rover is always the offerer, so this side only ever answers.
  QObject::connect(
    &signaling, &teleop_client_v2::SignalingClient::turn_offered,
    &session, &teleop_client_v2::WebRtcSession::set_turn);
  QObject::connect(
    &signaling, &teleop_client_v2::SignalingClient::offer_received,
    &session, &teleop_client_v2::WebRtcSession::handle_offer);
  QObject::connect(
    &signaling, &teleop_client_v2::SignalingClient::ice_received,
    &session, &teleop_client_v2::WebRtcSession::handle_ice);
  QObject::connect(
    &session, &teleop_client_v2::WebRtcSession::answer_ready,
    &signaling, &teleop_client_v2::SignalingClient::send_answer);
  QObject::connect(
    &session, &teleop_client_v2::WebRtcSession::ice_ready,
    &signaling, &teleop_client_v2::SignalingClient::send_ice);

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
    auto * video = new teleop_client_v2::VideoView(central);
    auto * drive = new QLabel(describe({}, false), central);
    auto * status = new QLabel("signaling disconnected", central);
    title->setStyleSheet("font-size: 24px; font-weight: 700");
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
    // Every display shows the same track; the frame is copied into each
    // view rather than decoded more than once.
    QObject::connect(
      &session, &teleop_client_v2::WebRtcSession::frame_ready,
      video, &teleop_client_v2::VideoView::submit_frame);
    QObject::connect(
      &session, &teleop_client_v2::WebRtcSession::status_changed,
      status, &QLabel::setText);
    drive_labels.push_back(drive);
    windows.push_back(std::move(window));
  }

  QElapsedTimer clock;
  clock.start();
  QTimer input_timer;
  QObject::connect(
    &input_timer, &QTimer::timeout, &app,
    [&input, &drive_labels, &clock, &session, robot = parser.value("robot"),
    session_id, session_epoch, sequence]() {
      const double dt = static_cast<double>(clock.restart()) / 1000.0;
      const auto command = input.poll(dt);

      const bool linked = session.control_open();
      const QString text = describe(command, linked);
      if (linked) {
        nev::teleop::v2::MotionCommand wire;
        wire.set_robot_id(robot.toStdString());
        wire.set_session_id(session_id->toStdString());
        wire.set_session_epoch(*session_epoch);
        wire.set_sequence(++*sequence);
        wire.set_sent_unix_ns(
          std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());
        wire.set_valid_for_ms(kCommandValidForMs);
        wire.set_linear_mps(static_cast<float>(command.linear_mps));
        wire.set_angular_rps(static_cast<float>(command.angular_rps));
        wire.set_deadman(command.deadman);

        std::string encoded;
        if (wire.SerializeToString(&encoded)) {
          session.send_control(QByteArray(encoded.data(), static_cast<int>(encoded.size())));
        }
      }

      for (auto * label : drive_labels) {
        label->setText(text);
      }
    });
  input_timer.start(kInputTickMs);

  // Frame accounting, reported once a second. Without it a headless run
  // cannot distinguish "negotiated" from "actually receiving pictures".
  auto frames = std::make_shared<int>(0);
  auto geometry = std::make_shared<QString>();
  QObject::connect(
    &session, &teleop_client_v2::WebRtcSession::frame_ready, &app,
    [frames, geometry](const QImage & frame) {
      ++*frames;
      *geometry = QString("%1x%2").arg(frame.width()).arg(frame.height());
    });
  QTimer frame_report;
  QObject::connect(
    &frame_report, &QTimer::timeout, &app, [frames, geometry, &log]() {
      if (*frames > 0) {
        log(QString("video %1 at %2 fps").arg(*geometry).arg(*frames));
        *frames = 0;
      }
    });
  frame_report.start(1000);

  // The drive state is on the window, but a headless run needs it on stdout
  // too, and it is the only way to see whether commands are leaving.
  QTimer drive_report;
  QObject::connect(
    &drive_report, &QTimer::timeout, &app, [&session, &input, &log]() {
      log(QString("control %1, %2")
      .arg(session.control_open() ? "link up" : "link down")
      .arg(input.focused() ? "window focused" : "window not focused"));
    });
  drive_report.start(2000);

  signaling.connect_to_server();
  return app.exec();
}
