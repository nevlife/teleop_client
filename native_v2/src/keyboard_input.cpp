#include "teleop_client_v2/keyboard_input.hpp"

#include <algorithm>
#include <cmath>

namespace teleop_client_v2
{
namespace
{
double sanitize(double value)
{
  return std::isfinite(value) ? value : 0.0;
}

double ramp(double current, double target, double step)
{
  // A non-positive step means no movement is permitted this tick. Returning
  // the target here instead would teleport to full speed whenever the tick
  // collapsed to zero -- a stalled clock, or a zero acceleration limit.
  if (!(step > 0.0)) {
    return current;
  }
  const double delta = target - current;
  if (std::abs(delta) <= step) {
    return target;
  }
  return current + std::copysign(step, delta);
}
}  // namespace

KeyboardInput::KeyboardInput(KeyboardConfig config)
: config_(config)
{
}

void KeyboardInput::set_action(DriveAction action, bool pressed)
{
  const std::lock_guard<std::mutex> lock(mutex_);
  held_[static_cast<std::size_t>(action)] = pressed;
}

void KeyboardInput::release_all()
{
  const std::lock_guard<std::mutex> lock(mutex_);
  held_.fill(false);
}

void KeyboardInput::set_focused(bool focused)
{
  const std::lock_guard<std::mutex> lock(mutex_);
  focused_ = focused;
  held_.fill(false);
}

bool KeyboardInput::focused() const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  return focused_;
}

bool KeyboardInput::held(DriveAction action) const
{
  return held_[static_cast<std::size_t>(action)];
}

bool KeyboardInput::armed() const
{
  const std::lock_guard<std::mutex> lock(mutex_);
  return focused_ && (!config_.require_deadman || held(DriveAction::kDeadman));
}

DriveCommand KeyboardInput::poll(double dt)
{
  const double tick = std::clamp(sanitize(dt), 0.0, kMaxTickSeconds);

  const std::lock_guard<std::mutex> lock(mutex_);
  const bool armed =
    focused_ && (!config_.require_deadman || held(DriveAction::kDeadman));

  double target_linear = 0.0;
  double target_angular = 0.0;
  if (armed) {
    if (held(DriveAction::kForward)) {
      target_linear += config_.max_linear_mps;
    }
    if (held(DriveAction::kBackward)) {
      target_linear -= config_.max_linear_mps;
    }
    if (held(DriveAction::kLeft)) {
      target_angular += config_.max_angular_rps;
    }
    if (held(DriveAction::kRight)) {
      target_angular -= config_.max_angular_rps;
    }
  }
  // Not armed leaves both targets at zero, so the vehicle ramps to a stop
  // rather than latching the last command.

  const bool speeding_up = std::abs(target_linear) > std::abs(linear_mps_) ||
    target_linear * linear_mps_ < 0.0;
  const double linear_rate = speeding_up ? config_.accel_mps2 : config_.decel_mps2;

  linear_mps_ = ramp(linear_mps_, target_linear, linear_rate * tick);
  angular_rps_ = ramp(angular_rps_, target_angular, config_.angular_rate_rps2 * tick);

  DriveCommand command;
  command.linear_mps = sanitize(
    std::clamp(linear_mps_, -config_.max_linear_mps, config_.max_linear_mps));
  command.angular_rps = sanitize(
    std::clamp(angular_rps_, -config_.max_angular_rps, config_.max_angular_rps));
  command.deadman = armed;
  return command;
}

}  // namespace teleop_client_v2
