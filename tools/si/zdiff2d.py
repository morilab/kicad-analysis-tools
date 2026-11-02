#!/usr/bin/env python3
"""2 層基板の差動線路 (エッジ結合マイクロストリップ / GND 付きコプレーナ = CPWG) の断面を 2D 有限差分 (有限体積) で解き、
Zodd / Zeven / Zdiff / Zcm と L・C 行列を出す。電界は準 TEM (Laplace 方程式)、損失なし。

形状 (半分だけ解く。x=0 が対称面):
  y=0 が裏面の GND ベタ、0..h が基材 (er)、y=h に厚さ t の線 (x = s/2 .. s/2+w)。
  CPWG は x >= s/2+w+g に同じ層の GND ベタ (厚さ t)。g=None なら普通のマイクロストリップ。
  ソルダーレジスト (mask=True): 基材の上 c1、線の上 c2、誘電率 cer (JLC の値 0.8 mil / 0.5 mil / 3.8)。
奇モード: 対称面 φ=0 (Dirichlet)。偶モード: 対称面で ∂φ/∂n=0 (Neumann)。
容量 C = Σ_辺 係数·(Δφ)² (V=1、離散エネルギー) なので離散化と矛盾しない。
Z = 1 / (c0 · sqrt(C · C_air))、Zdiff = 2·Zodd、Zcm = Zeven/2。

使い方:
  python3 zdiff2d.py --h 1.53 --w 0.3 --s 0.2 [--g 0.25] [--er 4.6] [--t 0.035] [--no-mask]
依存: numpy, scipy。
検証: python3 zdiff2d.py --selftest (単線 50 Ω の既知値などと比べる)
"""
import argparse, math, sys
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spl

C0 = 299792458.0
EPS0 = 8.854187817e-12
JLC = dict(er=4.6, t=0.035, c1=0.0203, c2=0.0127, cer=3.8)   # JLC の impedance 表の値 (T1 1.4 mil、C1 0.8 mil、C2 0.5 mil、CEr 3.8、7628 Er 4.6)


def _axis(fine_lo, fine_hi, d, lo, hi, must, grow=1.15):
    """[lo, hi] の節点。[fine_lo, fine_hi] は間隔 d の一様、外側は grow 倍ずつ広げる。must は必ず入れる"""
    pts = list(np.arange(fine_lo, fine_hi + d / 2, d))
    x, dx = fine_hi, d
    while x < hi:
        dx *= grow; x = min(hi, x + dx); pts.append(x)
    x, dx = fine_lo, d
    while x > lo:
        dx *= grow; x = max(lo, x - dx); pts.append(x)
    pts += [p for p in must if lo <= p <= hi]
    pts = np.unique(np.round(np.array(pts), 7))
    keep = [pts[0]]
    for p in pts[1:]:
        if p - keep[-1] > 1e-5:
            keep.append(p)
        elif p in must:
            keep[-1] = p
    return np.array(keep)


def solve(h, w, s, g=None, er=JLC['er'], t=JLC['t'], mask=True, c1=JLC['c1'], c2=JLC['c2'], cer=JLC['cer'], d=0.01, side=None, top=None):
    """戻り値 dict(zodd, zeven, zdiff, zcm, eeff_odd, eeff_even, L[2x2] H/m, C[2x2] F/m)"""
    x1, x2 = s / 2, s / 2 + w
    xg = x2 + g if g is not None else None
    side = side or (x2 + max(6 * h, 3.0))
    top = top or (h + t + max(6 * h, 3.0))
    xfine_hi = (xg + 0.3) if xg is not None else (x2 + min(1.0, 1.5 * h))
    xs = _axis(0.0, xfine_hi, d, 0.0, side, [x1, x2] + ([xg] if xg else []))
    ys = _axis(max(0.0, h - 0.2), h + t + 0.2, d / 2, 0.0, top, [h, h + t, h + c1, h + t + c2])
    nx, ny = len(xs), len(ys)
    hx, hy = np.diff(xs), np.diff(ys)
    xc, yc = (xs[:-1] + xs[1:]) / 2, (ys[:-1] + ys[1:]) / 2
    X, Y = np.meshgrid(xc, yc, indexing='ij')

    # 導体の節点: 線 (V=1) と GND (V=0)
    XN, YN = np.meshgrid(xs, ys, indexing='ij')
    in_cu = (YN >= h - 1e-9) & (YN <= h + t + 1e-9)
    trace = in_cu & (XN >= x1 - 1e-9) & (XN <= x2 + 1e-9)
    gnd = (YN <= 1e-9) | (XN >= xs[-1] - 1e-9) | (YN >= ys[-1] - 1e-9)
    if xg is not None:
        gnd |= in_cu & (XN >= xg - 1e-9)

    def eps_cells(diel):
        e = np.ones((nx - 1, ny - 1))
        if diel:
            e[Y < h] = er
            if mask:
                over_sub = (Y >= h) & (Y < h + c1)
                cu_x = ((X > x1) & (X < x2)) | ((X > xg) if xg is not None else False)
                e[over_sub & ~((Y < h + t) & cu_x)] = cer
                e[cu_x & (Y >= h + t) & (Y < h + t + c2)] = cer
        return e

    def cap(diel, odd):
        e = eps_cells(diel) * EPS0
        N = nx * ny
        idx = lambda i, j: i * ny + j
        rows, cols, vals = [], [], []
        # x 方向の辺 (i,j)-(i+1,j): 係数 = (e[i,j-1]*hy[j-1] + e[i,j]*hy[j]) / 2 / hx[i]
        ex = np.zeros((nx - 1, ny))
        ex[:, 1:] += e * hy[None, :] / 2
        ex[:, :-1] += e * hy[None, :] / 2
        kx = ex / hx[:, None]
        ey = np.zeros((nx, ny - 1))
        ey[1:, :] += e * hx[:, None] / 2
        ey[:-1, :] += e * hx[:, None] / 2
        ky = ey / hy[None, :]
        I, J = np.meshgrid(np.arange(nx - 1), np.arange(ny), indexing='ij')
        a, b, k = (I * ny + J).ravel(), ((I + 1) * ny + J).ravel(), kx.ravel()
        I, J = np.meshgrid(np.arange(nx), np.arange(ny - 1), indexing='ij')
        a = np.concatenate([a, (I * ny + J).ravel()]); b = np.concatenate([b, (I * ny + J + 1).ravel()]); k = np.concatenate([k, ky.ravel()])
        A = sp.coo_matrix((np.concatenate([k, k, -k, -k]), (np.concatenate([a, b, a, b]), np.concatenate([a, b, b, a]))), shape=(N, N)).tocsr()
        fixed = (trace | gnd).ravel().copy()
        val = np.zeros(N); val[trace.ravel()] = 1.0
        if odd:
            sym = np.zeros((nx, ny), bool); sym[0, :] = True
            fixed |= sym.ravel() & ~trace.ravel()
        free = ~fixed
        rhs = -A[free][:, fixed] @ val[fixed]
        phi = val.copy()
        phi[free] = spl.spsolve(A[free][:, free].tocsc(), rhs)
        return float(np.sum(k * (phi[a] - phi[b]) ** 2))   # 半分の領域 = 線 1 本あたり

    Co, Coa = cap(True, True), cap(False, True)
    Ce, Cea = cap(True, False), cap(False, False)
    zo = 1 / (C0 * math.sqrt(Co * Coa)); ze = 1 / (C0 * math.sqrt(Ce * Cea))
    # 2 線の C 行列 (C11 = (Ce+Co)/2、C12 = -(Co-Ce)/2) と L = μ0ε0 · inv(C_air)
    Cm = np.array([[(Ce + Co) / 2, -(Co - Ce) / 2], [-(Co - Ce) / 2, (Ce + Co) / 2]])
    Ca = np.array([[(Cea + Coa) / 2, -(Coa - Cea) / 2], [-(Coa - Cea) / 2, (Cea + Coa) / 2]])
    L = np.linalg.inv(Ca) / C0 ** 2
    return dict(zodd=zo, zeven=ze, zdiff=2 * zo, zcm=ze / 2, eeff_odd=Co / Coa, eeff_even=Ce / Cea, L=L, C=Cm, nodes=nx * ny)


def single(h, w, er=JLC['er'], t=JLC['t'], mask=False, **kw):
    """単線マイクロストリップ (検証用): 2 本を十分離して偶・奇の平均 ≈ 単線"""
    r = solve(h, w, 40 * h, None, er, t, mask, **kw)
    return math.sqrt(r['zodd'] * r['zeven'])


def ipc2141(h, w, s, er=JLC['er'], t=JLC['t']):
    """参考値: IPC-2141 の近似式 (kreview P15 と同じ。KiCad の計算機より粗い。CPWG とレジストは扱えない)"""
    z0 = 87 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))
    return 2 * z0 * (1 - 0.48 * math.exp(-0.96 * s / h))


def hammerstad(h, w, s, er=JLC['er'], t=JLC['t']):
    """参考値: 単線は Hammerstad-Jensen (KiCad の計算機の Microstrip と同じ系統の式、厚さ補正つき)、
    結合は IPC-2141 と同じ近似 Zdiff = 2·Z0·(1-0.48·exp(-0.96 s/h))。s が h よりずっと小さい (密結合) と大きく外れる"""
    u = w / h
    if t > 0:
        du = t / (math.pi * h) * math.log(1 + 4 * math.e / (t / h * (1 / math.tanh(math.sqrt(6.517 * u))) ** 2))
        u = u + du * (1 + 1 / math.cosh(math.sqrt(er - 1))) / 2
    a = 1 + math.log((u ** 4 + (u / 52) ** 2) / (u ** 4 + 0.432)) / 49 + math.log(1 + (u / 18.1) ** 3) / 18.7
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    ee = (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)
    f = 6 + (2 * math.pi - 6) * math.exp(-(30.666 / u) ** 0.7528)
    z0 = 60 / math.sqrt(ee) * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))
    return 2 * z0 * (1 - 0.48 * math.exp(-0.96 * s / h))


def _cps(w, s):
    from scipy.special import ellipk
    k = s / (s + 2 * w); kp = math.sqrt(1 - k * k)
    return 120 * math.pi * ellipk(k * k) / ellipk(kp * kp)


def selftest():
    ok = True
    for name, got, want, tol in [
        ('単線 w=2.9 h=1.53 er4.6 (Hammerstad 単線)', single(1.53, 2.9, t=0.035), hammerstad(1.53, 2.9, 1e9) / 2, 0.03),
        ('単線 w=0.3 h=0.2 er4.6', single(0.2, 0.3, t=0.035), hammerstad(0.2, 0.3, 1e9) / 2, 0.03),
        ('空気中の平行 2 線 (CPS) w=0.3 s=0.2 と楕円積分の厳密解', solve(20, 0.3, 0.2, None, er=1.0, t=0.001, mask=False, side=60, top=60)['zdiff'], _cps(0.3, 0.2), 0.03),
    ]:
        r = abs(got - want) / want
        ok &= r < tol
        print('OK ' if r < tol else 'NG ', f'{name}: FD {got:.1f} Ω / 式 {want:.1f} Ω (差 {r*100:.1f}%)')
    return ok


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--h', type=float, help='基材の厚さ mm (基板厚 - 2 x 銅厚 が目安)')
    ap.add_argument('--w', type=float); ap.add_argument('--s', type=float)
    ap.add_argument('--g', type=float, default=None, help='同じ層の GND ベタまでの距離 mm (無指定 = マイクロストリップ)')
    ap.add_argument('--er', type=float, default=JLC['er']); ap.add_argument('--t', type=float, default=JLC['t'])
    ap.add_argument('--no-mask', action='store_true'); ap.add_argument('--d', type=float, default=0.01, help='細かい格子の間隔 mm')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    r = solve(a.h, a.w, a.s, a.g, a.er, a.t, not a.no_mask, d=a.d)
    print(f"Zdiff {r['zdiff']:.1f} Ω  Zodd {r['zodd']:.1f}  Zeven {r['zeven']:.1f}  Zcm {r['zcm']:.1f}  "
          f"εeff odd {r['eeff_odd']:.2f} even {r['eeff_even']:.2f}  (節点 {r['nodes']})")
    print(f"参考 IPC-2141 {ipc2141(a.h, a.w, a.s, a.er, a.t):.1f} Ω、Hammerstad+結合近似 {hammerstad(a.h, a.w, a.s, a.er, a.t):.1f} Ω (どちらも CPWG・レジスト無視)")
