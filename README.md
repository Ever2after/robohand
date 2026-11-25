# Robohand

RealSense D405 × 3, D435 × 2, 4K UVC 웹캠 × 4(그리고 코드상으로는 MANUS 글러브, VIVE Tracker)를 **하나의 녹화 버튼과 하나의 시계**로 함께 기록하는 Windows용 손 동작 캡처 도구입니다.

| 도구 | 실행 | 하는 일 |
| --- | --- | --- |
| **Camera Studio** | `Camera Studio.cmd` | D405 / D435 / 4K RGB 미리보기(탭 전환)와 동시 녹화, D405 외부 보정 |
| **Depth Replay 3D** | `Depth Replay 3D.cmd` | 녹화 재생. 세 카메라의 포인트클라우드를 보드 좌표계에 겹치고, 글러브 3D 손 골격을 함께 표시 |
| **Tracker Studio** | `Tracker Studio.cmd` | VIVE Tracker 페어링, 베이스스테이션 보정, 트래커 단독 기록(웹 UI). [자세히](tracker_studio/README.md) |

## 준비물

- Windows 10/11 x64, Python 3.13 (64-bit)
- Intel RealSense D405 × 3. USB 3 연결을 권장하며, USB 2로 연결하면 15 fps로 동작합니다.
- 선택: RealSense D435(i) × 2, UVC MJPEG 웹캠 × 4 (OBSBOT Tiny 2 Lite로 확인). 4K 웹캠 1대는 약 150 Mbps라 USB 2 허브 하나에 여러 대를 꽂으면 fps가 떨어집니다.
- 선택: MANUS Metaglove Pro와 [MANUS Core 3.2.1](https://www.manus-meta.com/). Core가 실행 중이어야 하며, Core에 들어 있는 `ManusSDK.dll`을 사용합니다.
- 선택: VIVE Tracker 3.0과 Lighthouse 2.0 베이스스테이션. libsurvive를 쓰므로 SteamVR은 필요 없습니다.
- 보정용 ChArUco 보드: 7×5, DICT_6X6_250, 사각형 25 mm, 마커 18 mm 기준

## 설치

```powershell
git clone https://github.com/Ever2after/robohand.git
cd robohand
.\setup.cmd        # .venv를 만들고 requirements.txt를 설치합니다
```

설치가 끝나면 `Camera Studio.cmd`를 실행하세요. 다른 카메라 앱은 먼저 종료해야 합니다.

### 데이터 폴더

코드와 데이터는 분리되어 있습니다. 녹화, 스냅샷, 보정 결과, 설정, 로그는 모두 `DATA_ROOT` 아래에 저장됩니다.
`DATA_ROOT`의 기본값은 `<repo>/data/`이며 git에는 올라가지 않습니다.
다른 위치를 쓰려면 환경 변수를 지정하세요.

```powershell
setx ROBOHAND_DATA "D:\robohand-data"
```

```
DATA_ROOT/
├─ settings.json          # 카메라 일련번호 배치, 깊이 범위, UI 선택
├─ calibration.json       # 현재 적용 중인 카메라 외부 보정
├─ recordings/<세션>/      # 녹화 세션
├─ captures/<시각>/        # 스냅샷과 프레임 검증 결과
├─ calibrations/<시각>/    # 보정 촬영 원본, 오버레이, 이전 보정 백업
└─ logs/                  # camera_studio.log, depth_player.log ...
```

MANUS SDK가 기본 경로(`C:\Program Files\MANUS\Core 3\ManusCore`)에 없으면 `MANUS_SDK_DIR` 환경 변수로 지정합니다.

## Camera Studio

- 열마다 RGB-D 1·2·3이 배치되고, 위는 컬러, 아래는 컬러에 정렬된 깊이 영상입니다.
  - **D405는 USB 3에서 최대 해상도로 동작합니다.** 컬러와 깊이 모두 1280×720 · 30 fps입니다(`hub.py`의 `D405_PROFILE`).
  - USB 2로 잡히면 640×480 · 15 fps로 자동으로 낮춥니다.
- 영상을 클릭하면 그 지점의 깊이(5×5 유효 픽셀 중앙값, 광축 방향 거리)를 표시합니다. 더블클릭은 확대/복귀, F11은 전체 화면입니다.
- **장치·깊이** 탭에서 카메라를 일련번호로 슬롯에 배치하고, 카메라별 깊이 표시 범위를 정합니다.
- **스냅샷 저장**(Ctrl+S)은 모든 카메라의 컬러, 깊이 원본(`.npy`/16비트 PNG), 메타데이터를 `captures/`에 저장합니다.
- LIVE MONITOR 위의 **D405 × 3 / D435 × 2 / 4K RGB × 4** 버튼으로 카메라 묶음을 전환합니다. D435와 4K RGB는 화면에 보이거나 **녹화 탭에서 선택된** 동안만 켜집니다.
- 4K RGB(UVC 웹캠)는 PyAV로 DirectShow MJPEG를 그대로 받습니다. 탭의 **웹캠 캡처**에서 4K · 1080p · 720p(모두 30 fps)를 고르며, 화면에는 960 px · 10 fps로 줄여 표시합니다. OBSBOT Center가 켜져 있으면 웹캠 영상이 들어오지 않습니다.
- **스냅샷 저장**은 켜져 있는 D435(컬러 PNG, 깊이 `.npy`)와 웹캠(카메라 원본 JPEG)도 함께 저장합니다.
- MANUS 글러브와 VIVE 트래커 기능(`devices/`, 녹화 source, 3D 재생)은 코드에 남아 있지만 지금은 대시보드에서 숨겨져 있습니다. `robohand/ui/studio.py`의 `GLOVE_TRACKER_UI = True`로 되돌립니다.
- **D435는 USB 3에서 최대 해상도로 동작합니다.** 깊이 센서 1280×720을 컬러 1920×1080에 정렬하며 30 fps입니다(`extra.py`의 `D435_PROFILE`).
  - 미리보기, 녹화 트랙, 3D 재생 모두 1920×1080입니다.
  - USB 2로 잡히면 640×480 · 15 fps로 자동으로 낮춥니다.
  - 1080p D435 두 대는 깊이와 컬러를 합쳐 약 2.9 Gbps를 씁니다. 이 정도 대역폭에서는 D435를 D405와 같은 USB 컨트롤러에 두지 말고, 허브를 거치지 말고 직접 연결하세요. 대시보드에서 끄려면 `studio.py`의 `D435_UI = False`로 바꿉니다.
  - D435 한 대 1080p 녹화는 깊이 원본(FFV1)이 약 7 MB/s입니다.

### 녹화

**녹화** 탭에서 저장할 스트림(D405 1–3, D435 1–2의 컬러·깊이, 4K RGB 1–4)을 고른 뒤 **녹화 시작**(Ctrl+R)을 누릅니다.
선택한 카메라에 실시간 영상이 없으면 녹화는 시작되지 않고 이유가 표시됩니다.

**녹화는 항상 카메라 최대 설정으로 저장됩니다.**
- 해상도: D405 1280×720, D435 컬러 1920×1080·깊이 1280×720, 웹캠은 선택한 캡처 해상도이며 모두 30 fps입니다.
- 대시보드 미리보기는 녹화를 방해하지 않도록 줄여서 보여 줍니다.
  - RealSense는 640 px로 줄인 영상입니다.
  - **녹화 중에는 모든 미리보기가 초당 1장**으로 바뀝니다.
- **RealSense 카메라는 대당 별도 프로세스에서 동작합니다**(`cameras/rs_process.py`).
  - 각 프로세스가 수신, 깊이 정렬, 녹화 인코딩을 직접 합니다.
  - 대시보드에는 미리보기만 보냅니다. 스냅샷과 보정 검증에는 원본 해상도 프레임을 받아 옵니다.
  - 수신 스레드와 처리 스레드가 나뉘어 있어서, 처리가 잠깐 밀려도 프레임을 버리지 않습니다.
  - 녹화 프레임 `i`에는 `i / 30`초에 가장 가까운 시각에 촬영된 카메라 프레임이 들어갑니다.
- H.264는 가능하면 내장 GPU 인코더(AMD AMF)를 쓰고, 없으면 x264를 씁니다. 인코더는 우선순위를 낮춰 실행합니다.
- 9대(D405 ×3, D435 ×2, 4K 웹캠 ×4)를 동시에 녹화하면 약 130 MB/s(분당 약 8 GB)를 저장하며 CPU는 약 40%를 씁니다. 이때 반복 프레임은 0입니다(Ryzen 9 7900X에서 측정).

| 파일 | 내용 |
| --- | --- |
| `d405*_color.mp4`, `d435_*_color.mp4` | 컬러, 카메라 해상도, H.264, 30 fps 시간축. 프레임이 빠진 곳은 직전 프레임을 반복합니다. |
| `*_depth_raw.mkv` | 깊이 트랙의 유일한 파일(색상 깊이 영상은 저장하지 않음). 컬러에 정렬된 uint16 깊이, 컬러와 같은 해상도, FFV1 무손실. 미터 = 값 × `depth_scale_m`(D405 0.1 mm, D435 1 mm). 0과 65535는 무효값입니다. 크기는 `session.json`의 `raw_depth_resolution`에 있습니다. |
| `*_timestamps.csv` (RGB-D) | 출력 프레임별 시각, 원본 프레임 번호, 반복·미수신 여부 |
| `rgb4k_N.mkv` | 웹캠 MJPEG를 재인코딩 없이 그대로 담은 MKV(가변 프레임, pts = `t_host_s` ms). 4K는 약 19 MB/s, 1080p는 약 10 MB/s |
| `rgb4k_N_timestamps.csv` | 웹캠 프레임별 `t_host_s`(도착 시각), `t_device_s`(DirectShow 샘플 시각, 같은 시계 · 약 1.0 s 고정 오프셋), 바이트 수 |
| `manus.jsonl` | MANUS raw(IMU와 센서 5개), ergonomics(관절 40개, 도), skeleton(25노드, m). 약 66 Hz |
| `vive.jsonl` | 트래커 포즈(위치 m, 쿼터니언 wxyz, 광학/관성 상태). 약 250 Hz |
| `session.json` | 장치, 내부 파라미터, 깊이 스케일, 트랙별 통계, `webcams`(·`manus`/`vive`) 요약, 경고 |
| `calibration.json` | 녹화 시작 시점에 적용 중이던 외부 보정의 사본. `session.json`의 `calibration`에 요약이 들어갑니다. |

**시간 동기화:** 모든 RGB-D 영상 프레임, 웹캠 프레임(`rgb4k_N_timestamps.csv`), `manus.jsonl`·`vive.jsonl`의 `t_host_s`는 같은 PC의 monotonic 시계 기준이며, 세션 시작(`session.json`의 `start_monotonic_s`)부터 잰 초입니다.
RGB-D 영상 프레임 `i`의 시각은 `i / 30`초이고, 웹캠은 프레임마다 시각이 기록됩니다. 장치 간 하드웨어 동기화는 아닙니다.
원본 장치 시계도 함께 저장합니다. MANUS는 `publish_utc_ms`(UTC, 1 ms), libsurvive는 `pose_time_s`입니다.

```python
from robohand.devices.manus import load_manus
from robohand.devices.vive import load_vive

session = "data/recordings/20261116_230529_191799"
gloves = load_manus(session)       # gloves["skeleton"][glove_id]["nodes"]  -> (N, 25, 8)
trackers = load_vive(session)      # trackers["trackers"]["T20"]["position"] -> (N, 3)
```

```python
import cv2   # 원본 깊이 프레임 하나 읽기
cap = cv2.VideoCapture(f"{session}/d405_depth_raw.mkv", cv2.CAP_FFMPEG, [cv2.CAP_PROP_CONVERT_RGB, 0])
cap.set(cv2.CAP_PROP_POS_FRAMES, 100)
ok, depth_u16 = cap.read()         # uint16 (720, 1280); 2026-11-20 이전 녹화는 (480, 640)
```

**MANUS 주의:** MANUS 대시보드에서 녹화를 마치면 대시보드가 녹화 *재생 모드*로 바뀝니다. 이 상태에서 Core는 실시간 대신 재생 프레임만 보냅니다.
글러브 줄에 "재생 모드"가 보이면 대시보드에서 재생을 해제하세요.
SDK로 받는 스트림은 약 66 Hz이고, 글러브 자체 샘플링은 약 110 Hz입니다.

**VIVE 주의:** 트래커 장치는 한 프로세스만 열 수 있습니다. 페어링이나 베이스스테이션 보정을 할 때는 먼저 **트래커** 줄을 클릭해 연결을 해제하세요. 그런 다음 Tracker Studio에서 작업합니다.

### 카메라 외부 보정

**보정·검증** 탭의 기능입니다.

- **외부 보정 실행**
  1. 인쇄한 보드의 사각형 한 변(mm)을 자로 재서 입력합니다. 마커 크기는 25 : 18 비율로 자동 계산됩니다.
  2. 실행하면 실시간 연결이 잠시 끊기고, 카메라마다 1280×720 영상을 16장 촬영합니다.
  3. 공장 내부 보정값은 고정하고, ChArUco 코너로 카메라마다 `T_board_from_camera`를 구합니다.
  4. 결과는 `calibration.json`에 적용되고, 이전 파일은 `calibrations/<시각>/`에 백업됩니다.
  5. 촬영한 프레임 중 절반은 계산에서 빼고 검증(재투영 오차, depth 평면 오차)에 씁니다.
  6. **Depth 기준 스케일 추가 보정**을 켜면, 입력한 크기 대신 depth 평면 오차로 역산한 유효 크기를 적용합니다.
- **프레임 검증 / 3D 보기:** 현재 프레임으로 재투영·depth 오차를 다시 계산하고, 포인트클라우드와 가상 보드를 겹쳐 봅니다.
- **보드 좌표계:** 단위는 m입니다. X는 체커 열 방향, Y는 행 반대 방향, Z는 보드 위쪽입니다. 카메라를 옮기면 보정을 다시 해야 합니다.

#### 통합 보정 (D405 + D435 + 4K RGB)

모든 카메라를 같은 보드 좌표계에 놓습니다. **D435 · 4K RGB도 함께 보정**을 켜고 **외부 보정 실행**을 누르면,
D405 다음에 D435와 웹캠을 한 대씩 열어 같은 정지 보드를 16장씩 찍습니다(한 대씩 열어서 USB 대역폭을 나눠 쓰지 않습니다).

- **RealSense(D405, D435):** 공장 내부 보정값을 그대로 씁니다. D435는 1280×720에서 지원되는 가장 낮은 fps로 촬영하며, 깊이 평면 검증도 합니다.
- **4K RGB 웹캠:** 공장 내부 보정값이 없으므로 먼저 **4K RGB 렌즈(내부) 보정**을 웹캠마다 한 번 해야 합니다.
  1. 웹캠을 고르고 **렌즈 보정 시작**을 누릅니다.
  2. 보드를 손에 들고 화면 여기저기, 여러 각도로 옮기며 잠깐씩 멈춥니다. 멈춰 있고 이전과 다른 자세일 때만 자동으로 찍습니다.
  3. 20장이 모이면(또는 **중지하고 계산**) 렌즈 모델(fx, fy, 주점, 왜곡 5계수)을 풀어 `DATA_ROOT/webcam_intrinsics.json`에 저장합니다.
  - 줌과 자동 추적은 끄고 초점은 고정하세요. 바뀌면 다시 보정해야 합니다. 다른 캡처 해상도(1080p 등)는 같은 화각이면 자동으로 환산합니다.
  - 웹캠은 일련번호가 없어 슬롯(USB 경로 순서)으로 구분합니다. 다른 포트로 옮겨 꽂으면 슬롯이 바뀔 수 있으며, 경로가 바뀌면 경고를 표시합니다.
- 보드가 안 보이거나 렌즈 보정이 없는 D435·웹캠은 건너뛰고 `calibration.json`의 `failed_cameras`에 이유를 남깁니다.
- D405는 배치돼 있고 연결돼 있는 것만 촬영합니다. 연결된 D405가 하나도 없으면 D435·웹캠만으로 보정합니다(이때 D405 기준 Depth 스케일 보정은 건너뜁니다). 배치된 D405가 보드를 못 찾으면 보정은 실패합니다.
- **실시간 Real / Sim 오버레이**는 D435와 웹캠 화면에도 보드 모델을 겹쳐 보여 줍니다. 3D 재생은 D435 깊이도 보드 좌표계에 겹칩니다.
- `calibration.json`(format 2)의 카메라 항목: `T_board_from_camera`, `T_camera_from_board`, `intrinsics`, `kind`(`realsense`/`webcam`), `serial` 또는 `device_path`, 검증 오차. `relative_transforms`는 보정된 모든 카메라 쌍을 담습니다.

## Depth Replay 3D

- 최근 녹화(또는 지정한 세션)를 엽니다. 카메라 일련번호가 보정 파일에 있으면 **보드 좌표계에 겹쳐서** 표시합니다.
  내부 파라미터는 `session.json`에 기록된 값을 씁니다. 외부 보정은 녹화 당시 것을 아래 순서로 찾습니다. 어떤 보정을 썼는지는 화면 아래 설명에 표시됩니다.
  1. 녹화 폴더의 `calibration.json` (녹화 시작 때 저장된 사본)
  2. 사본이 없는 예전 녹화: `calibrations/`에서 녹화 시작 직전에 만든 보정
  3. 둘 다 없으면 현재 `calibration.json` (경고 표시)
- 상단의 **RGB-D 1·2·3** 체크박스로 카메라를 켜고 끕니다. 점 색상은 실제 컬러, 깊이 색상, 카메라별 색상 중에서 고릅니다.
- `manus.jsonl`이 있으면 오른쪽 패널에 글러브 손 골격이 영상 시각에 맞춰 함께 재생됩니다.
  아직 손목 트래커를 연동하지 않아서, 각 손은 자기 손목을 기준으로 그려집니다.
- **PLY 저장**은 표시 중인 카메라를 합쳐 보드 좌표계로 내보냅니다.
- Space는 재생, ←/→는 한 프레임 이동입니다. 마우스 왼쪽 드래그로 회전, 가운데 드래그로 이동, 휠로 확대합니다.

## 명령줄

```powershell
.venv\Scripts\python -m robohand.ui.studio                        # Camera Studio
.venv\Scripts\python -m robohand.replay.player [세션 폴더]          # Depth Replay 3D
.venv\Scripts\python -m robohand.calibration.viewer <스냅샷 폴더>   # Real / Sim 3D 검증
.venv\Scripts\python -m robohand.calibration.capture <폴더>        # 보정 영상 촬영만
.venv\Scripts\python -m robohand.calibration.board <폴더>/highres --square-mm 25
.venv\Scripts\python -m robohand.calibration.scale --apply         # depth 기준 보드 스케일 피팅
.venv\Scripts\python -m robohand.tools.verify_system --seconds 5   # 카메라 녹화·디코딩 종단 점검
```

## 구조

```
robohand/
├─ paths.py, settings.py      # 데이터 폴더와 설정
├─ cameras/                   # d405.py(카메라 1대), hub.py(3대 연결), recorder.py(동시 녹화)
├─ calibration/               # board.py(ChArUco 보정/검증), capture.py, scale.py, viewer.py
├─ replay/                    # reconstruction.py(깊이→포인트클라우드), player.py(Depth Replay 3D)
├─ devices/                   # manus.py(MANUS SDK), vive.py(libsurvive 수집기), jsonl.py
├─ ui/                        # studio.py(Camera Studio), widgets.py, theme.py
└─ tools/verify_system.py
tracker_studio/               # VIVE 웹 UI, survive_worker.py, 미리 빌드한 libsurvive
```

새 센서를 녹화에 추가하려면 `status()["live"]`, `begin(folder, start_monotonic)`, `end() -> summary`를 구현하세요.
그리고 `Recorder.start(..., sources={"이름": 장치})`로 넘기면 됩니다.

## 알려진 한계

- 카메라, 글러브, 트래커 사이는 하드웨어 동기화가 아니라 PC 시계 기준입니다. 각 장치에서 PC까지 오는 전송 지연은 보정하지 않았습니다.
- VIVE 좌표(Lighthouse)와 카메라 보드 좌표계 사이의 정렬, 트래커와 손목 사이의 정렬은 아직 구현하지 않았습니다.

## 서드파티

- [librealsense](https://github.com/IntelRealSense/librealsense) (Apache-2.0), OpenCV, PySide6/Qt (LGPL), pyqtgraph, FFmpeg(imageio-ffmpeg)
- [libsurvive](https://github.com/collabora/libsurvive) (MIT): 수정한 Windows 빌드를 포함합니다. [빌드 안내](tracker_studio/vendor/libsurvive/README.md)
- llvm-mingw 런타임 `libc++.dll`, `libunwind.dll` (Apache-2.0 WITH LLVM-exception)
- [three.js](https://github.com/mrdoob/three.js) (MIT), [Inter](https://github.com/rsms/inter) 글꼴 (SIL OFL 1.1)
- MANUS SDK는 이 저장소에 포함되어 있지 않으며, 설치된 MANUS Core의 DLL을 사용합니다.
