"""Lens (intrinsic) calibration of the UVC webcams from ~20 different board views.

RealSense cameras keep their factory intrinsics; webcams have none, so each one is calibrated
once. Only the relative pose matters: either move the board in front of the mounted webcam, or
take the webcam off its mount and aim it at a board lying on the table. Views are taken
automatically while the picture is still and the pose differs from the ones already collected;
the status names image regions that still lack the board and asks for tilted views. Results live in
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
TILT_DEG = 25             # board normal this far from the optical axis counts as a tilted view
TILTED_VIEWS = 6
REGIONS = [["왼쪽 위", "위", "오른쪽 위"], ["왼쪽", "가운데", "오른쪽"], ["왼쪽 아래", "아래", "오른쪽 아래"]]


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


def tilt_degrees(points, ids, size):
    """Angle between the board normal and the optical axis, from a rough pinhole model
    (fx = 0.6 x width, i.e. ~80 deg horizontal FOV). The angle barely depends on that guess."""
    width, height = size
    matrix = np.array([[.6 * width, 0, width / 2], [0, .6 * width, height / 2], [0, 0, 1]])
    ok, rotation, _ = cv2.solvePnP(world_corners()[np.asarray(ids)], np.asarray(points, dtype=float),
                                   matrix, None, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        return 0.
    normal_z = cv2.Rodrigues(rotation)[0][2, 2]
    return float(np.degrees(np.arccos(min(1., abs(normal_z)))))


class IntrinsicCollector:
    """Feed it webcam JPEGs; it keeps still, novel views and finally solves the lens model."""

    def __init__(self, slot, device, spec=None):
        self.slot, self.device = slot, dict(device or {})
        self.spec = copy.deepcopy(spec or BOARD_SPEC)
        self.folder = CALIBRATIONS / datetime.now().strftime(f"intrinsics_{slot}_%Y%m%d_%H%M%S")
        self.views = []            # (ids, corners) at full resolution
        self.descriptors = []
        self.regions = set()       # (row, column) of the 3x3 image grid where accepted boards were centred
        self.tilted = 0
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
            return "비슷한 장면 · " + self.guidance()
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        full = detect_corners(image, self.spec)
        if len(full) < MIN_CORNERS:
            return "원본 해상도 코너 부족"
        self.size = (image.shape[1], image.shape[0])
        full_ids = sorted(full)
        self.views.append((full_ids, [full[i] for i in full_ids]))
        self.descriptors.append(descriptor)
        centre = points.mean(axis=0)
        self.regions.add((min(2, int(3 * centre[1] / height)), min(2, int(3 * centre[0] / width))))
        self.tilted += tilt_degrees(points, ids, (width, height)) >= TILT_DEG
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"view_{len(self.views):02d}.jpg").write_bytes(jpeg)
        return f"촬영 {len(self.views)}/{TARGET_VIEWS} · " + self.guidance()

    def guidance(self):
        """What the next views should add: uncovered image regions first, then tilted views."""
        missing = [REGIONS[row][column] for row in range(3) for column in range(3) if (row, column) not in self.regions]
        if missing:
            return "보드가 화면의 " + ", ".join(missing[:3]) + (" 등" if len(missing) > 3 else "") + "에 오게"
        if self.tilted < TILTED_VIEWS:
            return f"보드를 비스듬히(30–45°) 본 장면 {TILTED_VIEWS - self.tilted}장 더"
        return "거리·각도를 바꿔 계속"

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
