from .base import Controller
from .joystick import JoystickController

CONTROLLERS = {
    'joystick': JoystickController,
}

__all__ = ['Controller', 'JoystickController', 'create_controller']


def create_controller(state, cfg: dict) -> Controller:
    name = cfg.get('controller_type', 'joystick')
    cls = CONTROLLERS.get(name)
    if cls is None:
        raise ValueError(f'Unknown controller_type: {name!r} (available: {list(CONTROLLERS)})')
    return cls(state, cfg.get(name, {}))
