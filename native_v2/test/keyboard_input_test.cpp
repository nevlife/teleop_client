#include "teleop_client_v2/keyboard_input.hpp"

#include <gtest/gtest.h>

#include <cmath>
#include <initializer_list>

using teleop_client_v2::DriveAction;
using teleop_client_v2::DriveCommand;
using teleop_client_v2::KeyboardConfig;
using teleop_client_v2::KeyboardInput;

namespace
{
constexpr double kTick = 0.02;
constexpr double kEps = 1e-9;

KeyboardConfig config()
{
  KeyboardConfig cfg;
  cfg.max_linear_mps = 0.6;
  cfg.max_angular_rps = 0.8;
  cfg.accel_mps2 = 1.5;
  cfg.decel_mps2 = 3.0;
  cfg.angular_rate_rps2 = 2.0;
  cfg.require_deadman = true;
  return cfg;
}

/// Poll for `seconds` at a fixed tick and return the last command.
DriveCommand advance(KeyboardInput & input, double seconds)
{
  DriveCommand last;
  const int ticks = static_cast<int>(seconds / kTick + 0.5);
  for (int i = 0; i < ticks; ++i) {
    last = input.poll(kTick);
  }
  return last;
}

void drive(KeyboardInput & input, std::initializer_list<DriveAction> keys)
{
  input.set_focused(true);
  for (const auto action : keys) {
    input.set_action(action, true);
  }
}
}  // namespace

TEST(KeyboardInput, NoCommandWithoutFocus)
{
  KeyboardInput input(config());
  input.set_action(DriveAction::kDeadman, true);
  input.set_action(DriveAction::kForward, true);
  const auto command = advance(input, 0.5);
  EXPECT_NEAR(command.linear_mps, 0.0, kEps);
  EXPECT_FALSE(command.deadman);
  EXPECT_FALSE(input.focused());
}

TEST(KeyboardInput, NoCommandWithoutDeadman)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kForward});
  const auto command = advance(input, 0.5);
  EXPECT_NEAR(command.linear_mps, 0.0, kEps);
  EXPECT_FALSE(command.deadman);
}

TEST(KeyboardInput, FocusGainDoesNotInheritHeldKeys)
{
  // A key already down when the window is activated has no matching key-down
  // event, so it must not count as held.
  KeyboardInput input(config());
  input.set_action(DriveAction::kDeadman, true);
  input.set_action(DriveAction::kForward, true);
  input.set_focused(true);
  const auto command = advance(input, 0.5);
  EXPECT_NEAR(command.linear_mps, 0.0, kEps);
}

TEST(KeyboardInput, ThrottleRampsAndSaturates)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward});
  EXPECT_NEAR(advance(input, 0.2).linear_mps, 0.3, 1e-9);
  const auto command = advance(input, 2.0);
  EXPECT_NEAR(command.linear_mps, 0.6, kEps);
  EXPECT_TRUE(command.deadman);
}

TEST(KeyboardInput, ReverseIsNegative)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kBackward});
  EXPECT_NEAR(advance(input, 2.0).linear_mps, -0.6, kEps);
}

TEST(KeyboardInput, OpposingKeysCancel)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward, DriveAction::kBackward});
  EXPECT_NEAR(advance(input, 2.0).linear_mps, 0.0, kEps);
}

TEST(KeyboardInput, YawLeftIsPositiveAndSaturates)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kLeft});
  EXPECT_NEAR(advance(input, 2.0).angular_rps, 0.8, kEps);

  input.set_action(DriveAction::kLeft, false);
  input.set_action(DriveAction::kRight, true);
  EXPECT_NEAR(advance(input, 2.0).angular_rps, -0.8, kEps);
}

TEST(KeyboardInput, DeadmanReleaseStops)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward});
  EXPECT_GT(advance(input, 2.0).linear_mps, 0.0);

  input.set_action(DriveAction::kDeadman, false);
  const auto command = advance(input, 1.0);
  EXPECT_NEAR(command.linear_mps, 0.0, kEps);
  EXPECT_FALSE(command.deadman);
}

TEST(KeyboardInput, FocusLossStopsAndDisarms)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward});
  EXPECT_GT(advance(input, 2.0).linear_mps, 0.0);

  input.set_focused(false);
  const auto command = advance(input, 1.0);
  EXPECT_NEAR(command.linear_mps, 0.0, kEps);
  EXPECT_FALSE(command.deadman);
  EXPECT_FALSE(input.focused());
}

TEST(KeyboardInput, StallDoesNotProduceAVelocityJump)
{
  // One poll may add at most accel * kMaxTickSeconds no matter how long the
  // caller was stalled.
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward});
  const auto command = input.poll(5.0);
  EXPECT_NEAR(command.linear_mps, 1.5 * KeyboardInput::kMaxTickSeconds, kEps);
}

TEST(KeyboardInput, DecelerationIsFasterThanAcceleration)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward});
  advance(input, 2.0);
  input.set_action(DriveAction::kForward, false);
  // decel 3.0 m/s^2 over 0.1 s removes 0.3 from the saturated 0.6.
  EXPECT_NEAR(advance(input, 0.1).linear_mps, 0.3, 1e-9);
}

TEST(KeyboardInput, NonFiniteTickIsIgnored)
{
  KeyboardInput input(config());
  drive(input, {DriveAction::kDeadman, DriveAction::kForward});
  const auto command = input.poll(std::nan(""));
  EXPECT_NEAR(command.linear_mps, 0.0, kEps);
}

TEST(KeyboardInput, DeadmanCanBeDisabled)
{
  KeyboardConfig cfg = config();
  cfg.require_deadman = false;
  KeyboardInput input(cfg);
  drive(input, {DriveAction::kForward});
  EXPECT_NEAR(advance(input, 2.0).linear_mps, 0.6, kEps);
}
