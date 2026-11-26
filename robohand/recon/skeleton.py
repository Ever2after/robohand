"""Fit a per-subject hand skeleton to triangulated keypoints.

Triangulated keypoints are independent points: bone lengths wobble and fingers can bend
sideways. This module rebuilds each hand from

* a rigid palm (wrist + the four finger knuckles), fitted per frame with weighted Kabsch,
* constant bone lengths, measured once per subject as medians over well-seen frames,
* bone directions taken from the observations, with the two distal finger joints (PIP, DIP)
  kept in the finger's flexion plane, as hinges.

The output keeps the 21-keypoint layout and adds a palm frame per hand (origin at the wrist;
y towards the middle knuckle; z the palm normal pointing out of the *back* of the hand for both
hands; x completes a right-handed frame). Retargeting reads joint angles from these.
"""
from __future__ import annotations

import numpy as np

from robohand.recon.hands3d import BONES

PALM = [0, 5, 9, 13, 17]
FINGERS = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14, 15, 16], [17, 18, 19, 20]]
PARENT = {b: a for a, b in BONES}


def _unit(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n > 0, n, 1)


def palm_frame(joints: np.ndarray, side: int) -> np.ndarray:
    """(..., 21, 3) -> (..., 3, 3) rotation, columns x, y, z. side 0 = left, 1 = right."""
    y = _unit(joints[..., 9, :] - joints[..., 0, :])
    across = joints[..., 5, :] - joints[..., 17, :]                  # little -> index knuckle
    z = _unit(np.cross(y, across) if side == 1 else np.cross(across, y))   # back of the hand
    x = np.cross(y, z)
    return np.stack([x, y, z], axis=-1)


def kabsch(template: np.ndarray, observed: np.ndarray, weight: np.ndarray):
    """R, t minimizing sum w |R template + t - observed|^2 (template, observed (N,3))."""
    w = weight / weight.sum()
    a = template - (w[:, None] * template).sum(0)
    b = observed - (w[:, None] * observed).sum(0)
    U, _, Vt = np.linalg.svd((w[:, None] * a).T @ b)
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    t = (w[:, None] * observed).sum(0) - R @ (w[:, None] * template).sum(0)
    return R, t


class HandModel:
    """Subject-specific bone lengths and palm template for one hand."""

    def __init__(self, lengths: dict, palm: np.ndarray, side: int):
        self.lengths, self.palm, self.side = lengths, palm, side

    @classmethod
    def calibrate(cls, joints: np.ndarray, valid: np.ndarray, views: np.ndarray, side: int, min_views: int = 3):
        """From (T, 21, 3) tracks: frames where all palm points are well seen define the palm,
        each bone's length is the median over frames where both its ends are."""
        good = valid & (views >= min_views)
        lengths = {}
        for a, b in BONES:
            ok = good[:, a] & good[:, b]
            if ok.sum() < 10:
                ok = valid[:, a] & valid[:, b]
            lengths[(a, b)] = float(np.median(np.linalg.norm(joints[ok, b] - joints[ok, a], axis=-1)))
        ok = good[:, PALM].all(1)
        if ok.sum() < 10:
            ok = valid[:, PALM].all(1)
        R = palm_frame(joints[ok], side)
        local = np.einsum("tji,tpj->tpi", R, joints[ok][:, PALM] - joints[ok][:, None, 0])
        return cls(lengths, np.median(local, axis=0), side)

    def fit(self, joints: np.ndarray, weight: np.ndarray):
        """(T,21,3) observations and (T,21) weights -> fitted (T,21,3) and palm frames (T,3,3), origins (T,3)."""
        T = len(joints)
        fitted = np.full_like(joints, np.nan)
        frames = np.full((T, 3, 3), np.nan)
        origins = np.full((T, 3), np.nan)
        for k in range(T):
            w = weight[k]
            if (w[PALM] > 0).sum() < 3:
                continue
            ok = w[PALM] > 0
            R, t = kabsch(self.palm[ok], joints[k, PALM][ok], w[PALM][ok])
            frames[k], origins[k] = R, t
            out = np.full((21, 3), np.nan)
            out[PALM] = self.palm @ R.T + t
            normal = R[:, 2]
            for chain in FINGERS:
                base = chain[0]
                if base not in PALM:                                   # thumb: CMC hangs off the wrist
                    d = joints[k, base] - out[0] if w[base] > 0 else None
                    if d is None:
                        continue
                    out[base] = out[0] + _unit(d) * self.lengths[(0, base)]
                previous = None
                for parent, child in zip(chain[:-1], chain[1:]):
                    if w[child] <= 0 or not np.isfinite(out[parent]).all():
                        break
                    d = _unit(joints[k, child] - out[parent])
                    if previous is not None and chain[0] != 1:         # finger PIP/DIP: hinge
                        axis = np.cross(previous, normal)              # flexion axis, across the finger
                        if np.linalg.norm(axis) < 0.3:                 # finger folded along the normal
                            axis = R[:, 0]
                        axis = _unit(axis)
                        d = _unit(d - np.dot(d, axis) * axis)
                    out[child] = out[parent] + d * self.lengths[(parent, child)]
                    previous = d
            fitted[k] = out
        return fitted, frames, origins
