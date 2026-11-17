# Tracker Studio

Windows에서 VIVE Tracker 3.0과 Lighthouse 2.0의 위치·회전을 libsurvive로 수집하는 로컬 웹 인터페이스입니다. SteamVR은 필요하지 않습니다.
Camera Studio도 같은 수집기(`survive_worker.py`)를 사용해 트래커를 녹화합니다.
이 화면은 **페어링**, **베이스스테이션 보정**, 트래커 단독 기록에 사용합니다.

## 실행

저장소 루트의 `Tracker Studio.cmd`를 실행하면 서버가 시작되고, 브라우저에서 <http://127.0.0.1:8765>가 열립니다.

> 트래커 장치는 한 프로세스만 열 수 있습니다. Camera Studio가 실행 중이면 먼저 사이드바의 **트래커** 줄을 클릭해 연결을 해제하세요.

1. 베이스스테이션과 트래커의 전원을 켭니다.
2. 왼쪽 장치 목록에서 트래커를 선택합니다.
3. 트래커 모형, 회전 축, 이동 궤적, 위치·회전값을 확인합니다.
4. 기록 이름을 입력하고 **기록 시작**을 누릅니다.
5. **기록 중지**를 누르면 `tracker_studio/recordings/`에 CSV와 JSON이 저장됩니다.

## 장치 연결과 보정

**트래커 페어링**을 누른 뒤 트래커 버튼을 약 2초 눌러 파란색 점멸로 바꿉니다. 초록색으로 바뀌면 센서를 가리지 말고, 베이스스테이션이 보이는 곳에 두세요.

USB 동글을 다시 꽂았거나 장치 전원을 껐다 켰다면 **장치 다시 검색**을 누르세요.
베이스스테이션 위치가 바뀌었다면 **보정 다시 시작**을 누른 뒤 트래커를 여러 위치에서 잠시씩 고정합니다.
보정 결과는 `tracker_studio/runtime/calibration.json`에 저장되며, Camera Studio도 이 보정을 사용합니다.

상태 표시는 다음과 같습니다.

- **추적 중**: 광학 신호를 최근에 받았습니다(0.25초 이내).
- **광학 신호 대기**: 관성(IMU) 예측값만 받고 있습니다.
- **수신 끊김**: 0.5초 넘게 좌표를 받지 못했습니다.

## 좌표와 기록 형식

- 위치는 libsurvive 보정 좌표계 기준이고 단위는 m, Z가 위쪽입니다. 원점과 바닥 높이는 보정 상태에 따라 정해집니다.
- 회전각은 오른손 좌표계의 `Rz(yaw) Ry(pitch) Rx(roll)` 분해이고 단위는 도입니다. 쿼터니언 순서는 `w, x, y, z`입니다.
- `utc_time`, `unix_time_s`는 서버가 받은 시각이고, `pose_time_s`는 libsurvive 원본 시계 값입니다.
- CSV에 들어 있는 항목: 시간, 트래커 일련번호, XYZ 위치, Roll·Pitch·Yaw, 원본 쿼터니언, 추적 상태, 광학 신호 경과 시간.
- 같은 이름의 JSON에는 라이브러리 버전, 좌표계, 보정 내용, 장치 정보가 들어 있습니다.

## 구성

- `server.py`: 로컬 HTTP API(`127.0.0.1` 전용), 장치 상태 관리, CSV 기록
- `survive_worker.py`: 별도 프로세스에서 libsurvive로 USB 데이터를 수집해 JSON lines로 출력
- `web/`: Three.js 3D 화면
- `vendor/libsurvive/`: 미리 빌드한 Windows용 libsurvive와 패치 파일([README](vendor/libsurvive/README.md))
- `build-info.json`: libsurvive 소스 커밋과 Windows 호환성 수정 내역
