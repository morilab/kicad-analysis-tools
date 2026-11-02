#!/usr/bin/env python3
"""部品 YAML の検査 (出典 or 仮定の明記) と pcbread の座標変換"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from core import parts, pcbread
HERE = os.path.dirname(os.path.abspath(__file__))
lib = parts.load_library(os.path.join(HERE, '..', 'data', 'parts'))
print('ok   data/parts:', ', '.join(f"{k} {v['mpn']}" for k, v in lib.items()))
d = tempfile.mkdtemp()
for name, body in (('出典なし', 'ja: {value: 50}'), ('ページなし', 'ja: {value: 50, basis: datasheet, src: {table: T1}}'),
                   ('仮定の計算式なし', 'ja: {value: 50, basis: assumed, reason: 一般値}')):
    open(os.path.join(d, 'x.yaml'), 'w').write('lcsc: C1\nmpn: X\ntheta:\n  ' + body + '\n')
    try:
        parts.load_library(d); print(f'FAIL {name} を通した'); sys.exit(1)
    except parts.DataError as e:
        print(f'ok   {name}は拒否:', e)
x, y = pcbread.rot(1.0, 0.0, 90)   # KiCad の 90° は画面上で反時計回り (y 下向き) → (0, -1)
assert abs(x) < 1e-9 and abs(y + 1) < 1e-9, (x, y)
print('ok   回転の向き')
# 基板の製造条件 YAML (core/boardspec)
from core import boardspec, stackup as S
bd = os.path.join(HERE, '..', 'data', 'board')
for f in sorted(os.listdir(bd)):
    boardspec.load(os.path.join(bd, f))
print('ok   data/board:', ', '.join(sorted(os.listdir(bd))))
base = "order:\n  Base Material: {value: FR-4, basis: design, from: x}\n  Layers: {value: 2, basis: design, from: x}\n  Outer Copper Weight: {value: '1 oz', basis: design, from: x}\n  Specify Stackup: {value: 'No', basis: design, from: x}\n"
for name, body in (('注文画面にない項目名', "  PCB Thickness: {value: 1.6, basis: design, from: x}\n  Copper: {value: 1, basis: design, from: x}\n"),
                   ('選択肢にない板厚', "  PCB Thickness: {value: 1.5, basis: design, from: x}\n"),
                   ('仮定の根拠なし', "  PCB Thickness: {value: 1.6, basis: assumed}\n")):
    open(os.path.join(d, 'b.yaml'), 'w').write(base + body)
    try:
        boardspec.load(os.path.join(d, 'b.yaml')); print(f'FAIL {name} を通した'); sys.exit(1)
    except parts.DataError as e:
        print(f'ok   基板 YAML {name}は拒否:', e)
open(os.path.join(d, 'b.yaml'), 'w').write(base.replace("Layers: {value: 2", "Layers: {value: 4") + "  PCB Thickness: {value: 1.2, basis: design, from: x}\n")
fake = dict(cu_layers=['F.Cu', 'B.Cu'], thickness=1.6, stackup=None)
r = S.resolve(fake, None, boardspec.load(os.path.join(d, 'b.yaml')))
assert any('層数' in w for w in r['warnings']) and r['cu'] == [0.035, 0.035], r
open(os.path.join(d, 'b.yaml'), 'w').write(base + "  PCB Thickness: {value: 1.2, basis: design, from: x}\n")
r = S.resolve(fake, None, boardspec.load(os.path.join(d, 'b.yaml')))
assert any('板厚' in w for w in r['warnings']) and abs(r['diel'][0] - (1.2 - 0.09)) < 1e-9, r
print('ok   基板 YAML と .kicad_pcb の食い違いを警告:', r['warnings'])
r = S.resolve(fake); assert r['warnings'] == [] and 'JLCPCB 標準' in r['src']
print('ok   基板 YAML なしは JLC 標準')
# signal: (data/signal/parts と同じ形) と 熱データなしの部品 YAML
import subprocess, yaml
KH = os.environ.get('KICAD_KNOWHOW_DIR', '/nonexistent')   # 任意: git 履歴のある kicad-knowhow の clone があれば追加の検査をする
d2 = tempfile.mkdtemp()
if os.path.isdir(KH):   # 読むだけ (git show bd287ae)。熱データの YAML に signal: を足した形で検査を通す
    names = subprocess.run(['git', '-C', KH, 'ls-tree', '--name-only', 'bd287ae', 'data/signal/parts/'], capture_output=True, text=True).stdout.split()
    for n in names:
        y = yaml.safe_load(subprocess.run(['git', '-C', KH, 'show', f'bd287ae:{n}'], capture_output=True, text=True).stdout)
        base_y = dict(lib.get(str(y['lcsc'])) or dict(lcsc=y['lcsc'], mpn=y['mpn']))
        base_y.pop('_file', None); base_y['signal'] = y['signal']
        yaml.safe_dump(base_y, open(os.path.join(d2, f"{y['lcsc']}.yaml"), 'w'), allow_unicode=True)
    L2 = parts.load_library(d2)
    print('ok   kicad-knowhow bd287ae の signal: を部品 YAML にまとめて検査通過:', ', '.join(sorted(L2)))
for name, sig in (('signal の未知キー', {'foo': {}}), ('signal の basis なし', {'default': {'tr_ns': {'value': 1.0}}}),
                  ('signal の生の数値', {'default': {'tr_ns': 1.0}})):
    open(os.path.join(d, 's.yaml'), 'w').write(yaml.safe_dump(dict(lcsc='C9', mpn='X', signal=sig)))
    try:
        parts.check_signal(yaml.safe_load(open(os.path.join(d, 's.yaml'))), 's.yaml'); print(f'FAIL {name} を通した'); sys.exit(1)
    except parts.DataError as e:
        print(f'ok   {name}は拒否:', e)
nt = dict(lcsc='C9', mpn='X', pins={'1': {'current': {'typ': {'value': 0.01, 'basis': 'design', 'from': 'x'}, 'max': {'value': 0.02, 'basis': 'design', 'from': 'x'}}}})
assert not parts.has_thermal(nt) and parts.has_thermal(lib['C27882'])
from thermal import report
r = report.judge([dict(tj=None, t_pad=60.0, path=None, tj_max=125, no_thermal=True)], 50.0)[0]
assert r['grade'] == 'info', r
print('ok   熱データなし (ピン電流だけ) の部品 YAML: has_thermal = False、熱側は info:', r['why'])
