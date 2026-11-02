"""熱 ⇄ IR ドロップ連成 (本物の IrSolver = irdrop.run.run_case を使う)。coupling/driver.py は IR がスタブ前提で
電圧源・GND・dx・基板 YAML を扱えないため、同じ流れ (温度 → IR → ジュール熱 → thermal.pcbmodel.build_and_solve) をここで回す。
熱と IR の格子が違うときは core/regrid (温度は補間・発熱は保存) で受け渡し、熱は前回の温度から解き始める (t_init)。既定は熱も IR と同じ 0.1 mm (AMG-CG)。
  python -m irdrop.couple <project> --thermal examples/<案件>/thermal.yaml --cond irdrop/examples/<案件>/irdrop.yaml --out DIR [--dx 0.1]
収束: 銅の最大温度変化 < --tol-t ℃ かつ 各系統の最大降下の変化 < --tol-v (相対)、上限 --maxit。"""
import gc
import argparse, copy, glob, json, os, sys, time
import numpy as np
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pcbread, parts as CP, cases as C, grid as CG, boardspec
from thermal import pcbmodel
from irdrop import run as IR, plating as PL

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def maxdrops(R):
    out = {}
    for r in R['rows']:
        if r['ratio'] is None:
            continue
        if r['net'] not in out or r['ratio'] > out[r['net']]['ratio']:
            out[r['net']] = dict(pin=f"{r['ref']}.{r['pin']}", drop_mv=r['drop_mv'], ratio=r['ratio'])
    return out


def tj_rows(res):
    out = {}
    for r in res['rows']:
        v = r.get('tj') if r.get('tj') is not None else r.get('t_pad')
        if v is not None:
            out[r['ref']] = round(float(v), 3)
    return out


class TGrid:
    """熱の格子と IR の格子の受け渡し (core/regrid.py: 温度は双一次補間、発熱は面積の重なりで保存)。dx が同じなら同じ格子をそのまま使う"""
    def __init__(self, board, tcfg, grid_ir, dx_t):
        from core import regrid
        self.R = regrid; self.gi = grid_ir
        self.same = abs(dx_t - grid_ir.meta['dx_mm']) < 1e-12
        if self.same:
            self.g = grid_ir
        else:
            self.g, _ = CG.build(board, 'artwork', dx_t, 4, tcfg.get('stackup'), board_spec=tcfg.get('_board_spec'), checks=False)
            self.g.meta['plating_mm'] = grid_ir.meta.get('plating_mm', self.g.meta.get('plating_mm'))
        self.mask_t = self.g.board_frac > 0

    def heat_to_t(self, h):
        return h if self.same else self.R.heat(h, self.gi.meta, self.g.meta, self.mask_t)

    def temp_to_ir(self, T):
        return T if self.same else self.R.temperature(T, self.g.meta, self.gi.meta)


def _ckpt(out, case, name):
    return os.path.join(out, 'progress', f'{case}_{name}')


def couple_case(board, grid, tg, tcfg, icfg, lib, case, dx, tol_t, tol_v, maxit, log=print, out=None):
    """途中経過: out/progress/<case>_hist.jsonl (反復ごと)、<case>_T0.npz / <case>_T.npz (温度)、<case>_tj0.json。再実行時は続きから"""
    cc = C.resolve(tcfg, case); lc = C.resolve_lib(lib, case)
    os.makedirs(os.path.join(out, 'progress'), exist_ok=True)
    hist = []; t0 = time.time()
    f0 = _ckpt(out, case, 'T0.npz'); fT = _ckpt(out, case, 'T.npz'); fh = _ckpt(out, case, 'hist.jsonl'); fj = _ckpt(out, case, 'tj0.json')
    if os.path.exists(f0) and os.path.exists(fj):
        T0 = CG.load_map(f0, tg.g.meta, 'temperature')[0]; th0 = dict(temp_cu=T0, rows=json.load(open(fj)))
        log(f"[{case}] 熱 (銅発熱なし): 途中経過から読み込み 銅最高 {np.nanmax(T0):.2f} ℃")
    else:
        th0 = pcbmodel.build_and_solve(board, cc, lc, 'artwork', tg.g.meta['dx_mm'], grid=tg.g, heat_map=None, log=lambda *a: None); T0 = th0['temp_cu']
        CG.save_map(f0, tg.g.meta, 'temperature', T0, '℃', dict(case=case, joule=False))
        json.dump([dict(ref=r['ref'], tj=r.get('tj'), t_pad=r.get('t_pad')) for r in th0['rows']], open(fj, 'w'))
        log(f"[{case}] 熱 (銅発熱なし): 銅最高 {np.nanmax(T0):.2f} ℃、{time.time() - t0:.0f} s")
    Ru = IR.run_case(board, grid, icfg, lib, case, None, log=lambda *a: None)     # 一様温度 (irdrop.yaml temperature)
    T = T0; prev = None; res = th0; R = None; start = 1
    if os.path.exists(fh) and os.path.exists(fT):
        hist = [json.loads(x) for x in open(fh) if x.strip()]
        T = CG.load_map(fT, tg.g.meta, 'temperature')[0]; start = len(hist) + 1
        prev = {n: dict(drop_mv=v) for n, v in hist[-1]['drops'].items()} if hist else None
        log(f"[{case}] 途中経過から再開: 反復 {len(hist)} 回済み")
        if hist and hist[-1]['max_dT_c'] < tol_t and (hist[-1]['max_drop_change'] if hist[-1]['max_drop_change'] is not None else 1) < tol_v:
            start = maxit + 1   # 収束済み: 最後の温度で IR だけ解き直す
    for it in range(start, maxit + 1):
        t1 = time.time()
        R = res = None; gc.collect()   # 前の反復の IR・熱の結果を先に手放す (同時に 2 組持つと 4 層 0.2 mm で OOM、2026-10-14)
        R = IR.run_case(board, grid, icfg, lib, case, tg.temp_to_ir(T), log=lambda *a: None)
        heat = sum(r['heat'] for r in R['res'].values()); md = maxdrops(R); R = None; gc.collect()
        res = pcbmodel.build_and_solve(board, cc, lc, 'artwork', tg.g.meta['dx_mm'], grid=tg.g, heat_map=tg.heat_to_t(heat), t_init=T, log=lambda *a: None)
        Tn = res['temp_cu']
        dT = float(np.nanmax(np.abs(Tn - T)))
        dV = max((abs(md[n]['drop_mv'] / prev[n]['drop_mv'] - 1) for n in md if prev and n in prev and prev[n]['drop_mv']), default=float('inf'))
        cu = np.where(tg.g.a['net_major'] > 0, Tn, np.nan)
        hist.append(dict(iter=it, joule_mw=round(float(heat.sum()) * 1e3, 4), cu_max_c=round(float(np.nanmax(cu)), 3), max_dT_c=round(dT, 4),
                         max_drop_change=None if dV == float('inf') else round(dV, 6), drops={n: v['drop_mv'] for n, v in md.items()}, s=round(time.time() - t1, 1)))
        log(f"[{case}] 反復 {it}: ジュール {heat.sum() * 1e3:.3f} mW、銅最高 {np.nanmax(cu):.2f} ℃、ΔT {dT:.4f} ℃、Δ降下 {dV * 100 if dV != float('inf') else float('nan'):.4f}%、"
            + ', '.join(f"{n} {v['drop_mv']:.2f}" for n, v in md.items()) + f" mV ({time.time() - t1:.0f} s)")
        T = Tn; prev = md
        CG.save_map(fT, tg.g.meta, 'temperature', T, '℃', dict(case=case, iter=it))
        CG.save_map(_ckpt(out, case, 'heat.npz'), grid.meta, 'heat', heat, 'W', dict(case=case, iter=it))
        open(fh, 'a').write(json.dumps(hist[-1], ensure_ascii=False) + '\n')
        if dT < tol_t and dV < tol_v:
            break
    # 最終温度での IR (表・PNG 用) と、熱の行 (Tj) が無ければ最後の熱を解き直さずに読み込んだ発熱で解く
    R = IR.run_case(board, grid, icfg, lib, case, tg.temp_to_ir(T), log=lambda *a: None)
    heat = sum(r['heat'] for r in R['res'].values())
    if res is None or 'pgrid' not in res:
        res = pcbmodel.build_and_solve(board, cc, lc, 'artwork', tg.g.meta['dx_mm'], grid=tg.g, heat_map=tg.heat_to_t(heat), t_init=T, log=lambda *a: None)
    conv = bool(hist and hist[-1]['max_dT_c'] < tol_t and (hist[-1]['max_drop_change'] if hist[-1]['max_drop_change'] is not None else 1) < tol_v)
    return dict(case=case, hist=hist, converged=conv, R_uniform=Ru, R=R, th0=th0, th=res, T=tg.temp_to_ir(T), T0=tg.temp_to_ir(T0), heat=heat)


def summary_text(summary):
    """最後の要約表示。部品発熱が無い条件 (tj_delta が空) でも落ちない (2026-10-14 修正、tests/test_irdrop_couple.py)"""
    return json.dumps({c: dict(it=v['iterations'], conv=v['converged'], cu=v['cu_max_c'], joule=v['joule_mw'], tjd=max(v['tj_delta'].values(), default=None),
                               drops={n: (d['uniform'] or {}).get('drop_mv', None) for n, d in v['drops'].items()},
                               drops_c={n: (d['coupled'] or {}).get('drop_mv', None) for n, d in v['drops'].items()}) for c, v in summary['cases'].items()},
                      ensure_ascii=False, indent=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project'); ap.add_argument('--thermal', required=True); ap.add_argument('--cond', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--dx', type=float, default=0.1); ap.add_argument('--thermal-dx', type=float, default=None, help='熱の格子 mm (既定 = --dx。違えば core/regrid で受け渡し)'); ap.add_argument('--tol-t', type=float, default=0.1); ap.add_argument('--tol-v', type=float, default=0.001)
    ap.add_argument('--maxit', type=int, default=10); ap.add_argument('--cases', default='typ,worst')
    a = ap.parse_args()
    pcb = [p for p in glob.glob(os.path.join(a.project, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
    board = pcbread.read_pcb(pcb)
    tcfg = yaml.safe_load(open(a.thermal, encoding='utf-8')); C.check_typmax(tcfg)
    bp = os.path.join(os.path.dirname(os.path.abspath(a.thermal)), tcfg['board']) if tcfg.get('board') else boardspec.find(tcfg.get('project', ''), os.path.join(HERE, 'data', 'board'))
    tcfg['_board_spec'] = boardspec.load(bp) if bp else None
    icfg = yaml.safe_load(open(a.cond, encoding='utf-8')); CP.walk(icfg, 'irdrop.yaml', CP.check_value)
    lib = CP.load_library(os.path.join(HERE, 'data', 'parts'))
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    grid, st = CG.build(board, 'artwork', a.dx, 4, tcfg.get('stackup'), board_spec=tcfg['_board_spec'])
    PL.apply(grid, icfg, tcfg['_board_spec'], log=print)
    print(f"格子 {grid.shape} dx {a.dx} mm、銅厚 {[l['cu_mm'] for l in grid.meta['layers']]} ({grid.meta['stackup_src']})、基板 YAML {bp}、{time.time() - t0:.0f} s")
    for w in grid.meta.get('warnings', []):
        print('  警告:', w)
    tdx = a.thermal_dx or a.dx
    tg = TGrid(board, tcfg, grid, tdx)
    print(f"熱の格子 dx {tg.g.meta['dx_mm']:.2f} mm {tg.g.cu_frac.shape}{'' if tg.same else ' (core/regrid で受け渡し)'}、熱の解法 {((tcfg.get('solver') or {}).get('method') or 'auto')}、熱の面内伝導 {((tcfg.get('solver') or {}).get('inplane') or 'edge (既定)')}")
    S = {}; pngs = []; summary = dict(grid=dict(dx_mm=a.dx, thermal_dx_mm=tdx, plating_mm=grid.meta['plating_mm'], plating_src=grid.meta.get('plating_src'), cu_mm=[l['cu_mm'] for l in grid.meta['layers']], stackup_src=grid.meta['stackup_src'],
                                               board_yaml=bp, warnings=grid.meta.get('warnings', [])), cases={})
    for case in a.cases.split(','):
        r = couple_case(board, grid, tg, tcfg, icfg, lib, case, a.dx, a.tol_t, a.tol_v, a.maxit, out=a.out); S[case] = r
        mu, mc = maxdrops(r['R_uniform']), maxdrops(r['R'])
        cu0 = np.where(grid.a['net_major'] > 0, r['T0'], np.nan); cu1 = np.where(grid.a['net_major'] > 0, r['T'], np.nan)
        tj0, tj1 = tj_rows(r['th0']), tj_rows(r['th'])
        summary['cases'][case] = dict(converged=r['converged'], iterations=len(r['hist']), history=r['hist'],
            uniform_temp_c=r['R_uniform']['temp_c'],
            drops={n: dict(uniform=mu.get(n), coupled=mc.get(n), lim=icfg['criterion']['ratio'],
                           grade_uniform=None if n not in mu else ('pass' if mu[n]['ratio'] <= icfg['criterion']['ratio'] else 'fail'),
                           grade_coupled=None if n not in mc else ('pass' if mc[n]['ratio'] <= icfg['criterion']['ratio'] else 'fail')) for n in icfg['rails']},
            cu_max_c=dict(no_joule=round(float(np.nanmax(cu0)), 3), coupled=round(float(np.nanmax(cu1)), 3)),
            cu_temp_on_net={n: dict(max_c=round(float(np.nanmax(np.where(grid.a['net_major'] == grid.net_index(n), r['T'], np.nan))), 2),
                                    min_c=round(float(np.nanmin(np.where(grid.a['net_major'] == grid.net_index(n), r['T'], np.nan))), 2)) for n in list(icfg['rails']) + ['GND']},
            joule_mw=r['hist'][-1]['joule_mw'], series=r['R'].get('series'), series_uniform=r['R_uniform'].get('series'),
            tj_no_joule=tj0, tj_coupled=tj1, tj_delta={k: round(tj1[k] - tj0[k], 4) for k in tj0 if k in tj1},
            max_temp_delta_c=round(float(np.nanmax(r['T'] - r['T0'])), 4),
            thermal_inplane=((r['th'] or {}).get('info') or {}).get('inplane'))   # 熱の面内伝導 (edge = main f5c2ded〜の既定 / cell = 旧方式)。thermal.yaml solver.inplane で選ぶ
        pngs += IR.plot(grid, board, dict(r['R'], case=f"{case}"), a.out, [n for n in icfg['rails'] if 'series' not in icfg['rails'][n]] + ['GND'])
        CG.save_map(os.path.join(a.out, f'tmap_{case}_coupled.npz'), grid.meta, 'temperature', r['T'], '℃', dict(case=case, coupled=True))
        CG.save_map(os.path.join(a.out, f'heat_{case}_coupled.npz'), grid.meta, 'heat', r['heat'], 'W', dict(case=case, source='irdrop'))
        # 銅の温度マップ PNG
        import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
        m = grid.meta; ext = [m['x0_mm'], m['x0_mm'] + m['nx'] * a.dx, m['y0_mm'] + m['ny'] * a.dx, m['y0_mm']]
        plt.rcParams['font.family'] = 'Noto Serif CJK JP'
        for l, ly in enumerate(m['layer_names']):
            fig, ax = plt.subplots(figsize=(8, 6.6)); im = ax.imshow(r['T'][l], extent=ext, cmap='inferno', interpolation='nearest')
            fig.colorbar(im, ax=ax, shrink=0.8).set_label('温度 [℃] (連成後)')
            ax.set_title(f"銅層温度 {ly} {C.NAME[case]} 連成 {len(r['hist'])} 回 最高 {np.nanmax(r['T'][l]):.2f} ℃", fontsize=10)
            fn = os.path.join(a.out, f"temp_{case}_coupled_{'F' if ly == 'F.Cu' else 'B'}.png"); fig.tight_layout(); fig.savefig(fn, dpi=130); plt.close(fig); pngs.append(fn)
        json.dump(dict(case=case, rows=r['R']['rows'], rows_uniform=r['R_uniform']['rows']), open(os.path.join(a.out, f'rows_{case}.json'), 'w'), indent=1, ensure_ascii=False)
    summary['pngs'] = pngs
    # kreview 形式 (worst の連成結果で判定)
    if 'worst' in S:
        M = IR.metrics(S['worst']['R'], S.get('typ', S['worst'])['R'], grid, icfg['criterion']['ratio'], list(icfg['rails']))
        sg = IR.stage_grade(M)
        json.dump(dict(tool='irdrop', version=IR.VERSION + ' coupled', project=os.path.splitext(os.path.basename(pcb))[0], out=os.path.abspath(a.out),
                       overall=dict(grade=sg['grade'], score=sg['score'], fail=sg['counts']['fail'], warn=sg['counts']['warn']),
                       stages=dict(irdrop=dict(title='IR ドロップ (熱連成)', **sg)), metrics=M), open(os.path.join(a.out, 'irdrop.json'), 'w'), indent=1, ensure_ascii=False, default=str)
    json.dump(summary, open(os.path.join(a.out, 'coupling.json'), 'w'), indent=1, ensure_ascii=False, default=str)
    print(summary_text(summary))


if __name__ == '__main__':
    main()
