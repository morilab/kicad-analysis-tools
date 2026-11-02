"""熱の面内伝導 (core/raster.edge_conduct = path_conduct k=None) の手計算テスト (2026-10-16)。
  1. 一様な銅 f = 1、銅なし f = 0
  2. 縁のセル: 銅がセル中心に届かないセル (配線の縁) も隣とつながる (端の空の列を除く)。d495b2f の式 (trim=False) では 0
  3. セル内の隙間: 空の端の向こうに銅があれば 0 (隙間を短絡しない)
  4. 空のセル (銅なし) を端の除外で素通りさせない: 銅 | 空セル | 銅 の両辺は 0
  5. 直線配線 (水平): f の合計 = 線幅/dx (細分の量子化の範囲)
  6. 45° の細い配線 (0.15 mm、dx 0.25): 単位長さの熱抵抗 / 真値 が 0.8〜1.7 (trim=False では最大 9 倍)
  7. サーマルスポーク: 隙間 0.2 mm + スポーク 0.25 mm を 8 通りの位置で、0.025 mm 格子 (旧 cell 方式) の基準の ±25%
使い方: python tests/test_thermal_edgecond.py"""
import math, os, sys
import numpy as np, scipy.sparse as sp, scipy.sparse.linalg as sla
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from core import raster

K, KF, T = 385.0, 0.81, 35e-6
FAIL = []


def check(ok, msg):
    print(('ok   ' if ok else 'FAIL ') + msg)
    if not ok:
        FAIL.append(msg)


def box(nx, ny, dx=0.25):
    g = raster.Grid([[(0, 0), (nx * dx, 0), (nx * dx, ny * dx), (0, ny * dx)]], dx, 4)
    return g, np.zeros(g.fine_board.shape, np.int32)


def gedges(g, lab, method, trim=True):
    if method == 'cell':
        phi = g.coarsen(lab != 0); gc = (phi * K + (1 - phi) * KF) * T; hm = lambda p, q: 2 * p * q / (p + q)
        return hm(gc[:, :-1], gc[:, 1:]), hm(gc[:-1, :], gc[1:, :])
    f = raster.edge_conduct(g, lab, trim=trim)
    return [(f[a] * K + (1 - f[a]) * KF) * T for a in ('x', 'y')]


def resist(g, gx, gy, hi, lo):
    n = g.ny * g.nx; idx = np.arange(n).reshape(g.ny, g.nx)
    I = np.r_[idx[:, :-1].ravel(), idx[:-1, :].ravel()]; J = np.r_[idx[:, 1:].ravel(), idx[1:, :].ravel()]; G = np.r_[gx.ravel(), gy.ravel()]
    A = sp.coo_matrix((np.r_[-G, -G], (np.r_[I, J], np.r_[J, I])), (n, n)).tocsr(); Lap = (sp.diags(-np.asarray(A.sum(1)).ravel()) + A).tocsr()
    hi, lo = hi.ravel(), lo.ravel(); fx = hi | lo; V = np.zeros(n); V[hi] = 1; fi = np.nonzero(~fx)[0]
    V[fi] = sla.spsolve(Lap[fi][:, fi].tocsc(), -(Lap[fi][:, np.nonzero(fx)[0]] @ V[fx]))
    return 1 / float((Lap[np.nonzero(hi)[0]] @ V).sum())


# 1
g, lab = box(4, 3); lab[:] = 1; f = raster.edge_conduct(g, lab)
check(np.allclose(f['x'], 1) and np.allclose(f['y'], 1), '一様な銅で f = 1')
g, lab = box(4, 3); f = raster.edge_conduct(g, lab)
check(not f['x'].any() and not f['y'].any(), '銅なしで f = 0')
# 2 縁のセル: 行 1 は全面銅、行 2 は上の 1 細分行だけ (中心に届かない)
g, lab = box(4, 4); lab[4:8, :] = 1; lab[8, :] = 1
f = raster.edge_conduct(g, lab); f0 = raster.edge_conduct(g, lab, trim=False)
check(np.all(f['y'][1] > 0.99) and np.all(f['x'][2] > 0.2), f"縁の行もつながる: 縦 f = {f['y'][1].min():.2f}、縁の行の横 f = {f['x'][2].min():.2f}")
check(np.all(f0['y'][1] == 0), 'trim=False (d495b2f の式) では縁の行が縦に 0 (切り離される)')
# 3 セル内の隙間: セル (1,1) の中央の 2 細分列が空 (両側に銅)
g, lab = box(3, 3); lab[:, :] = 1; lab[:, 5:7] = 0
f = raster.edge_conduct(g, lab)
check(f['x'][1, 0] == 0 and f['x'][1, 1] == 0, f"セル内の隙間 (両側に銅) は 0: {f['x'][1, 0]:.2f} / {f['x'][1, 1]:.2f}")
# 4 空のセル
g, lab = box(3, 1); lab[:, 0:4] = 1; lab[:, 8:12] = 1
f = raster.edge_conduct(g, lab)
check(f['x'][0, 0] == 0 and f['x'][0, 1] == 0, f"銅 | 空セル | 銅 は 0: {f['x'][0].tolist()}")
# 5 水平の直線配線: 幅 0.3 mm、dx 0.25、位置 4 通り
for oy in (0.0, 0.3, 0.55, 0.8):
    g, lab = box(8, 6); X, Y = np.meshgrid(g.fx, g.fy); y0 = 0.5 + oy * 0.25
    lab[np.abs(Y - y0) < 0.15] = 1; f = raster.edge_conduct(g, lab)
    w = (np.abs(g.fy - y0) < 0.15).sum() * g.fs
    check(abs(f['x'][:, 3].sum() * 0.25 - w) < 1e-6, f"水平配線 (中心 y={y0:.3f}): Σf·dx = {f['x'][:, 3].sum() * 0.25:.4f} mm = 細分の線幅 {w:.4f} mm")


# 6 45° の細い配線
def track45(W, dx, off, L, trim):
    c = s = math.sqrt(0.5); m = math.ceil(W / dx + 3) * dx
    a = np.array([m + off[0] * dx, m + off[1] * dx]); b = a + L * np.array([c, s])
    g = raster.Grid([[(0, 0), (b[0] + m, 0), (b[0] + m, b[1] + m), (0, b[1] + m)]], dx, 4); lab = np.zeros(g.fine_board.shape, np.int32)
    X, Y = np.meshgrid(g.fx, g.fy); lab[raster._capsule(X, Y, tuple(a), tuple(b), W / 2)] = 1
    XC, YC = np.meshgrid(g.xc, g.yc); t = (XC - a[0]) * c + (YC - a[1]) * s; band = 2 * W + 2 * dx
    gx, gy = gedges(g, lab, 'edge', trim)
    return resist(g, gx, gy, t < band, t > L - band), L - 2 * band


for trim in (True, False):
    ks = []
    for oy in np.arange(0, 1, 0.125):
        W, dx = 0.15, 0.25; (R1, D1), (R2, D2) = [track45(W, dx, (0.3, oy), L, trim) for L in (12 * dx + 8 * W, 24 * dx + 16 * W)]
        ks.append((R2 - R1) / (D2 - D1) * 1e3 * K * T * W * 1e-3)
    if trim:
        check(0.8 <= min(ks) and max(ks) <= 1.7, f"45° 0.15 mm 配線 (dx 0.25): 熱抵抗/真値 = {min(ks):.2f}〜{max(ks):.2f} (0.8〜1.7)")
    else:
        print(f"     参考 trim=False: {min(ks):.2f}〜{max(ks):.2f}")


# 7 サーマルスポーク
def spoke(dx, off, method):
    gap, ws, Lc, Wd, m = 0.2, 0.25, 2.0, 2.0, 1.0
    x1 = m + off * dx + Lc; x2 = x1 + gap; nx = int(math.ceil((x2 + Lc + m) / dx)); ny = int(math.ceil((Wd + 2 * m) / dx))
    g, lab = box(nx, ny, dx); X, Y = np.meshgrid(g.fx, g.fy); yin = (Y > m) & (Y < m + Wd)
    lab[yin & (X > m + off * dx) & (X < x1)] = 1; lab[yin & (X > x2) & (X < x2 + Lc)] = 1
    lab[(np.abs(Y - (m + Wd / 2)) < ws / 2) & (X >= x1) & (X <= x2)] = 1
    XC, _ = np.meshgrid(g.xc, g.yc); gx, gy = gedges(g, lab, method)
    return resist(g, gx, gy, XC < m + off * dx + 0.5, XC > x2 + Lc - 0.5)


ref = np.median([spoke(0.025, o, 'cell') for o in (0.0, 0.5)])
rs = [spoke(0.25, o, 'edge') for o in np.arange(0, 1, 0.125)]
check(all(abs(r / ref - 1) <= 0.25 for r in rs), f"スポーク (隙間 0.2、幅 0.25、dx 0.25): {min(rs):.0f}〜{max(rs):.0f} K/W、基準 {ref:.0f} K/W (±25%)")
print('thermal edgecond:', 'すべて ok' if not FAIL else f'{len(FAIL)} 件 FAIL')
sys.exit(1 if FAIL else 0)
