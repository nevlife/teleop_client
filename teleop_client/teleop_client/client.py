"""TeleopClient — Zenoh-based control/telemetry publisher.

Control-only client, fully decoupled from stream_client's StreamClient. All
published topics use the ``nev/teleop/{vehicle_id}/...`` prefix.

Publishes:
- ``client_heartbeat``      : 5Hz, GCS-side liveness signal.
- ``teleop``                : 20Hz, ``{linear_x, steer_angle}``.
- ``estop``                 : event, ``{active}``.
- ``cmd_mode``              : event, ``{mode}``.
- ``controller_heartbeat``  : 20Hz, ``{connected}``.
- ``telemetry_ping``        : 1Hz, ``{ts}`` (RTT measurement).

Subscribes:
- ``telemetry_pong``        : RTT response.
- ``telemetry``             : vehicle telemetry produced by the bot/server (JSON).

Does not publish any video channels (PLI / video_ctl / video_feedback /
rtx_request etc.) — those are owned by stream_client.
"""
import json
import logging
import threading
import time

import zenoh

from teleop_contracts import (
    IncompatibleSchemaError,
    make_envelope,
    parse_envelope,
    TOPIC_CLIENT_HEARTBEAT,
    TOPIC_CMD_MODE,
    TOPIC_CONTROLLER_HEARTBEAT,
    TOPIC_ESTOP,
    TOPIC_PING,
    TOPIC_PONG,
    TOPIC_TELEOP,
    key_for,
)

logger = logging.getLogger(__name__)

# Threshold of consecutive publish failures before triggering session reconnect.
_RECONNECT_FAILURE_THRESHOLD = 5
# Max E-stop retries per publish call (before falling back to send-loop tick retries).
_ESTOP_PUBLISH_RETRIES = 3
_ESTOP_RETRY_BACKOFF_S = 0.05
# Throttle for noisy parse-error warnings.
_PARSE_WARN_INTERVAL_S = 5.0

_SUFFIX_QOS = {
    TOPIC_CLIENT_HEARTBEAT: dict(
        reliability=zenoh.Reliability.BEST_EFFORT,
        congestion_control=zenoh.CongestionControl.DROP,
        priority=zenoh.Priority.DATA_LOW,
    ),
    TOPIC_TELEOP: dict(
        reliability=zenoh.Reliability.BEST_EFFORT,
        congestion_control=zenoh.CongestionControl.DROP,
        priority=zenoh.Priority.INTERACTIVE_HIGH,
    ),
    TOPIC_ESTOP: dict(
        reliability=zenoh.Reliability.RELIABLE,
        congestion_control=zenoh.CongestionControl.BLOCK,
        priority=zenoh.Priority.REAL_TIME,
    ),
    TOPIC_CMD_MODE: dict(
        reliability=zenoh.Reliability.RELIABLE,
        congestion_control=zenoh.CongestionControl.BLOCK,
        priority=zenoh.Priority.INTERACTIVE_HIGH,
    ),
    TOPIC_CONTROLLER_HEARTBEAT: dict(
        # H3: heartbeat goes RELIABLE so connection-state transitions are not
        # silently dropped. The volume is low enough (20Hz, tiny payload) that
        # back-pressure should not be an issue.
        reliability=zenoh.Reliability.RELIABLE,
        congestion_control=zenoh.CongestionControl.DROP,
        priority=zenoh.Priority.BACKGROUND,
    ),
    TOPIC_PING: dict(
        reliability=zenoh.Reliability.BEST_EFFORT,
        congestion_control=zenoh.CongestionControl.DROP,
        priority=zenoh.Priority.DATA_LOW,
    ),
}


class TeleopClient:
    """Control-only Zenoh publisher.

    Uses the ``nev/teleop/{vid}/`` topic prefix, fully separated from the
    ``nev/stream/{vid}/`` prefix that stream_client uses.
    """

    SUFFIXES = (
        TOPIC_CLIENT_HEARTBEAT,
        TOPIC_TELEOP,
        TOPIC_ESTOP,
        TOPIC_CMD_MODE,
        TOPIC_CONTROLLER_HEARTBEAT,
        TOPIC_PING,
    )

    def __init__(self):
        self._session = None
        self._vehicle_id: str = ''
        self._locator: str = ''
        self._pubs: dict = {}
        self._subs: list = []
        self._rtt_lock = threading.Lock()
        self._rtt_cli_bot_ms: float = 0.0
        self._last_pong_time: float = 0.0

        # Health tracking — used by GUI to surface a red status.
        self._health_lock = threading.Lock()
        self._consecutive_publish_failures = 0
        self._session_unhealthy = False
        self._estop_pending = False  # last estop publish failed; send loop must retry
        self._estop_pending_value = False
        self._estop_last_source: str = ''  # 'gui' / 'joystick' / 'auto' (joystick disconnect)
        self._estop_last_intent: bool = False
        self._estop_last_intent_ts: float = 0.0  # monotonic when last estop intent was set
        self._estop_observer = None  # optional callback(activate: bool, source: str)
        self._last_parse_warn_ts = 0.0
        # Re-declaration is not safe to do concurrently with publishes; serialize.
        self._session_lock = threading.RLock()

    def _key(self, suffix: str) -> str:
        return key_for(self._vehicle_id, suffix)

    def start(self, locator: str, vehicle_id: str) -> None:
        if not vehicle_id:
            raise ValueError('vehicle_id is required')
        self._vehicle_id = vehicle_id
        self._locator = locator
        self._open_session()

        logger.info(
            f'TeleopClient started → {locator or "auto-discovery"} '
            f'(vehicle_id={vehicle_id})'
        )

    def _open_session(self) -> None:
        """Open zenoh session and declare publishers/subscribers."""
        conf = zenoh.Config()
        if self._locator:
            conf.insert_json5('connect/endpoints', json.dumps([self._locator]))
        with self._session_lock:
            self._session = zenoh.open(conf)
            try:
                for suffix in self.SUFFIXES:
                    key = self._key(suffix)
                    self._pubs[suffix] = self._session.declare_publisher(
                        key, **_SUFFIX_QOS[suffix]
                    )
            except Exception:
                self._close_session()
                raise

            # telemetry_pong is published by teleop_server in response to ping.
            self._subs = [
                self._session.declare_subscriber(
                    key_for(self._vehicle_id, TOPIC_PONG), self._on_pong
                ),
            ]

    def _close_session(self) -> None:
        """Tear down publishers/subscribers and close the session (no-throw)."""
        with self._session_lock:
            for sub in self._subs:
                try:
                    sub.undeclare()
                except Exception:
                    pass
            self._subs.clear()
            for suffix, pub in self._pubs.items():
                try:
                    pub.undeclare()
                except Exception as e:
                    logger.warning(f'Error undeclaring publisher [{self._key(suffix)}]: {e}')
            self._pubs.clear()
            try:
                if self._session:
                    self._session.close()
            except Exception as e:
                logger.warning(f'Error closing zenoh session: {e}')
            self._session = None

    def stop(self) -> None:
        self._close_session()

    def _reconnect(self) -> None:
        """H4: tear down current session and re-open (re-declare pubs/subs)."""
        logger.warning('Triggering zenoh session reconnect')
        with self._session_lock:
            self._close_session()
            try:
                self._open_session()
            except Exception as e:
                logger.error(f'Reconnect failed: {e}')
                with self._health_lock:
                    self._session_unhealthy = True
                return
            logger.info('Zenoh session reconnected')
            with self._health_lock:
                self._consecutive_publish_failures = 0
                self._session_unhealthy = False

    def _publish(self, suffix: str, data: dict) -> bool:
        """Publish ``data`` on ``suffix``. Returns True on success."""
        with self._session_lock:
            pub = self._pubs.get(suffix)
            if pub is None:
                return False
            try:
                pub.put(json.dumps(make_envelope(data)))
            except Exception as e:
                logger.warning(f'zenoh put [{self._key(suffix)}]: {e}')
                self._note_publish_failure()
                return False
        self._note_publish_success()
        return True

    def _note_publish_failure(self) -> None:
        trigger_reconnect = False
        with self._health_lock:
            self._consecutive_publish_failures += 1
            if self._consecutive_publish_failures >= _RECONNECT_FAILURE_THRESHOLD:
                trigger_reconnect = True
                self._session_unhealthy = True
        if trigger_reconnect:
            # Spawn reconnect on a background thread so we don't block the
            # caller (which may be the send loop or a GUI signal).
            threading.Thread(target=self._reconnect, daemon=True).start()

    def _note_publish_success(self) -> None:
        with self._health_lock:
            self._consecutive_publish_failures = 0
            self._session_unhealthy = False

    # ---------------- control / heartbeat ----------------

    def send_client_heartbeat(self):
        self._publish(TOPIC_CLIENT_HEARTBEAT, {
            'ts': time.time(),
        })

    def send_teleop(self, linear_x: float, steer_angle: float):
        self._publish(TOPIC_TELEOP, {
            'linear_x':    round(linear_x,    3),
            'steer_angle': round(steer_angle, 4),
        })

    def send_estop(self, activate: bool, source: str = 'gui') -> bool:
        """Publish E-stop with retries.

        ``source`` tags the entry point ('gui' / 'joystick' / 'auto') so the
        GUI banner can show the source. An optional observer is invoked
        synchronously BEFORE the publish so the banner can flip to PENDING
        immediately.

        Returns True if any of the retries succeeded. On total failure, sets
        ``_estop_pending`` so the send loop can keep retrying until it goes
        through. The "cleared" signal is a successful publish without exception.
        """
        with self._health_lock:
            self._estop_last_source = source
            self._estop_last_intent = activate
            self._estop_last_intent_ts = time.monotonic()
            observer = self._estop_observer
        if observer is not None:
            try:
                observer(activate, source)
            except Exception as e:
                logger.warning(f'E-stop observer error: {e}')

        payload = {'active': activate}
        for attempt in range(_ESTOP_PUBLISH_RETRIES):
            if self._publish(TOPIC_ESTOP, payload):
                with self._health_lock:
                    self._estop_pending = False
                    self._estop_pending_value = False
                logger.info(f'E-stop → {activate} (src={source})')
                return True
            if attempt + 1 < _ESTOP_PUBLISH_RETRIES:
                time.sleep(_ESTOP_RETRY_BACKOFF_S * (attempt + 1))

        with self._health_lock:
            self._estop_pending = True
            self._estop_pending_value = activate
        logger.error(
            f'E-stop publish FAILED after {_ESTOP_PUBLISH_RETRIES} retries '
            f'(active={activate}, src={source}); send loop will keep retrying'
        )
        return False

    def set_estop_observer(self, callback) -> None:
        """Register a callback ``cb(activate: bool, source: str)`` invoked on
        every E-stop publish attempt (success or failure). Used by the GUI to
        flip the banner to PENDING immediately when ANY entry point fires.
        The callback may be invoked from arbitrary threads.
        """
        with self._health_lock:
            self._estop_observer = callback

    def retry_pending_estop(self) -> None:
        """Called by send-loop tick; one shot per tick."""
        with self._health_lock:
            if not self._estop_pending:
                return
            value = self._estop_pending_value
        if self._publish(TOPIC_ESTOP, {'active': value}):
            with self._health_lock:
                self._estop_pending = False
                self._estop_pending_value = False
            logger.info(f'E-stop (retry) → {value}')

    def send_cmd_mode(self, mode: int):
        self._publish(TOPIC_CMD_MODE, {
            'mode': mode,
        })
        logger.info(f'Cmd mode → {mode}')

    def send_controller_heartbeat(self, connected: bool):
        self._publish(TOPIC_CONTROLLER_HEARTBEAT, {
            'connected': connected,
        })

    # ---------------- ping / RTT ----------------

    def send_ping(self):
        self._publish(TOPIC_PING, {'ts': time.time()})

    def _on_pong(self, sample):
        try:
            _v, data = parse_envelope(bytes(sample.payload))
            ts = data.get('ts')
            if ts is None:
                return
            rtt_ms = (time.time() - ts) * 1000.0
            if rtt_ms < 0:
                return
            with self._rtt_lock:
                prev = self._rtt_cli_bot_ms
                if prev > 0:
                    smoothed = 0.7 * prev + 0.3 * rtt_ms
                else:
                    smoothed = rtt_ms
                self._rtt_cli_bot_ms = round(smoothed, 1)
                self._last_pong_time = time.monotonic()
        except IncompatibleSchemaError as e:
            # Drop messages whose major version doesn't match; throttle log.
            now = time.monotonic()
            if now - self._last_parse_warn_ts >= _PARSE_WARN_INTERVAL_S:
                logger.warning(f'pong drop: {e}')
                self._last_parse_warn_ts = now
        except Exception as e:
            # M3: throttle parse-error warnings.
            now = time.monotonic()
            if now - self._last_parse_warn_ts >= _PARSE_WARN_INTERVAL_S:
                logger.warning(f'pong parse error: {e}')
                self._last_parse_warn_ts = now

    def expire_rtt_if_stale(self) -> None:
        """H5: explicitly expire stale RTT. Caller drives this from a timer
        (GUI mode) or the send-loop tick (headless)."""
        with self._rtt_lock:
            if self._last_pong_time > 0 and (time.monotonic() - self._last_pong_time) > 3.0:
                self._rtt_cli_bot_ms = 0.0

    @property
    def rtt_cli_bot_ms(self) -> float:
        # Pure read — no side effects. Stale expiry is driven externally
        # (see expire_rtt_if_stale).
        with self._rtt_lock:
            return self._rtt_cli_bot_ms

    @property
    def session_unhealthy(self) -> bool:
        with self._health_lock:
            return self._session_unhealthy

    @property
    def estop_pending(self) -> bool:
        with self._health_lock:
            return self._estop_pending

    @property
    def estop_last_source(self) -> str:
        with self._health_lock:
            return self._estop_last_source

    @property
    def estop_last_intent(self) -> bool:
        with self._health_lock:
            return self._estop_last_intent

    @property
    def estop_last_intent_ts(self) -> float:
        with self._health_lock:
            return self._estop_last_intent_ts

    # ---------------- session accessor for GUI subscriber ----------------

    @property
    def session(self) -> zenoh.Session:
        return self._session
