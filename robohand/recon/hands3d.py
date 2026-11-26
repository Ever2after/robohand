"""3D hand keypoints in the board frame from the per-camera 2D keypoints of ``hands2d``.

1. Every camera's 2D keypoints are resampled onto one 30 Hz grid by linear interpolation
   between its own frames. This removes the up-to-half-frame skew between unsynchronised cameras.
2. A constant delay per camera is estimated by leaving the camera out, triangulating from the
   rest, and choosing the delay that best fits the left-out camera on moving keypoints.
3. Each keypoint is triangulated robustly (``triangulate.robust``), then smoothed in time with
   a weighted Whittaker smoother. Keypoints that disagree with the smoothed track are dropped
   and the track is smoothed again.

Writes ``<session>/recon/hands3d.npz`` and prints quality figures.

    python -m robohand.recon.hands3d <session folder> [--source auto|wholebody|hand5]
"""
from __future__ import annotations

import argparse
import json

import numpy as np
from scipy.linalg import solveh_banded

from robohand.recon import triangulate as tri
from robohand.recon.session import Session

RATE = 30.0
REFERENCE = "d435_1"           # its delay is defined as 0
SCORE_RANGE = {"wholebody": (2.0, 6.0), "hand5": (0.2, 0.6), "guided": (0.2, 0.6)}   # score -> weight 0..1
SOURCES = ("wholebody", "hand5", "guided")
THRESHOLD = 0.012              # m, inlier distance from an observed ray
MAX_GAP = 0.08                 # s, longest interval interpolated between two frames of one camera
BONES = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (0, 9), (9, 10), (10, 11),
         (11, 12), (0, 13), (13, 14), (14, 15), (15, 16), (0, 17), (17, 18), (18, 19), (19, 20)]
HANDS = ("left", "right")


class Observations:
    """One camera's 2D keypoints, resampled at arbitrary times."""

    def __init__(self, camera, data: dict, source: str):
        self.camera = camera
        self.times = data["times"]
        self.source = source
        keypoints = data[source]
        low, high = SCORE_RANGE[source]
        self.uv = keypoints[..., :2].astype(float)
        self.weight = np.clip((keypoints[..., 2] - low) / (high - low), 0, 1)
        self.weight[~np.isfinite(self.uv).all(-1)] = 0

    def at(self, times: np.ndarray, offset: float):
        """uv (T,2,21,2) pixels and weight (T,2,21) at session times ``times``."""
        corrected = self.times - offset
        right = np.clip(np.searchsorted(corrected, times), 1, len(corrected) - 1)
        left = right - 1
        span = corrected[right] - corrected[left]
        alpha = np.clip((times - corrected[left]) / np.where(span > 0, span, 1), 0, 1)
        a = alpha[:, None, None, None]
        uv = (1 - a) * self.uv[left] + a * self.uv[right]
        weight = np.minimum(self.weight[left], self.weight[right])
        weight[(span > MAX_GAP) | (times < corrected[0]) | (times > corrected[-1])] = 0
        return uv, weight

    def normalized_at(self, times, offset):
        uv, weight = self.at(times, offset)
        return self.camera.normalize(uv), weight


def stack(observations, times, offsets):
    """x (M,V,2) normalized and w (M,V) for M = T*2*21 points."""
    xs, ws = [], []
    for obs, offset in zip(observations, offsets):
        x, w = obs.normalized_at(times, offset)
        xs.append(x.reshape(-1, 2))
        ws.append(w.reshape(-1))
    return np.stack(xs, 1), np.stack(ws, 1)


def estimate_offsets(observations, times, rounds: int = 2, search=0.12, step=0.004, verbose=True):
    """Per-camera delay (s) that best explains each camera given the others."""
    V = len(observations)
    P = tri.projection_matrices([o.camera for o in observations])
    offsets = np.zeros(V)
    reference = next((i for i, o in enumerate(observations) if o.camera.name == REFERENCE), 0)
    sample = times[::2]
    candidates = np.arange(-search, search + step / 2, step)
    for _ in range(rounds):
        x, w = stack(observations, sample, offsets)
        for v in range(V):
            others = [u for u in range(V) if u != v]
            X, inliers, _ = tri.robust(P[others], x[:, others], w[:, others], THRESHOLD)
            track = X.reshape(len(sample), -1, 3)
            speed = np.linalg.norm(np.gradient(track, sample, axis=0), axis=-1).reshape(-1)
            moving = np.isfinite(speed) & (speed > 0.05) & (inliers.sum(1) >= 3)
            if moving.sum() < 50:
                continue
            costs = []
            for delta in candidates:
                xv, wv = observations[v].normalized_at(sample, offsets[v] + delta)
                error, _ = tri.residuals(P[[v]], X[moving], xv.reshape(-1, 1, 2)[moving])
                good = wv.reshape(-1)[moving] > 0.3
                costs.append(np.mean(np.minimum(error[good, 0], 0.03) ** 2))
            costs = np.asarray(costs)
            best = int(np.argmin(costs))
            if 0 < best < len(costs) - 1:            # parabolic sub-step
                c0, c1, c2 = costs[best - 1: best + 2]
                denominator = c0 - 2 * c1 + c2
                shift = 0.5 * (c0 - c2) / denominator if denominator > 0 else 0.0
            else:
                shift = 0.0
            offsets[v] += candidates[best] + shift * step
        offsets -= offsets[reference]
        if verbose:
            print("offsets ms:", {o.camera.name: round(1000 * d, 1) for o, d in zip(observations, offsets)})
    return offsets


def whittaker(y: np.ndarray, w: np.ndarray, lam: float) -> np.ndarray:
    """Weighted Whittaker smoother along axis 0 with a second-difference penalty.
    y (T, K) with NaN allowed where w == 0."""
    T = y.shape[0]
    # D^T D for second differences, as symmetric banded upper form (3, T)
    diag = np.full(T, 6.0)
    diag[[0, -1]] = 1
    diag[[1, -2]] = 5
    off1 = np.full(T - 1, -4.0)
    off1[[0, -1]] = -2
    off2 = np.ones(T - 2)
    out = np.empty_like(y)
    for k in range(y.shape[1]):
        wk = w[:, k]
        ab = np.zeros((3, T))
        ab[2] = lam * diag + wk
        ab[1, 1:] = lam * off1
        ab[0, 2:] = lam * off2
        out[:, k] = solveh_banded(ab, np.where(wk > 0, wk * np.nan_to_num(y[:, k]), 0.0))
    return out


def smooth(raw: np.ndarray, weight: np.ndarray, cutoff_hz: float = 6.0, rounds: int = 2,
           reject: float = 0.015, max_gap_s: float = 0.25):
    """Smooth (T, J, 3) tracks; returns smoothed, used-weight, valid mask (T, J)."""
    T, J, _ = raw.shape
    s4 = np.sin(np.pi * cutoff_hz / RATE) ** 4
    lam = 1.0 / (16 * s4)                       # half-power at cutoff_hz for unit weights
    w = np.where(np.isfinite(raw).all(-1), weight, 0.0)
    for round_ in range(rounds + 1):
        sm = whittaker(raw.reshape(T, -1), np.repeat(w, 3, axis=1), lam).reshape(T, J, 3)
        if round_ == rounds:
            break
        distance = np.linalg.norm(np.nan_to_num(raw) - sm, axis=-1)
        w = np.where(distance < reject, w, 0.0)
    # valid where an observation is within max_gap_s
    valid = np.zeros((T, J), bool)
    gap = int(round(max_gap_s * RATE))
    for j in range(J):
        observed = np.flatnonzero(w[:, j] > 0)
        if len(observed) == 0:
            continue
        index = np.arange(T)
        nearest = np.clip(np.searchsorted(observed, index), 1, len(observed) - 1)
        distance = np.minimum(np.abs(observed[nearest] - index), np.abs(observed[nearest - 1] - index))
        valid[:, j] = distance <= gap // 2
        valid[:observed[0], j] = False
        valid[observed[-1] + 1:, j] = False
    return sm, w, valid


def bone_lengths(joints: np.ndarray):
    """(..., 21, 3) -> (..., 20) bone lengths."""
    return np.stack([np.linalg.norm(joints[..., b, :] - joints[..., a, :], axis=-1) for a, b in BONES], -1)


def load_observations(session: Session, source: str):
    observations = []
    for name, camera in session.cameras.items():
        path = session.recon / f"hands2d_{name}.npz"
        if not path.exists():
            continue
        data = dict(np.load(path))
        if source == "auto":
            observations.append((camera, data))
        else:
            observations.append(Observations(camera, data, source))
    return observations


def choose_sources(pairs, times, offsets, verbose=True):
    """Per camera, the 2D source that agrees better with the other cameras' wholebody points."""
    base = [Observations(c, d, "wholebody") for c, d in pairs]
    P = tri.projection_matrices([o.camera for o in base])
    sample = times[::3]
    x, w = stack(base, sample, offsets)
    chosen = []
    for v, (camera, data) in enumerate(pairs):
        others = [u for u in range(len(base)) if u != v]
        X, inliers, _ = tri.robust(P[others], x[:, others], w[:, others], THRESHOLD)
        good = inliers.sum(1) >= 3
        scores = {}
        for source in [s for s in SOURCES if s in data]:
            obs = Observations(camera, data, source)
            xv, wv = obs.normalized_at(sample, offsets[v])
            error, _ = tri.residuals(P[[v]], X[good], xv.reshape(-1, 1, 2)[good])
            seen = wv.reshape(-1)[good] > 0
            scores[source] = (np.median(error[seen, 0]) * 1000, (error[seen, 0] < THRESHOLD).mean(), seen.mean())
        pick = min(scores, key=lambda s: scores[s][0])
        if verbose:
            print(f"  {camera.name:8s} " + "  ".join(f"{s}: median {m:.1f} mm, within {100*f:.0f}%, seen {100*c:.0f}%"
                                                     for s, (m, f, c) in scores.items()) + f"  -> {pick}")
        chosen.append(Observations(camera, data, pick))
    return chosen


def reconstruct(session: Session, source: str = "auto", verbose: bool = True) -> dict:
    loaded = load_observations(session, source)
    cams = [item[0] if isinstance(item, tuple) else item.camera for item in loaded]
    start = max(c.times[0] for c in cams)
    end = min(c.times[-1] for c in cams)
    times = np.arange(start, end, 1 / RATE)
    if source == "auto":
        first = [Observations(c, d, "wholebody") for c, d in loaded]
        offsets = estimate_offsets(first, times, rounds=2, verbose=verbose)
        if verbose:
            print("2D source per camera (median distance to the other cameras' points):")
        observations = choose_sources(loaded, times, offsets, verbose=verbose)
        offsets = estimate_offsets(observations, times, rounds=1, verbose=verbose)
    else:
        observations = loaded
        offsets = estimate_offsets(observations, times, rounds=2, verbose=verbose)
    P = tri.projection_matrices([o.camera for o in observations])
    x, w = stack(observations, times, offsets)
    X, inliers, error = tri.robust(P, x, w, THRESHOLD)
    T = len(times)
    raw = X.reshape(T, 42, 3)
    views = inliers.sum(1).reshape(T, 42)
    mean_error = np.where(inliers, error, 0).sum(1) / np.maximum(inliers.sum(1), 1)
    weight = np.clip(views - 1, 0, 3) / 3.0                         # 2 views: 1/3 ... 4+ views: 1
    joints, used, valid = smooth(raw, weight)
    result = {
        "times": times, "joints": joints.reshape(T, 2, 21, 3), "raw": raw.reshape(T, 2, 21, 3),
        "valid": valid.reshape(T, 2, 21), "views": views.reshape(T, 2, 21),
        "error": mean_error.reshape(T, 2, 21), "used": (used > 0).reshape(T, 2, 21),
        "inliers": inliers.reshape(T, 2, 21, -1),
        "cameras": np.array([o.camera.name for o in observations]),
        "sources": np.array([o.source for o in observations]), "offsets": offsets,
    }
    fit_skeleton(result)
    if verbose:
        report(result)
    return result


def fit_skeleton(result: dict):
    """Add the per-subject skeleton fit (``skeleton.HandModel``): constant bones, rigid palm."""
    from robohand.recon.skeleton import HandModel

    T = len(result["times"])
    fitted = np.full((T, 2, 21, 3), np.nan)
    frames = np.full((T, 2, 3, 3), np.nan)
    origins = np.full((T, 2, 3), np.nan)
    lengths = np.full((2, len(BONES)), np.nan)
    palms = np.full((2, 5, 3), np.nan)
    for h in range(2):
        model = HandModel.calibrate(result["joints"][:, h], result["valid"][:, h], result["views"][:, h], h)
        fitted[:, h], frames[:, h], origins[:, h] = model.fit(result["joints"][:, h], result["valid"][:, h].astype(float))
        lengths[h] = [model.lengths[bone] for bone in BONES]
        palms[h] = model.palm
    result.update(fitted=fitted, palm_rotation=frames, palm_origin=origins, bone_lengths=lengths, palm_template=palms)


def report(result: dict):
    valid, views, used = result["valid"], result["views"], result["used"]
    print(f"\n{len(result['times'])} frames, {result['times'][0]:.2f}-{result['times'][-1]:.2f} s")
    for h, hand in enumerate(HANDS):
        v = valid[:, h]
        frames_ok = v.all(-1).mean()
        print(f"{hand:5s}: all 21 joints valid in {100 * frames_ok:.0f}% of frames; "
              f"triangulated {100 * used[:, h].mean():.0f}% of joint-frames; "
              f"inlier views median {np.median(views[:, h][used[:, h]]):.0f}; "
              f"ray distance median {1000 * np.median(result['error'][:, h][used[:, h]]):.1f} mm")
        lengths = bone_lengths(result["joints"][:, h])
        ok = v[:, [a for a, _ in BONES]] & v[:, [b for _, b in BONES]]
        cv = [np.std(lengths[ok[:, i], i]) / np.mean(lengths[ok[:, i], i]) for i in range(len(BONES)) if ok[:, i].sum() > 10]
        median = [np.median(lengths[ok[:, i], i]) * 1000 for i in range(len(BONES)) if ok[:, i].sum() > 10]
        print(f"       bone length: median per bone {np.round(median, 0).astype(int).tolist()} mm, "
              f"variation (std/mean) median {100 * np.median(cv):.1f}%, worst {100 * np.max(cv):.1f}%")
        if "fitted" in result:
            fit = result["fitted"][:, h]
            ok = np.isfinite(fit).all(-1) & v
            moved = np.linalg.norm(fit - result["joints"][:, h], axis=-1)[ok] * 1000
            print(f"       skeleton fit (constant bones, rigid palm, hinge PIP/DIP): complete in "
                  f"{100 * np.isfinite(fit).all((1, 2)).mean():.0f}% of frames; joints moved median {np.median(moved):.1f} mm, "
                  f"p90 {np.percentile(moved, 90):.1f} mm")
    inliers = result["inliers"]
    seen_used = used[..., None] & inliers
    print("share of triangulated joints each camera supports:",
          {name: f"{100 * seen_used[..., i].sum() / used.sum():.0f}%" for i, name in enumerate(result["cameras"])})


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session")
    parser.add_argument("--source", default="auto", choices=["auto", *SOURCES])
    parser.add_argument("--out", default="hands3d.npz")
    args = parser.parse_args()
    session = Session(args.session)
    result = reconstruct(session, args.source)
    out = session.recon / args.out
    np.savez_compressed(out, **result)
    out.with_suffix(".json").write_text(json.dumps({
        "cameras": result["cameras"].tolist(), "sources": result["sources"].tolist(),
        "offsets_ms": (1000 * result["offsets"]).round(1).tolist(), "rate_hz": RATE,
        "threshold_m": THRESHOLD, "frames": len(result["times"])}, indent=2))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
