#!/usr/bin/env python3
"""kthermal — KiCad 基板の熱解析 (2.5D 有限差分)。
  python kthermal.py <project_dir> --cond thermal.yaml [--mode placement|artwork|both] [--grid 0.25] [--out DIR]
  python kthermal.py --selftest
プロジェクトは読むだけ。出力は --out (既定 ./out/<project>)。"""
import argparse, os, sys, glob, time
import yaml
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import pcbread, parts, cases as C, grid as CG
from thermal import pcbmodel, report

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project', nargs='?'); ap.add_argument('--cond'); ap.add_argument('--mode', default='both')
    ap.add_argument('--grid', type=float, default=None); ap.add_argument('--out'); ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--lib', default=os.path.join(HERE, 'data', 'parts'))
    ap.add_argument('--inplane', choices=['edge', 'cell'], help='面内の熱伝導: edge = セル中心間の経路の銅の割合 (既定、サーマルリリーフをスポーク幅で扱う) / cell = 旧方式 (セルの銅の面積率の調和平均)')
    ap.add_argument('--solver', choices=['auto', 'direct', 'amg', 'amg-gs', 'cg', 'gpu', 'gpu-emu'], help='熱の線形ソルバー (既定 auto = AMG 前処理付き CG、thermal.yaml の solver.method より優先)')
    ap.add_argument('--heat', help='外部発熱マップ npz (core.grid の交換形式、kind=heat、同じ格子)。--mode を 1 つにして使う')
    a = ap.parse_args()
    if a.selftest:
        import subprocess
        return max([subprocess.call([sys.executable, os.path.join(HERE, "tests", t)]) for t in ("test_analytic.py", "test_data.py", "test_grid.py", "test_solver.py")])
    cfg = yaml.safe_load(open(a.cond, encoding='utf-8'))
    cfg['_cond_file'] = os.path.basename(a.cond)
    if a.solver:
        cfg['solver'] = dict(cfg.get('solver') or {}, method=a.solver)
    if a.inplane:
        cfg['solver'] = dict(cfg.get('solver') or {}, inplane=a.inplane)
    pcb = glob.glob(os.path.join(a.project, '*.kicad_pcb'))
    pcb = [p for p in pcb if not os.path.basename(p).startswith('_')][0]
    board = pcbread.read_pcb(pcb)
    lib = parts.load_library(a.lib)
    from core import boardspec   # 基板の製造条件: thermal.yaml の board: (cond からの相対) > data/board/<project>*.yaml > なし
    bp = os.path.join(os.path.dirname(os.path.abspath(a.cond)), cfg['board']) if cfg.get('board') else boardspec.find(cfg.get('project', ''), os.path.join(HERE, 'data', 'board'))
    cfg['_board_spec'] = boardspec.load(bp) if bp else None
    print('基板の製造条件:', bp or 'なし (.kicad_pcb の stackup → JLC 標準)')
    dx = a.grid or cfg.get('grid_mm', 0.25)
    out = a.out or os.path.join('out', cfg.get('project', 'board'))
    modes = ['placement', 'artwork'] if a.mode == 'both' else [a.mode]
    import json, numpy as np
    C.check_typmax(cfg)
    R = {}
    os.makedirs(out, exist_ok=True)
    grids = {}
    for m in modes:   # 格子は モードごとに 1 回 (typ / worst で共通)
        grids[m], _ = CG.build(board, m, dx, 4, cfg.get('stackup'), board_spec=cfg['_board_spec'])
        for w in grids[m].meta.get('stackup_warnings', []):
            print('  警告:', w)
        grids[m].save(os.path.join(out, f'grid_{m}.npz'))
    for case in C.CASES:
        cc = C.resolve(cfg, case); cc['_cond_file'] = cfg['_cond_file']; lc = C.resolve_lib(lib, case)
        for m in modes:
            t = time.time()
            hm = CG.load_map(a.heat, grids[m].meta, 'heat')[0] if a.heat else None
            res = pcbmodel.build_and_solve(board, cc, lc, m, dx, grid=grids[m], heat_map=hm)
            CG.save_map(os.path.join(out, f'tmap_{case}_{m}.npz'), grids[m].meta, 'temperature', res['temp_cu'], '℃', dict(case=case))
            report.judge(res['rows'], res['info']['t_air'])
            res['info']['elapsed_s'] = time.time() - t
            print(f"{case}/{m}: 未知数 {res['info']['unknowns']}、反復 {res['info']['iterations']}、{res['info']['elapsed_s']:.1f} s、熱収支誤差 {res['info']['balance_err'] * 100:.3f}%")
            vr = (min(float(np.nanmin(res['fields'][f])) for f in 'FB'), max(float(np.nanmax(res['fields'][f])) for f in 'FB'))
            js = report.write(res, board, cc, out, m, vr, case)
            R[(case, m)] = (js, res)
            for mt in js['metrics']:
                print(f"  {mt['id']} {mt['grade']:5s} {mt['title']}: {mt['value']} ℃")
    for m in modes:
        jw = R[('worst', m)][0]; jt = R[('typ', m)][0]
        J = dict(jw); J['case'] = 'worst (合否) + typ (参考)'
        J['metrics'] = jw['metrics'] + jt['metrics']
        J['cases'] = {c: dict(conditions=R[(c, m)][0]['conditions'], max_board_temp=R[(c, m)][0]['max_board_temp'], png=R[(c, m)][0]['png'],
                              solver=R[(c, m)][0]['solver']) for c in C.CASES}
        J['parts_table'] = 'parts_table.csv'
        json.dump(J, open(os.path.join(out, f'thermal_{m}.json'), 'w'), indent=1, ensure_ascii=False, default=float)
    report.md(R, cfg, out)   # thermal.md と parts_table.csv は --mode を 1 つにした時も必ず出す (2026-10-19 まで両モードの時だけだった)
    print('出力:', out)
    return 1 if any(R[('worst', m)][0]['overall']['fail'] for m in modes) else 0


if __name__ == '__main__':
    sys.exit(main())
