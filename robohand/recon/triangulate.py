"""Robust multi-view triangulation, vectorised over many points.

Observations are undistorted normalized image coordinates. Residuals are *metric*: the
image-plane error times the point's depth, about the distance between the 3D point and the
observed ray. A keypoint detector's error grows with how small the hand is in the image, so a
metric threshold treats a close-up D405 and a distant 4K webcam alike.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np


def projection_matrices(cameras) -> np.ndarray:
    """(V, 3, 4) [R | t] for normalized coordinates."""
    return np.stack([np.hstack([c.R, c.t[:, None]]) for c in cameras])


def dlt(P: np.ndarray, x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted linear triangulation. P (V,3,4), x (M,V,2), w (M,V) -> X (M,3); NaN with < 2 views."""
    rows_x = x[..., 0, None] * P[None, :, 2] - P[None, :, 0]
    rows_y = x[..., 1, None] * P[None, :, 2] - P[None, :, 1]
    A = np.concatenate([rows_x, rows_y], axis=1) * np.concatenate([w, w], axis=1)[..., None]
    X = np.full((x.shape[0], 3), np.nan)
    ok = (w > 0).sum(1) >= 2
    if ok.any():
        _, _, vt = np.linalg.svd(A[ok])
        h = vt[:, -1]
        with np.errstate(divide="ignore", invalid="ignore"):
            X[ok] = h[:, :3] / h[:, 3:]
    return X


def residuals(P: np.ndarray, X: np.ndarray, x: np.ndarray):
    """Metric residual (M,V) and depth (M,V) of points X (M,3) against observations x (M,V,2)."""
    Xc = np.einsum("vij,mj->mvi", P[:, :, :3], X) + P[None, :, :, 3]
    depth = Xc[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        error = np.linalg.norm(Xc[..., :2] / depth[..., None] - x, axis=-1) * np.abs(depth)
    return error, depth


def refine(P: np.ndarray, X: np.ndarray, x: np.ndarray, w: np.ndarray, iterations: int = 3) -> np.ndarray:
    """Gauss-Newton on the weighted metric residual, starting from X."""
    R, t = P[:, :, :3], P[:, :, 3]
    X = X.copy()
    ok = np.isfinite(X).all(1) & ((w > 0).sum(1) >= 2)
    for _ in range(iterations):
        Xc = np.einsum("vij,mj->mvi", R, X[ok]) + t[None]
        z = Xc[..., 2:3]
        proj = Xc[..., :2] / z
        r = (proj - x[ok]) * z                                         # (m,V,2), ~metric
        # d(proj)/dXc = [[1/z, 0, -x/z^2], [0, 1/z, -y/z^2]]; scale by z (held fixed)
        J = np.zeros(Xc.shape[:2] + (2, 3))
        J[..., 0, 0] = 1
        J[..., 1, 1] = 1
        J[..., 0, 2] = -proj[..., 0]
        J[..., 1, 2] = -proj[..., 1]
        J = J @ R[None]                                                 # (m,V,2,3)
        ww = w[ok][..., None, None]
        H = np.einsum("mvki,mvkj->mij", J * ww, J)
        g = np.einsum("mvki,mvk->mi", J * ww, r)
        step = np.linalg.solve(H + 1e-9 * np.eye(3), g[..., None])[..., 0]
        X[ok] -= step
    return X


def robust(P: np.ndarray, x: np.ndarray, w: np.ndarray, threshold: float = 0.012):
    """MSAC over all view pairs, then weighted refinement on the inliers.

    Returns X (M,3), inlier mask (M,V), residual (M,V). Points seen in fewer than two views,
    or with no pair that agrees, are NaN.
    """
    M, V = w.shape
    seen = w > 0
    best_cost = np.full(M, np.inf)
    best = np.full((M, 3), np.nan)
    for a, b in combinations(range(V), 2):
        pair = seen[:, a] & seen[:, b]
        if not pair.any():
            continue
        wp = np.zeros_like(w)
        wp[:, [a, b]] = w[:, [a, b]]
        X = dlt(P, x[pair], wp[pair])
        error, depth = residuals(P, X, x[pair])
        error = np.where(depth > 0, error, np.inf)
        cost = np.where(seen[pair], np.minimum(error, threshold) ** 2, 0).sum(1)
        cost[~(error[:, [a, b]] < threshold).all(1)] = np.inf          # the pair itself must agree
        index = np.flatnonzero(pair)
        better = cost < best_cost[index]
        best_cost[index[better]] = cost[better]
        best[index[better]] = X[better]
    X = best
    for _ in range(2):
        error, depth = residuals(P, X, x)
        inliers = seen & (error < threshold) & (depth > 0)
        X = refine(P, dlt(P, x, np.where(inliers, w, 0)), x, np.where(inliers, w, 0))
    error, depth = residuals(P, X, x)
    inliers = seen & (error < threshold) & (depth > 0)
    X[inliers.sum(1) < 2] = np.nan
    return X, inliers, error
