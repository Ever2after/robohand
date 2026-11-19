"""Fit one shared effective board scale against training depth, then validate."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
from pathlib import Path

import numpy as np

from ..cameras.hub import CAMERA_KEYS
from ..paths import ACTIVE_CALIBRATION, CALIBRATIONS
from .board import board_plane_residuals, load_calibration, relative_transforms, reprojection_errors, verify_capture


def summarize(checks):
    return {"rms_mm": float(np.sqrt(np.mean([check["rms"]**2 for check in checks]))),
            "median_abs_mm": float(np.median([check["median"] for check in checks])),
            "median_signed_mm": float(np.median([check["median_signed_mm"] for check in checks])),
            "p95_abs_mm": float(np.median([check["p95"] for check in checks]))}


def fit_scale(calibration):
    source = Path(calibration["source_folder"])
    capture = json.loads((source/"capture.json").read_text(encoding="utf-8"))
    indices = calibration["training_indices"]
    if not indices or set(indices) & set(calibration["validation_indices"]):
        raise ValueError("Separate training and validation frames are required")
    residuals, heights, means = {}, {}, {}
    for key in CAMERA_KEYS:
        camera, info = calibration["cameras"][key], capture["devices"][key]
        if camera["serial"] != info["serial"]:
            raise ValueError(f"Serial mismatch for {key}")
        transform = np.asarray(camera["T_camera_from_board"])
        heights[key] = float(-transform[:3, 3] @ transform[:3, 2] * 1000)
        residuals[key] = []
        for index in indices:
            depth = np.load(source/f"frame_{index:03d}"/f"{key}_depth.npy")
            values, _, _, _ = board_plane_residuals(depth, camera, info)
            if not len(values):
                raise ValueError(f"No valid board depth for {key}, frame {index}")
            residuals[key].append(values)
        means[key] = float(np.mean([values.mean() for values in residuals[key]]))
    # For scale k each signed residual is r(k) = r(1) + height * (k - 1).
    # Equal weights for cameras and frames keep pixel counts from dominating.
    numerator = sum(heights[key]*means[key] for key in CAMERA_KEYS)
    denominator = sum(heights[key]**2 for key in CAMERA_KEYS)
    scale = 1 - numerator/denominator
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("No positive finite board scale fits these observations")

    def objective(candidate):
        return float(np.mean([
            np.mean([np.mean((values+heights[key]*(candidate-1))**2) for values in residuals[key]])
            for key in CAMERA_KEYS]))

    if objective(scale) > min(objective(scale-.0001), objective(scale+.0001)):
        raise RuntimeError("Scale optimizer failed its minimum check")
    result = copy.deepcopy(calibration)
    result["created_at"] = datetime.now().astimezone().isoformat()
    result["nominal_board"] = copy.deepcopy(calibration.get("nominal_board", calibration["board"]))
    board = result["board"]
    board["square_m"] *= scale
    board["marker_m"] *= scale
    board["size_m"] = [count*board["square_m"] for count in board["squares"]]
    board["dimension_source"] = "effective scale fitted to RealSense training depth"
    result["scale_optimization"] = {
        "method": "analytic least squares; equal camera and frame weights; all valid eroded-board pixels",
        "reference_square_m": calibration["board"]["square_m"], "scale_factor": scale,
        "training_indices": indices, "source_folder": str(source),
        "training_objective_before_rms_mm": float(np.sqrt(objective(1))),
        "training_objective_after_rms_mm": float(np.sqrt(objective(scale))),
        "interpretation": "Effective RGB-to-depth scale; includes sensor depth bias and pose uncertainty",
        "cameras": {key: {"height_reference_mm": heights[key], "mean_residual_before_mm": means[key],
                           "mean_residual_after_mm": means[key]+heights[key]*(scale-1)}
                    for key in CAMERA_KEYS}}
    for key, camera in result["cameras"].items():
        camera["board"] = copy.deepcopy(board)
        transform = np.asarray(camera["T_camera_from_board"])
        transform[:3, 3] *= scale
        inverse = np.linalg.inv(transform)
        camera["T_camera_from_board"] = transform.tolist()
        camera["T_board_from_camera"] = inverse.tolist()
        camera["camera_center_board_m"] = inverse[:3, 3].tolist()
        views = [result["observations"][key][index] for index in indices]
        camera["training_error_px"] = reprojection_errors(views, camera, camera.get("intrinsics") or camera["factory_intrinsics"])
    result["relative_transforms"] = relative_transforms(result["cameras"])   # every calibrated camera
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--calibration", type=Path, default=ACTIVE_CALIBRATION)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    baseline = load_calibration(arguments.calibration)
    if baseline is None:
        raise ValueError("Calibration file is missing")
    destination = CALIBRATIONS/datetime.now().strftime("depth_scale_%Y%m%d_%H%M%S_%f")
    destination.mkdir(parents=True)
    (destination/"calibration_before.json").write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    result = fit_scale(baseline)
    print("Effective square mm", result["board"]["square_m"]*1000, flush=True)
    validation = verify_capture(result["source_folder"], result, destination/"heldout_highres",
                                result["validation_indices"])
    comparison = {}
    for key, camera in result["cameras"].items():
        verified = validation["cameras"][key]
        camera["heldout_error_px"] = verified["reprojection_px"]
        camera["heldout_depth_plane_mm"] = verified["depth_plane_mm"]
        if not camera["heldout_depth_plane_mm"]:
            continue   # webcams: no depth to compare
        comparison[key] = {"before": summarize(baseline["cameras"][key]["heldout_depth_plane_mm"]),
                           "after": summarize(camera["heldout_depth_plane_mm"])}
        print(key, comparison[key], flush=True)
    normal_source = baseline.get("normal_resolution_validation", {}).get("source_folder")
    if normal_source:
        result["normal_resolution_validation"] = verify_capture(normal_source, result, destination/"normal_validation")
    result["scale_optimization"]["heldout_comparison"] = comparison
    result["scale_optimization"]["result_folder"] = str(destination)
    encoded = json.dumps(result, indent=2, ensure_ascii=False)
    (destination/"calibration.json").write_text(encoded, encoding="utf-8")
    if arguments.apply:
        temporary = arguments.calibration.with_suffix(".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(arguments.calibration)
        print("Applied", arguments.calibration, flush=True)
    print("Results", destination, flush=True)


if __name__ == "__main__":
    main()
