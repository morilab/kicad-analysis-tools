#!/usr/bin/env python3
"""熱ソルバーの方式 (direct / amg / amg-gs / cg / gpu-emu / gpu) が同じ温度を出すか。gpu は CuPy と GPU が無ければ auto に戻ることを確認"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from thermal import model as M

FAIL = []
ny, nx = 60, 80; act = np.ones((ny, nx), bool); act[:10, :15] = False
net = M.Network(act, 0.25e-3)
rng = np.random.default_rng(3)
net.add_layer(385 * 35e-6 * (0.2 + 0.8 * rng.random((ny, nx))))
net.add_layer(np.full((ny, nx), 0.8 * 1.5e-3))
net.add_layer(385 * 35e-6 * (0.2 + 0.8 * rng.random((ny, nx))))
net.zlinks = [np.full((ny, nx), 0.3 * (0.25e-3) ** 2 / 0.75e-3)] * 2
net.area = np.full((ny, nx), (0.25e-3) ** 2)
net.layers[0]['q'][30, 40] = 0.5
net.junctions.append(dict(name='U', P=0.3, links=[(0, 20 * nx + 60, 0.05)], g_air=0.001))
net.build()
sh = lambda face, Tf: (8.0, 6.0 * np.ones((ny, nx)) if Tf is None else 6.0 + 0.02 * (np.nan_to_num(Tf, nan=25.0) - 25))
ref, _ = net.solve(sh, 25.0, 25.0, method='direct')
for m in ('amg', 'amg-gs', 'cg', 'gpu-emu', 'gpu'):
    T, it = net.solve(sh, 25.0, 25.0, method=m)
    d = float(np.abs(T - ref).max()); used = net.solve_info['method']
    ok = d < 1e-3 and (m != 'gpu' or M._cupy() is not None or not used.startswith('gpu'))
    print(('ok   ' if ok else 'FAIL ') + f'{m}: 直接法との最大差 {d:.2e} ℃、使った方式 {used}、CG {net.solve_info["cg_iters"]}')
    ok or FAIL.append(m)
print('solver test:', 'FAIL ' + ', '.join(FAIL) if FAIL else 'すべて ok')
sys.exit(1 if FAIL else 0)
