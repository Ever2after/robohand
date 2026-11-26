"""Independent check of RGB-triangulated hand keypoints against RealSense depth.

The keypoints come from colour images only, so depth is a measurement they never saw. Two tests,
on keypoints that move slowly (< ``max_speed``; depth frames are matched by nearest time):

* **Surface distance.** The depth of all RealSense cameras around the hands is fused into one
  point cloud in the board frame, and each keypoint's distance to the nearest surface point is
  measured. Keypoints sit inside the hand: fingertips 0-5 mm from the skin, finger joints about a
  finger radius (6-10 mm), knuckles and wrist deeper (10-25 mm). A keypoint well outside the
  hand reads far from every surface.
* **Free space.** If a camera sees a surface clearly *behind* a keypoint along its ray (by more
  than ``free_space``), that camera looks through the keypoint's position, so the keypoint
  is in empty space. That is an error, unless the depth missed a thin finger.

    python -m robohand.recon.validate <session folder> [--result hands3d.npz]
"""
from __future__ import annotations

import argparse

import numpy as np
from scipy.spatial import cKDTree

from robohand.recon.hands3d import HANDS
from robohand.recon.session import Session

GROUPS = {"wrist": [0], "knuckles": [1, 5, 9, 13, 17], "middle joints": [2, 3, 6, 7, 10, 11, 14, 15, 18, 19],
          "fingertips": [4, 8, 12, 16, 20]}


def depth_frames(session: Session, name: str, wanted):
    import av

    path = session.folder / f"{name}_depth_raw.mkv"
    scale = session.info["devices"][name]["depth_scale_m"]
    wanted = set(int(i) for i in wanted)
    last = max(wanted)
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for index, frame in enumerate(container.decode(stream)):
            if index > last:
                break
            if index in wanted:
                depth = frame.to_ndarray().astype(np.float32)
                depth[(depth == 0) | (depth == 65535)] = np.nan
                yield index, depth * scale


def hand_cloud(camera, depth, joints, margin_px=40, stride=2, max_range=0.08):
    """Depth pixels around the hands, back-projected to the board frame; only points within
    ``max_range`` of a keypoint, so the table and the other objects do not count as hand."""
    pts = joints.reshape(-1, 3)
    pts = pts[np.isfinite(pts).all(1)]
    if len(pts) == 0:
        return np.zeros((0, 3), np.float32)
    uv = camera.project(pts)
    x0, y0 = np.maximum(uv.min(0) - margin_px, 0).astype(int)
    x1, y1 = np.minimum(uv.max(0) + margin_px, [camera.width - 1, camera.height - 1]).astype(int)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 3), np.float32)
    vs, us = np.mgrid[y0:y1:stride, x0:x1:stride]
    z = depth[vs, us]
    ok = np.isfinite(z)
    pix = np.stack([us[ok], vs[ok]], -1).astype(float)
    ray = camera.normalize(pix)
    Xc = np.concatenate([ray * z[ok, None], z[ok, None]], 1)
    Xb = (Xc - camera.t) @ camera.R
    near = cKDTree(pts).query(Xb, distance_upper_bound=max_range)[0] < max_range
    return Xb[near].astype(np.float32)


def check(session: Session, result: dict, every: int = 4, max_speed: float = 0.10, free_space: float = 0.015):
    times = result["times"]
    joints, valid = result["joints"], result["valid"]
    speed = np.linalg.norm(np.gradient(joints, times, axis=0), axis=-1)
    use = valid & (speed < max_speed)
    names = [str(n) for n in result["cameras"]]
    ks = np.arange(0, len(times), every)
    clouds = {k: [] for k in ks}
    violations = {k: np.zeros((2, 21), int) for k in ks}
    tested = {k: np.zeros((2, 21), int) for k in ks}
    cameras = []
    for name, camera in session.cameras.items():
        if camera.kind != "realsense" or not (session.folder / f"{name}_depth_raw.mkv").exists():
            continue
        cameras.append(name)
        if name in names:
            camera.offset = float(result["offsets"][names.index(name)])
        by_frame = {}
        for k, p in zip(ks, camera.frames_at(times[ks])):
            by_frame.setdefault(int(camera.frames[p]), []).append(k)
        for index, depth in depth_frames(session, name, by_frame):
            for k in by_frame[index]:
                clouds[k].append(hand_cloud(camera, depth, np.where(valid[k][..., None], joints[k], np.nan)))
                for h in range(2):
                    ok = use[k, h]
                    if not ok.any():
                        continue
                    z = camera.to_camera(joints[k, h][ok])[:, 2]
                    uv = np.round(camera.project(joints[k, h][ok])).astype(int)
                    inside = (uv[:, 0] >= 0) & (uv[:, 0] < camera.width) & (uv[:, 1] >= 0) & (uv[:, 1] < camera.height)
                    surface = np.full(len(z), np.nan)
                    surface[inside] = depth[uv[inside, 1], uv[inside, 0]]
                    seen = np.isfinite(surface)
                    idx = np.flatnonzero(ok)
                    tested[k][h, idx[seen]] += 1
                    violations[k][h, idx[seen & (surface - z > free_space)]] += 1
    distance = np.full((len(ks), 2, 21), np.nan)
    free = np.zeros((len(ks), 2, 21), bool)
    seen = np.zeros((len(ks), 2, 21), bool)
    for i, k in enumerate(ks):
        cloud = np.concatenate(clouds[k]) if clouds[k] else np.zeros((0, 3))
        if len(cloud) < 50:
            continue
        tree = cKDTree(cloud)
        for h in range(2):
            ok = use[k, h]
            if ok.any():
                distance[i, h, ok] = tree.query(joints[k, h][ok])[0]
        free[i] = violations[k] > 0
        seen[i] = tested[k] > 0
    return {"distance": distance, "free": free, "seen": seen, "cameras": cameras}


def summarize(report, label=""):
    d, free, seen = report["distance"], report["free"], report["seen"]
    print(f"\n{label} depth check with {', '.join(report['cameras'])} (keypoints moving < 0.1 m/s)")
    print(f"{'':6s}{'':15s}{'n':>6s}  {'surface distance mm: median':>28s}  {'p90':>5s}  {'>20mm':>6s}  {'in free space':>13s}")
    for h, hand in enumerate(HANDS):
        for group, members in GROUPS.items():
            values = d[:, h][:, members] * 1000
            ok = np.isfinite(values)
            if ok.sum() < 20:
                continue
            v = values[ok]
            f = free[:, h][:, members][seen[:, h][:, members]]
            print(f"{hand:6s}{group:15s}{ok.sum():6d}  {np.median(v):28.1f}  {np.percentile(v, 90):5.1f}  "
                  f"{100 * (v > 20).mean():5.0f}%  {100 * f.mean():12.0f}%")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--result", nargs="+", default=["hands3d.npz"])
    parser.add_argument("--fitted", action="store_true", help="also check the skeleton fit")
    args = parser.parse_args()
    session = Session(args.session)
    for name in args.result:
        result = dict(np.load(session.recon / name))
        summarize(check(session, result), name)
        if args.fitted and "fitted" in result:
            result["joints"] = np.where(np.isfinite(result["fitted"]), result["fitted"], result["joints"])
            result["valid"] = result["valid"] & np.isfinite(result["fitted"]).all(-1)
            summarize(check(session, result), name + " (skeleton fit)")


if __name__ == "__main__":
    main()
