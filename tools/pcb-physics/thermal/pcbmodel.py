"""pcbmodel — 読んだ基板 + 部品 + 環境 から Network を組み立てて解く。mode: 'placement' (配置案: pad のみ) / 'artwork' (アートワーク後)"""
import math, os
import numpy as np
from core import model3d, raster, stackup as S, parts as PT
from core import grid as CG
from . import env as E
from .model import Network

H_BODY_TOP = 15.0   # W/m²K 部品上面 (対流 ≈8 + 放射 ≈7、仮定) — θJC(上) の経路にだけ使う


def build_and_solve(board, cfg, lib, mode, dx_mm=0.25, sub=4, log=print, grid=None, heat_map=None, t_init=None):
    """grid: core.grid.PcbGrid (省略時は作る)。heat_map: 外部発熱 (L,Ny,Nx) [W] (ジュール熱など、銅層のセルに加える)"""
    envc = E.merge(E.DEFAULT, cfg.get('environment'))
    if grid is None:
        grid, st = CG.build(board, mode, dx_mm, sub, cfg.get('stackup'), board_spec=cfg.get('_board_spec'), project=cfg)
    else:
        st = S.resolve(board, cfg.get('stackup'), cfg.get('_board_spec'))
    g = grid.g
    dx = dx_mm * 1e-3; A = g.board * dx * dx
    active = g.board > 0.01
    net = Network(active, dx)
    ncu = len(board['cu_layers'])
    phis = list(grid.cu_frac)
    inplane = (cfg.get('solver') or {}).get('inplane', 'edge')   # edge (既定、2026-10-16〜) / cell (旧: セルの銅の面積率の調和平均)
    info_inplane = 'edge' if (inplane == 'edge' and 'edge_x_cu' in grid.a) else 'cell'
    # 節点層: cu0, d0, cu1, d1, ... cu(n-1)
    for l in range(ncu):
        t = st['cu'][l] * 1e-3
        gcell = (phis[l] * S.K_CU + (1 - phis[l]) * S.K_FR4_XY) * t * g.board
        if inplane == 'edge' and 'edge_x_cu' in grid.a:   # 隣どうしは経路の銅の割合 (core/raster.edge_conduct) で。外形の割合は調和平均
            bf = g.board; hb = lambda a, b: 2 * a * b / np.maximum(a + b, 1e-30)
            fx, fy = grid.a['edge_x_cu'][l], grid.a['edge_y_cu'][l]
            gx = (fx * S.K_CU + (1 - fx) * S.K_FR4_XY) * t * hb(bf[:, :-1], bf[:, 1:])
            gy = (fy * S.K_CU + (1 - fy) * S.K_FR4_XY) * t * hb(bf[:-1, :], bf[1:, :])
            net.add_layer(gcell, gx=gx, gy=gy)
        else:
            net.add_layer(gcell)
        if l < ncu - 1:
            net.add_layer(np.full(g.board.shape, S.K_FR4_XY * st['diel'][l] * 1e-3) * g.board)
    # 厚さ方向 (FR4)
    for l in range(ncu - 1):
        hd = st['diel'][l] * 1e-3 / 2
        Gz = S.K_FR4_Z * A / hd
        net.zlinks += [Gz.copy(), Gz.copy()]
    # ビア (格子の接続リスト。artwork: 全 via + めっき穴、placement: めっき穴の pad のみ)
    tp = grid.meta['plating_mm'] * 1e-3   # core が決めた値 (案件 YAML > 基板 YAML process.via_plating > 既定 18 µm、grid.meta.plating_src)
    V = grid.vias; holes = list(zip(V['x_mm'], V['y_mm'], V['drill_mm']))
    for k in range(len(V['j'])):
        j, i = int(V['j'][k]), int(V['i'][k]); d = float(V['drill_mm'][k])
        if not (0 <= j < g.ny and 0 <= i < g.nx) or not active[j, i]:
            continue
        r = d * 1e-3 / 2; Ab = math.pi * ((r + tp) ** 2 - r ** 2)
        for l in range(int(V['l_from'][k]), int(V['l_to'][k])):
            Gv = S.K_CU * Ab / (st['diel'][l] * 1e-3 / 2)
            net.zlinks[2 * l][j, i] += Gv; net.zlinks[2 * l + 1][j, i] += Gv
    # 外部発熱マップ (ジュール熱など)
    p_ext = 0.0
    if heat_map is not None:
        for l in range(ncu):
            hm = np.where(active, heat_map[l], 0.0)
            net.layers[2 * l]['q'] = net.layers[2 * l]['q'] + hm; p_ext += float(hm.sum())
    net.area = A
    # 部品
    rows = []; Ptot = 0.0
    fps = {f['ref']: f for f in board['footprints']}
    last = 2 * (ncu - 1)
    PT.check_project(cfg)
    proj_dir = os.path.dirname(board['path'])
    MISSING3D = []
    for ref, spec in (cfg.get('parts') or {}).items():
        fp = fps.get(ref)
        if fp is None:
            raise PT.DataError(f'{ref} が基板に無い')
        P, pformula, pitems = PT.power_of(spec, lib); Ptot += P
        lay = 0 if fp['side'] == 'F.Cu' else last
        cu = 'F.Cu' if fp['side'] == 'F.Cu' else 'B.Cu'
        pd = lib.get(str(fp['lcsc'])) if fp['lcsc'] else None
        path = PT.thermal_path(pd) if pd else None
        hp = (pd or {}).get('heat_path', {}).get('pads', 'all') if pd else 'all'
        # 3D モデル (外形・高さ・上面)
        m3 = None
        for m in fp.get('models', []):
            if m['hide']:
                continue
            m3 = model3d.lookup(m['path'], proj_dir, m['scale'], m['offset'])
            if m3:
                m3['kicad_path'] = m['path']; break
        if m3 is None and fp.get('models'):
            MISSING3D.append(ref)
        # pad のセル重み
        wts = {}; padinfo = []
        for p in fp['pads']:
            if not raster.pad_on(p, cu) or (hp != 'all' and str(p['num']) not in [str(x) for x in hp]):
                continue
            sl, m = raster.pad_mask(g, p)
            m = m & g.fine_board[sl]
            padinfo.append((str(p['num']), p['shape'], p['w'], p['h'], float(m.sum()) * g.fs ** 2))
            jf, if_ = np.nonzero(m)
            jc = (jf + sl[0].start) // g.sub; ic = (if_ + sl[1].start) // g.sub
            for (jj, ii) in zip(jc, ic):
                wts[(jj, ii)] = wts.get((jj, ii), 0) + 1.0 / g.sub ** 2
        if not wts:
            raise PT.DataError(f'{ref}: 熱を流す pad が見つからない')
        tw = sum(wts.values())
        row = dict(ref=ref, value=fp['value'], lcsc=fp['lcsc'], mpn=(pd or {}).get('mpn', fp['mpn']), P=P, p_note=pformula,
                   p_items=pitems, p_src='部品表 parts_table.csv の電力の行', p_assumed=[], notes=spec.get('notes', []),
                   side=fp['side'], fp=fp, pd=pd, m3=m3, padinfo=padinfo, n_cells=len(wts), layer_name=cu, spec=spec,
                   no_thermal=bool(pd) and not PT.has_thermal(pd))
        if path:
            links = [(lay, jj * g.nx + ii, (1 / path['theta']) * w / tw) for (jj, ii), w in wts.items()]
            g_air = 0.0; gcalc = 'θJC(上) が無いので上面経路なし (全量を pad へ)'
            if path['jc_top']:
                atop = (m3 or {}).get('top_area') or 0
                if atop:
                    g_air = 1 / (path['jc_top'] + 1 / (H_BODY_TOP * atop * 1e-6))
                    gcalc = (f"G上 = 1 / (θJC上 + 1/(h × A上)) = 1 / ({path['jc_top']} + 1/({H_BODY_TOP} × {atop:.2f}e-6)) = "
                             f"1 / ({path['jc_top']} + {1 / (H_BODY_TOP * atop * 1e-6):.0f}) = {g_air * 1000:.3f} mW/K (= {1 / g_air:.0f} ℃/W)")
                else:
                    gcalc = '3D モデルが無く上面の面積が不明なので上面経路なし'
            net.junctions.append(dict(name=ref, P=P, links=links, g_air=g_air))
            lim = spec.get('tj_limit') or pd.get('tj_max')
            row.update(junction=len(net.junctions) - 1, path=path, tj_max=_v(lim), tj_lim_obj=lim, tj_max_src=PT._src(lim, pd.get('datasheet')),
                       data_file=pd['_file'], data_assumed=[], pads=[(lay, jj, ii) for (jj, ii) in wts], g_air=g_air, g_air_calc=gcalc)
        else:
            q = net.layers[lay]['q'] if net.layers[lay]['q'].any() else np.zeros(g.board.shape)
            for (jj, ii), w in wts.items():
                q[jj, ii] += P * w / tw
            net.layers[lay]['q'] = q
            lim = spec.get('tj_limit')
            row.update(junction=None, path=None, tj_max=_v(lim), tj_lim_obj=lim, pads=[(lay, jj, ii) for (jj, ii) in wts])
        rows.append(row)
    net.build()
    t_air, t_rad, eps_w = E.sinks(envc)
    size = ((g.nx * dx_mm) * 1e-3, (g.ny * dx_mm) * 1e-3)
    pts = np.array([p for poly in board['outline'] for p in poly]); ext = pts.max(0) - pts.min(0)
    size = (ext[0] * 1e-3, ext[1] * 1e-3)
    hlog = {}

    def surface_h(face, Tf):
        Tm = np.nanmean(Tf)
        hc = E.h_conv(envc, face, Tm, size)
        eps = float(envc['emissivity'][face]) * eps_w
        Tl = np.where(np.isnan(Tf), t_air, Tf)
        hr = E.h_rad(eps, Tl, t_rad)
        hlog[face] = dict(h_conv=hc, h_rad_mean=float(np.nanmean(np.where(np.isnan(Tf), np.nan, hr))), t_mean=float(Tm))
        return hc, hr
    so = cfg.get('solver') or {}
    if t_init is not None and np.ndim(t_init) == 3:   # 銅層の温度マップ (L,Ny,Nx、同じ格子) → 節点の初期値 (誘電体は上下の平均、接合は周囲)
        tc = np.where(np.isnan(t_init), t_air, t_init); v = np.full(net.N, float(np.nanmean(tc)))
        for l in range(len(net.layers)):
            F = tc[l // 2] if l % 2 == 0 else 0.5 * (tc[l // 2] + tc[l // 2 + 1])
            v[net.idx[l][net.active]] = F[net.active]
        t_init = v
    T, nit = net.solve(surface_h, t_air, t_rad, t0=t_init, method=so.get('method', 'auto'))
    # エネルギー収支
    out = 0.0
    for face, l in (('top', 0), ('bottom', last)):
        F = net.field(l); hc = hlog[face]['h_conv']
        hr = E.h_rad(float(envc['emissivity'][face]) * eps_w, np.nan_to_num(F, nan=t_air), t_rad)
        out += np.nansum((hc * (F - t_air) + hr * (F - t_rad)) * np.where(active, A, 0))
    for k, j in enumerate(net.junctions):
        out += j['g_air'] * (net.tj(k) - t_air)
    for r in rows:
        if r['junction'] is not None:
            r['tj'] = float(net.tj(r['junction']))
        lay = r['pads'][0][0]; F = net.field(lay)
        r['t_pad'] = float(np.nanmax([F[jj, ii] for (_, jj, ii) in r['pads']]))
        if r['junction'] is None:
            r['tj'] = None
        # パッケージ上面の温度 (触れるかの目安): 上面経路があれば T上 = Ta + q上 / (h A上)、q上 = G上 (Tj - Ta)。無ければ pad (タブ) の最高温度で代用
        if r.get('g_air'):
            q_top = r['g_air'] * (r['tj'] - t_air); A = r['m3']['top_area'] * 1e-6
            r['t_case_top'] = float(t_air + q_top / (H_BODY_TOP * A)); r['t_case_how'] = f'T上 = Ta + G上(Tj-Ta)/(h A上) = {t_air} + {q_top * 1000:.1f} mW / ({H_BODY_TOP} × {A * 1e6:.1f} mm²)'
        else:
            r['t_case_top'] = r['t_pad']; r['t_case_how'] = 'θJC(上) が無いので pad (タブ) の最高温度で代用 (上面はこれより数 ℃ 高い可能性)'
    info = dict(P_external=p_ext, mode=mode, grid_mm=dx_mm, nx=g.nx, ny=g.ny, unknowns=net.N, iterations=nit, P_total=Ptot,
                solver=net.solve_info['method'], inplane=info_inplane, solve_s=net.solve_info['solve_s'], solver_info=net.solve_info,
                P_out=float(out), balance_err=float(abs(out - Ptot - p_ext) / max(Ptot + p_ext, 1e-12)), h=hlog, stackup=st,
                env=envc, t_air=t_air, cu_fraction={ly: float((phis[i] * g.board).sum() / g.board.sum()) for i, ly in enumerate(board['cu_layers'])},
                n_holes=len(holes), model3d_missing=MISSING3D,
                model3d_source={r['ref']: (r['m3'] or {}).get('source') for r in rows})
    if MISSING3D:
        log(f'警告: 3D モデル (STEP) もキャッシュも見つからない部品 {MISSING3D} → 上面放熱なし (Tj が高めに出る)。KICAD10_3DMODEL_DIR を設定するか tools/model3d_cache.py でキャッシュに追加')
    fields = {'F': net.field(0), 'B': net.field(last)}
    temp_cu = np.array([net.field(2 * l) for l in range(ncu)])   # 交換形式の温度マップ (L,Ny,Nx) ℃
    return dict(pgrid=grid, temp_cu=temp_cu, grid=g, net=net, rows=rows, info=info, fields=fields)


def _v(x):
    return x.get('value') if isinstance(x, dict) else x
