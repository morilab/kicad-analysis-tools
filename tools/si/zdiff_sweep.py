#!/usr/bin/env python3
"""zdiff2d.py で 基板厚 × 形状 (マイクロストリップ / CPWG の GND 距離) × 線幅 × 間隔 を総当たりし CSV に書く。
使い方: python3 zdiff_sweep.py out.csv [--boards 1.6,1.2,1.0,0.8] [--w ...] [--s ...] [--g none,0.5,...]
基材の厚さ h = 基板厚 - 2 x 銅厚 (1 oz 35 µm) とする (JLC の 2 層は impedance 管理の積層表が無いので目安)。"""
import argparse, csv, time
import zdiff2d as z

ap = argparse.ArgumentParser()
ap.add_argument('out')
ap.add_argument('--boards', default='1.6,1.2,1.0,0.8')
ap.add_argument('--w', default='0.127,0.15,0.2,0.25,0.3,0.4,0.5,0.6,0.8')
ap.add_argument('--s', default='0.127,0.15,0.2,0.3')
ap.add_argument('--g', default='none,0.5,0.3,0.2,0.15,0.127')
ap.add_argument('--no-mask', action='store_true')
a = ap.parse_args()
fl = lambda v: [float(x) for x in v.split(',')]
gs = [None if x == 'none' else float(x) for x in a.g.split(',')]
t0 = time.time()
with open(a.out, 'w', newline='') as f:
    wr = csv.writer(f)
    wr.writerow(['board_mm', 'h_mm', 'shape', 'g_mm', 'w_mm', 's_mm', 'zdiff', 'zodd', 'zeven', 'zcm', 'eeff_odd', 'ipc2141', 'hammerstad'])
    for T in fl(a.boards):
        h = round(T - 2 * z.JLC['t'], 3)
        for g in gs:
            for w in fl(a.w):
                for s in fl(a.s):
                    r = z.solve(h, w, s, g, mask=not a.no_mask)
                    wr.writerow([T, h, 'MS' if g is None else 'CPWG', '' if g is None else g, w, s, round(r['zdiff'], 1), round(r['zodd'], 1),
                                 round(r['zeven'], 1), round(r['zcm'], 1), round(r['eeff_odd'], 3), round(z.ipc2141(h, w, s), 1), round(z.hammerstad(h, w, s), 1)])
                    f.flush()
            print(f'{T} mm g={g} 済 {time.time()-t0:.0f} s', flush=True)
