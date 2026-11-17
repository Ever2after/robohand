"""Metric depth deprojection, or explicitly approximate legacy palette inversion."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
from pathlib import Path
import threading

import cv2
import numpy as np
import pyrealsense2 as rs

from ..cameras.d405 import LUT
from ..paths import RECORDINGS
from ..settings import read_settings


def latest_recording():
    roots = {RECORDINGS, Path(read_settings().get("recording_directory", RECORDINGS))}
    candidates = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*/session.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") == "complete" and any(key.endswith("_depth") for key in data.get("tracks", {})):
                    candidates.append((data.get("started_at", ""), path.parent))
            except (OSError, ValueError):
                continue
    if not candidates:
        raise FileNotFoundError("D405 깊이 영상이 포함된 완료된 녹화를 찾을 수 없습니다.")
    return max(candidates, key=lambda item: item[0])[1]


def camera_rays(intrinsics):
    """Use the SDK's calibrated inverse distortion, with axial depth=1 meter."""
    intr = rs.intrinsics()
    for key in ("width", "height", "fx", "fy", "ppx", "ppy"):
        setattr(intr, key, intrinsics[key])
    model_name = intrinsics.get("model", "distortion.none").split(".")[-1]
    if model_name == "modified_brown_conrady":
        raise ValueError("이 보정 모델은 직접 역투영을 지원하지 않습니다.")
    intr.model = getattr(rs.distortion, model_name)
    intr.coeffs = intrinsics.get("coeffs", [0.0]*5)
    points = [rs.rs2_deproject_pixel_to_point(intr, [x,y], 1.0)
              for y in range(intr.height) for x in range(intr.width)]
    return np.asarray(points, dtype=np.float32).reshape(intr.height, intr.width, 3)


class PaletteInverse:
    def __init__(self):
        self.anchors = LUT[::51].astype(np.float32)
        self.palette = LUT.astype(np.float32)

    def decode(self, rgb, near, far, clean=True):
        if not 0 < near < far:
            raise ValueError("깊이 범위는 0 < 가까움 < 멀어짐 순서로 입력하세요.")
        # Original invalid pixels are (9,17,26); reject their compression halos too.
        mask = (np.max(rgb, axis=2) > 55) & (np.ptp(rgb.astype(np.int16), axis=2) > 30)
        positions = np.flatnonzero(mask)
        depth = np.zeros(rgb.shape[:2], dtype=np.float32)
        if not len(positions):
            return depth
        query = np.ascontiguousarray(rgb.reshape(-1,3)[positions], dtype=np.float32)
        # Project onto the five known palette segments, then refine the nearest
        # integer palette entry. This avoids hundreds of thousands of tree queries.
        best_error=np.full(len(query),np.inf,np.float32)
        continuous=np.zeros(len(query),np.float32)
        for segment in range(5):
            direction=self.anchors[segment+1]-self.anchors[segment]
            relative=query-self.anchors[segment]
            norm=float(np.dot(direction,direction))
            dot=np.sum(relative*direction,axis=1)
            t=np.clip(dot/norm,0,1)
            error=np.sum(relative*relative,axis=1)-2*t*dot+t*t*norm
            better=error<best_error
            continuous[better]=(segment+t[better])*51
            best_error[better]=error[better]
        rounded=np.rint(continuous).astype(np.int32)
        best_error.fill(np.inf)
        indices=rounded.copy()
        for delta in [-1,0,1]:
            candidate=np.clip(rounded+delta,0,255)
            difference=query-self.palette[candidate]
            error=np.sum(difference*difference,axis=1)
            better=error<best_error
            indices[better]=candidate[better]
            best_error[better]=error[better]
        keep = best_error <= 26**2
        indices = indices[keep]
        depth.ravel()[positions[keep]] = near + np.minimum((indices+.5)/255, 1)*(far-near)
        if clean:
            valid = cv2.erode((depth>0).astype(np.uint8), np.ones((3,3),np.uint8))
            depth[valid==0] = 0
        return depth


@dataclass
class CloudFrame:
    index: int
    points: np.ndarray   # RealSense camera coordinates: X right, Y down, Z forward (meters).
    rgb_colors: np.ndarray
    depth_colors: np.ndarray
    color_image: np.ndarray
    depth_image: np.ndarray
    approximate: bool
    min_z: float
    max_z: float


class DepthSequence:
    def __init__(self, folder, camera_key=None):
        self.folder = Path(folder)
        self.manifest = json.loads((self.folder/"session.json").read_text(encoding="utf-8"))
        if self.manifest.get("status") != "complete":
            raise ValueError("녹화가 완료된 세션을 선택하세요.")
        self.camera_keys = [key.rsplit("_", 1)[0] for key in self.manifest.get("tracks", {}) if key.endswith("_depth")]
        if not self.camera_keys:
            raise ValueError("이 녹화에는 D405 깊이 영상이 없습니다.")
        self.camera_key = camera_key or self.camera_keys[0]
        self.track = self.manifest["tracks"].get(f"{self.camera_key}_depth")
        if not self.track:
            raise ValueError("선택한 카메라의 깊이 영상이 없습니다.")
        self.fps = float(self.track.get("fps", self.manifest.get("output_fps",30)))
        self.count = int(self.track["frames"])
        self.info = self.manifest["devices"][self.camera_key]
        self.scale = float(self.info["depth_scale_m"])
        self.intrinsics = self.info["color_intrinsics"]
        self.rays = None
        self.palette = PaletteInverse()
        self.lock = threading.Lock()
        self.cache = OrderedDict()
        self.caps = {}
        raw_name = self.track.get("raw_depth_file")
        self.approximate = not bool(raw_name and (self.folder/raw_name).is_file())
        self.depth_path = self.folder/(self.track["file"] if self.approximate else raw_name)
        color_track = self.manifest.get("tracks", {}).get(f"{self.camera_key}_color", {})
        self.color_path = self.folder/color_track.get("file", f"{self.camera_key}_color.mp4")
        self.assumed_range = self.manifest.get("depth_display_ranges_m", {}).get(self.camera_key,
                                                                                 self.manifest.get("depth_display_range_m"))
        self.range_recorded = self.assumed_range is not None
        if self.assumed_range is None:
            settings = read_settings()
            self.assumed_range = [float(settings.get("near_mm",70))/1000,
                                  float(settings.get("far_mm",500))/1000]

    def _read(self, key, path, index, raw=False):
        if key not in self.caps:
            if raw:
                cap = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG, [cv2.CAP_PROP_CONVERT_RGB,0])
            else:
                cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                raise OSError(f"영상을 열 수 없습니다: {path.name}")
            self.caps[key] = cap
        cap = self.caps[key]
        if round(cap.get(cv2.CAP_PROP_POS_FRAMES)) != index:
            if not cap.set(cv2.CAP_PROP_POS_FRAMES,index):
                raise OSError(f"프레임 이동 실패: {index+1}")
        ok, image = cap.read()
        if not ok:
            raise OSError(f"프레임을 읽을 수 없습니다: {index+1}")
        if raw:
            if image.dtype != np.uint16 or image.ndim != 2:
                raise ValueError("원본 깊이 영상이 16비트 형식으로 디코딩되지 않았습니다.")
            return image
        return cv2.cvtColor(image,cv2.COLOR_BGR2RGB)

    def load(self, index, stride=2, near=.07, far=.5, clean=True):
        index = max(0,min(self.count-1,int(index)))
        key=(index,stride,float(near),float(far),clean)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            if self.rays is None:
                self.rays = camera_rays(self.intrinsics)
            if self.approximate:
                depth_image=self._read("depth",self.depth_path,index)
                depth=self.palette.decode(depth_image,near,far,clean)
            else:
                raw=self._read("raw",self.depth_path,index,raw=True)
                depth=raw.astype(np.float32)*self.scale
                valid=(raw>0)&(raw<65535)&(depth>=near)&(depth<=far)
                depth[~valid]=0
                indices=(np.clip((depth-near)/(far-near),0,1)*255).astype(np.uint8)
                depth_image=LUT[indices].copy()
                depth_image[~valid]=(9,17,26)
                if clean:
                    valid=cv2.erode(valid.astype(np.uint8),np.ones((3,3),np.uint8)).astype(bool)
                    depth[~valid]=0
            if self.color_path.is_file():
                color=self._read("color",self.color_path,index)
            else:
                color=depth_image.copy()
            if color.shape[:2] != depth.shape or depth.shape != self.rays.shape[:2]:
                raise ValueError("깊이·컬러 영상과 카메라 보정의 크기가 일치하지 않습니다.")
            z=depth[::stride,::stride]
            mask=z>0
            points=(self.rays[::stride,::stride]*z[:,:,None])[mask]
            rgb=color[::stride,::stride][mask].astype(np.float32)/255
            heat=depth_image[::stride,::stride][mask].astype(np.float32)/255
            frame=CloudFrame(index,np.ascontiguousarray(points),np.ascontiguousarray(rgb),
                             np.ascontiguousarray(heat),color,depth_image,self.approximate,
                             float(points[:,2].min()) if len(points) else 0,
                             float(points[:,2].max()) if len(points) else 0)
            self.cache[key]=frame
            while len(self.cache)>12:
                self.cache.popitem(last=False)
            return frame

    def close(self):
        with self.lock:
            for cap in self.caps.values():
                cap.release()
            self.caps.clear()
            self.cache.clear()


class MultiSequence:
    """Every depth track of one session on the shared recording timeline.

    Tracks are written against one host clock with gaps repeated, so the same frame
    index is the same moment for every camera (not hardware synchronized)."""

    def __init__(self, folder, calibration=None):
        first = DepthSequence(folder)
        self.folder, self.manifest = first.folder, first.manifest
        self.camera_keys = list(first.camera_keys)
        self.sequences = {first.camera_key: first}
        try:
            for key in self.camera_keys:
                if key not in self.sequences:
                    self.sequences[key] = DepthSequence(folder, key)
        except Exception:
            self.close()
            raise
        self.count = min(sequence.count for sequence in self.sequences.values())
        self.fps = first.fps
        # Match extrinsics by serial so a slot reassignment cannot mix up cameras.
        by_serial = {camera.get("serial"): camera for camera in (calibration or {}).get("cameras", {}).values()}
        self.transforms = {}
        for key, sequence in self.sequences.items():
            camera = by_serial.get(sequence.info.get("serial"))
            self.transforms[key] = (np.asarray(camera["T_board_from_camera"], dtype=np.float32)
                                    if camera else None)
        self.calibrated = [key for key in self.camera_keys if self.transforms[key] is not None]
        self.pool = ThreadPoolExecutor(max_workers=len(self.camera_keys), thread_name_prefix="DepthLoad")

    def load(self, index, keys, **options):
        futures = {key: self.pool.submit(self.sequences[key].load, index, **options) for key in keys}
        return {key: future.result() for key, future in futures.items()}

    def close(self):
        pool = getattr(self, "pool", None)
        if pool:
            pool.shutdown(wait=True)
        for sequence in self.sequences.values():
            sequence.close()


def write_ply(path, frame, depth_color=False, assumed_range=None,
              coordinates="meters; X right, Y down, Z forward"):
    path=Path(path)
    colors=frame.depth_colors if depth_color else frame.rgb_colors
    records=np.empty(len(frame.points),dtype=[("x","<f4"),("y","<f4"),("z","<f4"),
                                              ("red","u1"),("green","u1"),("blue","u1")])
    for column,name in enumerate(["x","y","z"]):
        records[name]=frame.points[:,column]
    for column,name in enumerate(["red","green","blue"]):
        records[name]=np.clip(colors[:,column]*255,0,255).astype(np.uint8)
    mode="APPROXIMATE palette inversion; not original metric depth" if frame.approximate else "Original uint16 depth"
    header=("ply\nformat binary_little_endian 1.0\n"
            f"comment {mode}\ncomment coordinates: {coordinates}\n"
            f"comment frame_index {frame.index}\ncomment assumed_range_m {assumed_range}\n"
            f"element vertex {len(records)}\nproperty float x\nproperty float y\nproperty float z\n"
            "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
    with path.open("wb") as output:
        output.write(header.encode("ascii"))
        output.write(records.tobytes())
