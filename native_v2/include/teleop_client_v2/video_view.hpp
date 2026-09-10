#pragma once

#include <QImage>
#include <QMutex>
#include <QString>
#include <QWidget>

namespace teleop_client_v2
{

/// Paints the most recent decoded frame, scaled to fit while keeping aspect.
///
/// Frames arrive on a GStreamer streaming thread, so `submit_frame` only
/// stores the image under a lock and asks for a repaint; the paint itself
/// happens on the UI thread like any other widget.
class VideoView : public QWidget
{
  Q_OBJECT

public:
  explicit VideoView(QWidget * parent = nullptr);

  /// Thread-safe, and safe to call before the widget is shown. The image
  /// must own its pixels; it is stored, not copied.
  void submit_frame(const QImage & frame);

  /// Text shown while no frame has arrived.
  void set_placeholder(const QString & text);

protected:
  void paintEvent(QPaintEvent * event) override;

private:
  QMutex mutex_;
  QImage frame_;
  QString placeholder_{"Waiting for WebRTC video track"};
};

}  // namespace teleop_client_v2
