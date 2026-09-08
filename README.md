# teleop_client

> `native_v2/` is the new Qt/GStreamer client. The existing Python/Zenoh UI is
> retained as a legacy reference during migration.

NEV 텔레오프 시스템의 운전자측 (operator station) 통합 클라이언트
조이스틱으로 차량을 원격제어하고 텔레메트리·영상을 한 GUI 창에서 본다

## 설치

```bash
cd ~/teleop_client
git submodule update --init --recursive

uv venv --system-site-packages
source .venv/bin/activate
uv pip install -e .
```

## 실행

```bash
source .venv/bin/activate
teleop-ui
```

옵션 (필요할 때만):
```bash
teleop-ui --cameras front,rear     # 카메라 override
teleop-ui -v                        # DEBUG 로그
teleop-ui --teleop-config teleop_client/config.yaml \
          --stream-config stream_client/config.yaml
```

## 디렉토리

```
teleop_client/                    # repo root (= 단일 pyproject)
├── pyproject.toml
├── teleop_ui/                    # 통합 엔트리 (한 윈도우)
│   └── teleop_ui/main.py
├── teleop_client/                # 텔레메트리·제어 모듈
│   ├── teleop_client/            (controller/, gui/, client.py, send_loop.py …)
│   ├── config.yaml
│   ├── controller_main.py        # 헤드리스 joystick-only 디버그용
│   └── teleop_contracts/         # git submodule
└── stream_client/                # 영상 모듈
    ├── stream_client/            (gui/, client.py, gstreamer_tcp.py …)
    ├── config.yaml
    ├── viewer_h265_tcp.py        # 빠른 영상-only 뷰어
    └── test/
```

세 Python 패키지(`teleop_client` / `stream_client` / `teleop_ui`)는 모두
top-level `pyproject.toml` 하나로 같이 install 되며, 진짜 엔트리는
`teleop-ui` 하나. 디버깅용 보조 entry point 2개 (`controller_main.py`,
`viewer_h265_tcp.py`) 는 직접 실행.

## 와이어 컨트랙트

[`teleop_contracts`](https://github.com/nevlife/teleop_contracts) 를 `teleop_client/teleop_contracts/` 에 git submodule 로 pin
