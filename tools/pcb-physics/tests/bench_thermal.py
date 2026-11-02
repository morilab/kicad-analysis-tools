#!/usr/bin/env python3
"""熱求解の時間とメモリの計測 (1 ケース worst/artwork)。重いので 1 本ずつ。
  python tests/bench_thermal.py --dx 0.1 [--solver auto|direct|amg|cg] [--project ...]"""
import argparse, glob, os, resource, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import yaml
from core import pcbread, parts, cases as C, grid as CG, boardspec
from thermal import pcbmodel
ap = argparse.ArgumentParser(); ap.add_argument('--dx', type=float, default=0.25); ap.add_argument('--solver', default=None)
ap.add_argument('--project', required=True); ap.add_argument('--case', default='worst')
ap.add_argument('--repeat', type=int, default=1, help='同じプロセスで繰り返す回数。2 以上なら最後の回を採用 (GPU の初回 JIT・転送を除く)')
ap.add_argument('--json', help='結果を 1 行 JSON で追記するファイル (tools/bench_wsl.sh が使う)')
a = ap.parse_args()
R = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
cfg = yaml.safe_load(open(os.path.join(R, 'examples/usb-jtag-ft2232h/thermal.yaml'), encoding='utf-8')); cfg['_cond_file'] = 'thermal.yaml'
if a.solver:
    cfg['solver'] = {'method': a.solver}
pcb = [p for p in glob.glob(os.path.join(a.project, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
board = pcbread.read_pcb(pcb); lib = parts.load_library(os.path.join(R, 'data/parts'))
cfg['_board_spec'] = boardspec.load(boardspec.find(cfg['project'], os.path.join(R, 'data/board')))
t0 = time.time(); g, _ = CG.build(board, 'artwork', a.dx, 4, None, board_spec=cfg['_board_spec']); t1 = time.time()
cc = C.resolve(cfg, a.case); cc['_cond_file'] = 'thermal.yaml'
runs = []
for k in range(max(1, a.repeat)):
    t1 = time.time()
    res = pcbmodel.build_and_solve(board, cc, C.resolve_lib(lib, a.case), 'artwork', a.dx, grid=g, log=lambda *x: None); t2 = time.time()
    runs.append(dict(thermal_s=round(t2 - t1, 2), solve_s=res['info'].get('solve_s'), setup_s=res['info']['solver_info'].get('setup_s')))
if len(runs) > 1:
    print('回ごと (熱 / 求解 / setup s):', ' | '.join(f"{r['thermal_s']} / {r['solve_s']:.2f} / {r['setup_s']}" for r in runs), '→ 最後の回を採用')
i = res['info']
print(f"dx {a.dx} mm solver {i.get('solver', 'direct')}: 未知数 {i['unknowns']}、格子 {t1 - t0:.1f} s、熱 {t2 - t1:.1f} s (うち求解 {i.get('solve_s', float('nan')):.1f} s、反復 {i['iterations']})、"
      f"{i['solver_info']}、最大 RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB、Tj " + ', '.join(f"{r['ref']} {r['tj'] if r['tj'] is not None else r['t_pad']:.3f}" for r in res['rows']))
print('3D モデル:', i['model3d_source'], ('  ★見つからない: ' + str(i['model3d_missing'])) if i['model3d_missing'] else '')
if a.json:
    import json, platform
    gpu_mem = None
    try:
        import cupy
        gpu_mem = cupy.get_default_memory_pool().total_bytes() / 2**20
    except Exception:
        pass
    rec = dict(dx_mm=a.dx, solver_requested=a.solver or 'auto', solver_used=i.get('solver'), unknowns=i['unknowns'], grid_s=round(t1 - t0, 2),
               thermal_s=round(t2 - t1, 2), solve_s=i.get('solve_s'), setup_s=i['solver_info'].get('setup_s'), cg_iters=i['solver_info'].get('cg_iters'),
               outer_iters=i['iterations'], max_rss_mb=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024), gpu_pool_mb=gpu_mem,
               runs=runs, tj={r['ref']: (r['tj'] if r['tj'] is not None else r['t_pad']) for r in res['rows']}, model3d_source=i['model3d_source'], model3d_missing=i['model3d_missing'], host=platform.node(), python=platform.python_version())
    open(a.json, 'a').write(json.dumps(rec, ensure_ascii=False) + '\n')
