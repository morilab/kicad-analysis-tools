#!/usr/bin/env python3
"""derating — 降格判定 (ディレーティング、D14)。

  python3 derating.py <project_dir> [--md out.md] [--thermal map.json]

元データ: 部品表 (回路図フィールド) の定格 → 無ければ MPN の型番規則 → 値・footprint から推定 (「推定」と明記)。
印加電圧: ネット名から (VBUS = 5.5 V 最悪、+3V3 = 3.3 V、+1V8 = 1.8 V、+12V など)。review.toml [config.derating.net_v] で上書き。
温度: 周囲温度の仮定 [config.derating] ta = 50 (既定)。熱解析 (pcb-physics) の温度マップ JSON があれば部品ごとの温度を使う:
  - 置き場所: review.toml [config.derating] thermal_map = "review/thermal/map.json"、無ければ <project>/review/thermal*.json の一番新しいもの
  - 形式 (案): {"ambient_c": 40, "parts": {"U3": {"t_c": 78.2}, "R5": {"t_c": 55}}, "source": "...", "date": "..."}
    parts に無い部品は ambient_c (無ければ ta)。キー名は t_c / temp_c / max_c のどれか。

判定:
  コンデンサ耐圧: セラミック V/Vr ≤0.5 pass / ≤0.8 warn / 超 fail。電解・ポリマー ≤0.8 / ≤0.9 / 超。タンタル ≤0.5 / ≤0.67 / 超。
  DC バイアス (クラス II セラミック X5R/X7R、推定): 実効容量 <50% で warn (値は推定、メーカーの特性図で確認)。
  抵抗の電力: P = V²/R (片側が電源・他方が GND / 不明信号 = 最悪の短絡想定、両側が信号なら判定しない)、
    定格は 70 ℃ 超で 155 ℃ に向けて直線降格。P/P定格 ≤0.5 pass / ≤0.8 warn / 超 fail。0 Ω は電流が要るので info。
  半導体 Tj: レギュレータは review.toml [config.power] (DR-02) の損失 (Vin_max-Vout)×I と θJA で Tj = T周囲 + Pd·θJA。
    余裕 (Tj上限 - Tj) ≥25 ℃ pass / ≥0 warn / 未満 fail。予算の無い半導体は info (電流が不明)。
  LDO の損失は同じ式の Pd を表に出す。
"""
import argparse, glob, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import is_gnd, is_power, parse_value

RANK = ['pass', 'warn', 'fail']
def worst(*g): return max(g, key=RANK.index)

# Samsung CL 型番の電圧記号 (CL10A106KP8NNNC: 10 番目 P = 10 V)
SAMSUNG_V = {'R': 4, 'Q': 6.3, 'P': 10, 'O': 16, 'A': 25, 'L': 35, 'B': 50, 'C': 100, 'D': 200, 'E': 250}
SAMSUNG_DIEL = {'A': 'X5R', 'B': 'X7R', 'C': 'C0G', 'F': 'Y5V', 'X': 'X6S'}
# 抵抗のサイズ別定格 (推定、厚膜一般品)
R_POWER = {'0201': 0.05, '0402': 0.0625, '0603': 0.1, '0805': 0.125, '1206': 0.25, '1210': 0.5, '2010': 0.75, '2512': 1.0}
# セラミックの推定耐圧 (容量・サイズからの典型、推定)
def guess_cap_v(c, size):
    if c is None: return None
    if c <= 1e-9: return 50
    if c <= 100e-9: return 16 if size in ('0201', '0402') else 50
    if c <= 1e-6: return 16 if size in ('0201', '0402') else 25
    if c <= 10e-6: return 6.3 if size in ('0201', '0402') else 10 if size == '0603' else 16
    return 6.3 if size in ('0402', '0603') else 10


def size_of(fp):
    m = re.search(r'_(0201|0402|0603|0805|1206|1210|1812|2010|2512)_', fp + '_')
    return m.group(1) if m else ''


def net_voltage(net, over=None):
    if not net: return None
    n = net.split('/')[-1]
    if over and n in over: return float(over[n])
    if over and net in over: return float(over[net])
    if is_gnd(net): return 0.0
    if re.search(r'VBUS|USB_5V', n, re.I): return 5.5        # USB VBUS 最悪 5.5 V
    m = re.search(r'(\d+)V(\d+)', n) or re.search(r'(\d+)\.(\d+)V', n)
    if m: return float(f'{m.group(1)}.{m.group(2)}')
    m = re.search(r'\+?(\d+)V\b', n + ' ')
    if m: return float(m.group(1))
    return None


def rating_from_fields(nl, ref, keys):
    for k in keys:
        v = nl.field(ref, k)
        if v:
            m = re.search(r'([\d.]+)\s*(k|m)?', v)
            if m:
                x = float(m.group(1)); return x * {'k': 1e3, 'm': 1e-3, None: 1}[m.group(2)] if 'W' in v.upper() or 'V' in v.upper() else x
    return None


def cap_info(nl, ref, c):
    """-> (Vr, 種類, 誘電体, 出所)"""
    mpn = nl.field(ref, 'MPN'); fp = c['footprint']; size = size_of(fp)
    desc = (c['description'] + ' ' + c['lib'] + ':' + c['part'] + ' ' + fp).lower()
    kind = 'elec' if re.search(r'cp_elec|elec|polarized|aluminum', desc) and 'unpolarized' not in desc else 'tant' if 'tantal' in desc else 'ceramic'
    v = None
    for k in ('Voltage', '耐圧', 'Rated Voltage', 'V'):
        s = nl.field(ref, k)
        m = re.search(r'([\d.]+)\s*V', s or '')
        if m: return float(m.group(1)), kind, None, f'部品表 {k}'
    m = re.match(r'^CL(\d\d)([A-Z])(\d{3})([A-Z])([A-Z])', mpn or '')
    if m and m.group(5) in SAMSUNG_V:
        return SAMSUNG_V[m.group(5)], kind, SAMSUNG_DIEL.get(m.group(2)), f'MPN {mpn} (Samsung 型番規則)'
    m = re.match(r'^(\d{4})(CG|NP0|X7R|X5R)(\d{3})([A-Z])(\d{3})', mpn or '')     # FH 0402CG330J500NT
    if m:
        vv = int(m.group(5)[:2]) * 10 ** int(m.group(5)[2]) / 10
        return vv, kind, 'C0G' if m.group(2) in ('CG', 'NP0') else m.group(2), f'MPN {mpn} (FH 型番規則)'
    m = re.search(r'(\d+(?:\.\d+)?)V', c['value'])
    if m: return float(m.group(1)), kind, None, '値の表記'
    return guess_cap_v(parse_value(c['value']), size), kind, None, f'推定 (容量 {c["value"]}・{size or "サイズ不明"} の典型)'


def dc_bias_left(v, vr, size, cval, diel):
    """クラス II の実効容量の残り (推定、0..1)。V/Vr とサイズ・容量密度から。C0G は 1"""
    if diel == 'C0G' or not cval or not vr or cval < 10e-9:
        return 1.0
    # 経験式 (推定): 損失 = a·√(V/Vr)。a = 1.0 (≥1 µF の高誘電率品)、0.35 (<1 µF)。X7R は ×0.85、0402 以下は ×1.1
    # 目安: CL05A475MP5 (4.7µ/10V/0402) 3.3V で残り約 40%、100n X7R 16V 3.3V で約 85%
    a = 1.0 if cval >= 1e-6 else 0.35
    if diel == 'X7R': a *= 0.85
    if size in ('0201', '0402'): a *= 1.1
    return max(0.15, 1 - min(0.85, a * (v / vr) ** 0.5))


def load_thermal(pdir, dcfg):
    p = dcfg.get('thermal_map')
    if p and not os.path.isabs(p): p = os.path.join(pdir, p)
    if not p:
        c = sorted(glob.glob(os.path.join(pdir, 'review', 'thermal*.json')) + glob.glob(os.path.join(pdir, 'review', 'thermal', '*.json')), key=os.path.getmtime)
        p = c[-1] if c else None
    if p and os.path.exists(p):
        try:
            return json.load(open(p)), p
        except Exception:
            return None, p
    return None, None


def part_temp(ref, tm, ta):
    if not tm: return ta, '仮定'
    e = (tm.get('parts') or {}).get(ref) or {}
    for k in ('t_c', 'temp_c', 'max_c'):
        if k in e: return float(e[k]), '温度マップ'
    return float(tm.get('ambient_c', ta)), '温度マップ周囲'


def evaluate(nl, cfg, pdir):
    dc = cfg.get('derating', {}); ta = float(dc.get('ta', 50)); over = dc.get('net_v', {})
    tm, tmp = load_thermal(pdir, dc)
    rows = []; g = 'pass'; table = []
    pinnets = {}
    for (r, pin), n in nl.pin_net.items():
        pinnets.setdefault(r, {})[pin] = n
    for r, c in sorted(nl.comps.items(), key=lambda x: (re.sub(r'\d', '', x[0]), int(re.sub(r'\D', '', x[0]) or 0))):
        if c['dnp'] or r.startswith('#'): continue
        nets = list(pinnets.get(r, {}).values())
        T, tsrc = part_temp(r, tm, ta)
        if re.match(r'^C\d', r) and len(nets) == 2:
            vs = [net_voltage(n, over) for n in nets]
            if None in vs:
                table.append(dict(ref=r, kind='C', grade='info', msg=f'{r} {c["value"]}: 印加電圧不明 ({", ".join(nets)})')); continue
            v = abs(vs[0] - vs[1]); vr, kind, diel, src = cap_info(nl, r, c)
            if not vr: continue
            lim = {'ceramic': (0.5, 0.8), 'elec': (0.8, 0.9), 'tant': (0.5, 0.67)}[kind]
            ratio = v / vr
            gg = 'pass' if ratio <= lim[0] else 'warn' if ratio <= lim[1] else 'fail'
            msg = f'{r} {c["value"]} ({kind}{"/" + diel if diel else ""}): {v:g} V / 定格 {vr:g} V = {ratio * 100:.0f}% (基準 ≤{lim[0] * 100:.0f}%) [{src}]'
            if kind == 'ceramic':
                left = dc_bias_left(v, vr, size_of(c['footprint']), parse_value(c['value']), diel)
                if left < 0.5 and gg == 'pass': gg = 'warn'
                if left < 0.8:
                    msg += f'、DC バイアスで実効容量 約 {left * 100:.0f}% (推定、メーカー特性図で確認)'
            table.append(dict(ref=r, kind='C', grade=gg, msg=msg, ratio=ratio))
        elif re.match(r'^R\d', r) and len(nets) == 2:
            R = parse_value(c['value'].split()[0])
            size = size_of(c['footprint'])
            pr = None; psrc = ''
            for k in ('Power', '電力', 'Wattage'):
                m = re.search(r'([\d.]+)\s*(m?)W', nl.field(r, k) or '')
                if m: pr = float(m.group(1)) * (1e-3 if m.group(2) else 1); psrc = f'部品表 {k}'; break
            if pr is None and size in R_POWER: pr = R_POWER[size]; psrc = f'推定 ({size} 厚膜の一般定格)'
            if R is None or pr is None:
                table.append(dict(ref=r, kind='R', grade='info', msg=f'{r} {c["value"]}: 値か定格が不明')); continue
            if R == 0:
                table.append(dict(ref=r, kind='R', grade='info', msg=f'{r} 0Ω: 電力は電流 (I²R・定格電流) で見る。電流の記録なし')); continue
            vs = [net_voltage(n, over) for n in nets]
            if all(x is None for x in vs) or (vs[0] is None and vs[1] in (None, 0.0)) or (vs[1] is None and vs[0] in (None, 0.0)):
                table.append(dict(ref=r, kind='R', grade='info', msg=f'{r} {c["value"]}: 両端が信号 (電圧不明、ドライバで電流制限) → 判定なし')); continue
            v = abs((vs[0] or 0) - (vs[1] or 0))
            note = '' if None not in vs else ' (片側は信号 = Low 短絡の最悪想定)'
            der = 1.0 if T <= 70 else max(0.0, (155 - T) / 85)
            p = v * v / R; ratio = p / (pr * der) if der else 9
            gg = 'pass' if ratio <= 0.5 else 'warn' if ratio <= 0.8 else 'fail'
            table.append(dict(ref=r, kind='R', grade=gg, ratio=ratio,
                              msg=f'{r} {c["value"]}: {v:g}²/{R:g} = {p * 1000:.1f} mW / 定格 {pr * 1000:.1f} mW{"×" + format(der, ".2f") + " (" + format(T, ".0f") + "℃)" if der < 1 else ""} = {ratio * 100:.0f}% (基準 ≤50%){note} [{psrc}]'))
    # 半導体: レギュレータは [config.power]
    P = cfg.get('power', {})
    regs = set()
    for rail, e in P.items():
        reg = e.get('regulator')
        if not reg: continue
        regs.add(reg)
        if not all(k in e for k in ('vin_max', 'vout', 'theta_ja')):
            table.append(dict(ref=reg, kind='U', grade='warn', msg=f'{reg}: [config.power."{rail}"] に vin_max/vout/theta_ja が無い')); continue
        tot = sum(float(l['ma']) for l in e.get('loads', []))
        pd = (float(e['vin_max']) - float(e['vout'])) * tot / 1000
        Tamb, src = part_temp(reg, tm, float(e.get('ta_max', ta)))
        if src == '温度マップ':
            tj = Tamb + pd * float(e.get('theta_jc', 0) or 0) if e.get('theta_jc') else Tamb   # マップの部品温度 ≒ ケース温度
        else:
            tj = Tamb + pd * float(e['theta_ja'])
        tjm = float(e.get('tj_max', 125)); m = tjm - tj
        gg = 'pass' if m >= 25 else 'warn' if m >= 0 else 'fail'
        table.append(dict(ref=reg, kind='U', grade=gg, msg=f'{reg} ({rail} LDO): 損失 ({e["vin_max"]}-{e["vout"]}) V × {tot:.1f} mA = {pd:.3f} W、Tj = {Tamb:.0f} ℃ ({src}) + {pd:.3f} × {e["theta_ja"]} = {tj:.0f} ℃、余裕 {m:.0f} ℃ (基準 ≥25 ℃)'))
    semis = [r for r in nl.comps if re.match(r'^(U|Q|D)\d', r) and r not in regs and not nl.comps[r]['dnp']]
    if semis:
        table.append(dict(ref='-', kind='U', grade='info', msg=f'Tj 未計算 (消費電力の記録なし): {", ".join(sorted(semis, key=lambda x: (x[0], int(re.sub(r"\D", "", x)))))}。小信号・ロジックは通常問題なし。電力の大きいものは [config.power] か温度マップで'))
    for t in table:
        g = worst(g, t['grade']) if t['grade'] in RANK else g
    env = f'温度: {"温度マップ " + tmp if tm else "周囲温度の仮定 " + format(ta, "g") + " ℃ (温度マップなし)"}'
    return g, table, env


def to_md(name, table, env):
    L = [f'## 降格判定 (ディレーティング) ({name}、tools/review/derating.py)', '', f'- {env}', '', '| 判定 | 部品 | 内容 |', '|---|---|---|']
    L += [f"| {t['grade']} | {t['ref']} | {t['msg']} |" for t in sorted(table, key=lambda t: (-['info', 'pass', 'warn', 'fail'].index(t['grade']), t['kind']))]
    return '\n'.join(L) + '\n'


def main():
    from common import Netlist, export_netlist
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir'); ap.add_argument('--md'); ap.add_argument('--thermal')
    a = ap.parse_args()
    pdir = os.path.abspath(a.project_dir)
    name = os.path.splitext(os.path.basename(glob.glob(os.path.join(pdir, '*.kicad_pro'))[0]))[0]
    cfg = {}
    if os.path.exists(os.path.join(pdir, 'review.toml')):
        import tomllib
        cfg = tomllib.load(open(os.path.join(pdir, 'review.toml'), 'rb')).get('config', {})
    if a.thermal: cfg.setdefault('derating', {})['thermal_map'] = os.path.abspath(a.thermal)
    x = f'/tmp/derating-{name}.xml'
    ok, err = export_netlist(os.path.join(pdir, name + '.kicad_sch'), x)
    if not ok: print(err, file=sys.stderr); return 2
    g, table, env = evaluate(Netlist(x), cfg, pdir)
    md = to_md(name, table, env); print(g); print(md)
    if a.md: open(a.md, 'w').write(md)
    return 1 if g == 'fail' else 0


if __name__ == '__main__':
    sys.exit(main())
