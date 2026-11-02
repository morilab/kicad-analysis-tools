#!/usr/bin/env python3
"""直列素子の抵抗の読み方 (core/parts.series_resistance、2026-10-19 pdn-edge)。
部品 YAML は dc_resistance (IR 側の書き方) と resistance + resistance_min (熱側の書き方) の 2 通りがあり、IR・PDN はどちらも読む。
C207022・C468889 は main (熱側) の版に統一したので resistance から、C1015 は dc_resistance から読めること。値はデータシートの新しい版。"""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); sys.path.insert(0, ROOT)
from core import parts as CP
from irdrop import run as IR
lib = CP.load_library(os.path.join(ROOT, 'data', 'parts'))
FAIL = []


def check(ok, msg):
    print(('ok   ' if ok else 'FAIL ') + msg); ok or FAIL.append(msg)


v = lambda x: float(x['value'] if isinstance(x, dict) else x)
for lc, typ, mx, mn, key in (('C468889', 0.105, 0.160, 0.050, 'resistance'), ('C207022', 0.50, 0.850, 0.150, 'resistance'), ('C1015', 0.10, 0.15, None, 'dc_resistance')):
    d, k = CP.series_resistance(lib[lc])
    check(k == key, f'{lc}: 読んだキー {k} (期待 {key})')
    check(abs(v(d['typ']) - typ) < 1e-9 and abs(v(d['max']) - mx) < 1e-9, f"{lc}: typ {v(d['typ'])} / max {v(d['max'])} Ω (期待 {typ} / {mx})")
    if mn is not None:
        check(abs(v(d['min']) - mn) < 1e-9, f"{lc}: min {v(d['min'])} Ω (resistance_min、期待 {mn})")
check(CP.series_resistance({}) == (None, None) and CP.series_resistance({'resistance': 0.1}) == (None, None), '抵抗の無い部品は (None, None)')
both = {'dc_resistance': {'typ': 1, 'max': 2}, 'resistance': {'typ': 3, 'max': 4}}
check(CP.series_resistance(both)[1] == 'dc_resistance', '両方ある時は dc_resistance を優先')
# 保持電流の温度表は新しい版 (Revised 06/17/19): 70 ℃ 0.65 A、85 ℃ 0.52 A
dr = lib['C468889']['hold_current_derating']['value']
check(abs(dr[70] - 0.65) < 1e-9 and abs(dr[85] - 0.52) < 1e-9, f'C468889 hold_current_derating 70 ℃ {dr[70]} A / 85 ℃ {dr[85]} A (期待 0.65 / 0.52)')
check('dc_resistance' not in lib['C468889'] and 'ratings' not in lib['C468889'], 'C468889 は旧版の dc_resistance / ratings を持たない (値の二重管理なし)')
# irdrop.run.series_r: 案件に r が無ければ部品 YAML から、温度補正なし (tc_per_c = null)
board = {'footprints': [{'ref': 'F1', 'lcsc': 'C468889', 'pads': []}]}
for case, exp in (('worst', 0.160), ('typ', 0.105)):
    R, note = IR.series_r(board, lib, {}, {'ref': 'F1', 'from_pin': '1', 'to_pin': '2'}, case, None, None, 50.0)
    check(abs(R - exp) < 1e-12 and 'resistance' in note and '温度補正なし' in note, f'irdrop series_r F1 {case}: {R} Ω ({note})')
print('PASS' if not FAIL else f'FAIL {len(FAIL)}: {FAIL}'); sys.exit(1 if FAIL else 0)
