#!/usr/bin/env python3
"""irdrop の連成・めっき厚まわりの検証。
1 めっき厚の優先順 (案件 YAML > 基板 YAML process.via_plating > core 既定 18 µm) と meta への記録、ビア抵抗が厚さに従うこと
2 couple の要約表示が部品発熱なし (tj_delta が空) で落ちない
3 熱 ⇄ IR の格子の受け渡し (TGrid = core/regrid): 発熱の合計を保存、一様温度はそのまま、同じ dx なら同じ格子"""
import os, sys, math, tempfile
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from tests.test_irdrop_analytic import make, check
from irdrop import plating as PL, couple as CPL
from irdrop.solver import solve_net
from core import stackup as S


def board_dict():
    pad = lambda ref, xy: dict(ref=ref, side='F.Cu', lcsc='', pads=[dict(net='N', num='1', type='smd', shape='rect', x=xy[0], y=xy[1], rot=0,
                               w=1.0, h=1.0, layers=['F.Cu'], drill=0.0, rr=0)])
    f = tempfile.NamedTemporaryFile(suffix='.kicad_pcb', delete=False); f.write(b'x'); f.close()
    return dict(path=f.name, thickness=1.6, cu_layers=['F.Cu', 'B.Cu'], stackup=None, outline=[[(0, 0), (20, 0), (20, 10), (0, 10)]],
                footprints=[pad('V', (2, 5)), pad('S', (18, 5))], tracks=[dict(a=(2, 5), b=(18, 5), w=1.0, layer='F.Cu', net='N')], vias=[], zones=[])


def main():
    res = []
    # 1 めっき厚
    ic = dict(plating={'value': 20, 'basis': 'design', 'from': '試験基板 expected.yaml の仮定 20 µm'})
    bs = dict(_file='b.yaml', process=dict(via_plating={'value': 25, 'basis': 'design', 'from': 'テスト'}))
    mm1, s1 = PL.resolve(ic, bs); mm2, s2 = PL.resolve({}, bs); mm3, s3 = PL.resolve({}, None)
    ok = abs(mm1 - 0.020) < 1e-12 and s1.startswith('案件') and abs(mm2 - 0.025) < 1e-12 and s2.startswith('基板') and abs(mm3 - S.T_PLATING) < 1e-12 and abs(mm3 - 0.018) < 1e-12
    print(f"{'ok  ' if ok else 'FAIL'} 1a 優先順: 案件 {mm1 * 1e3:g} µm / 基板 {mm2 * 1e3:g} µm / 既定 {mm3 * 1e3:g} µm"); res.append(ok)
    m = np.zeros((2, 5, 5), bool); m[:, 2, 2] = True
    rv = {}
    for tp in (0.018, 0.020):
        g = make(m, diel=(1.5,), pads=[('V', '1', 0, [(2, 2)]), ('S', '1', 1, [(2, 2)])], vias=[(2, 2, 0.3)])
        PL.apply(g, dict(plating={'value': tp * 1e3, 'basis': 'design', 'from': 'テスト'}))
        r = solve_net(g, 'N', None, [('S', '1', 0.1)], [('V', '1', 1.0)]); rv[tp] = (1 - r['pin_v'][('S', '1')]) / 0.1
        ok = g.meta['plating_src'].startswith('案件'); res.append(ok)
    r0 = 0.15e-3
    hand = ((r0 + 0.018e-3) ** 2 - r0 ** 2) / ((r0 + 0.020e-3) ** 2 - r0 ** 2)
    res.append(check('1b ビア抵抗の比 R(20 µm)/R(18 µm) = 断面積の逆比', rv[0.020] / rv[0.018], hand, -1e-3, 1e-3))
    # 2 要約表示
    summ = dict(cases=dict(typ=dict(iterations=2, converged=True, cu_max_c=dict(coupled=30.0), joule_mw=1.0, tj_delta={},
                                    drops={'N': dict(uniform=dict(drop_mv=1.0), coupled=dict(drop_mv=1.1))})))
    try:
        t = CPL.summary_text(summ); ok = '"tjd": null' in t
    except Exception as e:
        ok = False; print('  例外', e)
    print(f"{'ok  ' if ok else 'FAIL'} 2 部品発熱なし (tj_delta 空) で要約表示が落ちない"); res.append(ok)
    # 3 格子の受け渡し
    from core import grid as CG
    b = board_dict()
    gi, _ = CG.build(b, 'artwork', 0.1, 4, checks=False)
    tg = CPL.TGrid(b, {}, gi, 0.3); tg1 = CPL.TGrid(b, {}, gi, 0.1); os.unlink(b['path'])
    h = np.zeros(gi.cu_frac.shape); h[0, 40:60, 20:180] = 1e-4
    ht = tg.heat_to_t(h)
    res.append(check('3a 発熱の合計を保存 (0.1 → 0.3 mm)', ht.sum(), h.sum(), -1e-9, 1e-9))
    T = np.full(tg.g.cu_frac.shape, 37.0); Ti = tg.temp_to_ir(T)
    res.append(check('3b 一様温度 37 ℃ はそのまま (0.3 → 0.1 mm、最大)', float(np.nanmax(Ti)), 37.0, -1e-9, 1e-9))
    ok = tg1.same and tg1.g is gi and not tg.same; print(f"{'ok  ' if ok else 'FAIL'} 3c 同じ dx は同じ格子 (受け渡しなし)"); res.append(ok)
    print(f'{sum(res)}/{len(res)} 通過')
    return all(res)


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
