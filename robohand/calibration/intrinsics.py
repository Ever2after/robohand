"""Lens (intrinsic) calibration of the UVC webcams from a hand-moved ChArUco board.

RealSense cameras keep their factory intrinsics; webcams have none, so each one is calibrated
once from ~20 different board poses. Views are taken automatically while the board is held
still in a pose that differs from the ones already collected. Results live in
DATA_ROOT/webcam_intrinsics.json, keyed by webcam slot (rgb4k_N) together with the USB device
path seen at calibration time and the capture resolution.

Keep the webcam's zoom fixed and autofocus off / settled: both change the intrinsics.
"""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path

import cv2
import numpy as np

from ..paths import CALIBRATIONS, WEBCAM_INTRINSICS
from .board import BOARD_SPEC, board_model, detect_corners, error_statistics, world_corners

TARGET_VIEWS = 20
MIN_CORNERS = 12          # of 24 inner corners on the 7x5 board
STILL_PX = 4.0            # mean corner motion (preview pixels) between checks to count as "held still"
NOVELTY = .12             # minimum pose-descriptor distance to every collected view


def load_store():
    try:
        return json.loads(WEBCAM_INTRINSICS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def scaled(intrinsics, size):
    """Intrinsics for another capture size of the same sensor (same aspect ratio, full field of view)."""
    width, height = size
    sx, sy = width / intrinsics["width"], height / intrinsics["height"]
    if abs(sx - sy) > .01:
        raise ValueError(f"내부 보정 해상도 {intrinsics['width']}×{intrinsics['height']}와 화면 비율이 다릅니다.")
    result = dict(intrinsics)
    result.update(width=int(width), height=int(height), fx=intrinsics["fx"]*sx, fy=intrinsics["fy"]*sy,
                  ppx=(intrinsics["ppx"]+.5)*sx-.5, ppy=(intrinsics["ppy"]+.5)*sy-.5)
    return result


def webcam_intrinsics(slot, size, path=None):
    """(intrinsics scaled to `size`, note) for a webcam slot, or (None, reason)."""
    entry = load_store().get(slot)
    if not entry:
        return None, "내부 보정 없음"
    note = ""
    if path and entry.get("device_path") and entry["device_path"].lower() != path.lower():
        note = "USB 경로가 내부 보정 때와 다름 (카메라를 바꿔 꽂았다면 다시 보정)"
    try:
        return scaled(entry["intrinsics"], size), note
    except ValueError as error:
        return None, str(error)


def pose_descriptor(corners, ids, size):
    """Coarse view descriptor in [0,1]-ish units: board centre, apparent size, in-plane angle, foreshortening."""
    width, height = size
    points = np.asarray(corners, dtype=float)
    centre = points.mean(axis=0) / [width, height]
    spread = np.sqrt(np.prod(points.max(axis=0) - points.min(axis=0))) / np.sqrt(width * height)
    model = world_corners()[np.asarray(ids)][:, :2]
    homography, _ = cv2.findHomography(model, points)
    if homography is None:
        return None
    normalised = homography / homography[2, 2]
    angle = np.arctan2(normalised[1, 0], normalised[0, 0]) / np.pi
    perspective = np.clip(normalised[2, :2] * .05, -1, 1)   # tilt of the board towards / away from the camera
    return np.array([*centre, spread * 2, angle, *perspective * 3])


class IntrinsicCollector:
    """Feed it webcam JPEGs; it keeps still, novel views and finally solves the lens model."""

    def __init__(self, slot, device, spec=None):
        self.slot, self.device = slot, dict(device or {})
        self.spec = copy.deepcopy(spec or BOARD_SPEC)
        self.folder = CALIBRATIONS / datetime.now().strftime(f"intrinsics_{slot}_%Y%m%d_%H%M%S")
        self.views = []            # (ids, corners) at full resolution
        self.descriptors = []
        self.size = None
        self.previous = None       # preview-scale corners from the last check (stillness test)

    @property
    def done(self):
        return len(self.views) >= TARGET_VIEWS

    def consider(self, jpeg):
        """Returns a short status string; adds the frame as a view when it qualifies."""
        data = np.frombuffer(jpeg, np.uint8)
        preview = cv2.imdecode(data, cv2.IMREAD_REDUCED_COLOR_4)
        if preview is None:
            return "디코딩 실패"
        found = detect_corners(preview, self.spec)
        if len(found) < MIN_CORNERS:
            self.previous = None
            return f"보드 코너 {len(found)}개 · 보드를 더 크게/선명하게 보여주세요"
        ids = sorted(found)
        points = np.asarray([found[i] for i in ids])
        still = False
        if self.previous is not None:
            common = [i for i in ids if i in self.previous]
            if len(common) >= MIN_CORNERS:
                motion = np.mean(np.linalg.norm(np.asarray([found[i] for i in common]) -
                                                 np.asarray([self.previous[i] for i in common]), axis=1))
                still = motion < STILL_PX
        self.previous = found
        if not still:
            return "보드를 잠시 멈춰 주세요"
        height, width = preview.shape[:2]
        descriptor = pose_descriptor(points, ids, (width, height))
        if descriptor is None:
            return "보드 자세 계산 실패"
        if self.descriptors and min(np.linalg.norm(descriptor - d) for d in self.descriptors) < NOVELTY:
            return "다른 위치·각도로 옮겨 주세요"
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        full = detect_corners(image, self.spec)
        if len(full) < MIN_CORNERS:
            return "원본 해상도 코너 부족"
        self.size = (image.shape[1], image.shape[0])
        full_ids = sorted(full)
        self.views.append((full_ids, [full[i] for i in full_ids]))
        self.descriptors.append(descriptor)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"view_{len(self.views):02d}.jpg").write_bytes(jpeg)
        return f"촬영 {len(self.views)}/{TARGET_VIEWS}"

    def calibrate(self):
        if len(self.views) < 8:
            raise ValueError(f"자세가 다른 보드 영상이 {len(self.views)}장뿐입니다 (8장 이상 필요).")
        board = board_model(self.spec)
        objects, images = [], []
        for ids, corners in self.views:
            object_points, image_points = board.matchImagePoints(
                np.asarray(corners, np.float32).reshape(-1, 1, 2), np.asarray(ids, np.int32).reshape(-1, 1))
            objects.append(object_points)
            images.append(image_points)
        rms, matrix, coefficients, rotations, translations = cv2.calibrateCamera(
            objects, images, self.size, None, None)
        per_view = []
        for obj, img, rvec, tvec in zip(objects, images, rotations, translations):
            projected = cv2.projectPoints(obj, rvec, tvec, matrix, coefficients)[0].reshape(-1, 2)
            per_view.append(float(np.sqrt(np.mean(np.sum((projected - img.reshape(-1, 2))**2, axis=1)))))
        coefficients = coefficients.ravel().tolist()[:5]
        intrinsics = {"width": self.size[0], "height": self.size[1], "fx": float(matrix[0, 0]),
                      "fy": float(matrix[1, 1]), "ppx": float(matrix[0, 2]), "ppy": float(matrix[1, 2]),
                      "model": "opencv.brown_conrady", "coeffs": coefficients}
        entry = {"created_at": datetime.now().astimezone().isoformat(), "slot": self.slot,
                 "device_path": self.device.get("path"), "device_name": self.device.get("name"),
                 "intrinsics": intrinsics, "rms_px": float(rms),
                 "per_view_rms_px": error_statistics(per_view), "views": len(self.views),
                 "board": self.spec, "source_folder": str(self.folder)}
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "intrinsics.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        store = load_store()
        store[self.slot] = entry
        WEBCAM_INTRINSICS.parent.mkdir(parents=True, exist_ok=True)
        temporary = WEBCAM_INTRINSICS.with_suffix(".tmp")
        temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(WEBCAM_INTRINSICS)
        return entry
