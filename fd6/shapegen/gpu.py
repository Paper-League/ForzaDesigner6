"""Optional CuPy (NVIDIA CUDA) acceleration for the shape search.

Design goals (per the maintainer's brief):
  * **Lean** — one optional dependency (CuPy). No PyTorch / ONNX.
  * **Never breaks the app** — if CuPy or a CUDA device is missing, or a GPU op
    raises, every entry point degrades to a signal the engine reads as "use CPU".
  * **Output-stable** — the GPU only *ranks* candidate ellipses each iteration.
    The engine still commits the chosen shape with the CPU `composite()`, so the
    final colors and canvas are byte-for-byte the CPU result regardless of GPU.

The batched scorer is written against an array module `xp` (NumPy or CuPy) so the
exact same math runs on CPU (for unit tests, proving numerical parity with
`scoring.score_shape`) and on GPU (CuPy) on a real device.

Scope: ellipses (`rotated_ellipse` / `ellipse`) only — the engine falls back to
the CPU path for any other shape type.
"""

from __future__ import annotations

import math
import random
from typing import Optional

import numpy as np

from fd6.shapegen.shapes import Shape
from fd6.shapegen.shapes.ellipse import RotatedEllipse


# Alpha the search assumes for every candidate (matches RotatedEllipse.random /
# Ellipse.random, which fix color alpha at 128). The committed alpha is whatever
# the CPU composite picks; this constant only affects ranking.
_SEARCH_ALPHA = 128.0 / 255.0

# Memory guard: cap a single scoring mini-batch's tile tensor to this many cells
# (B * T * T). Keeps peak VRAM bounded regardless of canvas size / sample count.
_MAX_TILE_CELLS = 48_000_000

_GPU_PROBE_CACHE: Optional[bool] = None


def gpu_available() -> bool:
    """True iff CuPy is importable, a CUDA device exists, and a tiny alloc works.

    Cached after the first probe. Any failure (no package, no driver, no device,
    runtime error) returns False — callers then use the CPU path.
    """
    global _GPU_PROBE_CACHE
    if _GPU_PROBE_CACHE is not None:
        return _GPU_PROBE_CACHE
    ok = False
    try:
        import cupy as cp  # type: ignore
        if cp.cuda.runtime.getDeviceCount() > 0:
            # Force a real allocation + kernel so we fail here, not mid-render.
            _ = (cp.arange(8, dtype=cp.float32) * 2.0).sum().item()
            ok = True
    except Exception:
        ok = False
    _GPU_PROBE_CACHE = ok
    return ok


def resolve_backend(requested: str) -> str:
    """Map a profile's compute_backend ('auto'|'cpu'|'gpu') to 'cpu' or 'gpu'.

    'auto' -> 'gpu' when available else 'cpu'. 'gpu' -> 'gpu' only when actually
    available (otherwise 'cpu', so a saved profile can't wedge the app on a
    machine without a GPU). 'cpu' is always honored.
    """
    req = (requested or "auto").lower().strip()
    if req == "cpu":
        return "cpu"
    if req in ("auto", "gpu"):
        return "gpu" if gpu_available() else "cpu"
    return "cpu"


def backend_label(backend: str) -> str:
    return "GPU (CUDA)" if backend == "gpu" else "CPU"


def _xp():
    """Return the cupy module (raises if unavailable — callers guard with try)."""
    import cupy as cp  # type: ignore
    return cp


class EllipseBatchSearcher:
    """Batched random-search + hill-climb for ellipses on a chosen array module.

    Mirrors `scoring.score_shape`'s edge-weighted formula
    (`total = full_sq - region_old + region_new`, normalized by the edge-weight
    sum) and its sticker overlap-rejection, but evaluates a whole batch of
    candidates at once over per-candidate tiles.
    """

    def __init__(self, target: np.ndarray, alpha_mask: Optional[np.ndarray],
                 edge_weight: np.ndarray, xp=None) -> None:
        self.xp = xp if xp is not None else _xp()
        xp = self.xp
        self.h, self.w = target.shape[:2]
        self.target = xp.asarray(target, dtype=xp.float32)
        self.edge = xp.asarray(edge_weight, dtype=xp.float32)
        self.alpha = None if alpha_mask is None else xp.asarray(alpha_mask, dtype=xp.float32)
        # Normalizer n = (sum of edge weights) * 3 channels — matches
        # precompute_canvas_error's edge-weighted branch.
        self.n = float(self.edge.sum().item()) * 3.0

    # ── public ────────────────────────────────────────────────────────────
    def search(self, canvas: np.ndarray, n_random: int, n_mutate: int,
               max_size_frac: Optional[float], rng: random.Random) -> tuple[float, Optional[Shape]]:
        """Return (best_score, best_shape) for one iteration. Shape may be None."""
        xp = self.xp
        cur = xp.asarray(canvas, dtype=xp.float32)
        # Full-canvas weighted squared error (constant for this canvas snapshot).
        full_sq = float(((cur - self.target) ** 2 * self.edge[:, :, None]).sum().item())

        # ── random search ──
        params = self._random_params(max(1, n_random), max_size_frac, rng)
        scores, _colors = self._score_batch(params, cur, full_sq)
        bi = int(xp.argmin(scores).item())
        best_score = float(scores[bi].item())
        best = params[bi].copy()
        if not math.isfinite(best_score):
            return float("inf"), None

        # ── hill climb (batched mutations) ──
        cap = max(1, n_mutate)
        batch = min(cap, 64)
        no_improve = 0
        steps = max(1, cap // batch)
        for _ in range(steps):
            muts = self._mutate_params(best, batch, rng)
            mscores, _mc = self._score_batch(muts, cur, full_sq)
            mbi = int(xp.argmin(mscores).item())
            ms = float(mscores[mbi].item())
            if ms < best_score:
                best_score, best = ms, muts[mbi].copy()
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= max(2, steps // 4):
                    break

        cx, cy, rx, ry, ang = (float(v) for v in best)
        shape = RotatedEllipse(color=(0, 0, 0, 128), x=cx, y=cy, rx=rx, ry=ry, angle=ang)
        return best_score, shape

    # ── internals ─────────────────────────────────────────────────────────
    def _random_params(self, b: int, max_size_frac: Optional[float], rng: random.Random) -> np.ndarray:
        """B x 5 float32 (cx, cy, rx, ry, angle_deg) on host (cheap to build)."""
        w, h = self.w, self.h
        if max_size_frac is None:
            rx_cap = max(2.0, w / 8.0)
            ry_cap = max(2.0, h / 8.0)
        else:
            rx_cap = max(2.0, (w * max_size_frac) / 2.0)
            ry_cap = max(2.0, (h * max_size_frac) / 2.0)
        rs = np.random.RandomState(rng.randint(0, 2**31 - 1))
        out = np.empty((b, 5), dtype=np.float32)
        out[:, 0] = rs.uniform(0, w - 1, b)
        out[:, 1] = rs.uniform(0, h - 1, b)
        out[:, 2] = rs.uniform(1, rx_cap, b)
        out[:, 3] = rs.uniform(1, ry_cap, b)
        out[:, 4] = rs.uniform(0, 180, b)
        return out

    def _mutate_params(self, base: np.ndarray, b: int, rng: random.Random) -> np.ndarray:
        """B jittered copies of `base` (mirrors RotatedEllipse.mutate distributions)."""
        w, h = self.w, self.h
        rs = np.random.RandomState(rng.randint(0, 2**31 - 1))
        out = np.tile(base.astype(np.float32), (b, 1))
        kind = rs.randint(0, 4, b)
        # position jitter
        m0 = kind == 0
        out[m0, 0] = np.clip(out[m0, 0] + rs.normal(0, 16, m0.sum()), 0, w - 1)
        out[m0, 1] = np.clip(out[m0, 1] + rs.normal(0, 16, m0.sum()), 0, h - 1)
        # radius jitter
        m1 = kind == 1
        out[m1, 2] = np.clip(out[m1, 2] + rs.normal(0, 16, m1.sum()), 1, w)
        out[m1, 3] = np.clip(out[m1, 3] + rs.normal(0, 16, m1.sum()), 1, h)
        # angle jitter
        m2 = kind == 2
        out[m2, 4] = np.mod(out[m2, 4] + rs.normal(0, 25, m2.sum()), 180.0)
        # small combined jitter
        m3 = kind == 3
        out[m3, 0] = np.clip(out[m3, 0] + rs.normal(0, 8, m3.sum()), 0, w - 1)
        out[m3, 1] = np.clip(out[m3, 1] + rs.normal(0, 8, m3.sum()), 0, h - 1)
        out[m3, 4] = np.mod(out[m3, 4] + rs.normal(0, 15, m3.sum()), 180.0)
        return out

    def _score_batch(self, params: np.ndarray, cur, full_sq: float):
        """Score a B x 5 param array. Returns (scores[B], colors[B,3]) on `xp`.

        Mini-batches over candidates so the (B, T, T) tile tensors stay within
        the VRAM cap. T is sized to the largest ellipse in the batch.
        """
        xp = self.xp
        b_total = params.shape[0]
        # Tile side covering the biggest candidate (centered tiles, half = radius).
        max_r = float(np.max(np.maximum(params[:, 2], params[:, 3]))) if b_total else 1.0
        T = int(min(max(self.w, self.h), 2 * math.ceil(max_r) + 2))
        T = max(2, T)
        per = max(1, int(_MAX_TILE_CELLS // (T * T)))
        scores = xp.empty(b_total, dtype=xp.float32)
        colors = xp.zeros((b_total, 3), dtype=xp.float32)
        for start in range(0, b_total, per):
            sl = slice(start, min(b_total, start + per))
            s, c = self._score_chunk(xp.asarray(params[sl], dtype=xp.float32), cur, full_sq, T)
            scores[sl] = s
            colors[sl] = c
        return scores, colors

    def _score_chunk(self, p, cur, full_sq: float, T: int):
        xp = self.xp
        B = p.shape[0]
        cx = p[:, 0][:, None, None]
        cy = p[:, 1][:, None, None]
        rx = xp.maximum(p[:, 2], 1e-6)[:, None, None]
        ry = xp.maximum(p[:, 3], 1e-6)[:, None, None]
        ang = xp.deg2rad(p[:, 4])[:, None, None]
        cos_a = xp.cos(ang)
        sin_a = xp.sin(ang)

        # Centered integer tile: top-left = round(center) - T//2.
        x0 = xp.round(p[:, 0]).astype(xp.int64) - T // 2  # (B,)
        y0 = xp.round(p[:, 1]).astype(xp.int64) - T // 2
        lx = xp.arange(T)
        gx = x0[:, None, None] + lx[None, None, :]   # (B,1,T) -> broadcast
        gy = y0[:, None, None] + lx[None, :, None]   # (B,T,1)
        gx = xp.broadcast_to(gx, (B, T, T))
        gy = xp.broadcast_to(gy, (B, T, T))

        in_x = (gx >= 0) & (gx < self.w)
        in_y = (gy >= 0) & (gy < self.h)
        valid = (in_x & in_y).astype(xp.float32)        # (B,T,T)
        gxc = xp.clip(gx, 0, self.w - 1)
        gyc = xp.clip(gy, 0, self.h - 1)

        cur_t = cur[gyc, gxc]                            # (B,T,T,3)
        tgt_t = self.target[gyc, gxc]
        edge_t = self.edge[gyc, gxc] * valid             # (B,T,T) zeroed out-of-bounds

        # Rotated-ellipse mask (binary, matches RotatedEllipse.rasterize_mask).
        xrel = gx.astype(xp.float32) - cx
        yrel = gy.astype(xp.float32) - cy
        xr = cos_a * xrel + sin_a * yrel
        yr = -sin_a * xrel + cos_a * yrel
        inside = ((xr / rx) ** 2 + (yr / ry) ** 2) <= 1.0
        mask = (inside.astype(xp.float32)) * valid       # (B,T,T)

        # Effective color mask = mask ∩ alpha (sticker-safe optimal color).
        if self.alpha is not None:
            alpha_t = self.alpha[gyc, gxc] * valid
            eff = mask * (alpha_t / 255.0)
        else:
            alpha_t = None
            eff = mask

        a = _SEARCH_ALPHA
        # Closed-form optimal color over the effective-masked region. The
        # weight<0.5 guard matches compute_optimal_color exactly.
        eff_sum = eff.sum(axis=(1, 2))                   # (B,)
        denom = eff_sum * a
        numer = (eff[..., None] * (tgt_t - (1.0 - a) * cur_t)).sum(axis=(1, 2))  # (B,3)
        safe = eff_sum > 0.5
        d = xp.where(safe, denom, xp.float32(1.0))[:, None]
        color = xp.where(safe[:, None], xp.clip(numer / d, 0, 255), 0.0)
        color = xp.floor(color)  # match compute_optimal_color's int32 truncation

        # Blended tile with that color, then edge-weighted region delta.
        m = mask[..., None]
        blended = m * (a * color[:, None, None, :] + (1.0 - a) * cur_t) + (1.0 - m) * cur_t
        w_t = edge_t[..., None]
        region_old = (w_t * (cur_t - tgt_t) ** 2).sum(axis=(1, 2, 3))   # (B,)
        region_new = (w_t * (blended - tgt_t) ** 2).sum(axis=(1, 2, 3))
        total = full_sq - region_old + region_new
        n = self.n if self.n >= 1.0 else 1.0
        score = xp.sqrt(xp.maximum(total, 0.0) / n)

        # Sticker overlap rejection (matches STICKER_OVERLAP_MIN=0.995) when an
        # alpha mask is present (it always is — edge-buffer ring or silhouette).
        if alpha_t is not None:
            body = (mask >= 0.5).astype(xp.float32)
            body_total = body.sum(axis=(1, 2))
            opaque = ((alpha_t >= 128.0) & (mask >= 0.5)).astype(xp.float32).sum(axis=(1, 2))
            ratio = xp.where(body_total >= 1.0, opaque / xp.maximum(body_total, 1.0), 0.0)
            reject = (body_total < 1.0) | (ratio < 0.995)
            score = xp.where(reject, xp.float32(np.inf), score)
        return score, color
