#!/usr/bin/env python3
"""実装性チェックの熱回路網 (P27 手はんだ・P28 チップの R_th・P29 列の溶融時刻差)。numpy + scipy が要る。

  python mount_thermal.py BOARD.kicad_pcb --chips C1,R2 --hand J2 --out mount_thermal.json [--params '{"tips_c":[300,350]}']

kreview (checks_mount.py) は scipy があればこのモジュールを直接呼び、無ければ review.toml [config.mount] thermal_python
(既定 python3) で nice 付きの別プロセスとして呼ぶ。結果は /tmp/kreview-cache に基板の sha1 でキャッシュ。

モデル (値はすべて仮、出典は notes/2026-10-16-mount-check-items.md「共通の前提」):
- 節点 = 基板内の画素 × 2 層 (F.Cu / B.Cu)。銅 385 W/mK × 35 µm、FR-4 面内 0.8・厚さ方向 0.3 W/mK (0.3 は JLC ブログと一致)、
  via は穴めっき 18 µm (JLC capabilities) の円筒、両面対流 h、窓の端で銅が続く所は半無限フィン √(k t · 2h)。
- チップ: パッド外接矩形 ±4 mm の窓、0.05 mm 画素、h = 10 W/m²K の定常でパッド平均の R_th (K/W)。
- 手はんだ: courtyard ±4 mm の窓、0.1 mm 画素。こて先 = 温度 T_tip の熱源、接触 0.05 W/K、232 ℃ (= 217 + 15) 到達時間 (陰解法 dt 0.02 s、15 s 打ち切り)。
"""
import argparse, hashlib, json, math, os, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'pcb-score'))
import kpcb
import mount_geom as mg

K_CU = 385.0; T_CU = 0.035e-3; RHOC_CU = 3.45e6
K_FR4_XY = 0.8; K_FR4_Z = 0.3; RHOC_FR4 = 2.0e6
T_PLATE = 18e-6; RHOC_SAC = 1.7e6
MODEL = '2026-10-16b'   # 計算法の版。キャッシュの鍵に入れる (式・格子を変えたら上げる。高速化で結果が同じなら据え置きでよい)
DEFAULTS = dict(h_hand=10.0, h_reflow=50.0, tips_c=[300, 350], tip_g=0.05, t_hand=232.0, t_amb=25.0, t_max=15.0, dt=0.02,
                chip_win=4.0, chip_res=0.05, hand_win=4.0, hand_res=0.1)


class Grid:
    def __init__(s, x0, y0, x1, y1, res):
        s.x0, s.y0, s.res = x0, y0, res
        s.nx = int(round((x1 - x0) / res)); s.ny = int(round((y1 - y0) / res))
        s.xc = x0 + (np.arange(s.nx) + 0.5) * res
        s.yc = y0 + (np.arange(s.ny) + 0.5) * res

    def zeros(s):
        return np.zeros((s.ny, s.nx), bool)

    def _win(s, bb, pad=0.0):
        i0 = max(0, int((bb[0] - pad - s.x0) / s.res) - 1); i1 = min(s.nx, int((bb[2] + pad - s.x0) / s.res) + 2)
        j0 = max(0, int((bb[1] - pad - s.y0) / s.res) - 1); j1 = min(s.ny, int((bb[3] + pad - s.y0) / s.res) + 2)
        return i0, i1, j0, j1

    def poly(s, m, pts):
        """画素の中心が多角形 (偶奇規則、KiCad の filled_polygon は穴を切れ目でつないだ 1 本の輪郭) の内側なら True。
        行ごとに、その行をまたぐ辺だけで交差判定する (走査線法)。matplotlib の contains_points (画素 × 全頂点) より大きなベタで数十倍速い。
        判定式は matplotlib の points_in_path と同じ (辺の上・頂点の上の画素も同じ側になる。2026-10-16、FT2232H の全ベタ・パッドで食い違い 0)"""
        i0, i1, j0, j1 = s._win(kpcb.bbox(pts))
        if i0 >= i1 or j0 >= j1:
            return
        P = np.asarray(pts, float)
        if len(P) < 3:
            return
        x0, y0 = P[:, 0], P[:, 1]; x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
        tx = s.xc[i0:i1][:, None]
        for r, ty in enumerate(s.yc[j0:j1]):
            e = (y0 >= ty) != (y1 >= ty)          # この行をまたぐ辺
            if not e.any():
                continue
            a0, b0, a1, b1 = x0[e], y0[e], x1[e], y1[e]
            flip = (((b1 - ty) * (a0 - a1)) >= ((a1 - tx) * (b0 - b1))) == (b1 >= ty)
            m[j0 + r, i0:i1] |= (flip.sum(1) & 1).astype(bool)

    def seg(s, m, a, b_, w):
        bb = (min(a[0], b_[0]), min(a[1], b_[1]), max(a[0], b_[0]), max(a[1], b_[1]))
        i0, i1, j0, j1 = s._win(bb, w / 2)
        if i0 >= i1 or j0 >= j1:
            return
        X, Y = np.meshgrid(s.xc[i0:i1], s.yc[j0:j1])
        dx, dy = b_[0] - a[0], b_[1] - a[1]; L = dx * dx + dy * dy
        t = np.zeros_like(X) if L == 0 else np.clip(((X - a[0]) * dx + (Y - a[1]) * dy) / L, 0, 1)
        m[j0:j1, i0:i1] |= np.hypot(X - a[0] - t * dx, Y - a[1] - t * dy) <= w / 2

    def circle(s, m, c, r):
        s.seg(m, c, c, 2 * r)

    def pad(s, m, p, grow=0.0):
        if p.shape == 'circle':
            s.circle(m, (p.x, p.y), p.w / 2 + grow)
        elif p.shape == 'oval':
            L = abs(p.w - p.h) / 2; r = min(p.w, p.h) / 2 + grow
            a, b_ = ((-L, 0), (L, 0)) if p.w >= p.h else ((0, -L), (0, L))
            a = kpcb.rot(*a, p.angle); b_ = kpcb.rot(*b_, p.angle)
            s.seg(m, (p.x + a[0], p.y + a[1]), (p.x + b_[0], p.y + b_[1]), 2 * r)
        else:
            hw, hh = p.w / 2 + grow, p.h / 2 + grow
            s.poly(m, [(p.x + dx, p.y + dy) for dx, dy in (kpcb.rot(-hw, -hh, p.angle), kpcb.rot(hw, -hh, p.angle), kpcb.rot(hw, hh, p.angle), kpcb.rot(-hw, hh, p.angle))])


def _items(b, layer, fills):
    """層の銅の図形と外接矩形 (窓ごとに全部を回さないよう、基板ごと・層ごとに 1 回だけ作る。2026-10-16 高速化)"""
    ck = getattr(b, '_mt_items', None)
    if ck is None:
        ck = b._mt_items = {}
    if layer not in ck:
        it = []
        for (L, net), polys in fills.items():
            if L == layer:
                it += [('poly', kpcb.bbox(p), p) for p in polys]
        for t in b.tracks + b.arcs:
            if t['layer'] == layer:
                for a, c in zip(t['pts'], t['pts'][1:]):
                    w = t['w'] / 2
                    it.append(('seg', (min(a[0], c[0]) - w, min(a[1], c[1]) - w, max(a[0], c[0]) + w, max(a[1], c[1]) + w), (a, c, t['w'])))
        for f in b.footprints:
            for p in f.pads:
                if layer in p.copper_layers(b.copper) and p.type != 'np_thru_hole':
                    r = math.hypot(p.w, p.h) / 2
                    it.append(('pad', (p.x - r, p.y - r, p.x + r, p.y + r), p))
        for v in b.vias:
            r = v['size'] / 2
            it.append(('via', (v['at'][0] - r, v['at'][1] - r, v['at'][0] + r, v['at'][1] + r), v))
        ck[layer] = (it, np.array([x[1] for x in it], float).reshape(-1, 4))
    return ck[layer]


def copper(b, g, layer, fills):
    """窓 g の銅 (ベタ・配線・パッド・via)。窓にかからない図形は外接矩形で先に落とす (描く順・結果は同じ)"""
    m = g.zeros()
    it, bb = _items(b, layer, fills)
    if not len(it):
        return m
    X1, Y1 = g.x0 + g.nx * g.res, g.y0 + g.ny * g.res; e = 2 * g.res
    hit = np.nonzero((bb[:, 2] >= g.x0 - e) & (bb[:, 0] <= X1 + e) & (bb[:, 3] >= g.y0 - e) & (bb[:, 1] <= Y1 + e))[0]
    for k in hit:
        kind, _, d = it[k]
        if kind == 'poly':
            g.poly(m, d)
        elif kind == 'seg':
            g.seg(m, d[0], d[1], d[2])
        elif kind == 'pad':
            g.pad(m, d)
        else:
            g.circle(m, d['at'], d['size'] / 2)
    return m


def _lu(A):
    """K + G は対称正定値。SuperLU を対称モード (A + Aᵀ の最小次数順、対角ピボット) で分解すると、既定 (COLAMD) の半分の時間・
    半分以下のメモリで同じ解 (相対差 1e-12 程度) になる (2026-10-16 高速化)"""
    import scipy.sparse.linalg as spla
    # panel_size 4・relax 32 は 0.05 mm の窓 (6.5 万節点) で既定より 13% 速い (解の差 4e-13)
    return spla.splu(A.tocsc(), permc_spec='MMD_AT_PLUS_A', diag_pivot_thresh=0.0, panel_size=4, relax=32, options=dict(SymmetricMode=True))


def build(g, cuF, cuB, inside, vias, h, t_pcb, fin_edges=True):
    import scipy.sparse as sp
    ny, nx = cuF.shape; dx = g.res * 1e-3; half = t_pcb / 2
    sF = np.where(cuF, K_CU * T_CU, K_FR4_XY * half); sB = np.where(cuB, K_CU * T_CU, K_FR4_XY * half)
    idx = -np.ones((2, ny, nx), int); nin = int(inside.sum())
    idx[0][inside] = np.arange(nin); idx[1][inside] = np.arange(nin) + nin
    N = 2 * nin; R, Cc, V = [], [], []
    def link(ia, ic, G):
        R.append(np.r_[ia, ic, ia, ic]); Cc.append(np.r_[ia, ic, ic, ia]); V.append(np.r_[G, G, -G, -G])
    for L, s in ((0, sF), (1, sB)):
        for dj, di in ((0, 1), (1, 0)):
            a = idx[L][:ny - dj, :nx - di]; c = idx[L][dj:, di:]
            sa = s[:ny - dj, :nx - di]; sc = s[dj:, di:]
            ok = (a >= 0) & (c >= 0)
            link(a[ok], c[ok], (2 * sa * sc / (sa + sc))[ok])
    Gz = K_FR4_Z * dx * dx / t_pcb
    link(idx[0][inside], idx[1][inside], np.full(nin, Gz))
    for v in vias:
        j, i = int((v['at'][1] - g.y0) / g.res), int((v['at'][0] - g.x0) / g.res)
        if 0 <= j < ny and 0 <= i < nx and idx[0][j, i] >= 0:
            link(np.array([idx[0][j, i]]), np.array([idx[1][j, i]]), np.array([K_CU * math.pi * v['drill'] * 1e-3 * T_PLATE / t_pcb]))
    K = sp.coo_matrix((np.concatenate(V), (np.concatenate(R), np.concatenate(Cc))), shape=(N, N)).tocsr()
    Gamb = np.full(N, h * dx * dx)
    if fin_edges:
        Gfin = math.sqrt(K_CU * T_CU * 2 * h) * dx
        for L, cu in ((0, cuF), (1, cuB)):
            for sl in ((0, slice(None)), (ny - 1, slice(None)), (slice(None), 0), (slice(None), nx - 1)):
                e = np.zeros_like(cu); e[sl] = True; e &= cu & inside
                Gamb[idx[L][e]] += Gfin
    C = np.zeros(N)
    C[:nin] = (RHOC_FR4 * half + np.where(cuF[inside], RHOC_CU * T_CU, 0)) * dx * dx
    C[nin:] = (RHOC_FR4 * half + np.where(cuB[inside], RHOC_CU * T_CU, 0)) * dx * dx
    return dict(K=K, C=C, Gamb=Gamb, idx=idx, N=N)


def _window(b, bb, win, res, fills, side):
    outline, holes, _ = b.outline()
    g = Grid(bb[0] - win, bb[1] - win, bb[2] + win, bb[3] + win, res)
    top, bot = (('F.Cu', 'B.Cu') if side == 'F' else ('B.Cu', 'F.Cu'))
    F = copper(b, g, top, fills); B = copper(b, g, bot, fills)
    ins = g.zeros(); g.poly(ins, outline)
    for hp in holes:
        hm = g.zeros(); g.poly(hm, hp); ins &= ~hm
    for f in b.footprints:
        for p in f.pads:
            if p.type == 'np_thru_hole' and p.drill:
                hm = g.zeros(); g.circle(hm, (p.x, p.y), p.drill / 2); ins &= ~hm
    vias = [v for v in b.vias if g.x0 <= v['at'][0] <= g.x0 + g.nx * res and g.y0 <= v['at'][1] <= g.y0 + g.ny * res]
    return g, F, B, ins, vias


def _ids(net, g, p, ins):
    m = g.zeros(); g.pad(m, p); m &= ins
    ids = net['idx'][0][m]
    return ids[ids >= 0]


def chip_rth(b, f, fills, P, t_pcb):
    import scipy.sparse as sp, scipy.sparse.linalg as spla
    sm = mg.smd_pads(f)
    g, F, B, ins, vias = _window(b, f.pads_bbox(), P['chip_win'], P['chip_res'], fills, f.side)
    net = build(g, F, B, ins, vias, P['h_hand'], t_pcb * 1e-3)
    lu = _lu(net['K'] + sp.diags(net['Gamb']))
    out = {}
    for p in sm:
        ids = _ids(net, g, p, ins)
        if not len(ids):
            continue
        q = np.zeros(net['N']); q[ids] = 1 / len(ids)
        out[p.num] = round(float(lu.solve(q)[ids].mean()), 2)
    return out


def hand(b, f, fills, P, t_pcb, pour_nets):
    """全 SMD パッドの R_th、と ベタ側で最も R_th が小さいパッド・ベタ以外で最も大きいパッドの こて先 → 232 ℃ 到達時間"""
    import scipy.sparse as sp, scipy.sparse.linalg as spla
    sm = mg.smd_pads(f)
    bb = f.courtyard_bbox() or f.pads_bbox()
    g, F, B, ins, vias = _window(b, bb, P['hand_win'], P['hand_res'], fills, f.side)
    net = build(g, F, B, ins, vias, P['h_hand'], t_pcb * 1e-3)
    A = (net['K'] + sp.diags(net['Gamb'])).tocsc(); lu = _lu(A)
    R = {}; ids_ = {}
    for p in sm:
        ids = _ids(net, g, p, ins)
        if not len(ids) or not p.net or p.net.startswith('unconnected'):
            continue
        q = np.zeros(net['N']); q[ids] = 1 / len(ids); ids_[p.num] = ids
        R[p.num] = round(float(lu.solve(q)[ids].mean()), 2)
    pour = [p.num for p in sm if p.num in R and p.net in pour_nets]
    other = [p.num for p in sm if p.num in R and p.net not in pour_nets]
    picks = ([min(pour, key=lambda n: R[n])] if pour else []) + ([max(other, key=lambda n: R[n])] if other else [])
    dt = P['dt']; Ad = (sp.diags(net['C'] / dt) + A).tocsc()
    tr = {}
    for num in picks:
        # 線形なので u = (T - T_amb) / (T_tip - T_amb) を 1 回だけ解けば、どのこて先温度でも T = T_amb + (T_tip - T_amb) u。
        # 行列 (C/dt + K + G_amb + g_tip) はこて先温度によらないので分解も 1 回 (2026-10-16 高速化、結果は同じ)
        ids = ids_[num]; tips = {}
        gc = np.zeros(net['N']); gc[ids] = P['tip_g'] / len(ids)
        lut = _lu(Ad + sp.diags(gc))
        need = {T_tip: (P['t_hand'] - P['t_amb']) / (T_tip - P['t_amb']) for T_tip in P['tips_c'] if T_tip > P['t_amb']}
        got = {T_tip: None for T_tip in P['tips_c']}
        u = np.zeros(net['N']); Cdt = net['C'] / dt
        for k in range(1, int(P['t_max'] / dt) + 1):
            if all(got[t] is not None for t in need):
                break
            u = lut.solve(Cdt * u + gc)
            um = u[ids].mean()
            for T_tip, th in need.items():
                if got[T_tip] is None and um >= th:
                    got[T_tip] = round(k * dt, 2)
        Rth = R[num]
        for T_tip in P['tips_c']:
            tips[str(int(T_tip))] = dict(t_reach_s=got[T_tip], T_ss=round((P['tip_g'] * T_tip + P['t_amb'] / Rth) / (P['tip_g'] + 1 / Rth), 1))
        tr[num] = tips
    return dict(Rth=R, pour=pour, other=other, transient=tr)


def run(pcb, chips, hands, params=None, pour_nets=None):
    P = dict(DEFAULTS, **(params or {}))
    b = kpcb.Board(pcb); fills = mg.zone_fills(b); t_pcb = mg.board_thickness(b)
    if pour_nets is None:
        pour_nets = sorted({n for (_, n) in fills})
    t0 = time.time(); out = dict(params=P, chips={}, hand={}, t_pcb=t_pcb)
    for ref in chips:
        out['chips'][ref] = chip_rth(b, b.fp(ref), fills, P, t_pcb)
    for ref in hands:
        out['hand'][ref] = hand(b, b.fp(ref), fills, P, t_pcb, set(pour_nets))
    out['elapsed_s'] = round(time.time() - t0, 1)
    return out


def sha1(path, extra=''):
    h = hashlib.sha1(open(path, 'rb').read()); h.update(extra.encode())
    return h.hexdigest()[:16]


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pcb'); ap.add_argument('--chips', default=''); ap.add_argument('--hand', default='')
    ap.add_argument('--pour-nets', default=None); ap.add_argument('--params', default='{}'); ap.add_argument('--out', required=True)
    a = ap.parse_args()
    R = run(a.pcb, [x for x in a.chips.split(',') if x], [x for x in a.hand.split(',') if x], json.loads(a.params),
            a.pour_nets.split(',') if a.pour_nets else None)
    json.dump(R, open(a.out, 'w'), ensure_ascii=False, indent=1)
    print('mount_thermal', len(R['chips']), 'chips', len(R['hand']), 'hand', R['elapsed_s'], 's')
