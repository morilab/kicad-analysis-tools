"""coverage.sh の補助: スイートごとの固有行 (ほかのスイートでは通らない行) の数を出す。冗長なスイートの目安"""
import os, sys, coverage
out = sys.argv[1]
data = {}
for n in sorted(os.listdir(out)):
    f = os.path.join(out, n, '.coverage')
    if not os.path.exists(f): continue
    d = coverage.CoverageData(basename=f); d.read()
    data[n] = {(os.path.basename(p), l) for p in d.measured_files() for l in (d.lines(p) or ())}
print('\n== スイートごとの行数 / 固有行 (ほかのスイートで通らない)')
for n, s in data.items():
    others = set().union(*[v for k, v in data.items() if k != n]) if len(data) > 1 else set()
    u = s - others
    by = {}
    for f, l in u: by.setdefault(f, []).append(l)
    print(f'{n:6s} 行 {len(s):5d}  固有 {len(u):4d}  ' + ', '.join(f'{f}:{len(v)}' for f, v in sorted(by.items())))
