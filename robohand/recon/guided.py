"""Second 2D pass: hand crops placed by the 3D reconstruction.

The first pass (``hands2d``) finds each hand from the whole-body pose of *that* camera. When a
camera loses a hand (occluded by the other hand or an instrument, or out of the whole-body
model's resolution) its crop lands in the wrong place, and the view is lost for that frame.
Here the crop comes instead from the 3D hands of ``hands3d``, projected into the camera at the
frame's time. The other cameras place it, and the 3D track fixes which hand is left and right.
Only the hand model (RTMPose-m hand5) runs, so the pass is much faster than the first.

Adds ``guided`` (N, 2, 21, 3) to ``<session>/recon/hands2d_<camera>.npz``; ``hands3d`` then
treats it as a third 2D source.

    python -m robohand.recon.guided <session folder> [--result hands3d.npz] [--workers 3]
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np

from robohand.recon.hands2d import HAND5, _limit_threads
from robohand.recon.session import Session, iter_frames


def joints_at(result: dict, times: np.ndarray):
    """3D joints (N,2,21,3) interpolated at ``times``, and where they are valid (N,2,21)."""
    grid = result["times"]
    right = np.clip(np.searchsorted(grid, times), 1, len(grid) - 1)
    left = right - 1
    a = np.clip((times - grid[left]) / (grid[right] - grid[left]), 0, 1)[:, None, None, None]
    joints = (1 - a) * result["joints"][left] + a * result["joints"][right]
    valid = result["valid"][left] & result["valid"][right] & (times >= grid[0])[:, None, None] & (times <= grid[-1])[:, None, None]
    return joints, valid


def crop(uv: np.ndarray, ok: np.ndarray, width: int, height: int, min_size: float = 32.0):
    if ok.sum() < 5:
        return None
    lo, hi = uv[ok].min(0), uv[ok].max(0)
    if hi[0] < 0 or hi[1] < 0 or lo[0] > width or lo[1] > height:
        return None
    size = max(float((hi - lo).max()) * 1.15, min_size)          # RTMPose pads by 1.25 itself
    center = (lo + hi) / 2
    return [*(center - size / 2), *(center + size / 2)]


def run_camera(folder: str, name: str, result_file: str, threads: int, device: str = "cpu") -> str:
    session = Session(folder)
    camera = session.cameras[name]
    result = dict(np.load(session.recon / result_file))
    names = [str(n) for n in result["cameras"]]
    offset = float(result["offsets"][names.index(name)]) if name in names else 0.0
    path = session.recon / f"hands2d_{name}.npz"
    data = dict(np.load(path))
    if threads and device == "cpu":
        _limit_threads(threads)
    from rtmlib import RTMPose

    model = RTMPose(HAND5, model_input_size=(256, 256), backend="onnxruntime", device=device)
    prior, valid = joints_at(result, camera.times - offset)
    n = len(camera.frames)
    guided = np.zeros((n, 2, 21, 3), np.float32)
    projected = np.full((n, 2, 21, 2), np.nan, np.float32)
    wanted = {int(f): i for i, f in enumerate(camera.frames)}
    started = time.time()
    for index, image in iter_frames(camera, wanted):
        row = wanted[index]
        for side in range(2):
            ok = valid[row, side]
            if ok.sum() < 5:
                continue
            uv = camera.project(np.nan_to_num(prior[row, side]))
            projected[row, side] = uv
            box = crop(uv, ok, camera.width, camera.height)
            if box is None:
                continue
            keypoints, scores = model(image, bboxes=[box])
            guided[row, side] = np.concatenate([keypoints[0], scores[0][:, None]], axis=-1)
    data["guided"], data["projected"] = guided, projected
    np.savez_compressed(path, **data)
    return f"{name}: {n} frames in {time.time() - started:.0f} s"


def _job(args):
    return run_camera(*args)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--result", default="hands3d.npz")
    parser.add_argument("--cameras", nargs="*")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--device", default="cpu", help="cpu or cuda (needs onnxruntime-gpu)")
    args = parser.parse_args()
    folder = str(Path(args.session).resolve())
    names = args.cameras or [n for n in Session(folder).cameras
                             if (Path(folder) / "recon" / f"hands2d_{n}.npz").exists()]
    jobs = [(folder, name, args.result, args.threads, args.device) for name in names]
    with mp.get_context("spawn").Pool(max(1, args.workers)) as pool:
        for message in pool.imap_unordered(_job, jobs):
            print(message, flush=True)


if __name__ == "__main__":
    main()
