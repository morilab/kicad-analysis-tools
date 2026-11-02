"""solver — 2.5D 有限差分の IR ドロップ (直流)。ネットごとに独立に解く。

モデル
  節点 = 銅層ごとのセル (net_major == そのネット。衝突セル -1 も多数決で含める)。
  同層の隣接: edge_*_net == そのネットの時だけ G = σ(T)·t_cu·edge_w [S] (正方セル: 長さ dx / 幅 edge_w·dx)。
  ビア・めっき穴: 筒 (内径 drill、めっき厚 plating) の R = ρ·h / (π((r+t)² − r²))、h = 層間の誘電体厚の和 (銅の中心間を近似)。
  電圧源 = pad のセルをディリクレ (pad 全体を等電位)。負荷 = pad のセルへ面積重み (padcell_w) で電流を配る。
  温度: セル温度で ρ(T) = ρ20 (1 + α (T − 20))、辺は両端セルの平均温度。
出力: 電位 (L,Ny,Nx)、ジュール熱 (辺・ビアの I²R を両端セルに半分ずつ)、辺の電流密度 [A/mm²]。
"""
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components
from .interface import IrSolver, IrResult, rho_cu, RHO_CU_20, ALPHA_CU


class NetSolveError(Exception):
    pass


def find_pads(grid, ref, pin, net_k=None):
    """grid の pad 行 (層ごと) の添字"""
    A = grid.a
    ks = np.nonzero((A['pad_ref'] == ref) & (A['pad_pin'] == str(pin)))[0]
    if net_k is not None:
        ks = [k for k in ks if A['pad_net'][k] == net_k]
    return list(ks)


def solve_net(grid, net, temp_c, sinks, fixed, cond=None):
    """1 ネットを解く。
    sinks: [(ref, pin, I [A], 正 = ネットから部品へ流れ出す)]、fixed: [(ref, pin, V)]
    戻り値 dict(volt (L,Ny,Nx) NaN 以外がネット, heat (L,Ny,Nx) W, pin_v {(ref,pin): V}, edges 電流密度リスト, info)"""
    cond = cond or {}
    A = grid.a; m = grid.meta; L, ny, nx = A['cu_frac'].shape; dx = m['dx_mm'] * 1e-3
    k = grid.net_index(net)
    T = np.full((L, ny, nx), float(cond.get('ref_temp_c', 20.0))) if temp_c is None else np.nan_to_num(np.asarray(temp_c, float), nan=20.0)
    rho = rho_cu(T, cond.get('rho_cu_ohm_m', RHO_CU_20), cond.get('alpha_per_c', ALPHA_CU))
    node_mask = A['net_major'] == k
    idx = -np.ones((L, ny, nx), np.int64); n = int(node_mask.sum()); idx[node_mask] = np.arange(n)
    if n == 0:
        raise NetSolveError(f'{net}: 格子にネットの銅が無い')
    R, C, G = [], [], []; E = []   # E: 辺 (l, j0, i0, j1, i1, G, 幅 m)
    for l in range(L):
        t = m['layers'][l]['cu_mm'] * 1e-3
        for ax, w, en in (('x', A['edge_x_w'][l], A['edge_x_net'][l]), ('y', A['edge_y_w'][l], A['edge_y_net'][l])):
            jj, ii = np.nonzero((en == k) & (w > 0))
            j1, i1 = (jj, ii + 1) if ax == 'x' else (jj + 1, ii)
            a, b = idx[l, jj, ii], idx[l, j1, i1]; ok = (a >= 0) & (b >= 0)
            jj, ii, j1, i1, a, b = jj[ok], ii[ok], j1[ok], i1[ok], a[ok], b[ok]
            ww = w[jj, ii].astype(float)
            r = 0.5 * (rho[l, jj, ii] + rho[l, j1, i1])
            g = t * ww / r          # σ t w (正方セル)
            R.append(a); C.append(b); G.append(g)
            E.append(dict(l=l, j0=jj, i0=ii, j1=j1, i1=i1, g=g, width_m=ww * dx, t=t))
    # ビア
    tp = m.get('plating_mm', 0.018) * 1e-3; diel = m['dielectric_mm']
    VIA = []
    for v in range(len(A['via_j'])):
        if A['via_net'][v] != k:
            continue
        j, i = int(A['via_j'][v]), int(A['via_i'][v])
        if not (0 <= j < ny and 0 <= i < nx):
            continue
        r0 = A['via_drill_mm'][v] * 1e-3 / 2; Ab = np.pi * ((r0 + tp) ** 2 - r0 ** 2)
        for l in range(int(A['via_l_from'][v]), int(A['via_l_to'][v])):
            a, b = idx[l, j, i], idx[l + 1, j, i]
            if a < 0 or b < 0:
                continue
            h = diel[l] * 1e-3 + 0.5 * (m['layers'][l]['cu_mm'] + m['layers'][l + 1]['cu_mm']) * 1e-3
            g = Ab / (0.5 * (rho[l, j, i] + rho[l + 1, j, i]) * h)
            R.append(np.array([a])); C.append(np.array([b])); G.append(np.array([g])); VIA.append((l, j, i, a, b, g, v))
    r = np.concatenate(R) if R else np.zeros(0, int); c = np.concatenate(C) if C else np.zeros(0, int); g = np.concatenate(G) if G else np.zeros(0)
    Y = sp.coo_matrix((np.r_[-g, -g, g, g], (np.r_[r, c, r, c], np.r_[c, r, r, c])), shape=(n, n)).tocsr()
    rhs = np.zeros(n); warn = []
    def pad_nodes(ref, pin):
        out = []
        for pk in find_pads(grid, ref, pin):
            js, is_, ws = grid.pad_cells(pk); l = int(A['pad_layer'][pk])
            nd = idx[l, js, is_]; ok = nd >= 0
            if ok.sum() < len(nd):
                warn.append(f'{net}: {ref}.{pin} の pad セル {len(nd) - ok.sum()}/{len(nd)} が別ネット/衝突で除外')
            out.append((nd[ok], ws[ok]))
        if not out or sum(len(o[0]) for o in out) == 0:
            raise NetSolveError(f'{net}: {ref}.{pin} の pad が格子上でネットにつながらない')
        nd = np.concatenate([o[0] for o in out]); ws = np.concatenate([o[1] for o in out])
        return nd, ws / ws.sum()
    for ref, pin, I in sinks:
        nd, ws = pad_nodes(ref, pin)
        np.add.at(rhs, nd, -I * ws)
    fix = {}
    for ref, pin, V in fixed:
        nd, _ = pad_nodes(ref, pin)
        for q in nd:
            fix[int(q)] = V
    if not fix:
        raise NetSolveError(f'{net}: 電圧源が無い')
    # 連結成分の検査 (電圧源の無い成分に負荷があれば止める)
    ncomp, lab = connected_components(Y, directed=False)
    fixed_comp = set(lab[list(fix)])
    loose = [cc for cc in np.unique(lab[np.nonzero(rhs)[0]]) if cc not in fixed_comp]
    if loose:
        raise NetSolveError(f'{net}: 電圧源につながらない銅に負荷 ({len(loose)} 成分)')
    act = np.isin(lab, list(fixed_comp))     # 源とつながる成分だけ解く
    fi = np.array(sorted(fix)); fv = np.array([fix[q] for q in fi])
    free = np.nonzero(act & ~np.isin(np.arange(n), fi))[0]
    V = np.full(n, np.nan); V[fi] = fv
    if len(free):
        Yff = Y[free][:, free].tocsc(); b = rhs[free] - Y[free][:, fi] @ fv
        V[free] = spla.spsolve(Yff, b)
    resid = float(np.abs(Y[free][:, :] @ np.nan_to_num(V) - rhs[free]).max()) if len(free) else 0.0
    volt = np.full((L, ny, nx), np.nan); volt[node_mask] = V
    heat = np.zeros((L, ny, nx)); J = []
    for e in E:
        va = volt[e['l'], e['j0'], e['i0']]; vb = volt[e['l'], e['j1'], e['i1']]
        I = e['g'] * (va - vb); P = np.nan_to_num(I * (va - vb))
        np.add.at(heat[e['l']], (e['j0'], e['i0']), P / 2); np.add.at(heat[e['l']], (e['j1'], e['i1']), P / 2)
        J.append((e['l'], e['j0'], e['i0'], e['j1'], e['i1'], np.nan_to_num(np.abs(I)) / (e['width_m'] * e['t'] * 1e6)))
    via_I = []
    for (l, j, i, a, b, gv, v) in VIA:
        I = gv * (V[a] - V[b]); P = float(np.nan_to_num(I * (V[a] - V[b])))
        heat[l, j, i] += P / 2; heat[l + 1, j, i] += P / 2; via_I.append((v, float(np.nan_to_num(I))))
    pin_v = {}
    for ref, pin, *_ in list(sinks) + list(fixed):
        nd, ws = pad_nodes(ref, pin); pin_v[(ref, str(pin))] = float(np.dot(V[nd], ws))
    warn = sorted(set(warn))
    I_in = float(sum(s[2] for s in sinks))
    return dict(volt=volt, heat=heat, pin_v=pin_v, jedges=J, via_i=via_I, info=dict(
        net=net, nodes=int(n), solved=int(act.sum()), unconnected_nodes=int((~act).sum()), edges=int(len(g)), vias=len(VIA),
        sink_total_a=I_in, joule_w=float(heat.sum()), residual_a=resid, warnings=warn))


def hot_spots(grid, res, top=10, min_sep_mm=1.0):
    """電流密度の高い辺 (A/mm²) を、min_sep_mm 以上離れたものから top 件"""
    m = grid.meta; dx = m['dx_mm']; rows = []
    for (l, j0, i0, j1, i1, Jd) in res['jedges']:
        if not len(Jd):
            continue
        o = np.argsort(-Jd)[:top * 50]
        for q in o:
            x = m['x0_mm'] + (0.5 * (i0[q] + i1[q]) + 0.5) * dx; y = m['y0_mm'] + (0.5 * (j0[q] + j1[q]) + 0.5) * dx
            rows.append((float(Jd[q]), m['layer_names'][l], x, y))
    rows.sort(key=lambda r: -r[0]); out = []
    for r in rows:
        if all((r[2] - o[2]) ** 2 + (r[3] - o[3]) ** 2 >= min_sep_mm ** 2 for o in out):
            out.append(r)
        if len(out) >= top:
            break
    return [dict(j_a_mm2=round(a, 3), layer=b, x_mm=round(c, 2), y_mm=round(d, 2)) for a, b, c, d in out]


class FdIrSolver(IrSolver):
    """IrSolver の実装。sources: [{ref, pin, net, current_a (正 = 部品へ流れ込む)}]、電圧源は {ref, pin, net, v_set}"""
    name = '2.5D 有限差分 (scipy spsolve)'

    def solve(self, grid, temp_c, sources, cond):
        L = grid.cu_frac.shape
        heat = np.zeros(L); volt = np.full(L, np.nan); drops = []; info = dict(nets={}, warnings=[])
        nets = sorted({s['net'] for s in sources if s.get('net')})
        for net in nets:
            ss = [s for s in sources if s['net'] == net]
            fixed = [(s['ref'], s['pin'], s['v_set']) for s in ss if 'v_set' in s]
            sinks = [(s['ref'], s['pin'], s['current_a']) for s in ss if 'v_set' not in s and s.get('current_a')]
            if not fixed:
                info['warnings'].append(f'{net}: 電圧源 (v_set) が無いので解かない'); continue
            r = solve_net(grid, net, temp_c, sinks, fixed, cond)
            heat += r['heat']; ok = ~np.isnan(r['volt']); volt[ok] = r['volt'][ok]
            v0 = fixed[0][2]
            for (ref, pin), v in r['pin_v'].items():
                drops.append(dict(net=net, **{'from': f'{fixed[0][0]}.{fixed[0][1]}'}, to=f'{ref}.{pin}', drop_v=v0 - v))
            info['nets'][net] = r['info']; info['warnings'] += r['info']['warnings']
        return IrResult(heat_w=heat, volt_v=volt, drops=drops, info=info)
