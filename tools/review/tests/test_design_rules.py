#!/usr/bin/env python3
"""DR-01 (misinsert) の判定の単体テスト。python3 tools/review/tests/test_design_rules.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import misinsert

ok = True
def check(name, got, want):
    global ok
    r = 'OK ' if got == want else 'NG '
    ok &= got == want
    print(r, name, got, '(期待', want, ')')

J = misinsert.judge_pair
check('GND に相手の電源 = fail', J('gnd', 'sup', None, 3.6, 25, 50)[0], 'fail')
check('出力と相手の出力、直列 150 ohm = ok (24mA)', J('out', 'out', 150, 3.6, 25, 50)[0], 'ok')
check('出力と相手の出力、直列 100 ohm = warn (36mA)', J('out', 'out', 100, 3.6, 25, 50)[0], 'warn')
check('出力と相手の出力、直列なし = fail', J('out', 'out', None, 3.6, 25, 50)[0], 'fail')
check('出力と GND、直列 33 ohm = fail (109mA)', J('out', 'gnd', 33, 3.6, 25, 50)[0], 'fail')
check('入力と相手の出力 = ok', J('in', 'out', None, 3.6, 25, 50)[0], 'ok')
check('自基板の電源と相手 GND = fail', J('sup', 'gnd', None, 3.6, 25, 50)[0], 'fail')
G = misinsert.geometry(8, 1)
check('1x8 逆刺し 1->8', G['逆刺し'][1], 8)
check('1x8 +1 ずれ 8->空き', G['+1 ずれ'][8], None)
G2 = misinsert.geometry(10, 2)
check('2x10 180度 1->10', G2['逆刺し (180 度)'][1], 10)
# キー付き: review.toml の keyed = 理由、または極性付き footprint は「キーで物理的に防止」= ok (全ケース表なし)
class _NL:
    comps = {'J1': {'footprint': 'Connector_PinHeader_2.00mm:PinHeader_2x07_P2.00mm_Vertical_SMD'},
             'J2': {'footprint': 'Connector_JST:JST_SH_SM04B-SRSS-TB_1x04-1MP_P1.00mm_Horizontal'},
             'J3': {'footprint': 'Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical'}}
t, sk = misinsert.targets(_NL, {'connector': {'J1': {'keyed': 'Molex 87832 シュラウド + 極性切欠き'}}})
check('keyed 宣言のピンヘッダは対象外', t, ['J3'])
check('keyed 宣言 + JST footprint = キーで物理的に防止 2 件', misinsert.n_prevented(sk), 2)
# DNP の拡張端子: 電源-GND 短絡以外の fail は warn、電源-GND 短絡は fail のまま
class _NL2:
    comps = {'J9': {'footprint': 'Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical', 'value': 'x', 'part': 'Conn_02x05', 'dnp': True}}
    nets = {}; pin_net = {('J9', str(i)): f'S{i}' for i in range(1, 11)}
    def pins(self, ref): return [str(i) for i in range(1, 11)]
roles = {str(i): 'bidir' for i in range(1, 11)}; roles['2'] = 'gnd'; roles['10'] = 'sup'
r = misinsert.analyze(_NL2(), 'J9', {'roles': roles})
check('DNP 2x5 (GND 2 / 3V3 10、直列 R なし) = warn', r['grade'], 'warn')
roles['9'] = 'gnd'; roles['2'] = 'bidir'
r = misinsert.analyze(_NL2(), 'J9', {'roles': roles})
check('DNP でも電源-GND 短絡 (GND 9 / 3V3 10、行ずれ) = fail', r['grade'], 'fail')
# DR-04 DNP の方針: SMD の DNP は warn、THT 2.54mm 未満は warn、DNP理由 の空欄は warn
import checks_design
C = {'F1': {'footprint': 'Fuse:Fuse_0805_2012Metric', 'dnp': True, 'fields': {}},
     'J4': {'footprint': 'Connector_PinHeader_2.54mm:PinHeader_2x05_P2.54mm_Vertical', 'dnp': True, 'fields': {'DNP理由': 'THT 2.54mm'}},
     'J8': {'footprint': 'Connector_PinHeader_1.27mm:PinHeader_1x04_P1.27mm_Vertical', 'dnp': True, 'fields': {'DNP理由': 'x'}},
     'J9': {'footprint': 'Connector_PinHeader_2.00mm:PinHeader_2x07_P2.00mm_Vertical_SMD', 'dnp': True, 'fields': {'DNP理由': 'x'}},
     'R1': {'footprint': 'Resistor_SMD:R_0402_1005Metric', 'dnp': False, 'fields': {}}}
got = sorted((r, k) for r, k, _ in checks_design.dnp_policy(C))
check('DNP の方針 (F1 SMD + 理由なし、J8 1.27mm、J9 SMD ヘッダ)', got, [('F1', 'reason'), ('F1', 'smd'), ('J8', 'pitch'), ('J9', 'smd')])
check('PCB の attr through_hole を優先', checks_design.dnp_policy({'X1': {'footprint': 'odd:Foo', 'dnp': True, 'fields': {'DNP理由': 'y'}}}, {'X1': 'through_hole'}), [])
sys.exit(0 if ok else 1)
