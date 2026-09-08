#pragma once

#include <array>
#include <cstddef>
#include <mutex>

namespace teleop_client_v2
{

/// One control intent. The UI layer maps physical keys onto these, so this
/// header carries no toolkit dependency and stays unit-testable.
enum class DriveAction : std::size_t
{
  kForward = 0,
  kBackward,
  kLeft,
  kRight,
  kDeadman,
  kCount,
};

struct KeyboardConfig
{
  double max_linear_mps{0.6};
  double max_angular_rps{0.8};
  /// Slowing down is allowed to be sharper than speeding up.
  double accel_mps2{1.5};
  double decel_mps2{3.0};
  double angular_rate_rps2{2.0};
  /// With the deadman required, motion is commanded only while it is held.
  bool require_deadman{true};
};

/// What one poll produced, in the units of nev.teleop.v2.MotionCommand.
struct DriveCommand
{
  double linear_mps{0.0};
  double angular_rps{0.0};
  bool deadman{false};
};

/// Turns held keys into a rate-limited motion command.
///
/// A key is binary, so the raw key state is never fed straight into the
/// vehicle command: the target implied by the held keys is approached at the
/// configured acceleration limits, which keeps a tap from becoming a
/// full-throttle step.
///
/// `set_action` / `set_focused` / `release_all` are called from the UI thread
/// and `poll` from whichever thread drives the data channel, so the key state
/// is guarded.
class KeyboardInput
{
public:
  explicit KeyboardInput(KeyboardConfig config = {});

  /// Record a key transition.
  void set_action(DriveAction action, bool pressed);

  /// Drop every held key. Qt delivers no key-up for keys that were down when
  /// a window lost focus, so a held key would otherwise latch forever.
  void release_all();

  /// Window focus is this controller's "connected" signal. Held keys are
  /// dropped on every transition, in both directions: on focus gain a key
  /// already down has no matching key-down event, and inheriting it would let
  /// an operator drive by alt-tabbing in with the deadman pressed.
  void set_focused(bool focused);

  [[nodiscard]] bool focused() const;

  /// Advance by `dt` seconds and return the command to send. `dt` is clamped
  /// so a scheduling stall cannot integrate into a large velocity step.
  DriveCommand poll(double dt);

  /// True while the window has focus and, if required, the deadman is held.
  [[nodiscard]] bool armed() const;

  static constexpr double kMaxTickSeconds = 0.1;

private:
  [[nodiscard]] bool held(DriveAction action) const;

  KeyboardConfig config_;
  mutable std::mutex mutex_;
  std::array<bool, static_cast<std::size_t>(DriveAction::kCount)> held_{};
  bool focused_{false};
  double linear_mps_{0.0};
  double angular_rps_{0.0};
};

}  // namespace teleop_client_v2
