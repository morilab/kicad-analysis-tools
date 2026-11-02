#!/usr/bin/env python3
"""rev0.2 用: results.json (typ / worst) と rev0.2 の expected.yaml を並べた Markdown 表を出す (stdout)。
  python examples/pitfall-4l/table_rev02.py --exp <rev0.2 expected.yaml> OUT_TYP[+OUT2] OUT_WORST"""
import argparse, json, os
import yaml

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--exp', required=True); ap.add_argument('outs', nargs='+'); a = ap.parse_args()
    X = yaml.safe_load(open(a.exp, encoding='utf-8')); TP = {p['id']: p for p in X['thermal']['patterns']}; CP = X['coupled']['patterns'][0]
    def load(o):   # 'dirA+dirB' は results を合わせる (区画を分けて流したとき)
        J = {'results': {}}
        for d in o.split('+'):
            J['results'].update(json.load(open(os.path.join(d, 'results.json')))['results'])
        return J
    Rs = [load(o) for o in a.outs]; amb = 25.0
    def items(J):
        R = J['results']; o = {}
        r = R['ldo']['rows']; o['U1_Tj_c'] = r['U1']['tj']; o['U2_Tj_c'] = r['U2']['tj']; o['U2_minus_U1_c'] = r['U2']['tj'] - r['U1']['tj']
        o['R10_minus_R8_c'] = r['R10']['t_pad'] - r['R8']['t_pad']
        d = max(v['t_pad'] for v in R['dense']['rows'].values()) - amb; s = max(v['t_pad'] for v in R['spread']['rows'].values()) - amb
        o['dense_pad_rise_k'] = d; o['spread_pad_rise_k'] = s; o['ratio_dense_over_spread'] = d / s
        dp = max(v['under']['In1.Cu'] for v in R['dense']['rows'].values()) - amb; sp = max(v['under']['In1.Cu'] for v in R['spread']['rows'].values()) - amb
        o['plate_dense'] = dp; o['plate_spread'] = sp; o['plate_ratio'] = dp / sp
        r = R['relief']['rows']; o['R19_minus_R20_c'] = r['R19']['t_pad'] - r['R20']['t_pad']; o['R19'] = r['R19']['t_pad']; o['R20'] = r['R20']['t_pad']
        o['plate_R19_minus_R20_c'] = r['R19']['under']['In1.Cu'] - r['R20']['under']['In1.Cu']
        h = R['th1a']['history']; o['dT_k'] = h[-1]['T_max_c'] - amb; o['th1a_R20'] = h[0]['R_mohm']; o['th1a_V'] = h[-1]['V_mV']; o['th1a_mean'] = h[-1]['T_mean_c']
        h = R['cp1a5']['history']; o['cp_h'] = h
        return o
    O = [items(J) for J in Rs]
    def rng(e): return f"{e['value']} ({e['range'][0]}〜{e['range'][1]})" if 'range' in e else str(e.get('value'))
    def jd(v, e): return ('合' if e['range'][0] <= v <= e['range'][1] else '**外**') if 'range' in e else ''
    L = ['| 区画 | 項目 | expected (範囲) | typ | worst | 判定 |', '|---|---|---|---|---|---|']
    e = TP['TH-LDO-VIA / TH-LDO-NOVIA']['expected']
    for k in ('U1_Tj_c', 'U2_Tj_c', 'U2_minus_U1_c', 'R10_minus_R8_c'):
        L.append(f"| TH-LDO | {k} | {rng(e[k])} | {O[0][k]:.2f} | {O[-1][k]:.2f} | {jd(O[-1][k], e[k])} |")
    e = TP['TH-DENSE / TH-SPREAD']['expected']
    for k in ('dense_pad_rise_k', 'spread_pad_rise_k', 'ratio_dense_over_spread'):
        L.append(f"| TH-DENSE/SPREAD | {k} | {rng(e[k])} | {O[0][k]:.3g} | {O[-1][k]:.3g} | {jd(O[-1][k], e[k])} |")
    pr = e['plate_rise_k']
    L.append(f"| TH-DENSE/SPREAD | (参考) 板 L2 dense / spread / 比 | {pr['dense']} / {pr['spread']} / {pr['ratio']} | {O[0]['plate_dense']:.1f} / {O[0]['plate_spread']:.1f} / {O[0]['plate_ratio']:.2f} | {O[-1]['plate_dense']:.1f} / {O[-1]['plate_spread']:.1f} / {O[-1]['plate_ratio']:.2f} | |")
    e = TP['TH-RELIEF / TH-SOLID']['expected']
    for k in ('R19_minus_R20_c', 'plate_R19_minus_R20_c'):
        L.append(f"| TH-RELIEF | {k} | {rng(e[k])} | {O[0][k]:.2f} | {O[-1][k]:.2f} | {jd(O[-1][k], e[k])} |")
    L.append(f"| TH-RELIEF | (参考) pad R19 / R20 ℃ | 65.0 / 58.1 | {O[0]['R19']:.1f} / {O[0]['R20']:.1f} | {O[-1]['R19']:.1f} / {O[-1]['R20']:.1f} | |")
    e = TP['TH-1A']['expected']
    L.append(f"| TH-1A | dT_k (配線最高 − 25) | {rng(e['dT_k'])} | {O[0]['dT_k']:.2f} | {O[-1]['dT_k']:.2f} | {jd(O[-1]['dT_k'], e['dT_k'])} |")
    L.append(f"| TH-1A | R20_mohm | {e['R20_mohm']} | {O[0]['th1a_R20']:.2f} | {O[-1]['th1a_R20']:.2f} | |")
    v = O[-1]['th1a_V']; ev = e['V_mV']['value']
    L.append(f"| TH-1A | V_mV (連成後、±2%) | {ev} | {O[0]['th1a_V']:.2f} | {v:.2f} | {'合' if abs(v / ev - 1) <= 0.02 else '**外**'} |")
    ce = CP['expected']; a_ = 0.00393
    for lab, f in (('uncoupled R_mohm (20 ℃、±2%)', lambda h: h[0]['R_mohm']), ('uncoupled V_mV', lambda h: h[0]['V_mV']), ('uncoupled P_w', lambda h: h[0]['P_w'])):
        key = lab.split()[1]; ev = ce['uncoupled'][key]; v = f(O[-1]['cp_h'])
        L.append(f"| CP-1A5 | {lab} | {ev} | {f(O[0]['cp_h']):.4g} | {v:.4g} | {'合' if abs(v / ev - 1) <= 0.02 else '**外**'} |")
    cr = ce['coupled_reference']
    for o_, nm in ((O[0], 'typ'), (O[-1], 'worst')):
        pass
    h = O[-1]['cp_h']; ht = O[0]['cp_h']
    L.append(f"| CP-1A5 | (参考) T_avg ℃ | {cr['T_avg_c']} ({cr['T_avg_range_c'][0]}〜{cr['T_avg_range_c'][1]}) | {ht[-1]['T_mean_c']:.2f} | {h[-1]['T_mean_c']:.2f} | {'範囲内' if cr['T_avg_range_c'][0] <= h[-1]['T_mean_c'] <= cr['T_avg_range_c'][1] else '範囲外(参考)'} |")
    L.append(f"| CP-1A5 | (参考) T_max ℃ | {cr['T_max_c']} | {ht[-1]['T_max_c']:.2f} | {h[-1]['T_max_c']:.2f} | |")
    L.append(f"| CP-1A5 | (参考) V_mV | {cr['V_mV']} ({cr['V_range_mV'][0]}〜{cr['V_range_mV'][1]}) | {ht[-1]['V_mV']:.2f} | {h[-1]['V_mV']:.2f} | {'範囲内' if cr['V_range_mV'][0] <= h[-1]['V_mV'] <= cr['V_range_mV'][1] else '範囲外(参考)'} |")
    # 合否 (1)(2)(3)
    def crit(h):
        Tm = [x['T_mean_c'] for x in h]; conv = len(h) <= 6 and abs(Tm[-1] - Tm[-2]) <= 0.1
        V25 = h[0]['V_mV'] * (1 + a_ * 5)  # 20 ℃ → 25 ℃ 一様
        ratio = h[-1]['V_mV'] / V25; th = (1 + a_ * (Tm[-1] - 20)) / (1 + a_ * 5); r2 = abs(ratio / th - 1) <= 0.01
        V = [x['V_mV'] for x in h]; dT = [Tm[i + 1] - Tm[i] for i in range(len(Tm) - 1)]; dV = [V[i + 1] - V[i] for i in range(len(V) - 1)]
        mono = all(x >= -1e-9 for x in dT + dV) and all(abs(dT[i + 1]) <= abs(dT[i]) + 1e-9 for i in range(len(dT) - 1)) and all(abs(dV[i + 1]) <= abs(dV[i]) + 1e-9 for i in range(len(dV) - 1))
        return conv, ratio, th, r2, mono
    for o_, nm in ((O[0], 'typ'), (O[-1], 'worst')):
        pass
    ct, cw = crit(ht), crit(h)
    L.append(f"| CP-1A5 | 合否(1) 収束 (≤6 回で 0.1 ℃) | ≤6 回 | {len(ht)} 回 {'合' if ct[0] else '外'} | {len(h)} 回 | {'合' if cw[0] else '**外**'} |")
    L.append(f"| CP-1A5 | 合否(2) 比 V/V25 vs ρ比 (1%) | 一致 | {ct[1]:.4f} / {ct[2]:.4f} | {cw[1]:.4f} / {cw[2]:.4f} | {'合' if cw[3] else '**外**'} |")
    L.append(f"| CP-1A5 | 合否(3) 単調収束 | 単調・増分減少 | {'合' if ct[4] else '外'} | {'合' if cw[4] else '外'} | {'合' if cw[4] else '**外**'} |")
    L += ['', '連成の反復 (worst):', '', '| k | 区画 | 配線最高 ℃ | 配線平均 ℃ | R mΩ | V mV | P W |', '|---|---|---|---|---|---|---|']
    J = Rs[-1]['results']
    for k in ('th1a', 'cp1a5'):
        L += [f"| {x['k']} | {k} | {x['T_max_c']:.2f} | {x['T_mean_c']:.2f} | {x['R_mohm']:.2f} | {x['V_mV']:.2f} | {x['P_w']:.4f} |" for x in J[k]['history']]
    print('\n'.join(L))

if __name__ == '__main__':
    main()
