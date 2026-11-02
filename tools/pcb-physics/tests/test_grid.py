#!/usr/bin/env python3
"""core の交換形式: 格子 npz + メタ、ネット ID (衝突 -1)、エッジ接続、pad → セル、ビア、孤立銅、細線警告、温度/発熱マップ、ピンの規則"""
import os, sys, tempfile
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from core import grid as CG, parts, cases as C

FAIL = []


def check(name, ok, info=''):
    print(('ok   ' if ok else 'FAIL ') + name + (f': {info}' if info != '' else ''))
    ok or FAIL.append(name)


d = tempfile.mkdtemp()
# 合成基板 10 x 6 mm: F.Cu に GND ゾーン (x 0..4.05) と SIG ゾーン (x 4.1..6)、B.Cu に VCC 配線 (pad なし = 孤立銅)、via 1、GND と SIG に pad
pad = lambda n, x, y, net: dict(num=n, type='smd', shape='rect', x=x, y=y, rot=0, w=1.0, h=1.0, layers=['F.Cu'], drill=0, rr=0, net=net)
board = dict(path=os.path.join(d, 'x.kicad_pcb'), thickness=1.6, cu_layers=['F.Cu', 'B.Cu'], stackup=None,
             outline=[[(0, 0), (10, 0), (10, 6), (0, 6)]],
             footprints=[dict(ref='J1', value='', lcsc='', mpn='', footprint='', side='F.Cu', x=0, y=0, rot=0, dnp=False, models=[], outline=[],
                              pads=[pad('1', 2.0, 3.0, 'GND'), pad('2', 5.0, 1.0, 'SIG')])],
             tracks=[dict(a=(1, 3), b=(9, 3), w=1.0, layer='B.Cu', net='VCC'), dict(a=(7, 5), b=(9, 5), w=0.15, layer='F.Cu', net='THIN')],
             vias=[dict(x=8, y=3, size=0.6, drill=0.3, layers=['F.Cu', 'B.Cu'], net='VCC')],
             zones=[dict(layer='F.Cu', net='GND', pts=[(0, 0), (4.05, 0), (4.05, 6), (0, 6)]), dict(layer='F.Cu', net='SIG', pts=[(4.1, 0), (6, 0), (6, 2), (4.1, 2)])],
             zones_unfilled=[])
open(board['path'], 'w').write('(kicad_pcb)')
g, st = CG.build(board, 'artwork', 0.25)
p = os.path.join(d, 'g.npz'); g.save(p); g2 = CG.PcbGrid.load(p)
check('格子の保存と読み込み', np.array_equal(g.net_id, g2.net_id) and np.allclose(g.cu_frac, g2.cu_frac) and g2.meta['grid_id'] == g.meta['grid_id'], g2.meta['nets'])
gnd = g2.net_mask('GND', 'F.Cu').sum() * 0.25 ** 2; vcc = g2.net_mask('VCC', 'B.Cu').sum() * 0.25 ** 2
check('ネット切り出し (多数決セル数 × dx²) GND F.Cu ≈ 24.25 mm²、VCC B.Cu ≈ 8.8 mm²', abs(gnd - 24.25) < 1.0 and abs(vcc - 8.785) < 0.6, f'{gnd:.2f}, {vcc:.2f}')
F = 0; col = 16   # セル x 4.00..4.25 に GND (4.00..4.05) と SIG (4.10..) が同居
check('① 衝突セル net_id = -1 (多数決 net_major は SIG)', g2.net_id[F, 2, col] == -1 and g2.meta['nets'][g2.net_major[F, 2, col] - 1] == 'SIG', (int(g2.net_id[F, 2, col]), int(g2.net_count[F, 2, col])))
check('① meta.checks.conflict_cells', g2.meta['checks']['conflict_cells']['F.Cu'] > 0, g2.meta['checks']['conflict_cells'])
ex_n, ex_w = g2.edge_x_net[F], g2.edge_x_w[F]
check('④ GND 内のエッジは接続 (w = 1)', ex_n[12, 5] == g2.net_index('GND') and ex_w[12, 5] == 1.0)
check('④ GND|SIG の境界 (セル 15-16、y 0..2) は GND が横切る / 16-17 は SIG', ex_n[2, 15] == g2.net_index('GND') and ex_n[2, 16] == g2.net_index('SIG'), (int(ex_n[2, 15]), int(ex_n[2, 16])))
tw = float(g2.edge_x_w[F, 19, 32] + g2.edge_x_w[F, 20, 32])
check('④ 細い配線 (0.15 mm、2 行にまたがる) のエッジ w の和 ≈ 0.15/0.25 = 0.6 (細分 1/4 刻み)', 0.4 <= tw <= 0.8, tw)
k = [i for i in range(len(g2.pad_ref)) if g2.pad_pin[i] == '1'][0]; js, is_, w = g2.pad_cells(k)
check('③ pad → セル (重みの和 1、面積 1 mm²)', abs(w.sum() - 1) < 1e-9 and abs(g2.pad_area_mm2[k] - 1.0) < 0.01, (len(js), float(g2.pad_area_mm2[k])))
check('ビア接続リスト (net 1 始まり)', len(g2.via_j) == 1 and g2.meta['nets'][g2.via_net[0] - 1] == 'VCC')
isl = g2.meta['checks']['islands']
check('⑥ 孤立銅: VCC (pad なし) と THIN を検出', {'VCC', 'THIN'} <= {x['net'] for x in isl} and 'GND' not in {x['net'] for x in isl}, [(x['net'], x['area_mm2']) for x in isl])
check('⑥ VCC は via で F/B がつながり 1 成分', [x['layers'] for x in isl if x['net'] == 'VCC'] == [['B.Cu', 'F.Cu']])
check('⑤ 細線の警告 (0.15 < 2×0.25)', g2.meta['checks']['thin_tracks'] == 1 and any('dx' in w for w in g2.meta['warnings']), g2.meta['checks']['thin_widths_mm'])
g1, _ = CG.build(board, 'artwork', 0.1)
check('⑤ dx = 0.1 mm の格子 (60×100)、細線 0.15 < 0.2 なので警告は残り推奨 dx 0.075', g1.shape == (2, 60, 100) and '0.075' in ' '.join(g1.meta['warnings']), g1.shape)
hm = np.zeros(g.shape); hm[1, 12, 20] = 0.1
CG.save_map(os.path.join(d, 'h.npz'), g.meta, 'heat', hm, 'W')
data, m = CG.load_map(os.path.join(d, 'h.npz'), g2.meta, 'heat')
check('発熱マップの保存と照合 (grid_id)', data.sum() == 0.1)
other = dict(g.meta); other['grid_id'] = 'xxxx'
try:
    CG.load_map(os.path.join(d, 'h.npz'), other, 'heat'); check('違う格子のマップは拒否', False)
except ValueError:
    check('違う格子のマップは拒否', True)
for cfg, name in (({'parts': {'U1': {'pins': {'1': {'current': {'max': {'value': 1, 'basis': 'design', 'from': 'x'}}}}}}}, 'ピン電流 typ なし'),
                  ({'parts': {'U1': {'pins': {'1': {'v_range': {'max': {'value': 3.6}}}}}}}, 'ピン許容電圧 basis なし')):
    try:
        C.check_typmax(cfg); check(f'{name}は拒否', False)
    except parts.DataError as e:
        check(f'{name}は拒否', True, e)
# ① 斜め配線の線幅ベース補正: 実際の格子 (core.grid.build) で 0.2 mm 線の単位長さ抵抗を 2 本の長さの差から求め、真値 1/W と比べる
import math
import scipy.sparse as sp, scipy.sparse.linalg as sla


def r_line(deg, dx, corr, L, a=(1.03, 1.07)):
    th = math.radians(deg if deg <= 90 else deg - 180)   # 左端の始点を長さによらず固定 (右上がりは y を 21 mm 下げた所から)
    a = (a[0], a[1] + (21.0 if deg > 90 else 0.0)); b = (a[0] + L * math.cos(th), a[1] + L * math.sin(th))
    bd = dict(path=board['path'], thickness=1.6, cu_layers=['F.Cu', 'B.Cu'], stackup=None, outline=[[(0, 0), (b[0] + 1, 0), (b[0] + 1, max(a[1], b[1]) + 1), (0, max(a[1], b[1]) + 1)]],
              footprints=[], tracks=[dict(a=a, b=b, w=0.2, layer='F.Cu', net='L')], vias=[], zones=[], zones_unfilled=[])
    gg, _ = CG.build(bd, 'artwork', dx, diag_corr=corr, checks=False)
    ny, nx = gg.shape[1:]; idx = np.arange(ny * nx).reshape(ny, nx); I, J, G = [], [], []
    for w, net, P, Q in ((gg.edge_x_w[0], gg.edge_x_net[0], idx[:, :-1], idx[:, 1:]), (gg.edge_y_w[0], gg.edge_y_net[0], idx[:-1], idx[1:])):
        ok = (net == 1) & (w > 0); I += [P[ok]]; J += [Q[ok]]; G += [w[ok].astype(float)]
    I, J, G = map(np.concatenate, (I, J, G))
    xc = gg.meta['x0_mm'] + (np.arange(nx) + 0.5) * dx; yc = gg.meta['y0_mm'] + (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(xc, yc); t = ((X - a[0]) * math.cos(th) + (Y - a[1]) * math.sin(th)).ravel()
    hi = t < 1.0; lo = t > L - 1.0; n = ny * nx
    A = sp.coo_matrix((np.r_[-G, -G], (np.r_[I, J], np.r_[J, I])), (n, n)).tocsr(); Lp = (sp.diags(-np.asarray(A.sum(1)).ravel()) + A).tocsr()
    used = np.zeros(n, bool); used[I] = used[J] = True; fi = np.nonzero(used & ~hi & ~lo)[0]; fx = np.nonzero(hi | lo)[0]
    V = np.zeros(n); V[hi] = 1; V[fi] = sla.spsolve(Lp[fi][:, fi].tocsc(), -(Lp[fi][:, fx] @ V[fx]))
    return 1 / float((Lp[np.nonzero(hi)[0]] @ V).sum()), L - 2.0


for deg in (30, 45, 135):   # 始点の格子内の位置 4 通り (45° は位置で誤差が 2〜12% と大きく変わる)
    e0, e1 = [], []
    for a0 in ((1.03, 1.07), (1.05, 1.0), (1.0, 1.02), (1.08, 1.04)):
        for corr, lst in ((False, e0), (True, e1)):
            R1, D1 = r_line(deg, 0.1, corr, 10.0, a0); R2, D2 = r_line(deg, 0.1, corr, 20.0, a0); lst.append((R2 - R1) / (D2 - D1) * 0.2 - 1)
    check(f'① 斜め {deg}° 0.2 mm 線 dx 0.1: 補正なし {min(e0) * 100:+.1f}〜{max(e0) * 100:+.1f}% → 補正あり ±3% 以内', max(map(abs, e1)) < 0.03,
          f'{min(e1) * 100:+.1f}〜{max(e1) * 100:+.1f}%')
# ② 異なる格子間の受け渡し (core.regrid): 温度は双一次補間 (線形の場は内部で厳密)、発熱は保存
from core import regrid
mA = dict(x0_mm=0.0, y0_mm=0.0, dx_mm=0.1, nx=60, ny=40, layer_names=['F.Cu', 'B.Cu'])
mB = dict(x0_mm=-0.05, y0_mm=0.02, dx_mm=0.3, nx=21, ny=14, layer_names=['F.Cu', 'B.Cu'])
xa = (np.arange(60) + 0.5) * 0.1; ya = (np.arange(40) + 0.5) * 0.1
TA = np.array([25 + 2 * xa[None, :] + 3 * ya[:, None]] * 2)
TB = regrid.temperature(TA, mA, mB)
xb = -0.05 + (np.arange(21) + 0.5) * 0.3; yb = 0.02 + (np.arange(14) + 0.5) * 0.3
inn = (xb[None, :] > xa[0]) & (xb[None, :] < xa[-1]) & (yb[:, None] > ya[0]) & (yb[:, None] < ya[-1])
ex = 25 + 2 * xb[None, :] + 3 * yb[:, None]
check('② 温度の補間 (線形の場は内部で誤差 < 1e-9 ℃)', np.abs(TB[0] - ex)[inn].max() < 1e-9, float(np.abs(TB[0] - ex)[inn].max()))
rng = np.random.default_rng(1); HA = rng.random((2, 40, 60)) * 1e-4
mask = np.ones((14, 21), bool); mask[:, -3:] = False
HB = regrid.heat(HA, mA, mB, mask)
check('② 発熱の保存 (0.1 → 0.3 mm、基板外セルの分は最寄りへ)', abs(HB.sum() - HA.sum()) < 1e-12 * HA.size and HB[:, ~mask].sum() == 0, f'{HA.sum():.6e} → {HB.sum():.6e}')
HC = regrid.heat(HB, mB, mA)
check('② 発熱の保存 (0.3 → 0.1 mm)', abs(HC.sum() - HA.sum()) < 1e-12 * HA.size, f'{HC.sum():.6e}')
# ① 重なった pad の別名 (USB-C A1/B12 のような同じ場所の 2 pad)
bd2 = dict(board); bd2['footprints'] = [dict(board['footprints'][0], ref='J9', pads=[pad('A1', 7.0, 1.5, 'GND'), pad('B12', 7.2, 1.5, 'GND'), pad('A2', 9.0, 1.5, 'SIG')])]
g9, _ = CG.build(bd2, 'artwork', 0.25)
al = g9.meta['pad_aliases']
check('① 重なった pad を別名グループに (J9.A1=B12、pad_alias 配列)', len(al) == 1 and al[0]['name'] == 'J9.A1=B12' and al[0]['same_net'] and (g9.pad_alias >= 0).sum() == 2, al)
check('③ pad 中心座標 pad_x_mm / pad_y_mm', abs(g9.pad_x_mm[list(g9.pad_pin).index('B12')] - 7.2) < 1e-12)
check('② meta.layers / meta.dielectric に比誘電率と出典', g9.meta['dielectric'][0]['er'] == 4.6 and 'jlcpcb.com/impedance' in g9.meta['layers'][0]['er_below_src'], g9.meta['dielectric'])
print('grid test:', 'FAIL ' + ', '.join(FAIL) if FAIL else 'すべて ok')
sys.exit(1 if FAIL else 0)
