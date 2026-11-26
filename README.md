# Robohand

수술 술기(절개·봉합)를 여러 카메라로 기록하는 연구 코드입니다. 기록에서 손과 기구의 3D 동작을 뽑고, MuJoCo 시뮬레이션에서 로봇 손 동작으로 옮깁니다.

| 단계 | 하는 일 | 문서 |
| --- | --- | --- |
| 캡처 | RealSense D405 × 3, D435 × 2, 4K 웹캠 × 4를 녹화 버튼 하나, PC 시계 하나로 함께 기록하고, ChArUco 보드 하나로 통합 보정합니다. 코드상으로는 MANUS 글러브와 VIVE 트래커도 기록합니다(Windows). | [robohand](robohand/README.md#camera-studio) |
| 트래커 | VIVE Tracker 페어링, 베이스스테이션 보정, 트래커 단독 기록(웹 UI, Windows) | [tracker_studio](tracker_studio/README.md) |
| 손 복원 | 녹화된 RGB 영상에서 양손 21개 키포인트를 보드 좌표계에 3D로 복원(macOS·Linux·Windows, CPU/GPU) | [robohand](robohand/README.md#다시점-손-복원-robohandrecon) |
| 시뮬레이션 | 보드 좌표계 그대로의 MuJoCo 장면: Shadow Hand / DG-5F-M, 니들홀더, 패드 | [robohand](robohand/README.md#시뮬레이션-mujoco-real2sim) |

기구 자세 추출과 로봇 손 리타겟은 아직 개발 중입니다.

## 빠른 시작

캡처 PC (Windows 10/11, Python 3.13):

```powershell
git clone https://github.com/Ever2after/robohand.git
cd robohand
.\setup.cmd                 # .venv를 만들고 requirements.txt를 설치합니다
"Camera Studio.cmd"         # 또는 "Depth Replay 3D.cmd", "Tracker Studio.cmd"
```

분석과 시뮬레이션 (macOS·Linux·Windows):

```bash
pip install -r requirements-recon.txt -r requirements-sim.txt
python -m robohand.recon.hands2d <세션 폴더>      # 이어지는 단계는 robohand/README.md
python -m robohand.sim.scene --render workspace.png
```

## 데이터

- 녹화, 보정, 설정, 로그는 `DATA_ROOT` 아래에 저장됩니다. 기본값은 `<repo>/data/`이고 git에는 올라가지 않습니다. 다른 위치를 쓰려면 `ROBOHAND_DATA` 환경 변수를 지정합니다.
- 공개 녹화는 Hugging Face 데이터셋 [Ever2after/robohand-data](https://huggingface.co/datasets/Ever2after/robohand-data)에 있습니다. 세션, 보정, 기구·패드 사진(`tool_images/`, `sam3d/`)이 들어 있습니다.
- 모든 3D 결과와 시뮬레이션은 같은 보드 좌표계를 씁니다(X 보드 열 방향, Y 행 반대 방향, Z 위쪽, 단위 m).

## 저장소 구성

```
robohand/            # Python 패키지: 캡처, 보정, 재생, recon/(손 복원), sim/(MuJoCo)   -> robohand/README.md
tracker_studio/      # VIVE 트래커 웹 UI와 미리 빌드한 libsurvive                     -> tracker_studio/README.md
*.cmd, setup.cmd     # Windows 실행 파일
requirements*.txt    # 캡처(Windows) / recon / sim 의존성
```

서드파티 구성 요소와 라이선스는 각 README의 "서드파티" 절에 있습니다.
