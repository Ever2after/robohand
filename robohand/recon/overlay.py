"""Check reconstructions by eye: 3D keypoints reprojected onto every camera, tiled.

    python -m robohand.recon.overlay <session folder> --times 5 12 30      # contact sheet (JPEG)
    python -m robohand.recon.overlay <session folder> --video              # whole take (MP4)

Reprojected 3D skeletons are drawn thick (left hand cyan, right hand orange). With ``--detections``
the camera's own 2D keypoints are drawn thin in white, and grey where the triangulation
rejected them.
"""
from __future__ import annotations

import argparse

import cv2
import numpy as np

from robohand.recon.hands3d import BONES
from robohand.recon.session import Session, iter_frames

COLORS = ((255, 200, 0), (0, 140, 255))      # BGR: left cyan, right orange
TILE = (640, 360)


def crop_box(session: Session, camera, margin=0.05):
    box = camera.project(session.workspace_box(margin=margin, height=0.3))
    x0, y0 = np.maximum(box.min(0), 0)
    x1, y1 = np.minimum(box.max(0), [camera.width, camera.height])
    # widen to the tile's aspect ratio
    w, h = x1 - x0, y1 - y0
    aspect = TILE[0] / TILE[1]
    if w / h < aspect:
        grow = h * aspect - w
        x0, x1 = x0 - grow / 2, x1 + grow / 2
    else:
        grow = w / aspect - h
        y0, y1 = y0 - grow / 2, y1 + grow / 2
    return np.array([x0, y0, x1, y1])


def draw_hand(image, points, ok, color, thickness):
    for a, b in BONES:
        if ok[a] and ok[b]:
            cv2.line(image, tuple(np.int32(points[a])), tuple(np.int32(points[b])), color, thickness, cv2.LINE_AA)
    for p, good in zip(points, ok):
        if good:
            cv2.circle(image, tuple(np.int32(p)), thickness + 1, color, -1, cv2.LINE_AA)


def render_tile(session, result, camera, image, k, detections=None):
    """Draw frame k of ``result`` on this camera's image and crop it to the workspace."""
    x0, y0, x1, y1 = crop_box(session, camera)
    scale = TILE[0] / (x1 - x0)
    M = np.array([[scale, 0, -x0 * scale], [0, scale, -y0 * scale]])
    tile = cv2.warpAffine(image, M, TILE, flags=cv2.INTER_AREA, borderValue=(40, 40, 40))
    to_tile = lambda uv: uv * scale - np.array([x0, y0]) * scale
    if detections is not None:
        uv, weight, used = detections
        for h in range(2):
            draw_hand(tile, to_tile(uv[h]), weight[h] > 0, (130, 130, 130), 1)
            draw_hand(tile, to_tile(uv[h]), used[h] & (weight[h] > 0), (255, 255, 255), 1)
    for h in range(2):
        joints = result["joints"][k, h]
        ok = result["valid"][k, h]
        if ok.any():
            uv = camera.project(np.nan_to_num(joints))
            draw_hand(tile, to_tile(uv), ok, COLORS[h], 2)
    cv2.putText(tile, camera.name, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA)
    return tile


def _detections(session, result, observations, k):
    """Per camera (uv, weight, used-by-triangulation) at grid frame k, or {}."""
    out = {}
    if observations is None:
        return out
    t = result["times"][k:k + 1]
    for v, obs in enumerate(observations):
        uv, w = obs.at(t, result["offsets"][v])
        out[obs.camera.name] = (uv[0], w[0], result["inliers"][k, :, :, v])
    return out


def load_observations_for(session, result):
    from robohand.recon.hands3d import Observations
    observations = []
    for name, source in zip(result["cameras"], result["sources"]):
        data = dict(np.load(session.recon / f"hands2d_{name}.npz"))
        observations.append(Observations(session.cameras[str(name)], data, str(source)))
    return observations


def grid(tiles, columns=4):
    rows = [tiles[i:i + columns] for i in range(0, len(tiles), columns)]
    rows[-1] = rows[-1] + [np.zeros_like(tiles[0])] * (columns - len(rows[-1]))
    return np.vstack([np.hstack(r) for r in rows])


def contact_sheet(session, result, times, path, detections=True):
    names = [str(n) for n in result["cameras"]]
    observations = load_observations_for(session, result) if detections else None
    ks = [int(np.argmin(np.abs(result["times"] - t))) for t in times]
    images = {}
    for v, name in enumerate(names):
        camera = session.cameras[name]
        camera.offset = float(result["offsets"][v])
        positions = [camera.frames_at(result["times"][k:k + 1])[0] for k in ks]
        wanted = {int(camera.frames[p]): i for i, p in enumerate(positions)}
        for index, image in iter_frames(camera, wanted):
            images[(name, wanted[index])] = image
    sheets = []
    for i, k in enumerate(ks):
        det = _detections(session, result, observations, k)
        tiles = [render_tile(session, result, session.cameras[n], images[(n, i)], k, det.get(n)) for n in names]
        sheet = grid(tiles)
        cv2.putText(sheet, f"t = {result['times'][k]:.2f} s", (TILE[0] * 4 - 220, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
        sheets.append(sheet)
    cv2.imwrite(str(path), np.vstack(sheets), [cv2.IMWRITE_JPEG_QUALITY, 88])
    return path


def video(session, result, path, detections=True, every=1):
    import av

    names = [str(n) for n in result["cameras"]]
    observations = load_observations_for(session, result) if detections else None
    readers, current = {}, {}
    for v, name in enumerate(names):
        camera = session.cameras[name]
        camera.offset = float(result["offsets"][v])
        readers[name] = iter_frames(camera)
        current[name] = (-1, None)
    ks = range(0, len(result["times"]), every)
    with av.open(str(path), "w") as container:
        stream = container.add_stream("libx264", rate=int(round(30 / every)))
        stream.width, stream.height = TILE[0] * 4, TILE[1] * ((len(names) + 3) // 4)
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "23", "preset": "veryfast"}
        for k in ks:
            tiles = []
            det = _detections(session, result, observations, k)
            for name in names:
                camera = session.cameras[name]
                target = int(camera.frames[camera.frames_at(result["times"][k:k + 1])[0]])
                index, image = current[name]
                while index < target:
                    index, image = next(readers[name])
                current[name] = (index, image)
                tiles.append(render_tile(session, result, camera, image, k, det.get(name)))
            frame = grid(tiles)
            cv2.putText(frame, f"t = {result['times'][k]:.2f} s", (TILE[0] * 4 - 220, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format="bgr24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--times", nargs="*", type=float, default=[5, 12, 20, 30])
    parser.add_argument("--video", action="store_true")
    parser.add_argument("--no-detections", action="store_true")
    parser.add_argument("--result", default="hands3d.npz")
    parser.add_argument("--fitted", action="store_true", help="draw the skeleton fit instead of the smoothed points")
    parser.add_argument("--out")
    args = parser.parse_args()
    session = Session(args.session)
    result = dict(np.load(session.recon / args.result))
    if args.fitted:
        result["joints"] = result["fitted"]
        result["valid"] = np.isfinite(result["fitted"]).all(-1)
    if args.video:
        out = args.out or session.recon / "hands3d_overlay.mp4"
        print(video(session, result, out, not args.no_detections))
    else:
        out = args.out or session.recon / "hands3d_sheet.jpg"
        print(contact_sheet(session, result, args.times, out, not args.no_detections))


if __name__ == "__main__":
    main()
