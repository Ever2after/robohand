"""Fixed-intrinsic ChArUco extrinsics and independent RGB/depth validation.

All cameras share the board frame: the three D405 (required) and, when captured, the D435s and
UVC webcams (rgb4k_N, intrinsics from calibration/intrinsics.py)."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path

import cv2
import numpy as np
import pyrealsense2 as rs

from ..cameras.hub import CAMERA_KEYS
from ..paths import ACTIVE_CALIBRATION

BOARD_SPEC = {"squares": [7, 5], "square_m": .025, "marker_m": .018,
              "dictionary": "DICT_6X6_250", "size_m": [.175, .125]}


def board_model(spec=None):
    spec = spec or BOARD_SPEC
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_6X6_250)
    return cv2.aruco.CharucoBoard(tuple(spec["squares"]), spec["square_m"], spec["marker_m"], dictionary)


def world_corners(spec=None):
    points = board_model(spec).getChessboardCorners().astype(np.float64)
    points[:, 1] *= -1
    return points


def camera_matrix(intrinsics):
    return np.array([[intrinsics["fx"], 0, intrinsics["ppx"]],
                     [0, intrinsics["fy"], intrinsics["ppy"]], [0, 0, 1]], dtype=float)


def sdk_intrinsics(data):
    intr = rs.intrinsics()
    for key in ("width", "height", "fx", "fy", "ppx", "ppy"):
        setattr(intr, key, data[key])
    intr.model = getattr(rs.distortion, data["model"].split(".")[-1])
    intr.coeffs = data["coeffs"]
    return intr


def detect_corners(image, spec=None):
    """Scale only for marker detection, then refine in original pixel coordinates."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    detector = cv2.aruco.CharucoDetector(board_model(spec))
    best = {}
    width = gray.shape[1]
    scales = (1,) if width >= 2500 else (1, 2) if width >= 1500 else (1, 2, 3)   # 4K: upscaling only costs time
    for scale in scales:
        enlarged = gray if scale == 1 else cv2.resize(gray, None, fx=scale, fy=scale)
        corners, ids, _, _ = detector.detectBoard(enlarged)
        if ids is None or len(ids) <= len(best):
            continue
        points = np.ascontiguousarray(corners.reshape(-1, 2) / scale, dtype=np.float32)
        cv2.cornerSubPix(gray, points, (2, 2), (-1, -1),
                         (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, .001))
        best = {int(index): point.tolist() for index, point in zip(ids.ravel(), points)}
    return best


def project(points, camera, intrinsics):
    transform = np.asarray(camera["T_camera_from_board"], dtype=float)
    rotation = cv2.Rodrigues(transform[:3, :3])[0]
    pixels = cv2.projectPoints(np.asarray(points, dtype=float), rotation, transform[:3, 3],
                               camera_matrix(intrinsics), np.asarray(intrinsics["coeffs"]))[0]
    return pixels.reshape(-1, 2)


def error_statistics(values):
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return {"count": 0}
    return {"count": int(values.size), "rms": float(np.sqrt(np.mean(values**2))),
            "median": float(np.median(values)), "p95": float(np.percentile(values, 95)),
            "max": float(np.max(values))}


MARKER_RATIO = BOARD_SPEC["marker_m"] / BOARD_SPEC["square_m"]  # 18 / 25, fixed by the printed design


def board_spec(square_mm, marker_mm=None, squares=None):
    """Physical board description from the measured square size in millimeters.

    The marker size follows the fixed 25:18 design ratio unless given explicitly."""
    square_mm = float(square_mm)
    marker_mm = square_mm * MARKER_RATIO if marker_mm is None else float(marker_mm)
    if not 1 <= square_mm <= 500:
        raise ValueError("사각형 크기는 1–500 mm 사이로 입력하세요.")
    if not 0 < marker_mm < square_mm:
        raise ValueError("마커 크기는 0보다 크고 사각형 크기보다 작아야 합니다.")
    spec = copy.deepcopy(BOARD_SPEC)
    if squares:
        spec["squares"] = list(squares)
    spec["square_m"], spec["marker_m"] = square_mm / 1000, marker_mm / 1000
    spec["size_m"] = [count * spec["square_m"] for count in spec["squares"]]
    return spec


def estimate_pose(observations, intrinsics, spec=None):
    pooled = {}
    for observation in observations:
        for index, point in observation.items():
            pooled.setdefault(int(index), []).append(point)
    ids = sorted(index for index, points in pooled.items() if len(points) >= 2)
    if len(ids) < 6:
        raise ValueError(f"Only {len(ids)} reliable ChArUco corners")
    objects = world_corners(spec)[ids]
    pixels = np.asarray([np.median(pooled[index], axis=0) for index in ids])
    matrix, coefficients = camera_matrix(intrinsics), np.asarray(intrinsics["coeffs"])
    _, rotations, translations, _ = cv2.solvePnPGeneric(
        objects, pixels, matrix, coefficients, flags=cv2.SOLVEPNP_IPPE)
    candidates = []
    for rotation, translation in zip(rotations, translations):
        rotation_matrix = cv2.Rodrigues(rotation)[0]
        center = -rotation_matrix.T @ translation
        depths = (objects @ rotation_matrix.T + translation.ravel())[:, 2]
        if center[2, 0] <= 0 or np.min(depths) <= 0:
            continue
        projection = cv2.projectPoints(objects, rotation, translation, matrix, coefficients)[0].reshape(-1, 2)
        candidates.append((np.mean(np.linalg.norm(projection-pixels, axis=1)), rotation, translation))
    if not candidates:
        raise ValueError("No physically valid board pose")
    _, rotation, translation = min(candidates, key=lambda candidate: candidate[0])
    keep = np.ones(len(ids), dtype=bool)
    for _ in range(3):
        rotation, translation = cv2.solvePnPRefineLM(objects[keep], pixels[keep], matrix,
                                                    coefficients, rotation, translation)
        projection = cv2.projectPoints(objects, rotation, translation, matrix, coefficients)[0].reshape(-1, 2)
        errors = np.linalg.norm(projection-pixels, axis=1)
        cutoff = max(.8, float(np.median(errors) + 3 * 1.4826 * np.median(abs(errors-np.median(errors)))))
        keep = errors <= cutoff
        if np.count_nonzero(keep) < 6:
            raise ValueError("Too few geometrically consistent corners")
    rotation, translation = cv2.solvePnPRefineLM(objects[keep], pixels[keep], matrix,
                                                coefficients, rotation, translation)
    projection = cv2.projectPoints(objects, rotation, translation, matrix, coefficients)[0].reshape(-1, 2)
    errors = np.linalg.norm(projection-pixels, axis=1)
    transform = np.eye(4)
    transform[:3, :3] = cv2.Rodrigues(rotation)[0]
    transform[:3, 3] = translation.ravel()
    inverse = np.linalg.inv(transform)
    return {"T_camera_from_board": transform.tolist(), "T_board_from_camera": inverse.tolist(),
            "camera_center_board_m": inverse[:3, 3].tolist(),
            "fit_corner_ids": np.asarray(ids)[keep].tolist(),
            "excluded_corner_ids": np.asarray(ids)[~keep].tolist(),
            "fit_error_px": error_statistics(errors[keep])}


def reprojection_errors(observations, camera, intrinsics):
    errors = []
    for observation in observations:
        if not observation:
            continue
        ids = list(observation)
        projected = project(world_corners(camera.get("board"))[np.asarray(ids, dtype=int)], camera, intrinsics)
        errors.extend(np.linalg.norm(projected-np.asarray(list(observation.values())), axis=1))
    return error_statistics(errors)


def board_polygon(camera, intrinsics):
    spec = camera.get("board", BOARD_SPEC)
    width, height = np.asarray(spec["squares"]) * spec["square_m"]
    return project([[0, 0, 0], [width, 0, 0], [width, -height, 0], [0, -height, 0]], camera, intrinsics)


def board_plane_residuals(depth, camera, info):
    """Return signed metric residuals for valid depth pixels inside the board."""
    intrinsics = info["color_intrinsics"]
    mask = np.zeros(depth.shape, dtype=np.uint8)
    cv2.fillConvexPoly(mask, np.rint(board_polygon(camera, intrinsics)).astype(np.int32), 1)
    mask = cv2.erode(mask, np.ones((5, 5), np.uint8))
    selected = (mask != 0) & (depth > 0) & (depth < 65535)
    y, x = np.where(selected)
    if not len(x):
        return np.empty(0), y, x, int(mask.sum())
    intr = sdk_intrinsics(intrinsics)
    rays = np.asarray([rs.rs2_deproject_pixel_to_point(intr, [int(px), int(py)], 1.)
                       for px, py in zip(x, y)])
    points = rays * (depth[y, x] * info["depth_scale_m"])[:, None]
    transform = np.asarray(camera["T_camera_from_board"])
    residuals = (points-transform[:3, 3]) @ transform[:3, 2] * 1000
    return residuals, y, x, int(mask.sum())


def depth_plane_validation(image, depth, camera, info):
    residuals, y, x, board_pixels = board_plane_residuals(depth, camera, info)
    if not len(residuals):
        return {"count": 0, "board_pixels": board_pixels}
    white = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)[y, x] >= 120
    result = error_statistics(abs(residuals))
    result.update(median_signed_mm=float(np.median(residuals)),
                  board_pixels=board_pixels, valid_fraction=float(len(x)/max(1, board_pixels)),
                  fraction_over_10mm=float(np.mean(abs(residuals) > 10)),
                  white_paper=error_statistics(abs(residuals[white])))
    return result


def draw_overlay(image, camera, intrinsics, observations=None):
    output = image.copy()
    size = max(1, round(image.shape[1]/640))
    spec = camera.get("board", BOARD_SPEC)
    columns, rows = spec["squares"]
    square = spec["square_m"]
    width, height = columns*square, rows*square

    def line(a, b, color, width=1):
        pixels = np.rint(project([a, b], camera, intrinsics)).astype(np.int32)
        cv2.line(output, tuple(pixels[0]), tuple(pixels[1]), color, width*size, cv2.LINE_AA)

    for column in range(columns+1):
        line([column*square, 0, 0], [column*square, -height, 0], (255, 220, 50))
    for row in range(rows+1):
        line([0, -row*square, 0], [width, -row*square, 0], (255, 220, 50))
    # A 50 mm virtual cube provides an out-of-plane pose reference.
    base = np.array([[.05, -.05, 0], [.1, -.05, 0], [.1, -.1, 0], [.05, -.1, 0]])
    top = base + [0, 0, .05]
    for index in range(4):
        line(base[index], base[(index+1) % 4], (240, 100, 240))
        line(top[index], top[(index+1) % 4], (240, 100, 240))
        line(base[index], top[index], (240, 100, 240))
    for endpoint, color, caption in [([.06, 0, 0], (60, 80, 255), "X"),
                                      ([0, .06, 0], (80, 240, 80), "Y"),
                                      ([0, 0, .06], (255, 130, 60), "Z")]:
        line([0, 0, 0], endpoint, color, 2)
        position = tuple(np.rint(project([endpoint], camera, intrinsics)[0]).astype(int))
        cv2.putText(output, caption, position, cv2.FONT_HERSHEY_SIMPLEX, .5*size, color, size, cv2.LINE_AA)
    if observations:
        ids = list(observations)
        simulated = project(world_corners(spec)[np.asarray(ids, dtype=int)], camera, intrinsics)
        for real, predicted in zip(observations.values(), simulated):
            real, predicted = tuple(np.rint(real).astype(int)), tuple(np.rint(predicted).astype(int))
            cv2.circle(output, real, 2*size, (70, 255, 70), size, cv2.LINE_AA)
            cv2.drawMarker(output, predicted, (255, 80, 240), cv2.MARKER_CROSS, 7*size, size)
    return output


def read_color(folder, key):
    """Colour view of `key` in a capture frame folder (RealSense PNG or webcam JPEG)."""
    for name in (f"{key}_color.png", f"{key}_color.jpg"):
        if (Path(folder) / name).exists():
            return cv2.imread(str(Path(folder) / name))
    return None


def load_calibration(path=ACTIVE_CALIBRATION):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def verify_snapshot(folder, calibration, output_folder=None):
    """Validate a fresh normal-resolution snapshot without changing extrinsics."""
    folder = Path(folder)
    destination = Path(output_folder) if output_folder else folder
    destination.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((folder/"metadata.json").read_text(encoding="utf-8"))
    result = {"verified_at": datetime.now().astimezone().isoformat(),
              "source_folder": str(folder.resolve()), "calibration_created_at": calibration["created_at"],
              "board": calibration["board"], "cameras": {}}
    for key in CAMERA_KEYS:
        camera = calibration["cameras"][key]
        info = metadata["devices"][key]
        if camera["serial"] != info.get("serial"):
            raise ValueError(f"{key}: calibration serial does not match connected camera")
        image = cv2.imread(str(folder/f"{key}_color.png"))
        if image is None:
            raise ValueError(f"No current image for {key}")
        observations = detect_corners(image, calibration["board"])
        depth = np.load(folder/f"{key}_depth_raw.npy")
        result["cameras"][key] = {
            "serial": info["serial"], "corners": len(observations),
            "reprojection_px": reprojection_errors([observations], camera, info["color_intrinsics"]),
            "depth_plane_mm": depth_plane_validation(image, depth, camera, info)}
        cv2.imwrite(str(destination/f"{key}_overlay.png"),
                    draw_overlay(image, camera, info["color_intrinsics"], observations))
    (destination/"calibration_validation.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def verify_capture(folder, calibration, output_folder=None, indices=None):
    """Re-check every calibrated camera that is present in a capture folder."""
    folder = Path(folder)
    destination = Path(output_folder) if output_folder else folder
    destination.mkdir(parents=True, exist_ok=True)
    capture = json.loads((folder/"capture.json").read_text(encoding="utf-8"))
    result = {"verified_at": datetime.now().astimezone().isoformat(), "source_folder": str(folder.resolve()),
              "profile": capture["profile"], "calibration_created_at": calibration["created_at"],
              "board": calibration["board"], "cameras": {}}
    samples = [sample for sample in capture["samples"] if indices is None or sample["index"] in indices]
    if not samples:
        raise ValueError("No validation samples selected")
    result["sample_indices"] = [sample["index"] for sample in samples]
    for key, camera in calibration["cameras"].items():
        info = capture["devices"].get(key)
        if info is None:
            continue
        if camera.get("serial") and info.get("serial") != camera["serial"]:
            raise ValueError(f"Serial mismatch for {key}")
        intrinsics = info.get("color_intrinsics") or camera.get("intrinsics")
        views, depth_checks = [], []
        image = None
        for sample in samples:
            source = folder/f"frame_{sample['index']:03d}"
            image = read_color(source, key)
            if image is None:
                continue
            views.append(detect_corners(image, calibration["board"]))
            if (source/f"{key}_depth.npy").exists():
                depth_checks.append(depth_plane_validation(image, np.load(source/f"{key}_depth.npy"), camera, info))
        result["cameras"][key] = {
            "serial": info.get("serial"), "corner_counts": [len(view) for view in views],
            "reprojection_px": reprojection_errors(views, camera, intrinsics),
            "depth_plane_mm": depth_checks}
        if image is not None and views:
            cv2.imwrite(str(destination/f"{key}_overlay.png"), draw_overlay(image, camera, intrinsics, views[-1]))
    (destination/"calibration_validation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def calibrate(folder, spec=None):
    """Board-frame pose of every captured camera. The D405s must succeed; other cameras that cannot
    see the board (or have no intrinsics) are listed under "failed_cameras" instead of aborting."""
    folder = Path(folder).resolve()
    spec = copy.deepcopy(spec or BOARD_SPEC)
    capture = json.loads((folder/"capture.json").read_text(encoding="utf-8"))
    count = len(capture["samples"])
    split = count // 2
    result = {"format_version": 2, "created_at": datetime.now().astimezone().isoformat(),
              "board": spec, "intrinsics_source": "RealSense factory calibration (held fixed); "
                                                  "webcams: ChArUco lens calibration (webcam_intrinsics.json)",
              "world_axes": "X along columns, Y opposite rows, Z above paper; meters",
              "capture_mode": capture.get("capture_mode"), "source_folder": str(folder),
              "training_indices": list(range(split)), "validation_indices": list(range(split, count)),
              "cameras": {}, "failed_cameras": {}}
    observations = {}
    report_folder = folder.parent / "overlays"
    report_folder.mkdir(exist_ok=True)
    keys = [key for key in CAMERA_KEYS if key in capture["devices"]]
    keys += [key for key in capture["devices"] if key not in CAMERA_KEYS]
    for key in keys:
        info = capture["devices"][key]
        intrinsics = info.get("color_intrinsics")
        try:
            if not intrinsics:
                raise ValueError(info.get("intrinsics_note") or "내부 파라미터 없음")
            views = []
            for index in range(count):
                image = read_color(folder/f"frame_{index:03d}", key)
                views.append(detect_corners(image, spec) if image is not None else {})
            observations[key] = views
            camera = estimate_pose(views[:split], intrinsics, spec)
        except ValueError as error:
            if key in CAMERA_KEYS:
                raise ValueError(f"{key}: {error}") from error
            result["failed_cameras"][key] = str(error)
            print(key, "skipped:", error, flush=True)
            continue
        camera["board"] = copy.deepcopy(spec)
        camera.update(serial=info.get("serial"), kind=info.get("kind", "realsense"), intrinsics=intrinsics,
                      training_error_px=reprojection_errors(views[:split], camera, intrinsics),
                      heldout_error_px=reprojection_errors(views[split:], camera, intrinsics),
                      detected_corner_counts=[len(view) for view in views])
        if info.get("kind", "realsense") == "realsense":
            camera["factory_intrinsics"] = intrinsics
        else:
            camera.update(device_path=info.get("path"), intrinsics_note=info.get("intrinsics_note", ""))
        depth_checks = []
        for index in range(split, count):
            source = folder / f"frame_{index:03d}"
            if (source/f"{key}_depth.npy").exists():
                image = read_color(source, key)
                depth_checks.append(depth_plane_validation(image, np.load(source/f"{key}_depth.npy"), camera, info))
        camera["heldout_depth_plane_mm"] = depth_checks
        result["cameras"][key] = camera
        image = read_color(folder/f"frame_{count-1:03d}", key)
        cv2.imwrite(str(report_folder/f"{key}_overlay.png"), draw_overlay(image, camera, intrinsics, views[-1]))
        print(key, "corners", camera["detected_corner_counts"],
              "heldout px", camera["heldout_error_px"],
              "depth median mm", [round(check.get("median", -1), 2) for check in depth_checks], flush=True)
    result["observations"] = observations
    result["relative_transforms"] = relative_transforms(result["cameras"])
    destination = folder.parent / "calibration.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result, destination


def relative_transforms(cameras):
    return {f"T_{target}_from_{source}": (
                np.asarray(cameras[target]["T_camera_from_board"]) @
                np.asarray(cameras[source]["T_board_from_camera"])).tolist()
            for target in cameras for source in cameras if target != source}


def main():
    parser = argparse.ArgumentParser(description="ChArUco extrinsics from a capture_calibration folder")
    parser.add_argument("folder", type=Path, help="folder containing capture.json and frame_XXX/")
    parser.add_argument("--square-mm", type=float, default=BOARD_SPEC["square_m"]*1000,
                        help="measured printed square size; marker follows the 25:18 ratio")
    arguments = parser.parse_args()
    _, destination = calibrate(arguments.folder, board_spec(arguments.square_mm))
    print(destination)


if __name__ == "__main__":
    main()
