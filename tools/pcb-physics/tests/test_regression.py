#!/usr/bin/env python3
"""回帰テスト (面内伝導 cell = 旧方式で比較、--inplane edge で新方式との差): FT2232H rev0.2 の typ/worst × placement/artwork の温度場と Tj が、kicad-thermal (commit ac64d1b) の結果と同じか。
  python tests/test_regression.py --project <usb-jtag-ft2232h を 64b10fe で checkout したディレクトリ> [--cond tests/regression/thermal.yaml] [--inplane cell|edge]
  (基準は基板 64b10fe + 条件 tests/regression/thermal.yaml。案件リポジトリの作業ツリーは先へ進むので、64b10fe の clone を指定する)
  4 計算。--solver direct で ac64d1b と 1e-6 ℃ 一致を確認、既定 auto は 1e-3 ℃ 以内)"""
import json, os, subprocess, sys, tempfile
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); REF = os.path.join(HERE, 'regression', 'ref')
SOLVER = sys.argv[sys.argv.index('--solver') + 1] if '--solver' in sys.argv else 'auto'
# 許容: 直接法は 1e-6 ℃ (ac64d1b と同じ計算)。反復法 (auto = AMG+CG、2026-10-14〜) は放射の反復の収束判定 1e-3 ℃ 以内
TOL = 1e-6 if SOLVER == 'direct' else 1e-3
# 面内伝導: ref (kicad-thermal ac64d1b) はセルの面積率の調和平均 (cell)。2026-10-16 から既定は edge (core/raster.edge_conduct) なので、移植の一致は --inplane cell で確かめる。
# --inplane edge で流すと新方式と ref の差を表示する (差が出るのは仕様、FAIL は参考)
COND = sys.argv[sys.argv.index('--cond') + 1] if '--cond' in sys.argv else os.path.join(HERE, 'regression', 'thermal.yaml')   # 条件は固定 (案件の thermal.yaml は FB1 追加などで変わる)
INPLANE = sys.argv[sys.argv.index('--inplane') + 1] if '--inplane' in sys.argv else 'cell'
proj = sys.argv[sys.argv.index('--project') + 1] if '--project' in sys.argv else sys.exit('--project <usb-jtag-ft2232h の checkout> が必要 (基板は本リポに含めない)')
out = tempfile.mkdtemp(prefix='pcbphys-reg-')
rc = subprocess.call([sys.executable, os.path.join(ROOT, 'kthermal.py'), proj, '--cond', COND, '--out', out, '--solver', SOLVER, '--inplane', INPLANE],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
FAIL = []
for case in ('typ', 'worst'):
    for mode in ('placement', 'artwork'):
        a = np.load(os.path.join(REF, f'temp_{case}_{mode}.npz')); b = np.load(os.path.join(out, f'temp_{case}_{mode}.npz'))
        d = max(float(np.nanmax(np.abs(a[f] - b[f]))) for f in 'FB')
        ok = d < TOL
        print(f"{'ok  ' if ok else 'FAIL'} 温度場 {case}/{mode}: 最大差 {d:.2e} ℃ (許容 {TOL:g}、ソルバー {SOLVER}、面内 {INPLANE})"); ok or FAIL.append(f'{case}/{mode}')
for mode in ('placement', 'artwork'):
    A = {m['id']: m['value'] for m in json.load(open(os.path.join(REF, f'thermal_{mode}.json')))['metrics']}
    B = {m['id']: m['value'] for m in json.load(open(os.path.join(out, f'thermal_{mode}.json')))['metrics']}
    bad = [(k, A[k], B.get(k)) for k in A if B.get(k) != A[k]]
    print(f"{'ok  ' if not bad else 'FAIL'} Tj/pad 温度 {mode}: {len(A)} 指標 {'一致' if not bad else bad}"); bad and FAIL.append(f'metrics {mode}')
print('regression:', 'FAIL ' + ', '.join(FAIL) if FAIL else 'すべて ok', f'(出力 {out})')
sys.exit(1 if FAIL else 0)
