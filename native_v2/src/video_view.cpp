#include "teleop_client_v2/video_view.hpp"

#include <QMutexLocker>
#include <QPainter>

namespace teleop_client_v2
{

VideoView::VideoView(QWidget * parent)
: QWidget(parent)
{
  setMinimumSize(640, 360);
  setAutoFillBackground(false);
  // The whole surface is painted every frame, so Qt need not clear it first.
  setAttribute(Qt::WA_OpaquePaintEvent);
}

void VideoView::submit_frame(const QImage & frame)
{
  {
    const QMutexLocker lock(&mutex_);
    // `frame` must already own its pixels -- WebRtcSession deep-copies before
    // emitting, precisely so this hot path does not copy a second time.
    frame_ = frame;
  }
  // Queued: this runs on a GStreamer thread, and only the UI thread may
  // touch the widget.
  QMetaObject::invokeMethod(this, qOverload<>(&QWidget::update), Qt::QueuedConnection);
}

void VideoView::set_placeholder(const QString & text)
{
  {
    const QMutexLocker lock(&mutex_);
    placeholder_ = text;
  }
  QMetaObject::invokeMethod(this, qOverload<>(&QWidget::update), Qt::QueuedConnection);
}

void VideoView::paintEvent(QPaintEvent *)
{
  QImage frame;
  QString placeholder;
  {
    const QMutexLocker lock(&mutex_);
    frame = frame_;
    placeholder = placeholder_;
  }

  QPainter painter(this);
  painter.fillRect(rect(), QColor(17, 17, 17));

  if (frame.isNull()) {
    painter.setPen(QColor(170, 170, 170));
    painter.drawText(rect(), Qt::AlignCenter, placeholder);
    return;
  }

  const QSize scaled = frame.size().scaled(size(), Qt::KeepAspectRatio);
  const QRect target(
    (width() - scaled.width()) / 2, (height() - scaled.height()) / 2,
    scaled.width(), scaled.height());
  painter.setRenderHint(QPainter::SmoothPixmapTransform, true);
  painter.drawImage(target, frame);
}

}  // namespace teleop_client_v2
