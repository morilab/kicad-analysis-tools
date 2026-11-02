#!/usr/bin/env python3
"""zdiff_sweep.py の CSV から Markdown の表を作る。
使い方: python3 zdiff_table.py sweep.csv [--target 90 --tol 0.10 --pairs 0.3/0.2,0.3/0.15,0.4/0.15,0.25/0.127]"""
import argparse, csv, collections

ap = argparse.ArgumentParser()
ap.add_argument('csv'); ap.add_argument('--target', type=float, default=90); ap.add_argument('--tol', type=float, default=0.10)
ap.add_argument('--pairs', default='0.3/0.2,0.3/0.15,0.25/0.15,0.4/0.15,0.25/0.127,0.4/0.2,0.5/0.15')
ap.add_argument('--top', type=int, default=4)
a = ap.parse_args()
R = list(csv.DictReader(open(a.csv)))
key = lambda r: (r['shape'], r['g_mm'])
shapes = sorted({key(r) for r in R}, key=lambda k: (k[0] != 'MS', -float(k[1] or 99)))
lab = lambda k: 'マイクロストリップ' if k[0] == 'MS' else f'CPWG (GND まで {k[1]} mm)'
pairs = a.pairs.split(',')
Z = {(r['board_mm'], key(r), f"{float(r['w_mm']):g}/{float(r['s_mm']):g}"): r for r in R}
lo, hi = a.target * (1 - a.tol), a.target * (1 + a.tol)
out = []
for T in sorted({r['board_mm'] for r in R}, key=lambda x: -float(x)):
    h = next(r['h_mm'] for r in R if r['board_mm'] == T)
    out += [f'### 基板 {T} mm (基材 h = {h} mm)', '', '| 形 \\ 線幅/間隔 (mm) | ' + ' | '.join(pairs) + ' |', '|---|' + '---|' * len(pairs)]
    for k in shapes:
        cells = []
        for p in pairs:
            r = Z.get((T, k, p))
            if not r:
                cells.append('—'); continue
            zd = float(r['zdiff'])
            cells.append(f'**{zd:.0f}**' if lo <= zd <= hi else f'{zd:.0f}')
        out.append(f'| {lab(k)} | ' + ' | '.join(cells) + ' |')
    ms = next((Z[(T, ("MS", ""), p)] for p in pairs if (T, ("MS", ""), p) in Z), None)
    out += ['', f'太字 = {a.target:.0f} Ω ±{a.tol*100:.0f}% ({lo:.0f}〜{hi:.0f} Ω)。' +
            (f' 参考 (式、CPWG・レジスト無視) {pairs[0]}: IPC-2141 {ms["ipc2141"]} Ω / Hammerstad+結合近似 {ms["hammerstad"]} Ω' if ms else ''), '']
out += [f'### {a.target:.0f} Ω ±{a.tol*100:.0f}% に入る組み合わせ (各 基板厚・形で {a.target:.0f} Ω に近い順 {a.top} 個、線幅 ≥0.2 mm)', '',
        '| 基板 mm | 形 | 線幅 mm | 間隔 mm | Zdiff Ω | Zcm Ω |', '|---|---|---|---|---|---|']
grp = collections.defaultdict(list)
for r in R:
    zd = float(r['zdiff'])
    if lo <= zd <= hi and float(r['w_mm']) >= 0.2:
        grp[(r['board_mm'], key(r))].append(r)
for (T, k), rs in sorted(grp.items(), key=lambda x: (-float(x[0][0]), x[0][1][0] != 'MS', -float(x[0][1][1] or 99))):
    for r in sorted(rs, key=lambda r: abs(float(r['zdiff']) - a.target))[:a.top]:
        out.append(f"| {T} | {lab(k)} | {r['w_mm']} | {r['s_mm']} | {r['zdiff']} | {r['zcm']} |")
print('\n'.join(out))
