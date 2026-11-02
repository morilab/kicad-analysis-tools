"""extract — 格子 (core grid/2) からポート間の R・L を抽出する (準静的)。

- R: irdrop と同じ同ネットの辺 G = σ t edge_w (斜め補正込み) とビアの筒。
- L: 電源の配線と GND (反対層のベタ) がつくるループの単位長インダクタンスを辺に配る。
  L' = Z0_air(w, h) / c0 (空気中マイクロストリップの特性インピーダンス、Hammerstad/Wheeler)、w = その辺を含む同ネットの帯の局所幅、h = 層間の誘電体厚。
  帯の中の各辺には L_e = L'(w)·dx·(w / w_e) (並列にすると L'·dx)。GND は L を持たない (ループの L は電源側に含めた)。
  ビア: L_via = μ0 h / (2π) · (ln(4h/d) + 1) (h = 板厚、d = 穴径)。
- ポート = pad (pad のセルを 1 節点にまとめる)。重なった pad は core の pad_alias で 1 ポート。内部節点を Kron 縮約し、ポート間の枝 g_ij (S)・γ_ij (1/H) を返す。
  回路では各枝を R_ij = 1/g_ij と L_ij = 1/γ_ij の直列で表す (直流と高周波で正確、中間は近似 → tests の 6 で確認)。"""
import math
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components
from irdrop.interface import rho_cu

MU0 = 4e-7 * math.pi
C0 = 299792458.0


def z0_air(w, h):
    """空気中マイクロストリップ (薄い導体) の特性インピーダンス [Ω] (Hammerstad)。w, h は同じ単位"""
    u = np.asarray(w, float) / h
    narrow = 60 * np.log(8 / u + u / 4)
    wide = 120 * np.pi / (u + 1.393 + 0.667 * np.log(u + 1.444))
    return np.where(u <= 1, narrow, wide)


def lprime(w_m, h_m):
    """単位長ループインダクタンス [H/m]"""
    return z0_air(w_m, h_m) / C0


def via_l(h_m, d_m):
    return MU0 * h_m / (2 * math.pi) * (math.log(4 * h_m / d_m) + 1)


def _runs_width(E, W, axis):
    """E (bool) の帯 (axis に沿った連続) ごとに W の和を求め、各要素に帯の幅の和を返す"""
    E = np.moveaxis(E, axis, 0); W = np.moveaxis(W, axis, 0)
    out = np.zeros(E.shape)
    n = E.shape[0]
    start = np.zeros(E.shape[1:], int); acc = np.zeros(E.shape[1:])
    for k in range(n + 1):
        on = E[k] if k < n else np.zeros(E.shape[1:], bool)
        # 帯が終わったところで、その帯の全要素に acc を書く
        if k > 0:
            ended = E[k - 1] & ~on
            if ended.any():
                idx = np.nonzero(ended)
                for a in zip(*idx):
                    out[(slice(start[a], k),) + a] = acc[a]
        newrun = on & ~(E[k - 1] if k > 0 else np.zeros_like(on))
        start[newrun] = k; acc[newrun] = 0.0
        acc[on] += W[k][on] if k < n else 0
    return np.moveaxis(out, 0, axis)


def build_edges(grid, net, with_l=True, temp_c=20.0):
    """ネットの節点 (セル) と枝 (a, b, g [S], gam [1/H]) を返す"""
    A = grid.a; m = grid.meta; L, ny, nx = A['cu_frac'].shape; dx = m['dx_mm'] * 1e-3
    k = grid.net_index(net)
    node = A['net_major'] == k
    idx = -np.ones((L, ny, nx), np.int64); n = int(node.sum()); idx[node] = np.arange(n)
    rho = float(rho_cu(temp_c))
    diel = m['dielectric_mm']
    a_, b_, g_, gam_ = [], [], [], []
    for l in range(L):
        t = m['layers'][l]['cu_mm'] * 1e-3
        h = (diel[min(l, len(diel) - 1)] if diel else 1.5) * 1e-3
        for ax in ('x', 'y'):
            w = A[f'edge_{ax}_w'][l].astype(float); en = A[f'edge_{ax}_net'][l]
            E = (en == k) & (w > 0)
            jj, ii = np.nonzero(E)
            j1, i1 = (jj, ii + 1) if ax == 'x' else (jj + 1, ii)
            a, b = idx[l, jj, ii], idx[l, j1, i1]; ok = (a >= 0) & (b >= 0)
            jj, ii, a, b = jj[ok], ii[ok], a[ok], b[ok]
            we = w[jj, ii]
            g = t * we / rho
            a_.append(a); b_.append(b); g_.append(g)
            if with_l:
                # x 方向の辺の帯は y 方向 (axis 0) に並ぶ、y 方向の辺の帯は x 方向 (axis 1)
                run = _runs_width(E, np.where(E, w, 0.0), 0 if ax == 'x' else 1)
                wl = np.maximum(run[jj, ii], 1e-3) * dx
                Le = lprime(wl, h) * dx * (wl / (we * dx))
                gam_.append(1.0 / Le)
            else:
                gam_.append(np.zeros(len(g)))
    H = (m.get('board_thickness_mm') or 1.6) * 1e-3; tp = m.get('plating_mm', 0.018) * 1e-3
    nv = 0
    for v in range(len(A['via_j'])):
        if A['via_net'][v] != k:
            continue
        j, i = int(A['via_j'][v]), int(A['via_i'][v])
        if not (0 <= j < ny and 0 <= i < nx):
            continue
        d = A['via_drill_mm'][v] * 1e-3; r0 = d / 2; Ab = math.pi * ((r0 + tp) ** 2 - r0 ** 2)
        for l in range(int(A['via_l_from'][v]), int(A['via_l_to'][v])):
            a, b = idx[l, j, i], idx[l + 1, j, i]
            if a < 0 or b < 0:
                continue
            hl = diel[l] * 1e-3 + 0.5 * (m['layers'][l]['cu_mm'] + m['layers'][l + 1]['cu_mm']) * 1e-3
            a_.append(np.array([a])); b_.append(np.array([b])); g_.append(np.array([Ab / (rho * hl)]))
            gam_.append(np.array([1.0 / via_l(H, d)]) if with_l else np.zeros(1)); nv += 1
    cat = lambda x, dt=float: np.concatenate(x).astype(dt) if x else np.zeros(0, dt)
    return dict(idx=idx, n=n, a=cat(a_, np.int64), b=cat(b_, np.int64), g=cat(g_), gam=cat(gam_), n_via=nv, k=k)


def port_cells(grid, net, ports):
    """ports: [(名前, ref, pin)] → 各ポートのセル (l, j, i) (層をまたいでまとめる)"""
    from irdrop.solver import find_pads
    A = grid.a; out = []
    for name, ref, pin in ports:
        cells = []
        for q in find_pads(grid, ref, pin):
            js, is_, _ = grid.pad_cells(q); l = int(A['pad_layer'][q])
            cells += [(l, int(j), int(i)) for j, i in zip(js, is_)]
        out.append(cells)
    return out


def reduce(grid, net, ports, with_l=True, temp_c=20.0, log=print):
    """ポート間の縮約: 戻り値 dict(names, G (P,P) [S] ラプラシアン, Gam (P,P) [1/H], info)"""
    e = build_edges(grid, net, with_l, temp_c); idx = e['idx']; n = e['n']
    pc = port_cells(grid, net, ports)
    P = len(ports)
    lab = np.arange(n) + P            # 節点 → 新番号 (ポートは 0..P-1)
    miss = []; alias = {}
    # 重なった pad (USB-C の A1/B12 など) は core の grid.a['pad_alias'] (同じ層で銅が重なる pad の別名グループ) で 1 ポートにまとめる。
    # pad_alias の無い古い格子だけ、セルの重なりで判定する (2026-10-14 置き換え)
    pa = grid.a.get('pad_alias') if hasattr(grid.a, 'get') else None
    if pa is not None:
        from irdrop.solver import find_pads
        first = {}
        for p, (name, ref, pin) in enumerate(ports):
            gs = {int(pa[q]) for q in find_pads(grid, ref, pin) if int(pa[q]) >= 0}
            hit = sorted(first[g] for g in gs if g in first)
            if hit:
                alias[name] = ports[hit[0]][0]
            for g in gs:
                first.setdefault(g, hit[0] if hit else p)
    for p, cells in enumerate(pc):
        nd = [idx[c] for c in cells if idx[c] >= 0]
        if not nd:
            miss.append(ports[p][0]); continue
        if ports[p][0] in alias:
            q = [x[0] for x in ports].index(alias[ports[p][0]]); lab[nd] = q; continue
        prev = sorted({int(lab[x]) for x in nd if lab[x] < P})
        if prev:
            # 重なった pad (USB-C の A1/B12 など): 先のポートと同じ節点にする (p は別名)
            q = prev[0]; alias[ports[p][0]] = ports[q][0]
            lab[nd] = q; continue
        lab[nd] = p
    a, b = lab[e['a']], lab[e['b']]; keep = a != b
    a, b, g, gam = a[keep], b[keep], e['g'][keep], e['gam'][keep]
    N = n + P
    def lap(w):
        M = sp.coo_matrix((np.r_[-w, -w, w, w], (np.r_[a, b, a, b], np.r_[b, a, a, b])), shape=(N, N)).tocsr()
        return M
    Gm = lap(g)
    used = np.zeros(N, bool); used[np.unique(np.r_[a, b])] = True; used[:P] = True
    nc, cl = connected_components(Gm, directed=False)
    port_comp = set(cl[:P][[p for p in range(P) if ports[p][0] not in miss and ports[p][0] not in alias]])
    keepn = np.isin(cl, list(port_comp)) & used
    keepn[:P] = True
    inter = np.nonzero(keepn)[0]; inter = inter[inter >= P]
    pi = np.arange(P)
    out = {}
    for key, w in (('G', g), ('Gam', gam)):
        if key == 'Gam' and not with_l:
            out[key] = np.zeros((P, P)); continue
        M = lap(w)
        Mpp = M[pi][:, pi].toarray(); Mpi = M[pi][:, inter]; Mii = M[inter][:, inter].tocsc()
        if len(inter):
            # 小さな正則化 (ポートにつながらない内部の浮き = 既に除外、数値誤差対策)
            Mii = Mii + sp.identity(len(inter), format='csc') * (abs(Mii.diagonal()).max() * 1e-14)
            lu = spla.splu(Mii)
            X = lu.solve(Mpi.T.toarray())
            red = Mpp - Mpi @ X
        else:
            red = Mpp
        out[key] = 0.5 * (red + red.T)
    info = dict(net=net, cells=int(n), internal=int(len(inter)), ports=P, missing_ports=miss, vias=e['n_via'],
                edges=int(len(g)), with_l=with_l, alias=alias)
    return dict(names=[p[0] for p in ports], G=out['G'], Gam=out['Gam'], info=info, alias=alias)


def branches(red, rel=1e-6):
    """ラプラシアン → 枝 [(i, j, R, L)]。g が最大の rel 倍未満の枝は捨てる"""
    G, Gm = red['G'], red['Gam']; P = len(G); out = []
    gmax = max((-G[i, j] for i in range(P) for j in range(i + 1, P)), default=0)
    for i in range(P):
        for j in range(i + 1, P):
            g = -G[i, j]
            if g <= gmax * rel:
                continue
            gm = -Gm[i, j]
            out.append((i, j, 1.0 / g, (1.0 / gm) if gm > 0 else 0.0))
    return out
