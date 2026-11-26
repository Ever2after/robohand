"""A recorded session for offline reconstruction: calibrated cameras in the board frame, the
capture time of every stored frame, and frame decoding.

Times are seconds on the shared host monotonic clock since ``start_monotonic_s``. They are
arrival times, so each camera carries an unknown transport delay. ``hands3d`` estimates a
constant per-camera offset on top of these times.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

# 2026-11-26 takes: d405_2 ran on USB 2.1 at 640x480 / 15 fps, half its frames repeated.
EXCLUDED = ("d405_2",)


@dataclass
class Camera:
    name: str
    kind: str                      # "realsense" or "webcam"
    width: int
    height: int
    K: np.ndarray                  # 3x3, pixels
    dist: np.ndarray               # OpenCV distortion coefficients, as the extrinsic fit used them
    T_cam_board: np.ndarray        # 4x4, board -> camera
    video: Path
    frames: np.ndarray             # video frame index of every distinct stored frame
    times: np.ndarray              # capture time of those frames, seconds since session start
    offset: float = 0.0            # estimated delay to subtract from ``times``
    meta: dict = field(default_factory=dict)

    @property
    def R(self) -> np.ndarray:
        return self.T_cam_board[:3, :3]

    @property
    def t(self) -> np.ndarray:
        return self.T_cam_board[:3, 3]

    @property
    def center(self) -> np.ndarray:
        return -self.R.T @ self.t

    @property
    def focal(self) -> float:
        return float((self.K[0, 0] + self.K[1, 1]) / 2)

    def to_camera(self, X: np.ndarray) -> np.ndarray:
        return np.asarray(X, float) @ self.R.T + self.t

    def project(self, X: np.ndarray) -> np.ndarray:
        """Board-frame points (..., 3) -> distorted pixels (..., 2)."""
        X = np.asarray(X, float)
        flat = X.reshape(-1, 3)
        uv = cv2.projectPoints(flat, cv2.Rodrigues(self.R)[0], self.t, self.K, self.dist)[0]
        return uv.reshape(X.shape[:-1] + (2,))

    def normalize(self, uv: np.ndarray) -> np.ndarray:
        """Distorted pixels (..., 2) -> undistorted normalized image coordinates (..., 2)."""
        uv = np.asarray(uv, float)
        flat = np.ascontiguousarray(uv.reshape(-1, 1, 2))
        out = cv2.undistortPoints(flat, self.K, self.dist,
                                  criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 20, 1e-9))
        return out.reshape(uv.shape)

    def frames_at(self, times: np.ndarray) -> np.ndarray:
        """Nearest stored frame (position in ``frames``) for each corrected time."""
        corrected = self.times - self.offset
        index = np.clip(np.searchsorted(corrected, times), 1, len(corrected) - 1)
        before = times - corrected[index - 1] < corrected[index] - times
        return np.where(before, index - 1, index)


def _realsense_times(path: Path, start: float):
    frames, times, last = [], [], None
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            source = row["source_frame"]
            if row["repeated_frame"] == "1" or row["source_unavailable"] == "1" or source == last:
                continue
            last = source
            frames.append(int(row["output_frame"]))
            times.append(float(row["source_received_monotonic_s"]) - start)
    return np.asarray(frames), np.asarray(times)


def _webcam_times(path: Path):
    """Device sample times moved onto the host clock: steadier than arrival times."""
    rows = list(csv.DictReader(open(path, newline="")))
    host = np.array([float(r["t_host_s"]) for r in rows])
    device = np.array([float(r["t_device_s"]) for r in rows])
    return np.array([int(r["frame"]) for r in rows]), device + np.median(host - device)


class Session:
    def __init__(self, folder: str | Path, exclude=EXCLUDED):
        self.folder = Path(folder)
        self.info = json.loads((self.folder / "session.json").read_text())
        self.calibration = json.loads((self.folder / "calibration.json").read_text())
        start = self.info["start_monotonic_s"]
        self.cameras: dict[str, Camera] = {}
        for name, entry in self.calibration["cameras"].items():
            if name in exclude:
                continue
            if entry["kind"] == "realsense":
                video = self.folder / f"{name}_color.mp4"
                stamps = self.folder / f"{name}_color_timestamps.csv"
                if not video.exists() or not stamps.exists():
                    continue
                # Recording resolution can differ from the calibration capture (D435: 1080p vs 720p);
                # session.json has the intrinsics of the stream that was recorded.
                intr = self.info["devices"][name]["color_intrinsics"]
                frames, times = _realsense_times(stamps, start)
            else:
                video = self.folder / f"{name}.mkv"
                stamps = self.folder / f"{name}_timestamps.csv"
                if not video.exists() or not stamps.exists():
                    continue
                intr = entry["intrinsics"]
                frames, times = _webcam_times(stamps)
            K = np.array([[intr["fx"], 0, intr["ppx"]], [0, intr["fy"], intr["ppy"]], [0, 0, 1]], float)
            self.cameras[name] = Camera(
                name=name, kind=entry["kind"], width=int(intr["width"]), height=int(intr["height"]),
                K=K, dist=np.asarray(intr["coeffs"], float),
                T_cam_board=np.asarray(entry["T_camera_from_board"], float),
                video=video, frames=frames, times=times,
                meta={"heldout_error_px": entry.get("heldout_error_px", {}).get("rms")})
        self.duration = float(self.info.get("duration_s") or max(c.times[-1] for c in self.cameras.values()))

    @property
    def recon(self) -> Path:
        path = self.folder / "recon"
        path.mkdir(exist_ok=True)
        return path

    def workspace_box(self, margin=0.05, height=0.25) -> np.ndarray:
        """Corners of a box over the board where the hands work, board frame."""
        board = self.calibration["board"]
        size_x, size_y = board["size_m"]
        xs, ys, zs = (-margin, size_x + margin), (-size_y - margin, margin), (0.0, height)
        return np.array([[x, y, z] for x in xs for y in ys for z in zs])


def iter_frames(camera: Camera, wanted=None):
    """Yield (video frame index, BGR image) in order; ``wanted`` limits which frames are converted."""
    import av   # imported here: cv2 and av both bundle libavdevice; keep av out of cv2-only users

    wanted = None if wanted is None else set(int(i) for i in wanted)
    last = None if wanted is None else max(wanted)
    with av.open(str(camera.video)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for index, frame in enumerate(container.decode(stream)):
            if last is not None and index > last:
                break
            if wanted is None or index in wanted:
                yield index, frame.to_ndarray(format="bgr24")
