#!/usr/bin/env python3
"""実装性チェック (checks_mount.py / mount_geom.py、P18b・P26〜P33) の単体テスト。合成した小さな基板だけを使う (熱回路網は 11. だけ。scipy が無ければ省略)。
python3 tools/review/tests/test_mount_checks.py"""
import math, os, sys, tempfile
H = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(H, '..')); sys.path.insert(0, os.path.join(H, '..', '..', 'pcb-score'))
import kpcb, mount_geom as mg, checks_mount as cm

ok = True
def check(name, got, want):
    global ok
    r = 'OK ' if got == want else 'NG '
    ok &= got == want
    print(r, name, got, '(期待', want, ')')

def net(n): return f'(net "{n}")' if n else ''   # KiCad 10 形式 (名前だけ)
def chip(ref, x, y, n1, n2, zc=None, size='0402'):
    w, h, dx = (0.56, 0.62, 0.48) if size == '0402' else (0.8, 0.95, 0.775)
    z = f' (zone_connect {zc})' if zc is not None else ''
    pads = ''.join(f'\t\t(pad "{k}" smd roundrect (at {s * dx} 0) (size {w} {h}) (layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25) {net(n)}{z})\n'
                   for k, s, n in ((1, -1, n1), (2, 1, n2)))
    lib = 'Resistor_SMD:R_0402_1005Metric' if size == '0402' else 'Resistor_SMD:R_0603_1608Metric'
    return (f'\t(footprint "{lib}" (layer "F.Cu") (at {x} {y})\n\t\t(property "Reference" "{ref}" (at 0 -1) (layer "F.SilkS"))\n'
            f'\t\t(property "Value" "10k" (at 0 1) (layer "F.Fab"))\n\t\t(attr smd)\n{pads}'
            f'\t\t(fp_rect (start -0.93 -0.47) (end 0.93 0.47) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))\n\t)\n')
def via(x, y, n='GND', d=0.3, s=0.6): return f'\t(via (at {x} {y}) (size {s}) (drill {d}) (layers "F.Cu" "B.Cu") {net(n)})\n'
def seg(a, b_, n, w=0.2): return f'\t(segment (start {a[0]} {a[1]}) (end {b_[0]} {b_[1]}) (width {w}) (layer "F.Cu") {net(n)})\n'
def rect(x0, y0, x1, y1): return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
def zone(polys, connect='(connect_pads (clearance 0.2))', extra=''):
    fp = ''.join('\t\t(filled_polygon (layer "F.Cu") (pts ' + ' '.join(f'(xy {x} {y})' for x, y in p) + '))\n' for p in polys)
    return (f'\t(zone (net_name "GND") (layer "F.Cu") {connect} (min_thickness 0.2) {extra}\n'
            '\t\t(polygon (pts (xy 0 0) (xy 40 0) (xy 40 20) (xy 0 20)))\n' + fp + '\t)\n')
def board(body, setup='(setup (pad_to_mask_clearance 0.05))'):
    nets = ''
    return ('(kicad_pcb (version 20241229)\n\t(general (thickness 1.6))\n\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
            f'\t{setup}\n{nets}{body}'
            '\t(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") (stroke (width 0.1) (type solid)))\n)\n')

d = tempfile.mkdtemp()
def run(body, cfg=None, toml='', setup=None):
    p = os.path.join(d, 't.kicad_pcb'); open(p, 'w').write(board(body, setup) if setup else board(body))
    open(os.path.join(d, 'review.toml'), 'w').write(toml)
    c = {'mount': {'thermal': False, 'gerber': False}}
    for k, v in (cfg or {}).items():
        c['mount'][k] = v
    ctx = dict(dir=d, pcb=p, cfg=c, out=d, board=kpcb.Board(p))
    return {m['id']: m for m in cm.run(ctx)}, ctx

# --- 1. 吸い込み (P33): 0402 GND パッドの中心に φ0.3 via → 穴 0.113 mm³ / はんだ ≈ 0.0187 mm³ ≫ 25% で fail
pour_r = [rect(10.5, 9, 14, 11)]   # R1.2 の右側を覆うベタ (R1.1 には届かない)
B1 = chip('R1', 10, 10, 'SIG', 'GND') + via(10.48, 10) + seg((9.52, 10), (5, 10), 'SIG') + zone(pour_r)
M, ctx = run(B1)
check('P33 grade (via in pad)', M['P33_solder_wicking']['grade'], 'fail')
w = [l for l in M['P33_solder_wicking']['locs'] if l['ref'] == 'R1']
check('P33 R1.2 が丸の対象', [l['label'].split()[1] for l in w], ['R1.2'])
# 手計算: 穴 π·0.15²·1.6 = 0.1131、はんだ = (0.56·0.62 の paste) × 0.12 × 0.9 × 0.5
f = ctx['board'].fp('R1'); p = [q for q in f.pads if q.num == '2'][0]
st = mg.setup_info(ctx['board']); mp = mg.mask_paste(f, p, st)
vr = mg.via_relations(ctx['board'], p, mp['mask_margin'])
wk = mg.wicking(vr, mg.solder_mm3(mp), 1.6)
check('穴容積', wk['hole_mm3'], round(math.pi * 0.15 ** 2 * 1.6, 4))
check('吸い込み比 > 25%', wk['ratio'] > 0.25, True)
check('穴埋め (filled) なら 0', mg.wicking(vr, mg.solder_mm3(mp), 1.6, filled=True)['ratio'], 0.0)
# 穴埋めの指定 (mount.via_filled) で P33 は pass、via_in_pad_ok (P18 の許容) では 0 にしない
check('P33 mount.via_filled=true → pass', run(B1, {'via_filled': True})[0]['P33_solder_wicking']['grade'], 'pass')
M2, _ = run(B1); ctx2 = dict(ctx); ctx2['cfg'] = {'via_in_pad_ok': True, 'mount': {'thermal': False}}
check('via_in_pad_ok では 0 扱いにしない', {m['id']: m for m in cm.run(ctx2)}['P33_solder_wicking']['grade'], 'fail')

# --- 2. テント (P18b): 穴縁がマスク開口から 0.05 mm (< 0.1 mm) → warn、P33 には入れない
# R2.2 の開口右端 = 20.48+0.28+0.05 = 20.81、via 中心 20.81+0.15+0.05 = 21.01
B2 = chip('R2', 20, 10, 'SIG', 'GND') + via(21.01, 10) + zone([rect(20.5, 9, 24, 11)])
M, _ = run(B2)
check('P18b grade (穴縁 0.05 mm)', M['P18b_via_near_pad']['grade'], 'warn')
check('P18b に R2.2', any(l['label'].startswith('P18b R2.2') for l in M['P18b_via_near_pad']['locs']), True)
check('P33 は開口にかからないので pass', M['P33_solder_wicking']['grade'], 'pass')
M, _ = run(chip('R2', 20, 10, 'SIG', 'GND') + via(21.5, 10) + zone([rect(20.5, 9, 24, 11)]))
check('P18b 離せば pass', M['P18b_via_near_pad']['grade'], 'pass')

# --- 3. スポーク数え: thermal (gap 0.25) のパッドに塗りのスポーク 2 本 + 同ネットの配線 1 本 → spokes 2・tracks 1 (配線をスポークに数えない)
gp = 0.25; px, py = 30.48, 10
sp = [rect(px + 0.28 - 0.01, py - 0.1, px + 0.28 + gp + 0.5, py + 0.1),     # 右のスポーク (パッド縁から外へ)
      rect(px - 0.1, py - 0.31 - gp - 0.5, px + 0.1, py - 0.31 + 0.01)]     # 上のスポーク
B3 = (chip('R3', 30, 10, 'SIG', 'GND', zc=None) + seg((px, py), (px, py + 2), 'GND')
      + zone(sp, connect='(connect_pads (clearance 0.25))', extra='(thermal_gap 0.25) (thermal_bridge_width 0.2)'))
p3 = os.path.join(d, 't.kicad_pcb'); open(p3, 'w').write(board(B3)); b3 = kpcb.Board(p3)
f = b3.fp('R3'); p = [q for q in f.pads if q.num == '2'][0]
zc = mg.resolve_zone_connection(b3, f, p, mg.zone_settings(b3))
check('ゾーン既定 thermal', zc['mode'], 'thermal')
rg = mg.pad_ring(b3, f, p, mg.CopperIndex(b3, mg.zone_fills(b3)), zc)
check('スポーク本数', rg['spokes'], 2)
check('配線は別に数える', rg['tracks'], 1)

# --- 4. P28 (ベタ直結 vs 配線のみ): solid の R1 は warn、keep_solid / mount.exclude で外れる
B4 = chip('R1', 10, 10, 'SIG', 'GND', size='0402') + seg((9.52, 10), (5, 10), 'SIG') + zone(pour_r, connect='(connect_pads yes (clearance 0.2))')
M, _ = run(B4)
check('P28 solid と配線のみ → warn', M['P28_pad_pour_balance']['grade'], 'warn')
M, _ = run(B4, toml='[pad_thermal.keep_solid]\n"R1.2" = "テスト: 意図的な直結"\n')
check('keep_solid で対象外', M['P28_pad_pour_balance']['grade'], 'pass')
M, _ = run(B4, {'exclude': {'R1': 'テスト'}})
check('mount.exclude で対象外', M['P28_pad_pour_balance']['grade'], 'pass')
check('P28 に JLC 推奨値の表示', any('JLC' in x for x in M['P28_pad_pour_balance']['detail']), True)
check('P26 は info (点に入れない)', M['P26_zone_connect_audit']['grade'], 'info')

# --- 5. しきい値は [config.mount] で変えられる: wick_fail を 100 倍にすると warn に下がる
M, _ = run(B1, {'wick_fail': 100.0})
check('wick_fail を上げると warn', M['P33_solder_wicking']['grade'], 'warn')

# --- 6. 開口用パッド (番号なし・銅なし、F.Mask/F.Paste だけ) でパッドの開口を延長: 延長の先の via は P33 に入り、ペースト量も増える
def conn(ref, x, y, ext=0.0, lib='Connector_Molex:Molex_87832-1420_2x07_P2.00mm_Vertical'):
    pads = ''
    for k in range(2):
        px_ = k * 2.0
        pads += f'\t\t(pad "{k + 1}" smd rect (at {px_} 0) (size 1.0 2.75) (layers "F.Cu" "F.Mask" "F.Paste") {net("GND" if k == 0 else "SIG")})\n'
        if ext:
            pads += f'\t\t(pad "" smd rect (at {px_} {1.375 + ext / 2 - 0.01}) (size 1.0 {ext + 0.02}) (layers "F.Mask" "F.Paste"))\n'
    return (f'\t(footprint "{lib}" (layer "F.Cu") (at {x} {y})\n\t\t(property "Reference" "{ref}" (at 0 -3) (layer "F.SilkS"))\n'
            f'\t\t(property "Value" "C" (at 0 3) (layer "F.Fab"))\n\t\t(attr smd)\n{pads}'
            f'\t\t(fp_rect (start -1 -2) (end 3 2) (layer "F.CrtYd") (stroke (width 0.05) (type solid)))\n\t)\n')
vy = 10 + 1.375 + 0.2     # パッド端の 0.2 mm 外 (延長 0.3 mm の中)
B6 = conn('J9', 10, 10, ext=0.3) + via(10, vy) + zone([rect(9, 7, 11, 13)])
M, _ = run(B6, {'gerber': False})
check('開口用パッドの延長の中の via → P33 に入る', any(l['label'].startswith('P33 J9.1') for l in M['P33_solder_wicking']['locs']), True)
M, _ = run(conn('J9', 10, 10) + via(10, vy) + zone([rect(9, 7, 11, 13)]), {'gerber': False})
check('延長なしなら P33 に入らない', any(l['label'].startswith('P33 J9.1') for l in M['P33_solder_wicking']['locs']), False)
# P32: J9 は connectors.json の Molex_87832-1420 (コプラナリティ 0.15) に一致。0.12 × (2.75+0.3)/2.75 = 0.133 < 0.15 → warn、ステンシル 0.15 なら pass
M, _ = run(conn('J9', 10, 10, ext=0.3), {'gerber': False})
check('P32 延長 0.3 mm でも 0.133 mm < 0.15 → warn', M['P32_paste_vs_coplanarity']['grade'], 'warn')
check('P32 体積換算の厚さ 0.13x を表示 (重なり 0.01 mm 分で 0.134)', '最小 0.13' in M['P32_paste_vs_coplanarity']['detail'][0], True)
M, _ = run(conn('J9', 10, 10, ext=0.3), {'gerber': False, 'stencil_mm': 0.15})
check('P32 ステンシル 0.15 → pass', M['P32_paste_vs_coplanarity']['grade'], 'pass')

# --- 7. gerber 実データの読み込み (mount_gerber.Openings): 手書きの F.Paste (パッド 1.0×2.75 + 延長 0.3 の 2 つのフラッシュ) で面積と延長
import mount_gerber as mgb
gd = tempfile.mkdtemp()
def gbr(fn, body):
    p = os.path.join(gd, fn); open(p, 'w').write('%FSLAX46Y46*%\n%MOMM*%\n%LPD*%\n%ADD10R,1.000000X2.750000*%\n%ADD11R,1.000000X0.320000*%\n' + body + 'M02*\n'); return p
# KiCad (10, 10) → gerber (10, -10)。延長の中心 y = 10 + 1.375 + 0.15 - 0.01 → gerber -11.515
fp_ = gbr('t-F_Paste.gtp', 'D10*\nX10000000Y-10000000D03*\nD11*\nX10000000Y-11515000D03*\n')
b7p = os.path.join(d, 't.kicad_pcb'); open(b7p, 'w').write(board(conn('J9', 10, 10, ext=0.3))); b7 = kpcb.Board(b7p)
O = mgb.Openings(b7, {'F.Paste': fp_})
o = O.pad_open(b7.fp('J9'), [q for q in b7.fp('J9').pads if q.num == '1'][0], 'Paste')
check('gerber のペースト面積 ≈ 1.0×3.05 (±3%)', abs(o['area_mm2'] - 3.05) / 3.05 < 0.03, True)
check('gerber の延長 ≈ 0.3 mm (±0.02)', abs(o['ext_mm'] - 0.3) <= 0.02, True)

# --- 8. P27 の工程: 熱回路網の結果を差し替えて、比 10 倍の fail が reflow 部品では warn 止まり、process = hand なら fail
fake = {'hand': {'J9': {'pour': ['1'], 'other': ['2'], 'Rth': {'1': 60.0, '2': 150.0},
                        'transient': {'1': {'300': {'t_reach_s': 9.0, 'T_ss': 240.0}, '350': {'t_reach_s': 1.0, 'T_ss': 290.0}},
                                      '2': {'300': {'t_reach_s': 0.5, 'T_ss': 270.0}, '350': {'t_reach_s': 0.1, 'T_ss': 320.0}}}}}, 'chips': {}}
orig = cm.thermal_results
cm.thermal_results = lambda ctx, C, chips, hands, pnets, extra=None: (fake, 'テストの差し替え')
B8 = conn('J9', 10, 10) + zone([rect(9, 7, 11, 13)])
M, _ = run(B8, {'gerber': False})
check('P27 reflow 部品は warn 止まり', M['P27_thermal_balance']['grade'], 'warn')
M, _ = run(B8, {'gerber': False, 'process': {'J9': 'hand'}})
check('P27 process = hand なら fail', M['P27_thermal_balance']['grade'], 'fail')
M, _ = run(B8, {'gerber': False, 'hand_solder': ['J9']})
check('P27 hand_solder も hand 扱い', M['P27_thermal_balance']['grade'], 'fail')
cm.thermal_results = orig

# --- 10. 熱計算の 2 段 (2026-10-16 高速化): 粗い格子で全チップ → 点 < tomb_pass + refine_margin のチップだけ 0.05 mm で解き直す
calls = []
def fake2(ctx, C, chips, hands, pnets, extra=None):
    calls.append((sorted(chips), sorted(hands), dict(extra or {})))
    if extra and extra.get('chip_res'):     # 粗い: R1 は明らかに pass (比 1.0)、R2 は境目 (比 1.6 → 点 ≈ 66)
        return {'chips': {'R1': {'1': 100.0, '2': 100.0}, 'R2': {'1': 100.0, '2': 160.0}}, 'hand': {}}, '粗い'
    return {'chips': {r: {'1': 100.0, '2': 300.0} for r in chips}, 'hand': {}}, '細かい'
cm.thermal_results = fake2
B10 = chip('R1', 10, 10, 'A', 'B') + chip('R2', 20, 10, 'C', 'D')
M, _ = run(B10, {'gerber': False, 'thermal': True})
check('2 段: 1 回目は粗い格子 0.1 mm で全チップ', calls[0], (['R1', 'R2'], [], {'chip_res': 0.1}))
check('2 段: 2 回目は境目の R2 だけ既定の格子で', calls[1], (['R2'], [], {}))
P28 = M['P28_pad_pour_balance']
check('2 段: R2 は解き直した値 (比 3.0) で fail', (P28['tomb_scores'].get('R2', 999) < 50, P28['grade']), (True, 'fail'))
check('2 段: R1 は粗い値のまま pass (点 100)', P28['tomb_scores'].get('R1'), 100)
calls.clear(); M, _ = run(B10, {'gerber': False, 'thermal': True, 'chip_res_coarse': 0})
check('chip_res_coarse = 0 なら 1 回だけ (全部 0.05 mm)', [c[2] for c in calls], [{}])
cm.thermal_results = orig

# --- 11. mount_thermal の高速化が結果を変えないこと
import numpy as np
import mount_thermal as mt
from matplotlib.path import Path
g = mt.Grid(0, 0, 3, 3, 0.1); rng = np.random.default_rng(1)
same = True
for k in range(30):     # 格子の中心・辺の上に頂点が乗る多角形も含める (境目の扱いが matplotlib と同じか)
    P = [(round(float(x), 1 if k % 2 else 3), round(float(y), 1 if k % 3 else 3)) for x, y in rng.uniform(0.2, 2.8, (int(rng.integers(3, 9)), 2))]
    if k % 5 == 0: P = [(0.55, 0.55), (2.45, 0.55), (2.45, 2.45), (0.55, 2.45)]
    m1 = g.zeros(); g.poly(m1, P)
    X, Y = np.meshgrid(g.xc, g.yc); m2 = Path(P).contains_points(np.c_[X.ravel(), Y.ravel()]).reshape(X.shape)
    same &= bool((m1 == m2).all())
check('Grid.poly (走査線) が matplotlib contains_points と画素単位で一致', same, True)
try:
    import scipy.sparse as sp_, scipy.sparse.linalg as spla_
except ImportError:
    print('-- scipy が無いので 熱の過渡 (こて先温度の線形性) のテストは省略 (scipy の入った python で実行すれば通す)')
else:
    p = os.path.join(d, 'th.kicad_pcb')
    open(p, 'w').write(board(conn('J9', 10, 10) + zone([rect(5, 5, 15, 15)])))
    b = kpcb.Board(p); fills = mg.zone_fills(b); P = dict(mt.DEFAULTS, hand_win=1.0, hand_res=0.2, t_max=3.0)
    f = b.fp('J9'); new = mt.hand(b, f, fills, P, 1.6, {'GND'})
    # 旧方式: こて先温度ごとに分解し直して T を直接解く
    g_, F_, B_, ins_, vias_ = mt._window(b, f.courtyard_bbox() or f.pads_bbox(), P['hand_win'], P['hand_res'], fills, f.side)
    nt = mt.build(g_, F_, B_, ins_, vias_, P['h_hand'], 1.6e-3); A = (nt['K'] + sp_.diags(nt['Gamb'])).tocsc()
    agree = True
    for num, tips in new['transient'].items():
        pd = next(q for q in mg.smd_pads(f) if q.num == num); ids = mt._ids(nt, g_, pd, ins_)
        for T_tip in P['tips_c']:
            gc = np.zeros(nt['N']); gc[ids] = P['tip_g'] / len(ids)
            lu = spla_.splu((sp_.diags(nt['C'] / P['dt']) + A + sp_.diags(gc)).tocsc()); T = np.full(nt['N'], P['t_amb']); t_ = None
            for kk in range(1, int(P['t_max'] / P['dt']) + 1):
                T = lu.solve(nt['C'] / P['dt'] * T + nt['Gamb'] * P['t_amb'] + gc * T_tip)
                if T[ids].mean() >= P['t_hand']:
                    t_ = round(kk * P['dt'], 2); break
            agree &= tips[str(T_tip)]['t_reach_s'] == t_
    check('こて先温度の線形性 (1 回の分解・1 回の時間積分) で到達時間が旧方式と同じ', (agree, len(new['transient']) > 0), (True, True))

print('\n全体', 'OK' if ok else 'NG')
sys.exit(0 if ok else 1)
