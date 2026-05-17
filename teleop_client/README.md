# teleop_client

NEV 차량용 **제어 / 텔레메트리 전용** 클라이언트.

`stream_client` 와 완전히 분리된 별도 프로세스로, 조이스틱 입력 → 차량 명령
발행 + 차량 텔레메트리 GUI 만 담당한다. 영상 디코드/렌더링/PLI 는 일절
다루지 않는다.

## 구조

```
main.py                          # 제어 통합 진입점 (GUI + 조이스틱 + send_loop)
controller_main.py               # 헤드리스 (no-GUI) 진입점
config.yaml                      # 제어 측 설정
teleop_client/
├── client.py                    # TeleopClient (Zenoh, nev/teleop/{vid}/...)
├── config.py                    # 제어 측 설정 검증
├── send_loop.py                 # heartbeat + teleop + ping 루프
├── state.py                     # 공유 상태 (linear_x, steer_angle, estop, ...)
├── controller/
│   ├── base.py                  # Controller ABC
│   └── joystick.py              # JoystickController (pygame)
└── gui/
    ├── main_window.py           # E-STOP 배너 / READY 인디케이터 / 명령 바
    └── telemetry_panel.py       # AUTHORITY / ALERTS / MUX / NETWORK / ...
```

## 실행

GUI + 조이스틱 + 텔레메트리:

```bash
python3 main.py --config config.yaml
```

헤드리스 (조이스틱 → publish 만):

```bash
python3 controller_main.py --config config.yaml
```

헤드리스 모드는 GUI 가 없으므로 텔레메트리를 구독하지 않으며 (의도된 제약),
`cmd_mode` UI 도 없다. SIGINT/SIGTERM 은 종료 이벤트만 set 하고, 컨트롤러는
별도 스레드에서 동작한다.

teleop_server 직접 지정:

```bash
python3 main.py --server-tcp-locator "tcp/192.168.0.10:7447"
```

`stream_client` 는 별도 셸에서 띄운다. 두 클라가 서로 import 하지 않으므로
순서나 가용성은 무관하다.

## Wire contracts

토픽 suffix 상수 (`TOPIC_TELEOP`, `TOPIC_ESTOP`, …) 와 JSON envelope
(`{"v": 1, "ts": ..., ...payload}`) 직렬화는 모두 형제 패키지
`teleop_contracts` 의 `make_envelope` / `parse_envelope` 와 `TOPIC_*`
상수를 통해 일원화되어 있다. 토픽 이름이나 envelope 포맷을 바꿀 때는
`teleop_contracts` 한 곳만 손대면 된다.

## Zenoh 토픽 (모두 `nev/teleop/{vid}/...`)

| Pub/Sub | suffix                  | 주기 / 타입         | QoS         | 설명 |
|---------|-------------------------|---------------------|-------------|------|
| pub     | `client_heartbeat`      | 5 Hz                | best-effort | `{ts}` — GCS 프로세스 alive |
| pub     | `controller_heartbeat`  | 20 Hz               | reliable    | `{connected}` — 조이스틱 연결 상태 |
| pub     | `teleop`                | 20 Hz               | best-effort | `{linear_x, steer_angle}` |
| pub     | `estop`                 | 이벤트              | reliable    | `{active}` (실패 시 재시도, 아래 참조) |
| pub     | `cmd_mode`              | 이벤트              | reliable    | `{mode}` (GUI 전용) |
| pub     | `telemetry_ping`        | 1 Hz                | best-effort | RTT 핑 — server 통과 → 봇이 echo |
| sub     | `telemetry`             | 서버 → 클라         |             | 집계 JSON broadcast (멀티차량) |
| sub     | `telemetry_pong`        | 봇 → 클라           |             | 단일 cli↔bot RTT |

## GUI

상단 명령 바와 텔레메트리 패널로 구성된다.

- **E-STOP 배너** — 메인 영역 상단에 항상 표시되는 시각적 상태 머신.
  상태는 `NORMAL` (얇은 띠로 접힘) / `PENDING` (노랑, 발행 대기/재시도) /
  `CONFIRMED` (빨강, 봇이 ack) / `RETRY` (주황) / `CLEAR_PENDING` /
  `STALE_NOACK` (펄스, 2초 내 ack 없음). 트리거 출처(GUI / Joystick /
  Auto-on-disconnect)와 경과 시간을 함께 표시한다. **음향 없이 시각만** 사용.
- **READY / HOLD / UNSAFE 인디케이터** — 명령 바 좌상단의 큰 알약형 배지
  (~200×64). 모든 안전 조건의 집계 결과를 보여주고, 툴팁에 실패 중인
  서브컨디션 목록 (예: "Joystick disconnected", "Mode != REMOTE — input
  ignored") 을 나열한다.
- **AUTHORITY 카드** — 텔레메트리 패널 최상단. mux / twist 상관관계를 보고
  실제 누가 봇을 움직이고 있는지를 표시: `OPERATOR` (초록) / `AUTONOMOUS`
  (노랑) / `STOPPED` (회색) / `OVERRIDDEN` (주황).
- **ALERTS 카드** — AUTHORITY 바로 아래, MUX 위로 올라옴.
- **E-STOP 클릭 게이트** — 마지막 텔레메트리가 3초 이상 stale 이면 GUI
  E-STOP 버튼은 비활성화된다 (응답 없는 채널로 명령을 던지지 않기 위함).

## 안전

여러 장애 모드에서 봇이 자동으로 정지하도록 다층 방어가 들어 있다.

- **E-stop 자동 트리거** — `joystick.estop_on_disconnect: true` (기본 ON)
  이면 조이스틱이 물리적으로 빠질 때 자동으로 E-stop 을 발행한다.
- **E-stop 재시도** — 발행이 실패하면 `_estop_pending` 플래그가 세팅되고,
  send loop 가 매 틱마다 성공할 때까지 재시도한다. GUI 배너는 그 동안
  `PENDING` / `RETRY` 로 보인다.
- **Joystick freshness deadline** — 200ms 내에 신선한 축 읽기가 없으면
  `linear_x` / `steer_angle` 을 강제로 0 으로 잡는다 (스턱 입력 방지).
- **입력 검증** — `linear_x` / `steer_angle` 는 config 의 max 값으로 클램프
  되고, NaN / Inf 는 controller→state 경계에서 0 으로 거부된다.
- **세션 자동 재연결** — 연속 5회 publish 실패 시 zenoh 세션을 닫고 다시
  연다 (모든 publisher / subscriber 재선언). 재연결이 진행 중이거나
  E-stop 이 pending 인 동안에는 LINK 배지에 `session_unhealthy` 가
  표시된다.
- **Heartbeat QoS** — `controller_heartbeat` 는 RELIABLE 로 발행되어
  연결 상태 전이가 유실되지 않는다.

## config.yaml

```yaml
server_tcp_locator: "tcp/127.0.0.1:7447"
vehicle_id: "0"

heartbeat_rate: 5.0
teleop_rate: 20.0
ping_rate: 1.0

controller_type: joystick

joystick:
  axis_speed: 1
  axis_steer: 3
  btn_estop: 4              # E-stop 토글 버튼
  max_speed: 1.0            # m/s   — 명령 클램프 한계
  max_steer_deg: 27.0       #        — 명령 클램프 한계
  deadzone: 0.05
  invert_speed: true        # 일부 컨트롤러에서 전진이 음수
  estop_on_disconnect: true # 조이스틱 분리 시 자동 E-stop (기본 ON)
```

## 의존성

- eclipse-zenoh, pygame, PyYAML, PySide6 (`requirements.txt`)
- 형제 패키지 `teleop_contracts` (토픽 / envelope)
- ROS 미사용. 시스템 ROS 환경변수와 충돌하지 않도록 venv 권장.
- 헤드리스 박스에서 오디오 / 비디오 디바이스를 열지 않도록 pygame 초기화
  전에 `SDL_AUDIODRIVER=dummy`, `SDL_VIDEODRIVER=dummy` 가 자동 설정된다.

## stream_client 와의 관계

teleop_client 는 stream_client 의 어떤 모듈도 import 하지 않는다.
두 클라는 서로 다른 zenoh 서버(teleop_server 7447 vs stream_server
7457/7458) 에 붙어 토픽 prefix(`nev/teleop/...` vs `nev/stream/...`)
도 분리되어 있으므로, 한쪽이 죽어도 다른 쪽이 그대로 동작한다.
