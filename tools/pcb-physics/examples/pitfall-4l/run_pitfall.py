#!/usr/bin/env python3
"""pitfall-4l の熱の各区画を 1 つずつ解き、expected.yaml と同じ項目で表にする (results.md / results.json)。
  python examples/pitfall-4l/run_pitfall.py [--grid 0.25] [--grid-ir 0.1] [--out out/pitfall-4l] [--only ldo,dense,...]
細線 (TH-1A)・連成 (CP-1A5) のジュール熱は IR を解いて熱側の外部発熱にする (coupling.driver、温度は補間・発熱は保存)。
IR ソルバー: irdrop.solver.FdIrSolver (IR ドロップ担当、main に入っていれば) を使い、無ければこのファイルの SimpleIr (同じモデル: 同ネット辺 G = σ(T) t w、ビアは筒、
源の pad 全面を等電位、負荷は pad のセルへ面積重み、ジュール熱は辺の I²R を両端セルに半分ずつ)。"""
import argparse, glob, json, os, sys, time
import numpy as np, yaml
import scipy.sparse as sp, scipy.sparse.linalg as spla
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(HERE)); sys.path.insert(0, ROOT)
from core import pcbread, parts, cases as C, grid as CG, boardspec
from thermal import pcbmodel
from coupling import driver
from irdrop.interface import IrSolver, IrResult, rho_cu
PROJ = os.environ.get('PITFALL_4L_DIR', os.path.join(os.path.dirname(os.path.dirname(ROOT)), 'testboards', 'pitfall-4l'))


class SimpleIr(IrSolver):
    name = 'SimpleIr (run_pitfall.py、1 ネット直流)'

    def solve(self, grid, temp_c, sources, cond):
        A = grid.a; m = grid.meta; L, ny, nx = A['cu_frac'].shape
        heat = np.zeros((L, ny, nx)); volt = np.full((L, ny, nx), np.nan); drops = []
        T = np.full((L, ny, nx), 20.0) if temp_c is None else np.nan_to_num(np.asarray(temp_c, float), nan=20.0)
        rho = rho_cu(T)
        for net in sorted({s['net'] for s in sources}):
            ss = [s for s in sources if s['net'] == net]; k = grid.net_index(net)
            node = A['net_major'] == k; idx = -np.ones((L, ny, nx), np.int64); n = int(node.sum()); idx[node] = np.arange(n)
            R, Cc, G, E = [], [], [], []
            for l in range(L):
                t = m['layers'][l]['cu_mm'] * 1e-3
                for ax, w, en in (('x', A['edge_x_w'][l], A['edge_x_net'][l]), ('y', A['edge_y_w'][l], A['edge_y_net'][l])):
                    jj, ii = np.nonzero((en == k) & (w > 0)); j1, i1 = (jj, ii + 1) if ax == 'x' else (jj + 1, ii)
                    a, b = idx[l, jj, ii], idx[l, j1, i1]; ok = (a >= 0) & (b >= 0)
                    g = t * w[jj, ii][ok] / (0.5 * (rho[l, jj, ii][ok] + rho[l, j1, i1][ok]))
                    R.append(a[ok]); Cc.append(b[ok]); G.append(g)
            tp = m['plating_mm'] * 1e-3; diel = m['dielectric_mm']
            for v in range(len(A['via_j'])):
                if A['via_net'][v] != k:
                    continue
                j, i = A['via_j'][v], A['via_i'][v]; r = A['via_drill_mm'][v] * 0.5e-3
                for l in range(A['via_l_from'][v], A['via_l_to'][v]):
                    a, b = idx[l, j, i], idx[l + 1, j, i]
                    if a >= 0 and b >= 0:
                        h = diel[l] * 1e-3 + 0.5 * (m['layers'][l]['cu_mm'] + m['layers'][l + 1]['cu_mm']) * 1e-3
                        Rv = 0.5 * (rho[l, j, i] + rho[l + 1, j, i]) * h / (np.pi * ((r + tp) ** 2 - r ** 2))
                        R.append(np.array([a])); Cc.append(np.array([b])); G.append(np.array([1 / Rv]))
            R, Cc, G = map(np.concatenate, (R, Cc, G))
            Kd = sp.coo_matrix((np.r_[-G, -G], (np.r_[R, Cc], np.r_[Cc, R])), (n, n)).tocsr()
            Kd = (Kd + sp.diags(-np.asarray(Kd.sum(1)).ravel())).tocsr()

            def cells(ref, pin):
                out = []
                for p in np.nonzero((A['pad_ref'] == ref) & (A['pad_pin'] == str(pin)))[0]:
                    js, is_, w = grid.pad_cells(p); ids = idx[A['pad_layer'][p], js, is_]
                    out += [(a, ww) for a, ww in zip(ids, w) if a >= 0]
                return out
            src = [s for s in ss if 'v_set' in s][0]; fixed = np.array(sorted({a for a, _ in cells(src['ref'], src['pin'])}))
            q = np.zeros(n)
            for s in ss:
                if 'v_set' in s:
                    continue
                cw = cells(s['ref'], s['pin']); tw = sum(w for _, w in cw)
                for a, w in cw:
                    q[a] -= s['current_a'] * w / tw
            V = np.zeros(n); V[fixed] = src['v_set']; free = np.setdiff1d(np.arange(n), fixed)
            V[free] = spla.spsolve(Kd[free][:, free].tocsc(), q[free] - Kd[free][:, fixed] @ V[fixed])
            # ジュール熱: 辺の G ΔV² を両端に半分ずつ
            P = G * (V[R] - V[Cc]) ** 2; Pn = np.bincount(R, P / 2, n) + np.bincount(Cc, P / 2, n)
            heat[node] += Pn; volt[node] = V
            for s in ss:
                if 'v_set' in s:
                    continue
                cw = cells(s['ref'], s['pin']); tw = sum(w for _, w in cw)
                vs = sum(V[a] * w for a, w in cw) / tw
                drops.append(dict(net=net, **{'from': f"{src['ref']}.{src['pin']}"}, to=f"{s['ref']}.{s['pin']}", drop_v=src['v_set'] - vs, current_a=s['current_a']))
        return IrResult(heat_w=heat, volt_v=volt, drops=drops, info={})


def ir_solver():
    try:
        from irdrop.solver import FdIrSolver
        return FdIrSolver()
    except ImportError:
        return SimpleIr()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--grid', type=float, default=0.25); ap.add_argument('--grid-ir', type=float, default=0.1)
    ap.add_argument('--out', default=os.path.join(ROOT, 'out', 'pitfall-4l')); ap.add_argument('--only', default=None); ap.add_argument('--solver', default=None)
    ap.add_argument('--proj', default=PROJ); ap.add_argument('--case', default='worst', choices=['typ', 'worst'])
    a = ap.parse_args()
    cfg0 = yaml.safe_load(open(os.path.join(HERE, 'thermal.yaml'), encoding='utf-8')); cfg0['_cond_file'] = 'thermal.yaml'
    if a.solver:
        cfg0['solver'] = {'method': a.solver}
    C.check_typmax(cfg0)
    bs = boardspec.load(os.path.join(HERE, cfg0['board'])); cfg0['_board_spec'] = bs
    board = pcbread.read_pcb(glob.glob(os.path.join(a.proj, '*.kicad_pcb'))[0]); lib = parts.load_library(os.path.join(ROOT, 'data', 'parts'))
    os.makedirs(a.out, exist_ok=True)
    R = {}; amb = 25.0
    for name, sc in cfg0['scenarios'].items():
        if a.only and name not in a.only.split(','):
            continue
        cfg = {k: v for k, v in cfg0.items() if k not in ('scenarios', 'parts')}
        cfg['parts'] = {r: cfg0['parts'][r] for r in sc['parts']}; cfg['_board_spec'] = bs
        t0 = time.time()
        if not sc.get('joule'):
            cc = C.resolve(cfg, a.case); cc['_cond_file'] = 'thermal.yaml'; cc['_board_spec'] = bs
            res = pcbmodel.build_and_solve(board, cc, C.resolve_lib(lib, a.case), 'artwork', a.grid, log=lambda *x: None)
            g = res['pgrid']; ln = g.meta['layer_names']; fps = {f['ref']: f for f in board['footprints']}
            def under(ref):   # 部品中心の直下の各層の温度 (L2 GND = In1.Cu が expected の「基板温度」のフィン近似に相当)
                f = fps[ref]; i = int((f['x'] - g.meta['x0_mm']) / g.meta['dx_mm']); jj = int((f['y'] - g.meta['y0_mm']) / g.meta['dx_mm'])
                return {n: float(res['temp_cu'][l, jj, i]) for l, n in enumerate(ln)}
            rows = {r['ref']: dict(P=r['P'], tj=r['tj'], t_pad=r['t_pad'], under=under(r['ref'])) for r in res['rows']}
            R[name] = dict(rows=rows, max_board=float(np.nanmax(res['temp_cu'])), s=round(time.time() - t0, 1), solver=res['info']['solver'])
        else:
            j = sc['joule']; ir = ir_solver(); hist = []
            src = [dict(ref=j['source'][0], pin=str(j['source'][1]), net=j['net'], v_set=0.0),
                   dict(ref=j['sink'][0], pin=str(j['sink'][1]), net=j['net'], current_a=float(j['current_a']))]

            def rec(it, r, res, gir, gth):
                d = r.drops[0]; Tn = res['temp_cu']; msk = gth.net_mask(j['net'], 'F.Cu')
                hist.append(dict(k=it, R_mohm=d['drop_v'] / j['current_a'] * 1e3, V_mV=d['drop_v'] * 1e3, P_w=float(r.heat_w.sum()),
                                 T_max_c=float(np.nanmax(Tn[0][msk])), T_mean_c=float(np.nanmean(Tn[0][msk]))))
            res, h2 = driver.run(board, cfg, lib, a.case, 'artwork', a.grid, ir=ir, tol=0.1, maxit=10 if sc.get('coupled') or True else 1,
                                 dx_ir=a.grid_ir, board_spec=bs, t_start=20.0, sources=src, on_iter=rec, log=lambda *x: None)
            R[name] = dict(history=hist, ir=ir.name, s=round(time.time() - t0, 1), iterations=len(hist))
        print(name, json.dumps(R[name], ensure_ascii=False, default=float)[:600], flush=True)
    json.dump(dict(case=a.case, proj=a.proj, grid_mm=a.grid, grid_ir_mm=a.grid_ir, results=R), open(os.path.join(a.out, 'results.json'), 'w'), indent=1, ensure_ascii=False, default=float)


if __name__ == '__main__':
    main()
