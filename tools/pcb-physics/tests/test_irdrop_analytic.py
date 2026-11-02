#!/usr/bin/env python3
"""irdrop の手計算検証 5 ケース (合否 ±5%)。合成した格子 (pcb-physics-grid/2 の配列) で solver を直接解く。
1 一様な直線 R = ρL/(wt)   2 正方形シート R = ρ/t (1 □)   3 ビア 1 本 R = ρh/(π((r+tp)²−r²))
4 並列 2 経路の電流分配 (幅 1:3 → 1:3)   5 温度補正 ρ(70 ℃)/ρ(20 ℃) = 1 + 0.00393×50"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from core.grid import PcbGrid
from irdrop.solver import solve_net
from irdrop.interface import rho_cu

TOL = 0.05


def make(masks, dx=0.1, cu=(0.035, 0.035), diel=(1.5,), pads=(), vias=(), edge_w=1.0):
    """masks (L,ny,nx) bool = ネット 'N' の銅。pads: [(ref, pin, layer, [(j,i)...])]。vias: [(j, i, drill_mm)]"""
    M = np.asarray(masks, bool); L, ny, nx = M.shape
    nid = M.astype(np.int32)
    ex = (M[:, :, :-1] & M[:, :, 1:]); ey = (M[:, :-1, :] & M[:, 1:, :])
    A = dict(cu_frac=M.astype(float), net_id=nid, net_major=nid, net_count=M.astype(np.int8), board_frac=np.ones((ny, nx)),
             edge_x_w=(ex * edge_w).astype(np.float32), edge_x_net=ex.astype(np.int32), edge_y_w=(ey * edge_w).astype(np.float32), edge_y_net=ey.astype(np.int32))
    V = dict(x_mm=[], y_mm=[], drill_mm=[], j=[], i=[], net=[], l_from=[], l_to=[], kind=[])
    for j, i, d in vias:
        for kk, v in zip(V, (0, 0, d, j, i, 1, 0, L - 1, 0)):
            V[kk].append(v)
    A.update({f'via_{k}': np.array(v) for k, v in V.items()})
    P = dict(ref=[], pin=[], layer=[]); C = dict(j=[], i=[], w=[]); ptr = [0]
    for ref, pin, l, cells in pads:
        P['ref'].append(ref); P['pin'].append(pin); P['layer'].append(l)
        for j, i in cells:
            C['j'].append(j); C['i'].append(i); C['w'].append(1 / len(cells))
        ptr.append(len(C['j']))
    A.update(pad_ref=np.array(P['ref'], 'U16'), pad_pin=np.array(P['pin'], 'U8'), pad_layer=np.array(P['layer'], np.int8),
             pad_net=np.ones(len(P['ref']), np.int32), pad_cell_ptr=np.array(ptr, np.int64),
             padcell_j=np.array(C['j'], np.int32), padcell_i=np.array(C['i'], np.int32), padcell_w=np.array(C['w']))
    meta = dict(dx_mm=dx, x0_mm=0.0, y0_mm=0.0, nx=nx, ny=ny, layer_names=[f'L{l}' for l in range(L)],
                layers=[dict(name=f'L{l}', cu_mm=c) for l, c in enumerate(cu[:L])], dielectric_mm=list(diel), plating_mm=0.018, nets=['N'])
    return PcbGrid(meta, A)


def R_between(g, I=0.1, temp=None, cond=None):
    r = solve_net(g, 'N', temp, [('S', '1', I)], [('V', '1', 1.0)], cond)
    return (1.0 - r['pin_v'][('S', '1')]) / I, r


def check(name, got, ref, lo=-TOL, hi=TOL):
    e = got / ref - 1; ok = lo <= e <= hi
    print(f"{'ok  ' if ok else 'FAIL'} {name}: 計算 {got:.6g} / 手計算 {ref:.6g} (誤差 {e * 100:+.2f}%)")
    return ok


def raster_cases():
    import math, tempfile
    from core import grid as CG
    out = []; rho = 1.72e-8; t = 35e-6
    # 斜め配線: 補正なしの 4 近傍格子では dx 0.1 の 0.2 mm 線で 30° +8%、45° +12% だった。core/diag.py (f5022d7) の補正込みで全件 ±5%
    for name, ang, w, dx, lo, hi in (('6a 実ラスタ 0.2 mm 水平 dx 0.1 (格子からずらす)', 0, 0.2, 0.1, -TOL, TOL),
                                     ('6b 実ラスタ 0.2 mm 30° dx 0.05', 30, 0.2, 0.05, -TOL, TOL),
                                     ('6c 実ラスタ 0.2 mm 45° dx 0.05', 45, 0.2, 0.05, -TOL, TOL),
                                     ('6d 実ラスタ 0.2 mm 45° dx 0.1 (斜め補正 core/diag)', 45, 0.2, 0.1, -TOL, TOL),
                                     ('6e 実ラスタ 0.3 mm 30° dx 0.1 (斜め補正)', 30, 0.3, 0.1, -TOL, TOL),
                                     ('6f 実ラスタ 0.2 mm 30° dx 0.1 (斜め補正)', 30, 0.2, 0.1, -TOL, TOL),
                                     ('6g 実ラスタ 0.2 mm 15° dx 0.1 (斜め補正)', 15, 0.2, 0.1, -TOL, TOL)):
        L = 20.0; x0, y0 = 5.037, 5.063; c, s_ = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        a = (x0, y0); b = (x0 + L * c, y0 + L * s_)
        pad = lambda ref, xy: dict(ref=ref, side='F.Cu', lcsc='', pads=[dict(net='N', num='1', type='smd', shape='rect', x=xy[0], y=xy[1], rot=0,
                                   w=w, h=w, layers=['F.Cu'], drill=0.0, rr=0)])
        f = tempfile.NamedTemporaryFile(suffix='.kicad_pcb', delete=False); f.write(b'x'); f.close()
        board = dict(path=f.name, thickness=1.6, cu_layers=['F.Cu', 'B.Cu'], stackup=None,
                     outline=[[(0, 0), (30, 0), (30, 20), (0, 20)]], footprints=[pad('V', a), pad('S', b)],
                     tracks=[dict(a=a, b=b, w=w, layer='F.Cu', net='N')], vias=[], zones=[])
        g, _ = CG.build(board, 'artwork', dx, 4, checks=False); os.unlink(f.name)
        R, _ = R_between(g)
        out.append(check(name, R, rho * L * 1e-3 / (w * 1e-3 * t), lo, hi))
    return out


def main():
    rho = 1.72e-8; t = 35e-6; dx = 0.1e-3; res = []
    # 1 直線: 幅 3 セル (0.3 mm)、長さ 201 セル → 両端の列の中心間 200 dx = 20 mm
    ny, nx = 7, 201; M = np.zeros((1, ny, nx), bool); M[0, 2:5, :] = True
    col = lambda i: [(j, i) for j in range(2, 5)]
    g = make(M, cu=(0.035,), diel=(), pads=[('V', '1', 0, col(0)), ('S', '1', 0, col(nx - 1))])
    R, _ = R_between(g); res.append(check('1 直線 0.3 mm × 20 mm', R, rho * 200 * dx / (3 * dx * t)))
    # 1b 細線の部分幅 (edge_w = 0.5 → 実効幅 0.15 mm 相当)
    g = make(M, cu=(0.035,), diel=(), pads=[('V', '1', 0, col(0)), ('S', '1', 0, col(nx - 1))], edge_w=0.5)
    R, _ = R_between(g); res.append(check('1b 直線 edge_w 0.5', R, rho * 200 * dx / (1.5 * dx * t)))
    # 2 正方形シート: 101×101 セル、左右端の列を電極 → 中心間 100 dx × 幅 101 dx ≈ 1 □
    n = 101; M = np.ones((1, n, n), bool)
    g = make(M, cu=(0.035,), diel=(), pads=[('V', '1', 0, [(j, 0) for j in range(n)]), ('S', '1', 0, [(j, n - 1) for j in range(n)])])
    R, _ = R_between(g); res.append(check('2 正方形シート 1 □', R, rho / t * 100 / 101))
    # 3 ビア 1 本: 2 層各 1 セル + ビア (drill 0.3、めっき 18 µm、誘電体 1.5 mm)
    M = np.ones((2, 1, 1), bool)
    g = make(M, pads=[('V', '1', 0, [(0, 0)]), ('S', '1', 1, [(0, 0)])], vias=[(0, 0, 0.3)])
    r0 = 0.15e-3; tp = 18e-6; h = 1.5e-3 + 35e-6
    R, _ = R_between(g); res.append(check('3 ビア 1 本', R, rho * h / (np.pi * ((r0 + tp) ** 2 - r0 ** 2))))
    # 4 並列 2 経路: 幅 1 セルと 3 セル、長さ同じ → 電流比 1:3 (分離は 2 セル)。両端を縦の電極で結ぶ
    ny, nx = 8, 101; M = np.zeros((1, ny, nx), bool); M[0, 0, :] = True; M[0, 3:6, :] = True; M[0, :6, 0] = True; M[0, :6, -1] = True
    g = make(M, cu=(0.035,), diel=(), pads=[('V', '1', 0, [(j, 0) for j in range(6)]), ('S', '1', 0, [(j, nx - 1) for j in range(6)])])
    R, r = R_between(g)
    l, j0, i0, j1, i1, Jd = r['jedges'][0]
    mid = i0 == nx // 2
    I1 = float((Jd[mid & (j0 == 0)] * 1 * dx * t * 1e6).sum()); I3 = float((Jd[mid & (j0 >= 3) & (j0 <= 5) & (j1 == j0)] * dx * t * 1e6).sum())
    # x 方向の辺は edge_x (j1 == j0)。r['jedges'][0] が x、[1] が y
    res.append(check('4 並列 2 経路 電流比 I(3 セル)/I(1 セル)', I3 / I1, 3.0))
    res.append(check('4 並列 2 経路 合成抵抗', R, 1 / (1 / (rho * 100 * dx / (dx * t)) + 1 / (rho * 100 * dx / (3 * dx * t)))))
    # 5 温度補正: 70 ℃ 一様
    ny, nx = 7, 201; M = np.zeros((1, ny, nx), bool); M[0, 2:5, :] = True
    g = make(M, cu=(0.035,), diel=(), pads=[('V', '1', 0, col(0)), ('S', '1', 0, col(nx - 1))])
    R20, _ = R_between(g); R70, r = R_between(g, temp=np.full((1, ny, nx), 70.0))
    res.append(check('5 温度補正 R(70)/R(20)', R70 / R20, 1 + 0.00393 * 50))
    # ジュール熱 = I²R
    res.append(check('5b ジュール熱 合計 = I²R', r['info']['joule_w'], 0.1 ** 2 * R70))
    # 6 実ラスタ (core.grid.build、dx 0.1 mm): 0.2 mm 配線 20 mm (格子とずらした水平 / 30° 斜め)。pad 中心間 = 配線長
    res += raster_cases()
    print('全件 ok' if all(res) else 'FAIL あり')
    return 0 if all(res) else 1


if __name__ == '__main__':
    sys.exit(main())
