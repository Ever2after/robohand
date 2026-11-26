"""2D hand keypoints in every frame of every camera.

Two detectors run on each frame, both through rtmlib / ONNX Runtime (CPU):

* **RTMW-x** whole-body (YOLOX-m person boxes, 384x288 pose input). It sees the arms, so its
  left/right labels are the operator's anatomical hands, and it keeps working in close-ups.
* **RTMPose-m hand5** (256x256) on a crop around each RTMW hand: a second, hand-only estimate
  at higher resolution. ``hands3d`` decides per camera which source to trust.

Per camera the result is ``<session>/recon/hands2d_<camera>.npz``:
``frames`` (N,), ``times`` (N,), ``wholebody`` and ``hand5`` (N, 2, 21, 3) as x, y, score with
hand 0 = left and 1 = right, ``body`` (N, 23, 3) COCO body + feet, ``person`` (N, 4) xyxy.
Keypoint order is the usual 21-point hand: wrist, then thumb, index, middle, ring, little
from base to tip. RTMW scores are raw SimCC maxima (roughly 0-10), hand5 scores are 0-1.

    python -m robohand.recon.hands2d <session folder> [--cameras d405 rgb4k_1 ...] [--workers 3]
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np

from robohand.recon.session import Session, iter_frames

PERSON = "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/yolox_m_8xb8-300e_humanart-c2c7a14a.zip"
WHOLEBODY = "https://download.openmmlab.com/mmpose/v1/projects/rtmw/onnx_sdk/rtmw-dw-x-l_simcc-cocktail14_270e-384x288_20231122.zip"
HAND5 = "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-m_simcc-hand5_pt-aic-coco_210e-256x256-74fb594_20230320.zip"
HANDS = (slice(91, 112), slice(112, 133))     # COCO-WholeBody left, right hand
DETECT_EVERY = 3                               # person boxes barely move; pose runs every frame


def _limit_threads(threads: int):
    """rtmlib builds its ONNX Runtime sessions without options; give them a thread budget."""
    import onnxruntime as ort

    base = ort.InferenceSession

    def session(*args, **kwargs):
        options = kwargs.pop("sess_options", None) or ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.log_severity_level = 3
        return base(*args, sess_options=options, **kwargs)

    ort.InferenceSession = session


class Detector:
    def __init__(self, threads: int = 0, device: str = "cpu"):
        if threads and device == "cpu":
            _limit_threads(threads)
        from rtmlib import YOLOX, RTMPose

        self.person = YOLOX(PERSON, model_input_size=(640, 640), backend="onnxruntime", device=device)
        self.wholebody = RTMPose(WHOLEBODY, model_input_size=(288, 384), backend="onnxruntime", device=device)
        self.hand = RTMPose(HAND5, model_input_size=(256, 256), backend="onnxruntime", device=device)

    def people(self, image):
        return np.asarray(self.person(image), float).reshape(-1, 4)

    def pose(self, image, boxes):
        keypoints, scores = self.wholebody(image, bboxes=list(boxes))
        return np.concatenate([keypoints, scores[..., None]], axis=-1)       # (P, 133, 3)

    def refine(self, image, hand):
        """hand5 on a square crop around one RTMW hand; None if the hand is not there."""
        good = hand[:, 2] > 2.0
        if good.sum() < 5:
            return None
        lo, hi = hand[good, :2].min(0), hand[good, :2].max(0)
        size = max(float((hi - lo).max()) * 1.15, 32.0)                   # RTMPose pads by 1.25 itself
        center = (lo + hi) / 2
        box = [*(center - size / 2), *(center + size / 2)]
        keypoints, scores = self.hand(image, bboxes=[box])
        return np.concatenate([keypoints[0], scores[0][:, None]], axis=-1)


def largest_in(boxes: np.ndarray, roi: np.ndarray, keep: int = 2) -> np.ndarray:
    """The ``keep`` largest person boxes that overlap the workspace: the operator is close."""
    overlap = ((np.minimum(boxes[:, 2], roi[2]) > np.maximum(boxes[:, 0], roi[0]))
               & (np.minimum(boxes[:, 3], roi[3]) > np.maximum(boxes[:, 1], roi[1])))
    boxes = boxes[overlap]
    area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    return boxes[np.argsort(-area)[:keep]]


def choose_operator(poses: np.ndarray, roi: np.ndarray) -> int:
    """The person whose hands are big and inside the workspace: background people are small."""
    best, choice = -1.0, 0
    x0, y0, x1, y1 = roi
    for index, pose in enumerate(poses):
        value = 0.0
        for part in HANDS:
            hand = pose[part]
            good = hand[:, 2] > 2.0
            if good.sum() < 5:
                continue
            inside = ((hand[good, 0] > x0) & (hand[good, 0] < x1) & (hand[good, 1] > y0) & (hand[good, 1] < y1)).mean()
            size = np.linalg.norm(hand[good, :2].max(0) - hand[good, :2].min(0))
            value += inside * size
        if value > best:
            best, choice = value, index
    return choice


def run_camera(folder: str, name: str, threads: int, force: bool = False, device: str = "cpu") -> str:
    session = Session(folder)
    camera = session.cameras[name]
    out = session.recon / f"hands2d_{name}.npz"
    if out.exists() and not force:
        return f"{name}: exists"
    detector = Detector(threads, device)
    box = camera.project(session.workspace_box())
    roi = np.array([*box.min(0), *box.max(0)])
    wanted = {int(f): i for i, f in enumerate(camera.frames)}
    n = len(camera.frames)
    wholebody = np.zeros((n, 2, 21, 3), np.float32)
    hand5 = np.zeros((n, 2, 21, 3), np.float32)
    body = np.zeros((n, 23, 3), np.float32)
    person = np.zeros((n, 4), np.float32)
    boxes = np.zeros((0, 4))
    started = time.time()
    for count, (index, image) in enumerate(iter_frames(camera, wanted)):
        row = wanted[index]
        if count % DETECT_EVERY == 0 or len(boxes) == 0:
            boxes = largest_in(detector.people(image), roi)
            if len(boxes) == 0:
                continue
        poses = detector.pose(image, boxes)
        chosen = choose_operator(poses, roi)
        pose = poses[chosen]
        person[row] = boxes[chosen]
        body[row] = pose[:23]
        for side, part in enumerate(HANDS):
            wholebody[row, side] = pose[part]
            refined = detector.refine(image, pose[part])
            if refined is not None:
                hand5[row, side] = refined
        if count % 200 == 0:
            print(f"{name}: {count}/{n} frames, {(time.time() - started) / (count + 1):.2f} s/frame", flush=True)
    np.savez_compressed(out, frames=camera.frames, times=camera.times, wholebody=wholebody,
                        hand5=hand5, body=body, person=person)
    return f"{name}: {n} frames in {time.time() - started:.0f} s -> {out}"


def _job(args):
    return run_camera(*args)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--cameras", nargs="*")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--threads", type=int, default=3, help="ONNX Runtime threads per worker")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--device", default="cpu", help="cpu or cuda (needs onnxruntime-gpu)")
    args = parser.parse_args()
    names = args.cameras or list(Session(args.session).cameras)
    jobs = [(str(Path(args.session).resolve()), name, args.threads, args.force, args.device) for name in names]
    if args.workers <= 1:
        for job in jobs:
            print(_job(job), flush=True)
        return
    with mp.get_context("spawn").Pool(args.workers) as pool:
        for message in pool.imap_unordered(_job, jobs):
            print(message, flush=True)


if __name__ == "__main__":
    main()
