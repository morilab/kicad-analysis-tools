"""diag — 斜め配線の線幅ベース補正 (IR ドロップ用、熱は使わない)。
4 近傍の格子では斜めの細い配線が階段になり、抵抗を大きめに出す (dx 0.1 mm の 0.2 mm 線で 45° +12%、IR ドロップ担当 2026-10-14 測定)。
補正: 同じ角度・線幅/dx の直線配線を小さな格子に同じ方法 (細分ラスタ → edges) で描き、単位長さあたりの抵抗 r_fd を数値で求め、
真値 r = 1/(σ t W) との比 k = r_fd·σtW を出す。その配線だけが横切るエッジの w に k を掛ける (w は 1 を超えることがある)。
k は (角度 1° 刻み、W/dx 0.05 刻み、sub) でキャッシュ。配線の位置ずれ 3 通りの平均。水平・垂直 (±1° 以内) は補正しない。"""
import math
from functools import lru_cache
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as sla
from . import raster

AXIS_TOL_DEG = 1.0


def _r_per_len(theta, W, dx, sub, off, L):
    c, s = math.cos(theta), math.sin(theta)
    m = math.ceil(W + 3) * dx   # 格子の原点に整列 (off = 配線始点の格子内の位置)
    a = np.array([m + off[0] * dx, m + math.ceil(L * max(-s, 0) / dx) * dx + off[1] * dx]); b = a + L * np.array([c, s])
    outline = [[(0, 0), (b[0] + m, 0), (b[0] + m, max(a[1], b[1]) + m), (0, max(a[1], b[1]) + m)]]
    g = raster.Grid(outline, dx, sub)
    lab = np.zeros(g.fine_board.shape, np.int32)
    sl, X, Y = g.window(min(a[0], b[0]) - W, min(a[1], b[1]) - W, max(a[0], b[0]) + W, max(a[1], b[1]) + W)
    lab[sl][raster._capsule(X, Y, tuple(a), tuple(b), W / 2)] = 1
    e = raster.edges(g, lab)
    n = g.ny * g.nx; idx = np.arange(n).reshape(g.ny, g.nx)
    I, J, G = [], [], []
    for ax, (w, net) in e.items():
        ok = (net == 1) & (w > 0)
        p = idx[:, :-1][ok] if ax == 'x' else idx[:-1, :][ok]; q = idx[:, 1:][ok] if ax == 'x' else idx[1:, :][ok]
        I += [p]; J += [q]; G += [w[ok]]
    I = np.concatenate(I); J = np.concatenate(J); G = np.concatenate(G).astype(float)
    XC, YC = np.meshgrid(g.xc, g.yc); t = ((XC - a[0]) * c + (YC - a[1]) * s).ravel()
    band = 2 * W + 2 * dx
    hi = t < band; lo = t > L - band
    used = np.zeros(n, bool); used[I] = used[J] = True
    free = used & ~hi & ~lo
    V = np.zeros(n); V[hi] = 1.0
    A = sp.coo_matrix((np.r_[-G, -G], (np.r_[I, J], np.r_[J, I])), (n, n)).tocsr()
    D = np.asarray(-A.sum(axis=1)).ravel()
    Lap = (sp.diags(D) + A).tocsr()
    fi = np.nonzero(free)[0]
    rhs = -(Lap[fi][:, np.nonzero(hi | lo)[0]] @ V[hi | lo])
    V[fi] = sla.spsolve(Lap[fi][:, fi].tocsc(), rhs)
    cur = float((Lap[np.nonzero(hi)[0]] @ V).sum())
    return 1.0 / cur, L - 2 * band


@lru_cache(maxsize=65536)
def factor(deg, w_dx, sub, off=None):
    """k = r_fd / r_true (> 1 なら格子が抵抗を大きく出している)。deg は 0..180 の角度 (整数)、w_dx = W/dx (0.05 刻み)、
    off = 配線始点の格子内の位置 (x, y)/dx (0.1 刻み)。45° 付近は位置で 2〜12% と大きく変わるので、実際の位置で解く。None は 3 通りの平均"""
    d90 = deg % 90
    if d90 <= AXIS_TOL_DEG or d90 >= 90 - AXIS_TOL_DEG:
        return 1.0
    dx = 1.0; W = w_dx * dx; th = math.radians(deg if deg <= 90 else deg - 180); ks = []
    for off in ((off,) if off is not None else ((0.0, 0.0), (0.37, 0.61), (0.73, 0.19))):
        L1, L2 = 12 * dx + 8 * W, 24 * dx + 16 * W
        R1, D1 = _r_per_len(th, W, dx, sub, off, L1); R2, D2 = _r_per_len(th, W, dx, sub, off, L2)
        ks.append((R2 - R1) / (D2 - D1) * W)
    return float(np.mean(ks))


def track_factor(t, g):
    """g = raster.Grid。始点を左 (x 小) にそろえ、角度 0..180 (y 下向き) と格子内の位置で k を引く"""
    (ax, ay), (bx, by) = t['a'], t['b']
    if ax == bx and ay == by:
        return 1.0
    if (bx, by) < (ax, ay):
        ax, ay, bx, by = bx, by, ax, ay
    deg = int(round(math.degrees(math.atan2(by - ay, bx - ax)))) % 180
    ox = ((ax - g.x0) / g.dx) % 1.0; oy = ((ay - g.y0) / g.dx) % 1.0   # deg > 90 = 右上がり (y 下向き) もそのまま解く
    q = lambda v: round(round(v * 20) / 20 % 1.0, 2)   # 0.05 dx 刻み (45° は 0.1 刻みだと ±3% ずれる)
    return factor(deg, round(t['w'] / g.dx / 0.05) * 0.05, g.sub, (q(ox), q(oy)))
