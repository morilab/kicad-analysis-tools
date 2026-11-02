#!/usr/bin/env python3
"""P22 部品の外周距離 (courtyard / 部品外形と基板外形の最短距離、しきい値は gerber-drc user.edge_keepout_parts) の単体テスト。
python3 tools/review/tests/test_p22_parts.py   (合成した 40x30 mm の基板だけを使う)"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import board_rules as br, kpcb

ok = True
def check(name, got, want):
    global ok
    r = 'OK ' if got == want else 'NG '
    ok &= got == want
    print(r, name, got, '(期待', want, ')')

def fp(lib, ref, x, y, crtyd=None, fab=None, pad=(0.6, 0.6)):
    """crtyd/fab: 部品中心からの半幅・半高 (mm)。None なら描かない"""
    g = ''
    for L, hw in (('F.CrtYd', crtyd), ('F.Fab', fab)):
        if hw:
            w, h = hw
            pts = [(-w, -h), (w, -h), (w, h), (-w, h)]
            for i in range(4):
                a, c = pts[i - 1], pts[i]
                g += f'(fp_line (start {a[0]} {a[1]}) (end {c[0]} {c[1]}) (stroke (width 0.05) (type solid)) (layer "{L}"))\n'
    return (f'(footprint "{lib}" (layer "F.Cu") (at {x} {y})\n(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n{g}'
            f'(pad "1" smd rect (at 0 0) (size {pad[0]} {pad[1]}) (layers "F.Cu" "F.Mask")))\n')

def board(fps):
    e = ''.join(f'(gr_line (start {a[0]} {a[1]}) (end {c[0]} {c[1]}) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))\n'
                for a, c in (((0, 0), (40, 0)), ((40, 0), (40, 30)), ((40, 30), (0, 30)), ((0, 30), (0, 0))))
    s = '(kicad_pcb (version 20240108) (generator "test")\n(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n' + e + ''.join(fps) + ')\n'
    f = tempfile.NamedTemporaryFile('w', suffix='.kicad_pcb', delete=False); f.write(s); f.close()
    return kpcb.Board(f.name)

def yaml_file(txt):
    f = tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False); f.write(txt); f.close()
    return f.name

# ---- しきい値の読み込み
mm, pats, notes = br.parts_rule()
if os.path.exists(br.PARTS_RULE_YAML):
    check('morilab.yaml から読む (既定ファイル)', (mm, notes), (3.0, []))
else:
    check('morilab.yaml が無い (未 push の checkout) → 既定 3 mm と注意', (mm, len(notes)), (3.0, 1))
y25 = yaml_file('rules:\n  - id: user.edge_keepout_parts\n    value: 2.5\n')
check('YAML の値 2.5 mm を使う', br.parts_rule(y25)[:1] + (br.parts_rule(y25)[2],), (2.5, []))
m, _, n = br.parts_rule('/nonexistent/morilab.yaml')
check('ファイルが無い → 既定 3 mm と注意', (m, len(n) == 1 and '無い' in n[0]), (3.0, True))
m, _, n = br.parts_rule(yaml_file('rules:\n  - id: user.edge_keepout_copper\n    value: 1.0\n'))
check('キーが無い → 既定 3 mm と注意', (m, len(n) == 1 and 'user.edge_keepout_parts' in n[0]), (3.0, True))
m, _, n = br.parts_rule(yaml_file('rules:\n  - id: user.edge_keepout_parts\n    value: abc\n'))
check('値が数値でない → 既定 3 mm と注意', (m, len(n)), (3.0, 1))

# ---- 判定 (外形 40x30、左端 x=0 から courtyard 端までの距離)
B = board([
    fp('Resistor_SMD:R_0603', 'R1', 3.5, 15, crtyd=(1.0, 0.5)),            # courtyard 端 2.5 mm → 違反 (パッド中心は 3.5 mm)
    fp('Resistor_SMD:R_0603', 'R2', 4.5, 15, crtyd=(1.0, 0.5)),            # courtyard 端 3.5 mm → OK
    fp('Package_SO:SOIC-8', 'U1', 20, 2.8, fab=(2.0, 1.5)),                 # courtyard 無し → Fab+パッドの外接矩形 (上端 1.3 mm) → 違反
    fp('Connector_PinHeader_2.54mm:PinHeader_1x02', 'P1', 1.5, 25, crtyd=(1, 1)),  # Connector*:* → 例外
    fp('MountingHole:MountingHole_3.2mm_M3', 'H1', 3, 3, crtyd=(2, 2)),     # MountingHole:* → 例外
    fp('TestPoint:TestPoint_Pad_D1.0mm', 'TP1', 39, 15, crtyd=(0.7, 0.7)),  # TestPoint:* → 例外
    fp('Custom:JTAG_2x7', 'J9', 20, 29, crtyd=(1, 0.5)),                    # リファレンス J* → 例外
    fp('Custom:SW_Tact', 'SW1', 38, 28, crtyd=(1, 1)),                      # review.toml 個別指定 → 例外
    fp('Custom:LED', 'D1', 38.5, 5, crtyd=(1, 1)),                          # 右端 0.5 mm → 違反 (名前は例外に当たらない)
    fp('myproj:MountingHole_3.2mm_M3', 'H2', 37, 27, crtyd=(2, 2)),            # プロジェクトライブラリの写し (名前が MountingHole*) → 例外
    fp('myproj:TestPoint_Pad_D1.0mm', 'TP2', 1, 10, crtyd=(0.7, 0.7)),      # 同 TestPoint* → 例外
    fp('Custom:Hole_Like_Part', 'U9', 1.5, 20, crtyd=(0.5, 0.5)),           # 名前が規則に当たらない → 違反
])
cfg = {'board_edge': {'exempt': {'SW1': 'テスト用の個別指定'}}}
E = br.edge_checks(B, cfg)
bad = {x['ref']: round(x['d'], 2) for x in E['parts']}
check('違反の部品と最短距離', bad, {'R1': 2.5, 'U1': 1.3, 'D1': 0.5, 'U9': 1.0})
check('U1 は部品外形の外接矩形で判定', next(x['how'] for x in E['parts'] if x['ref'] == 'U1'), '部品外形 (外接矩形)')
check('R1 は courtyard で判定', next(x['how'] for x in E['parts'] if x['ref'] == 'R1'), 'courtyard')
check('部品の例外', sorted(x.split(':')[0] for x in E['parts_exempt']), ['H1', 'H2', 'J9', 'P1', 'SW1', 'TP1', 'TP2'])
check('しきい値 3 mm', E['part_mm'], 3.0)
E2 = br.edge_checks(B, {'board_edge': {'parts_rule_yaml': y25, 'exempt': {'SW1': ''}}})
check('YAML 2.5 mm では R1 (2.50) は OK', sorted(x['ref'] for x in E2['parts']), ['D1', 'U1', 'U9'])
check('写しの例外の理由', next(x for x in E['parts_exempt'] if x.startswith('H2')), 'H2: フットプリント名 myproj:MountingHole_3.2mm_M3 (ライブラリの写し、MountingHole:* と同じ名前規則)')
E3 = br.edge_checks(B, {'board_edge': {'parts_rule_yaml': '/nonexistent.yaml'}})
check('YAML 無し → 注意が出る', len(E3['notes']), 1)
check('銅箔 1 mm は従来どおり (TP1・TP2 パッド 0.7 mm は違反、D1 1.2 mm は OK、P1 は銅箔の例外)', sorted(x.get('ref') for x in E['copper']), ['TP1', 'TP2'])

print('OK' if ok else 'NG あり')
sys.exit(0 if ok else 1)
