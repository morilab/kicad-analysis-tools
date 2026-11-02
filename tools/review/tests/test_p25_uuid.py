#!/usr/bin/env python3
"""P25_uuid_unique (uuid_check.py) の単体テスト。合成した小さな基板・回路図・ネットリストだけを使う。
python3 tools/review/tests/test_p25_uuid.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import uuid_check as uc

ok = True
def check(name, got, want):
    global ok
    r = 'OK ' if got == want else 'NG '
    ok &= got == want
    print(r, name, got, '(期待', want, ')')

U = lambda n: f'{n:08x}-0000-4000-8000-000000000000'
def fp(ref, val, uid, path, pads=(1, 2), board_only=False):
    p = f'\t\t(path "{path}")\n' if path is not None else ''
    a = '\t\t(attr smd board_only)\n' if board_only else '\t\t(attr smd)\n'
    pd = ''.join(f'\t\t(pad "{n}" smd rect (at {n} 0) (size 0.5 0.5) (layers "F.Cu") (uuid "{U(uid * 100 + n)}"))\n' for n in pads)
    return (f'\t(footprint "C:C" (layer "F.Cu")\n\t\t(uuid "{U(uid)}")\n\t\t(at 0 0)\n'
            f'\t\t(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS") (uuid "{U(uid * 100 + 50)}"))\n'
            f'\t\t(property "Value" "{val}" (at 0 0) (layer "F.Fab") (uuid "{U(uid * 100 + 51)}"))\n{p}{a}{pd}\t)\n')
def pcb(*fps, extra=''):
    return '(kicad_pcb\n\t(version 20241229)\n' + ''.join(fps) + extra + ')\n'
def netlist(comps):
    c = ''.join(f'<comp ref="{r}"><value>{v}</value><sheetpath names="/" tstamps="/"/><tstamps>{t}</tstamps></comp>' for r, v, t in comps)
    return f'<?xml version="1.0"?><export><components>{c}</components></export>'
def sch(uids):
    return '(kicad_sch\n' + ''.join(f'\t(symbol (lib_id "Device:C") (at 0 0 0)\n\t\t(uuid "{u}")\n\t\t(pin "1" (uuid "{U(9000)}"))\n\t)\n' for u in uids) + ')\n'

S1, S2, S3 = U(1001), U(1002), U(1003)
NL = [('C1', '1uF', S1), ('C2', '1uF', S2), ('C3', '1uF', S3)]
d = tempfile.mkdtemp()
def run(board, nl=NL, s=None):
    pp = os.path.join(d, 'b.kicad_pcb'); open(pp, 'w').write(board)
    np = os.path.join(d, 'n.xml'); open(np, 'w').write(netlist(nl))
    sf = []
    if s is not None:
        sf = [os.path.join(d, 'b.kicad_sch')]; open(sf[0], 'w').write(s)
    return uc.check(pp, sf, np)

# 1. 正常 (回路図のピン uuid の重複はシンボル本体ではないので数えない)
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S2), fp('C3', '1uF', 3, '/' + S3)), s=sch([S1, S2, S3]))
check('正常 grade', R['grade'], 'pass')
# 2. 本体と子要素を丸ごとコピー (2026-10-16 usb-jtag-ft2232h の C23/C24/C25) → fail、子要素も区別して出す
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 1, '/' + S2), fp('C3', '1uF', 3, '/' + S3)))
check('本体重複 grade', R['grade'], 'fail')
check('本体重複 refs', [x['refs'] for x in R['fp_dup']], [['C1', 'C2']])
check('子要素重複 個数', len(R['child_dup']), 4)   # pad 1/2・Reference・Value
check('値 本体/子要素/path', R['value'], '1/4/0')
# 3. 子要素だけの重複 → warn
b = pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S2), fp('C3', '1uF', 3, '/' + S3)).replace(U(201), U(101))
R = run(b)
check('子要素だけ grade', R['grade'], 'warn')
check('子要素だけ 本体重複', R['fp_dup'], [])
# 4. path 空 → fail、board_only は可
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S2), fp('C3', '1uF', 3, '/' + S3), fp('LOGO1', 'x', 4, None, board_only=True)))
check('board_only は pass', (R['grade'], R['board_only']), ('pass', ['LOGO1']))
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, None), fp('C3', '1uF', 3, '/' + S3)))
check('path 空 fail', (R['grade'], R['path_empty']), ('fail', ['C2']))
# 5. path 重複 → fail
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S1), fp('C3', '1uF', 3, '/' + S3)))
check('path 重複 fail', (R['grade'], [x['refs'] for x in R['path_dup']]), ('fail', [['C1', 'C2']]))
# 6. 対応シンボルなし → fail
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + U(7777)), fp('C3', '1uF', 3, '/' + S3)))
check('対応なし fail', (R['grade'], [x['ref'] for x in R['path_missing']]), ('fail', ['C2']))
# 7. 部品番号・値の食い違い → warn
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C3', '1uF', 2, '/' + S2), fp('C2', '4.7uF', 3, '/' + S3)))
check('食い違い warn', (R['grade'], sorted(x['ref'] for x in R['mismatch'])), ('warn', ['C2', 'C3']))
# 8. 回路図シンボルの uuid 重複 → fail
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S2), fp('C3', '1uF', 3, '/' + S3)), s=sch([S1, S1, S3]))
check('回路図重複 fail', (R['grade'], list(R['sch_dup'])), ('fail', ['b.kicad_sch']))
# 9. 本体以外 (via どうし) の重複 → warn
R = run(pcb(fp('C1', '1uF', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S2), fp('C3', '1uF', 3, '/' + S3),
            extra=f'\t(via (at 1 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0) (uuid "{U(5)}"))\n' * 2))
check('via 重複 warn', (R['grade'], [x['kinds'] for x in R['other_dup']]), ('warn', [['via']]))
# 10. 文字列中の括弧で壊れない
R = run(pcb(fp('C1', 'a(b', 1, '/' + S1), fp('C2', '1uF', 2, '/' + S2), fp('C3', '1uF', 3, '/' + S3)), nl=[('C1', 'a(b', S1)] + NL[1:])
check('値に括弧 pass', R['grade'], 'pass')
print('全体', 'OK' if ok else 'NG')
sys.exit(0 if ok else 1)
