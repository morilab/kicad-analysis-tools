#!/usr/bin/env python3
"""results.json (run_pitfall.py) と expected.yaml (testboards/pitfall-4l、読むだけ) を同じ項目で並べた表 results.md を作る。
  python examples/pitfall-4l/table.py [--out out/pitfall-4l]"""
import argparse, json, os
import yaml
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(HERE))
EXP = os.path.join(os.path.dirname(os.path.dirname(ROOT)), 'testboards', 'pitfall-4l', 'expected.yaml')


def judge(v, e):
    r = e.get('range') if isinstance(e, dict) else None
    if r:
        return '範囲内' if r[0] <= v <= r[1] else '範囲外'
    return ''


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--out', default=os.path.join(ROOT, 'out', 'pitfall-4l')); ap.add_argument('--exp', default=EXP); a = ap.parse_args()
    J = json.load(open(os.path.join(a.out, 'results.json'))); R = J['results']
    X = yaml.safe_load(open(a.exp, encoding='utf-8')); TP = {p['id']: p for p in X['thermal']['patterns']}; CP = X['coupled']['patterns'][0]
    amb = 25.0; rows = []

    def add(pid, item, exp, val, unit, note=''):
        e = exp.get('value') if isinstance(exp, dict) else exp
        rg = exp.get('range') if isinstance(exp, dict) else None
        rows.append([pid, item, f"{e}" + (f" ({rg[0]}〜{rg[1]})" if rg else ''), f"{val:.4g}" if isinstance(val, float) else str(val), unit,
                     judge(val, exp) if isinstance(val, float) and isinstance(exp, dict) else '', note])
    if 'ldo' in R:
        e = TP['TH-LDO-VIA / TH-LDO-NOVIA']['expected']; r = R['ldo']['rows']
        add('TH-LDO', 'U2_Tj_c (ビアなし)', e['U2_Tj_c'], r['U2']['tj'], '℃', f"P {r['U2']['P']:.3f} W、θJC(タブ) 15 → タブの pad")
        add('TH-LDO', 'U1_Tj_c (ビア 9 本)', e['U1_Tj_c'], r['U1']['tj'], '℃', f"P {r['U1']['P']:.3f} W")
        add('TH-LDO', 'U2_minus_U1_c', e['U2_minus_U1_c'], r['U2']['tj'] - r['U1']['tj'], 'K', '負荷抵抗 R7〜R10 (各 0.495 W) も同時に発熱')
    if 'dense' in R and 'spread' in R:
        e = TP['TH-DENSE / TH-SPREAD']['expected']
        d = max(v['t_pad'] for v in R['dense']['rows'].values()) - amb; s = max(v['t_pad'] for v in R['spread']['rows'].values()) - amb
        add('TH-DENSE', 'dense_board_rise_k (抵抗 pad の最高 − 25)', e['dense_board_rise_k'], d, 'K', '抵抗 4 個 × 0.49 W。pad 温度 = 抵抗直下の基板')
        add('TH-SPREAD', 'spread_board_rise_k', e['spread_board_rise_k'], s, 'K', '')
        add('TH-DENSE/SPREAD', 'ratio_dense_over_spread', e['ratio_dense_over_spread'], d / s, '', '')
        dp = max(v['under']['In1.Cu'] for v in R['dense']['rows'].values()) - amb; spp = max(v['under']['In1.Cu'] for v in R['spread']['rows'].values()) - amb
        add('TH-DENSE', '(参考) dense: 抵抗中心直下の L2 GND の最高 − 25', e['dense_board_rise_k'], dp, 'K', 'expected のフィン近似 (L2+L3+FR-4 の板) に相当する量')
        add('TH-SPREAD', '(参考) spread: 同上', e['spread_board_rise_k'], spp, 'K', '')
        add('TH-DENSE/SPREAD', '(参考) 比 (L2 GND)', e['ratio_dense_over_spread'], dp / spp, '', '')
    if 'relief' in R:
        e = TP['TH-RELIEF / TH-SOLID']['expected']; r = R['relief']['rows']
        add('TH-RELIEF', '(参考) 中心直下 L2 GND の差 R19 − R20', e['R19_minus_R20_c'], r['R19']['under']['In1.Cu'] - r['R20']['under']['In1.Cu'], 'K', '')
        add('TH-RELIEF', 'R19_minus_R20_c (pad 温度の差)', e['R19_minus_R20_c'], r['R19']['t_pad'] - r['R20']['t_pad'], 'K',
            f"R19 {r['R19']['t_pad']:.1f} / R20 {r['R20']['t_pad']:.1f} ℃")
    if 'th1a' in R:
        e = TP['TH-1A']['expected']; h = R['th1a']['history']; f = h[-1]; z = h[0]
        add('TH-1A', 'dT_k (配線の最高 − 25)', e['dT_k'], f['T_max_c'] - amb, 'K', f"配線の平均 +{f['T_mean_c'] - amb:.1f} K、反復 {len(h)} 回")
        add('TH-1A', 'R20_mohm (20 ℃、連成前)', {'value': e['R20_mohm']}, z['R_mohm'], 'mΩ', 'J4.1 pad → J5.1 pad (pad・引き出し込み)')
        add('TH-1A', 'R_hot_mohm (連成後)', {'value': e['R_hot_mohm']}, f['R_mohm'], 'mΩ', '')
        add('TH-1A', 'V_mV (連成後)', {'value': e['V_mV']}, f['V_mV'], 'mV', '')
    if 'cp1a5' in R:
        e = CP['expected']; h = R['cp1a5']['history']; f = h[-1]; z = h[0]
        add('CP-1A5', 'uncoupled R_mohm (20 ℃)', {'value': e['uncoupled']['R_mohm']}, z['R_mohm'], 'mΩ', '')
        add('CP-1A5', 'uncoupled V_mV', {'value': e['uncoupled']['V_mV']}, z['V_mV'], 'mV', '')
        add('CP-1A5', 'uncoupled P_w', {'value': e['uncoupled']['P_w']}, z['P_w'], 'W', '')
        add('CP-1A5', 'coupled T_c (配線の最高)', {'value': e['coupled']['T_c']}, f['T_max_c'], '℃', f"配線の平均 {f['T_mean_c']:.1f} ℃")
        add('CP-1A5', 'coupled R_mohm', {'value': e['coupled']['R_mohm']}, f['R_mohm'], 'mΩ', '')
        add('CP-1A5', 'coupled V_mV', {'value': e['coupled']['V_mV']}, f['V_mV'], 'mV', '')
        add('CP-1A5', 'coupled P_w', {'value': e['coupled']['P_w']}, f['P_w'], 'W', '')
        add('CP-1A5', 'increase V_rel', {'value': e['increase']['V_rel']}, f['V_mV'] / z['V_mV'] - 1, '', '')
        add('CP-1A5', 'convergence (0.1 ℃ 以内の反復回数)', {'value': '4 回'}, f"{len(h)} 回", '', ' → '.join(f"{x['T_max_c']:.1f}" for x in h) + ' ℃')
    L = [f"# pitfall-4l 熱の区画 — pcb-physics の結果と expected.yaml", '',
         f"熱格子 {J['grid_mm']} mm、IR 格子 {J['grid_ir_mm']} mm (TH-1A・CP-1A5 のジュール熱は IR を解いて熱の外部発熱に、温度は補間・発熱は保存)。周囲 25 ℃、水平・無風、基板 YAML pitfall-4l-rev0.1 (JLC04161H-7628 仮定)。",
         '', '| 区画 | 項目 | expected (範囲) | pcb-physics | 単位 | 判定 | 備考 |', '|---|---|---|---|---|---|---|']
    L += ['| ' + ' | '.join(map(str, r)) + ' |' for r in rows]
    for k in ('th1a', 'cp1a5'):
        if k in R:
            L += ['', f"### {k} の反復 (IR {R[k]['ir']})", '', '| k | 配線最高 ℃ | 配線平均 ℃ | R mΩ | V mV | P W |', '|---|---|---|---|---|---|']
            L += [f"| {x['k']} | {x['T_max_c']:.2f} | {x['T_mean_c']:.2f} | {x['R_mohm']:.2f} | {x['V_mV']:.2f} | {x['P_w']:.4f} |" for x in R[k]['history']]
    open(os.path.join(a.out, 'results.md'), 'w').write('\n'.join(L) + '\n'); print('\n'.join(L))


if __name__ == '__main__':
    main()
