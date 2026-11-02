"""連成ドライバ: 温度マップ → IR (銅抵抗を温度で補正) → ジュール発熱マップ → 熱解析 を収束まで反復。
  python -m coupling.driver <project_dir> --cond thermal.yaml [--case worst] [--mode artwork] [--grid 0.25] [--grid-ir 0.1] [--tol 0.1] [--maxit 10] [--out DIR]
熱と IR は別の格子でよい (温度は補間・発熱は保存、core.regrid)。IR は IrSolver (irdrop.interface) を渡す。既定は irdrop.stub (発熱 0)。
一本化の方針 (2026-10-14): 連成の本体は IR 側 irdrop/couple.py に寄せる。core はこの run() の部品 (regrid・board_spec・t_init・収束判定) を提供する。"""
import argparse, glob, json, os, sys, time
import numpy as np
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pcbread, parts, cases as C, grid as CG
from thermal import pcbmodel, report


def pin_sources(board, cfg):
    """thermal.yaml の parts.<ref>.pins.<pin>.current (cases で typ/max 選択済み) → IR の sources"""
    fps = {f['ref']: f for f in board['footprints']}; S = []
    for ref, sp in (cfg.get('parts') or {}).items():
        for pn, pv in ((sp or {}).get('pins') or {}).items():
            c = (pv or {}).get('current')
            if not c or ref not in fps:
                continue
            for p in fps[ref]['pads']:
                if str(p['num']) == str(pn):
                    S.append(dict(ref=ref, pin=str(pn), net=p.get('net', ''), x_mm=p['x'], y_mm=p['y'], layer=fps[ref]['side'],
                                  current_a=float(c['value'])))
    return S


def _drop_map(r):
    return {(d.get('net'), str(d.get('from')), str(d.get('to'))): float(d['drop_v']) for d in (r.drops or [])}


def run(board, cfg, lib, case='worst', mode='artwork', dx=0.25, ir=None, tol=0.1, maxit=10, out=None, log=print,
        dx_ir=None, board_spec=None, tol_drop=0.001, t_start=None, sources=None, on_iter=None):
    """熱 (格子 dx) と IR (格子 dx_ir、省略時 dx) を、温度は補間・発熱は保存で受け渡して反復 (core.regrid)。
    sources: IR の sources を直接渡す (省略時 thermal.yaml の pins.current)。on_iter(it, IrResult, 熱の結果, IR 格子, 熱格子): 反復ごとの記録用。
    board_spec: 基板 YAML (core.boardspec、省略時 cfg['_board_spec'])。t_start: 最初の IR の銅温度 (省略時 そのケースの周囲温度)。
    収束: 銅の最大温度変化 < tol [℃] かつ 降下の最大相対変化 < tol_drop (IR が drops を返す時)。熱は前回の温度から解き始める"""
    from core import regrid
    from irdrop.stub import StubIrSolver
    ir = ir or StubIrSolver()
    cc = C.resolve(cfg, case); lc = C.resolve_lib(lib, case)
    bs = board_spec if board_spec is not None else cfg.get('_board_spec'); cc['_board_spec'] = bs
    gth, _ = CG.build(board, mode, dx, 4, cfg.get('stackup'), board_spec=bs)
    gir = gth if not dx_ir or abs(dx_ir - dx) < 1e-12 else CG.build(board, mode, dx_ir, 4, cfg.get('stackup'), board_spec=bs)[0]
    same = gir is gth
    src = sources if sources is not None else pin_sources(board, cc)
    amb = float((cc.get('environment') or {}).get('ambient_c', 25.0))
    T_ir = np.full(gir.cu_frac.shape, amb if t_start is None else float(t_start)); T_th = None; hist = []; prev_drops = None
    cond = dict(case=case, ref_temp_c=20.0, ambient_c=amb)
    mask_th = gth.board_frac > 0
    for it in range(1, maxit + 1):
        t0 = time.time()
        r = ir.solve(gir, T_ir, src, cond)
        heat = r.heat_w if same else regrid.heat(r.heat_w, gir.meta, gth.meta, mask_th)
        res = pcbmodel.build_and_solve(board, cc, lc, mode, dx, grid=gth, heat_map=heat, t_init=T_th)
        Tn = res['temp_cu']
        dT = float(np.nanmax(np.abs(Tn - T_th))) if T_th is not None else float('inf')
        dm = _drop_map(r)
        dd = max((abs(v - prev_drops.get(k, 0.0)) / max(abs(v), 1e-12) for k, v in dm.items()), default=0.0) if prev_drops is not None else (float('inf') if dm else 0.0)
        hist.append(dict(iter=it, joule_w=float(heat.sum()), max_temp_c=float(np.nanmax(Tn)), max_dT_c=dT, max_drop_rel_change=dd,
                         solver=res['info'].get('solver'), s=round(time.time() - t0, 1)))
        log(f"連成 {it}: ジュール熱 {heat.sum() * 1000:.2f} mW、最高 {np.nanmax(Tn):.2f} ℃、変化 {dT:.3f} ℃、降下の変化 {dd * 100:.3f}% ({ir.name})")
        if on_iter:
            on_iter(it, r, res, gir, gth)
        T_th = Tn; prev_drops = dm
        T_ir = Tn if same else regrid.temperature(Tn, gth.meta, gir.meta)
        if (dT < tol and dd < tol_drop) or (it >= 2 and heat.sum() == 0):
            break
    if out:
        os.makedirs(out, exist_ok=True)
        gth.save(os.path.join(out, f'grid_{mode}.npz'))
        CG.save_map(os.path.join(out, f'tmap_{case}_{mode}.npz'), gth.meta, 'temperature', T_th, '℃', dict(case=case, coupled=True))
        CG.save_map(os.path.join(out, f'heat_{case}_{mode}.npz'), gth.meta, 'heat', heat, 'W', dict(case=case, source=ir.name))
        json.dump(dict(case=case, mode=mode, ir=ir.name, dx_thermal=dx, dx_ir=dx_ir or dx, history=hist), open(os.path.join(out, f'coupling_{case}_{mode}.json'), 'w'), indent=1, ensure_ascii=False)
    return res, hist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project'); ap.add_argument('--cond', required=True); ap.add_argument('--case', default='worst')
    ap.add_argument('--mode', default='artwork'); ap.add_argument('--grid', type=float, default=None); ap.add_argument('--grid-ir', type=float, default=None)
    ap.add_argument('--tol', type=float, default=0.1); ap.add_argument('--maxit', type=int, default=10); ap.add_argument('--out')
    ap.add_argument('--lib', default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'parts'))
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.cond, encoding='utf-8')); C.check_typmax(cfg)
    pcb = [p for p in glob.glob(os.path.join(a.project, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
    board = pcbread.read_pcb(pcb); lib = parts.load_library(a.lib)
    from core import boardspec
    bp = os.path.join(os.path.dirname(os.path.abspath(a.cond)), cfg['board']) if cfg.get('board') else boardspec.find(cfg.get('project', ''), os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'board'))
    res, hist = run(board, cfg, lib, a.case, a.mode, a.grid or cfg.get('grid_mm', 0.25), tol=a.tol, maxit=a.maxit, out=a.out,
                    dx_ir=a.grid_ir, board_spec=boardspec.load(bp) if bp else None)
    report.judge(res['rows'], res['info']['t_air'])
    for r in res['rows']:
        print(f"  {r['ref']}: Tj {r['tj'] if r['tj'] is not None else r['t_pad']:.2f} ℃")


if __name__ == '__main__':
    main()
