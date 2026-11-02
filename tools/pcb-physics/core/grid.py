"""grid — 2.5D 格子 (共通の交換形式)。thermal / irdrop / 連成ドライバが同じ格子を使う。

PcbGrid = npz + メタ JSON (format 'pcb-physics-grid/2')。配列は [layer, j, i]、層は meta.layer_names の順 (F.Cu → 内層 → B.Cu)。
  board_frac  (Ny,Nx)    float  セルのうち基板 (外形の内側) の割合 0..1
  cu_frac     (L,Ny,Nx)  float  銅の面積率 0..1 (熱はこれを使う)
  net_id      (L,Ny,Nx)  int32  0 = 銅なし、-1 = 衝突 (セル内に 2 ネット以上)、k ≥ 1 = meta.nets[k-1]
  net_major   (L,Ny,Nx)  int32  衝突セルも含めた多数決のネット (細分 sub×sub の面積が最大)。0 = 銅なし
  net_count   (L,Ny,Nx)  int8   セル内のネット数
  edge_x_w    (L,Ny,Nx-1) float32  セル (j,i)-(j,i+1) の境界を同じネットの銅が横切る割合 0..1 (エッジ導電率係数)。斜め配線だけが横切るエッジは線幅ベース補正 k を掛けてあり 1 を超えうる (meta.diag_corr、core/diag.py)
  edge_x_net  (L,Ny,Nx-1) int32    そのネット (0 = 接続なし、-1 = 境界を 2 ネットが横切る)
  edge_y_w / edge_y_net (L,Ny-1,Nx) 同じく セル (j,i)-(j+1,i)
  edge_x_cu / edge_y_cu (L,Ny,Nx-1)/(L,Ny-1,Nx) float32  セル中心 → 隣のセル中心 の経路の銅の割合 0..1 (ネットは問わない、core/raster.edge_conduct)。
              両端の空の列は除く (銅がセル中心に届かない縁のセルが切り離されない、空の端の向こうに銅があればセル内の隙間として 0、meta.edge_cu.trim)。
              熱の面内伝導に使う (隙間がセルより細いサーマルリリーフもスポーク幅だけでつながる)。2026-10-16 追加、無い格子 (古い npz) では熱側は cu_frac に戻る
     → 同層の隣接セルは edge_*_net ≥ 1 の時だけ接続。シートの導電率で G = σ(T)·t_cu·edge_w [S] (線幅 w の直線配線なら edge_w = w/dx)
  via_*       (Nv,)  ビア・めっき穴: x_mm, y_mm, drill_mm, j, i, net (1 始まり、0 = なし), l_from, l_to (銅層の添字), kind (0 via / 1 めっき穴 pad)
  pad_*       (Np,)  pad の一覧 (層ごとに 1 行。PTH は両層): ref, pin, net (1 始まり), layer, x_mm, y_mm, area_mm2, cell_ptr
  padcell_*   (Nc,)  pad → セル対応 (CSR): pad k のセルは padcell_[cell_ptr[k]:cell_ptr[k+1]]。j, i, area_mm2 (セル内の pad 面積)、w (= area / pad 面積、合計 1)
  island      (L,Ny,Nx) int32  銅の連結成分の番号 (同ネットのエッジ + ビアでつながる。0 = 銅なし)。meta.islands に 孤立銅 (pad の無い成分)
meta: format、pcb (ファイル名・sha256)、mode、dx_mm、x0_mm、y0_mm、nx、ny、sub、layers [{name, cu_mm}]、dielectric_mm、plating_mm、plating_src、stackup_src、
      nets [名前]、grid_id、checks {conflict_cells, thin_tracks, zones_unfilled, islands (孤立銅)、split_nets (pad を持つ成分が 2 つ以上のネット)}、warnings [文]。
座標: KiCad の mm、y 下向き。セル (j, i) の中心 = (x0 + (i + 0.5) dx, y0 + (j + 0.5) dx)。

マップ (同じ格子の npz、save_map / load_map):
  温度マップ  kind='temperature'  data (L,Ny,Nx) [℃]  銅層ごとのセル温度 (基板外 NaN)
  発熱マップ  kind='heat'         data (L,Ny,Nx) [W]  銅層ごとのセルの発熱 (ジュール熱など)
  meta_json に grid_id を持ち、読み込み時に格子と照合する。
"""
import hashlib, json, os
import numpy as np
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from . import raster, stackup as S, diag

FORMAT = 'pcb-physics-grid/2'
THIN_RATIO = 2.0     # 線幅 < THIN_RATIO × dx で警告


class PcbGrid:
    def __init__(self, meta, arrays, g=None):
        self.meta = meta; self.a = arrays; self.g = g

    def __getattr__(self, k):
        a = self.__dict__.get('a', {})
        if k in a:
            return a[k]
        if k == 'vias':
            return {n[4:]: v for n, v in a.items() if n.startswith('via_')}
        raise AttributeError(k)

    @property
    def shape(self):
        return self.a['cu_frac'].shape

    def save(self, path_npz):
        np.savez_compressed(path_npz, **self.a, meta_json=json.dumps(self.meta, ensure_ascii=False))
        json.dump(self.meta, open(os.path.splitext(path_npz)[0] + '.json', 'w'), indent=1, ensure_ascii=False)

    @staticmethod
    def load(path_npz):
        z = np.load(path_npz, allow_pickle=False)
        return PcbGrid(json.loads(str(z['meta_json'])), {k: z[k] for k in z.files if k != 'meta_json'})

    def net_index(self, name):
        return self.meta['nets'].index(name) + 1

    def net_mask(self, name, layer=None, major=True):
        """ネットの切り出し (major=True は衝突セルも多数決で含める)"""
        m = (self.a['net_major'] if major else self.a['net_id']) == self.net_index(name)
        return m if layer is None else m[self.meta['layer_names'].index(layer)]

    def pad_cells(self, k):
        p0, p1 = self.a['pad_cell_ptr'][k], self.a['pad_cell_ptr'][k + 1]
        return self.a['padcell_j'][p0:p1], self.a['padcell_i'][p0:p1], self.a['padcell_w'][p0:p1]


def _grid_id(meta):
    key = json.dumps([meta['nx'], meta['ny'], round(meta['x0_mm'], 6), round(meta['y0_mm'], 6), meta['dx_mm'], meta['layer_names']])
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def build(board, mode, dx_mm=0.25, sub=4, stackup_override=None, checks=True, board_spec=None, diag_corr=True, project=None):
    """project: 案件 YAML (dict、plating があればめっき厚に使う)。めっき厚は S.resolve_plating (案件 > 基板 YAML process.via_plating > 既定)"""
    st = S.resolve(board, stackup_override, board_spec)
    g = raster.Grid(board['outline'], dx_mm, sub)
    nets = sorted({p.get('net', '') for fp in board['footprints'] for p in fp['pads']} | {t.get('net', '') for t in board['tracks']} |
                  {v.get('net', '') for v in board['vias']} | {z.get('net', '') for z in board['zones']} - {''})
    netidx = {n: i for i, n in enumerate(nets)}
    L = len(board['cu_layers']); li = {n: i for i, n in enumerate(board['cu_layers'])}
    A = dict(board_frac=g.board)
    phis, ids, majs, cnts, ex_w, ex_n, ey_w, ey_n = [], [], [], [], [], [], [], []; n_corr = 0; ex_c, ey_c = [], []
    for ly in board['cu_layers']:
        phi, lab, trk = raster.copper_layer(g, board, ly, mode, netidx, return_trk=True)
        maj, nn = raster.cell_nets(g, lab, len(nets))
        nid = maj.copy(); nid[nn >= 2] = -1
        e = raster.edges(g, lab); ec = raster.edge_conduct(g, lab); ex_c.append(ec['x']); ey_c.append(ec['y'])
        if diag_corr and mode == 'artwork' and board['tracks']:   # 斜め配線の線幅ベース補正 (core/diag.py)。熱は cu_frac を使うので影響なし
            kt = np.array([diag.track_factor(t, g) for t in board['tracks']] + [1.0])
            for ax, et in raster.edge_tracks(g, lab, trk).items():
                f = kt[et]; f[et < 0] = 1.0
                n_corr += int((f != 1.0).sum())
                e[ax] = ((e[ax][0] * f).astype(np.float32), e[ax][1])
        phis.append(phi); ids.append(nid); majs.append(maj); cnts.append(nn)
        ex_w.append(e['x'][0]); ex_n.append(e['x'][1]); ey_w.append(e['y'][0]); ey_n.append(e['y'][1])
    A.update(cu_frac=np.array(phis), net_id=np.array(ids, np.int32), net_major=np.array(majs, np.int32), net_count=np.array(cnts, np.int8),
             edge_x_w=np.array(ex_w), edge_x_net=np.array(ex_n, np.int32), edge_y_w=np.array(ey_w), edge_y_net=np.array(ey_n, np.int32), edge_x_cu=np.array(ex_c), edge_y_cu=np.array(ey_c))
    # ビア接続リスト
    V = dict(x_mm=[], y_mm=[], drill_mm=[], j=[], i=[], net=[], l_from=[], l_to=[], kind=[])

    def addv(x, y, d, net, l0, l1, kind):
        j, i = g.cell_of(x, y)
        for k, v in zip(V, (x, y, d, j, i, netidx[net] + 1 if net in netidx else 0, l0, l1, kind)):
            V[k].append(v)
    if mode == 'artwork':
        for v in board['vias']:
            ls = [li[n] for n in v['layers'] if n in li] or [0, L - 1]
            addv(v['x'], v['y'], v['drill'], v.get('net', ''), min(ls), max(ls), 0)
    for fp in board['footprints']:
        for p in fp['pads']:
            if p['type'] == 'thru_hole' and p['drill'] > 0:
                addv(p['x'], p['y'], p['drill'], p.get('net', ''), 0, L - 1, 1)
    dt = dict(x_mm=float, y_mm=float, drill_mm=float, j=np.int32, i=np.int32, net=np.int32, l_from=np.int32, l_to=np.int32, kind=np.int8)
    A.update({f'via_{k}': np.array(v, dtype=dt[k]) for k, v in V.items()})
    # pad → セル (面積重み)
    P = dict(ref=[], pin=[], net=[], layer=[], x_mm=[], y_mm=[], area_mm2=[]); C = dict(j=[], i=[], area_mm2=[], w=[]); ptr = [0]; MASKS = []
    for fp in board['footprints']:
        for p in fp['pads']:
            if p['type'] == 'np_thru_hole':
                continue
            for ly in board['cu_layers']:
                if not raster.pad_on(p, ly):
                    continue
                sl, m = raster.pad_mask(g, p); m = m & g.fine_board[sl]
                jf, if_ = np.nonzero(m)
                if not len(jf):
                    continue
                jc = (jf + sl[0].start) // sub; ic = (if_ + sl[1].start) // sub
                key = jc * g.nx + ic; uk, cnt = np.unique(key, return_counts=True)
                area = cnt * g.fs ** 2; tot = area.sum()
                C['j'] += list(uk // g.nx); C['i'] += list(uk % g.nx); C['area_mm2'] += list(area); C['w'] += list(area / tot)
                ptr.append(len(C['j']))
                for k, v in zip(P, (fp['ref'], str(p['num']), netidx[p['net']] + 1 if p.get('net') in netidx else 0, li[ly], p['x'], p['y'], tot)):
                    P[k].append(v)
                MASKS.append((sl, m))
    # 重なった pad (USB-C の A1/B12 など、同じ層で銅が重なる) → 別名グループ。pad_alias[k] = グループ番号 (単独は -1)
    NP = len(P['ref']); par = list(range(NP))

    def root(a):
        while par[a] != a:
            par[a] = par[par[a]]; a = par[a]
        return a
    for a_ in range(NP):
        (ja, ia), ma = MASKS[a_]
        for b_ in range(a_ + 1, NP):
            if P['layer'][b_] != P['layer'][a_]:
                continue
            (jb, ib), mb = MASKS[b_]
            j0, j1 = max(ja.start, jb.start), min(ja.stop, jb.stop); i0, i1 = max(ia.start, ib.start), min(ia.stop, ib.stop)
            if j0 >= j1 or i0 >= i1:
                continue
            if (ma[j0 - ja.start:j1 - ja.start, i0 - ia.start:i1 - ia.start] & mb[j0 - jb.start:j1 - jb.start, i0 - ib.start:i1 - ib.start]).any():
                par[root(b_)] = root(a_)
    grp = {}
    for k in range(NP):
        grp.setdefault(root(k), []).append(k)
    alias = -np.ones(NP, np.int32); aliases = []
    for ks in grp.values():
        if len(ks) < 2:
            continue
        g_id = len(aliases); alias[ks] = g_id
        nn = sorted({nets[P['net'][k] - 1] if P['net'][k] > 0 else '' for k in ks})
        refs = sorted({P['ref'][k] for k in ks})
        aliases.append(dict(group=g_id, layer=board['cu_layers'][P['layer'][ks[0]]], pads=[f"{P['ref'][k]}.{P['pin'][k]}" for k in ks], pad_index=[int(k) for k in ks],
                            name=refs[0] + '.' + '='.join(P['pin'][k] for k in ks) if len(refs) == 1 else '='.join(f"{P['ref'][k]}.{P['pin'][k]}" for k in ks),
                            nets=nn, same_net=len(nn) == 1))
    A.update(pad_alias=alias)
    A.update(pad_ref=np.array(P['ref'], dtype='U16'), pad_pin=np.array(P['pin'], dtype='U8'), pad_net=np.array(P['net'], np.int32),
             pad_layer=np.array(P['layer'], np.int8), pad_x_mm=np.array(P['x_mm']), pad_y_mm=np.array(P['y_mm']),
             pad_area_mm2=np.array(P['area_mm2']), pad_cell_ptr=np.array(ptr, np.int64),
             padcell_j=np.array(C['j'], np.int32), padcell_i=np.array(C['i'], np.int32),
             padcell_area_mm2=np.array(C['area_mm2']), padcell_w=np.array(C['w']))
    plating_mm, plating_src = S.resolve_plating(project, board_spec)
    meta = dict(format=FORMAT, pcb=os.path.basename(board['path']),
                pcb_sha256=hashlib.sha256(open(board['path'], 'rb').read()).hexdigest(), mode=mode,
                dx_mm=dx_mm, x0_mm=float(g.x0), y0_mm=float(g.y0), nx=g.nx, ny=g.ny, sub=sub,
                layer_names=list(board['cu_layers']), layers=[dict(name=n, cu_mm=t, **(dict(er_below=st['er'][i], er_below_src=st['er_src'][i], h_below_mm=st['diel'][i]) if i < len(st['diel']) and st.get('er') else {}))
                                                               for i, (n, t) in enumerate(zip(board['cu_layers'], st['cu']))],
                dielectric=[dict(between=[board['cu_layers'][i], board['cu_layers'][i + 1]], h_mm=h, er=(st.get('er') or [None] * len(st['diel']))[i],
                                 er_src=(st.get('er_src') or [None] * len(st['diel']))[i]) for i, h in enumerate(st['diel'])],
                dielectric_mm=list(st['diel']), stackup_src=st['src'], stackup_assumed=st['assumed'], stackup_warnings=list(st.get('warnings', [])), board_thickness_mm=st.get('thickness', board['thickness']),
                plating_mm=plating_mm, plating_src=plating_src, nets=nets, diag_corr=dict(enabled=bool(diag_corr and mode == 'artwork'), edges=n_corr, method='core/diag.py 線幅ベース (同角度・同線幅の直線を数値で解いた比 k を、その配線だけが横切るエッジの w に掛ける)'),
                coords='KiCad mm、y 下向き。セル (j,i) の中心 = (x0 + (i+0.5) dx, y0 + (j+0.5) dx)。配列は [layer, j, i]')
    meta['pad_aliases'] = aliases
    meta['edge_cu'] = dict(arrays=['edge_x_cu', 'edge_y_cu'], method='core/raster.edge_conduct (= path_conduct、k=None、trim): セル中心間の経路 (細分 sub×sub) の列ごとの銅の割合を直列 (調和平均)、両端の空の列は除く (空の端の向こうに銅があればセル内の隙間として 0)、ネットは問わない。熱の面内伝導用 (2026-10-16、端の除外は同日夕)', trim=True)
    meta['grid_id'] = _grid_id(meta)
    pg = PcbGrid(meta, A, g)
    if checks:
        run_checks(pg, board)
    return pg, st


def run_checks(pg, board):
    """衝突、細い配線、未充填ゾーン、孤立銅、分断ネット → meta.checks / meta.warnings、island 配列"""
    m = pg.meta; A = pg.a; dx = m['dx_mm']; L, ny, nx = A['cu_frac'].shape; W = list(m.get('stackup_warnings', []))
    nets = m['nets']
    # ① 衝突
    conf = {m['layer_names'][l]: int((A['net_id'][l] == -1).sum()) for l in range(L)}
    if sum(conf.values()):
        W.append(f"セル内に複数ネット (net_id = -1): " + ', '.join(f'{k} {v} セル' for k, v in conf.items()) + f" (dx {dx} mm。細かくすると減る)")
    # ⑤ 細い配線
    ws = sorted({round(t['w'], 4) for t in board['tracks']}) if m['mode'] == 'artwork' else []
    thin = [w for w in ws if w < THIN_RATIO * dx]
    n_thin = sum(1 for t in board['tracks'] if t['w'] < THIN_RATIO * dx) if m['mode'] == 'artwork' else 0
    if thin:
        W.append(f"線幅 < {THIN_RATIO:g}×dx ({THIN_RATIO * dx:.3f} mm) の配線 {n_thin} 本 (幅 {thin} mm)。電流・電圧降下には dx = {min(thin) / THIN_RATIO:.3f} mm 以下 (例 0.1 mm) を推奨")
    # 未充填ゾーン
    if board.get('zones_unfilled'):
        W.append(f"未充填のゾーン {len(board['zones_unfilled'])} 個 (KiCad で塗りつぶしてから保存): {board['zones_unfilled']}")
    # ⑥ 連結成分 (同ネットのエッジ + ビア)
    N = L * ny * nx
    node = lambda l, j, i: (l * ny + j) * nx + i
    cop = A['net_major'] > 0
    r, c = [], []
    for l in range(L):
        jj, ii = np.nonzero(A['edge_x_net'][l] > 0); r.append(node(l, jj, ii)); c.append(node(l, jj, ii + 1))
        jj, ii = np.nonzero(A['edge_y_net'][l] > 0); r.append(node(l, jj, ii)); c.append(node(l, jj + 1, ii))
    for k in range(len(A['via_j'])):
        j, i = int(A['via_j'][k]), int(A['via_i'][k])
        if not (0 <= j < ny and 0 <= i < nx):
            continue
        for l in range(int(A['via_l_from'][k]), int(A['via_l_to'][k])):
            if cop[l, j, i] and cop[l + 1, j, i]:
                r.append(np.array([node(l, j, i)])); c.append(np.array([node(l + 1, j, i)]))
    r = np.concatenate(r) if r else np.zeros(0, int); c = np.concatenate(c) if c else np.zeros(0, int)
    G = sp.coo_matrix((np.ones(len(r)), (r, c)), shape=(N, N))
    ncomp, labels = connected_components(G, directed=False)
    labels = labels.reshape(L, ny, nx)
    lab_cu = np.where(cop, labels + 1, 0)
    comps = np.unique(lab_cu[lab_cu > 0])
    remap = np.zeros(ncomp + 2, np.int32); remap[comps] = np.arange(1, len(comps) + 1)
    island = remap[lab_cu]; A['island'] = island.astype(np.int32)
    # pad のある成分
    padcomp = {}
    for k in range(len(A['pad_ref'])):
        js, is_, _ = pg.pad_cells(k); l = int(A['pad_layer'][k])
        for cid in np.unique(island[l, js, is_]):
            if cid > 0:
                padcomp.setdefault(int(cid), set()).add(f"{A['pad_ref'][k]}.{A['pad_pin'][k]}")
    isl = []; per_net = {}
    area_cell = dx * dx
    for cid in range(1, len(comps) + 1):
        msk = island == cid
        ls, js, is_ = np.nonzero(msk)
        nmaj = np.bincount(A['net_major'][msk]).argmax()
        net = nets[nmaj - 1] if nmaj > 0 else '?'
        area = float((A['cu_frac'][msk]).sum() * area_cell)
        if cid in padcomp:
            per_net.setdefault(net, []).append(dict(id=cid, pads=sorted(padcomp[cid])[:6], n_pads=len(padcomp[cid])))
        elif area >= area_cell:   # 1 セル未満のかけら (細分の端) は数えない
            isl.append(dict(id=cid, net=net, layers=sorted({m['layer_names'][x] for x in ls}), cells=int(msk.sum()), area_mm2=round(area, 3),
                            bbox_mm=[round(m['x0_mm'] + is_.min() * dx, 2), round(m['y0_mm'] + js.min() * dx, 2),
                                     round(m['x0_mm'] + (is_.max() + 1) * dx, 2), round(m['y0_mm'] + (js.max() + 1) * dx, 2)]))
    split = {n: v for n, v in per_net.items() if len(v) >= 2 and n} if m['mode'] == 'artwork' else {}
    if isl:
        W.append(f"孤立銅 (pad につながらない銅) {len(isl)} 個、合計 {sum(x['area_mm2'] for x in isl):.2f} mm² (meta.checks.islands)")
    if split:
        W.append(f"pad を持つ成分が 2 つ以上に分かれたネット {len(split)} 本 (格子上で未接続、または dx が粗く細い配線が切れた): {sorted(split)[:10]}")
    m['checks'] = dict(conflict_cells=conf, min_track_mm=min(ws) if ws else None, thin_tracks=n_thin, thin_widths_mm=thin,
                       zones_unfilled=board.get('zones_unfilled', []), islands=isl, split_nets=split, n_components=int(len(comps)))
    al = m.get('pad_aliases', [])
    diff = [x for x in al if not x['same_net']]
    if diff:
        W.append(f"別ネットの pad が重なっている {len(diff)} 組 (ショート?): " + ', '.join(f"{x['name']} {x['nets']}" for x in diff[:5]))
    m['checks']['pad_aliases'] = len(al)
    m['warnings'] = W


def save_map(path_npz, grid_meta, kind, data, unit, extra=None):
    assert kind in ('temperature', 'heat')
    m = dict(format='pcb-physics-map/1', kind=kind, unit=unit, grid_id=grid_meta['grid_id'], shape=list(data.shape),
             layer_names=grid_meta['layer_names'], **(extra or {}))
    np.savez_compressed(path_npz, data=data, meta_json=json.dumps(m, ensure_ascii=False))


def load_map(path_npz, grid_meta=None, kind=None):
    z = np.load(path_npz, allow_pickle=False); m = json.loads(str(z['meta_json']))
    if grid_meta is not None and m['grid_id'] != grid_meta['grid_id']:
        raise ValueError(f"{path_npz}: 格子が違う (grid_id {m['grid_id']} ≠ {grid_meta['grid_id']})")
    if kind and m['kind'] != kind:
        raise ValueError(f"{path_npz}: kind {m['kind']} ≠ {kind}")
    return z['data'], m
