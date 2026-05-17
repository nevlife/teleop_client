import time
import logging
import threading
from abc import ABC, abstractmethod

from ..state import StationState

logger = logging.getLogger(__name__)


class Controller(ABC):

    def __init__(self, state: StationState):
        self.state = state
        self._stop_event = threading.Event()
        self._client = None
        self._loop = None
        self._last_broadcast = 0.0

    def setup(self, client, loop):
        self._client = client
        self._loop = loop

    def start(self):
        """Main controller loop.

        Subclasses implement ``_setup`` / ``poll`` / ``_teardown`` rather than
        overriding ``start`` — see M7. If ``_setup`` raises (e.g. a backend
        such as pygame is missing), we degrade to a wait-for-stop loop instead
        of crashing the process.
        """
        self._stop_event.clear()
        try:
            self._setup()
        except Exception as e:
            logger.warning(
                f'{self.name()} controller setup failed: {e} — '
                f'idling until shutdown'
            )
            while not self._stop_event.is_set():
                time.sleep(0.1)
            return

        logger.info(f'{self.name()} controller started')
        try:
            while not self._stop_event.is_set():
                self.state.controller_connected = self.poll()
                self._broadcast_status()
                time.sleep(0.02)
        finally:
            try:
                self._teardown()
            except Exception as e:
                logger.warning(f'{self.name()} controller teardown error: {e}')

    def stop(self):
        self._stop_event.set()

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def poll(self) -> bool:
        ...

    def on_disconnect(self):
        self.state.reset_control(connected=False)

    def _setup(self):
        pass

    def _teardown(self):
        pass

    def _broadcast_status(self):
        now = time.monotonic()
        if self._client and now - self._last_broadcast >= 0.05:
            self._client.send_controller_heartbeat(self.state.controller_connected)
            self._last_broadcast = now
