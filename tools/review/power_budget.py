#!/usr/bin/env python3
"""power_budget — 電源の予算表と判定 (設計ルール DR-02、skills/design-rules/SKILL.md)。

  python3 power_budget.py <project_dir> [--md out.md]

review.toml の [config.power."<rail>"] に、レギュレータの定格 (データシート値) と負荷の最大電流を書く:

  [config.power."+3V3"]
  regulator = "U3"              # 部品番号 (ネットリストと照合)
  mpn = "AP7361C-33E-13"
  rating_ma = 1000              # 定格出力電流
  vout = 3.3
  vin_min = 4.40                # 入力の最低 (USB VBUS は 4.40 V)
  vin_max = 5.50                # 入力の最高 (発熱の計算)
  dropout_mv = 140              # データシートの最大値と、その時の電流
  dropout_at_ma = 300
  theta_ja = 110                # 接合-周囲 熱抵抗 (データシート、最小パッド)
  tj_max = 125                  # 設計上の上限 (絶対最大ではなく推奨動作)
  ta_max = 50                   # 周囲温度の想定上限
  source = "AP7361C データシート Rev. 2020-10"
  loads = [ { ref = "U1", ma = 180, note = "FT2232H ..." }, ... ]   # ref はネットリストに無くてもよい (プルアップ等は "R*")

判定 (DR-02): 余裕 = 定格 / 最悪電流 ≥ 2.0 (200%) pass、≥ 1.5 warn、未満 fail。
  ドロップアウト: vin_min - vout - dropout ≥ 0.2 V pass、≥ 0 warn、未満 fail。
  発熱: Tj = ta_max + (vin_max - vout) × I × θJA ≤ tj_max - 25 pass、≤ tj_max warn、超 fail。
  レギュレータに予算が無い → warn。レール上の IC が loads に無い → warn。
"""
import argparse, glob, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import is_gnd

REG = re.compile(r'Regulator|LDO|AMS1117|AP7361|XC6206|ME6211|LDL1117|RT9013|TLV7|SPX3819|LM1117|NCP1117|AP2112|HT7833|MCP1700', re.I)
RANK = ['pass', 'warn', 'fail']


def worst(*g):
    return max(g, key=RANK.index)


def evaluate(nl, cfg):
    """-> (grade, rows[str], tables[dict])"""
    P = cfg.get('power', {})
    rows, tables, g = [], [], 'pass'
    regs = sorted(r for r, c in nl.comps.items() if r.startswith('U') and REG.search(c['lib'] + ':' + c['part'] + ' ' + c['value']))
    covered = {v.get('regulator') for v in P.values()}
    for r in regs:
        if r not in covered:
            rows.append(f'{r} ({nl.comps[r]["value"]}): 電源予算が review.toml [config.power] に無い'); g = worst(g, 'warn')
    if not regs and not P:
        return 'skip', ['レギュレータなし'], []
    for rail, e in P.items():
        loads = e.get('loads', [])
        tot = sum(float(l['ma']) for l in loads)
        T = dict(rail=rail, entry=e, total_ma=tot, checks=[])
        reg = e.get('regulator')
        if reg and reg in nl.comps:
            mpn = nl.field(reg, 'MPN') or nl.comps[reg]['value']
            if e.get('mpn') and e['mpn'] != mpn:
                T['checks'].append(('fail', f'{reg} の MPN が回路図 "{mpn}" と予算表 "{e["mpn"]}" で違う'))
        elif reg:
            T['checks'].append(('fail', f'{reg} が回路図に無い'))
        rating = float(e.get('rating_ma', 0))
        m = rating / tot if tot else None
        gm = 'fail' if not m else 'pass' if m >= 2.0 else 'warn' if m >= 1.5 else 'fail'
        T['margin'] = m
        T['checks'].append((gm, f'電流余裕 {rating:.0f} mA / 最悪 {tot:.1f} mA = {m * 100:.0f}% (基準 200% 以上)' if m else '負荷 0'))
        if all(k in e for k in ('vin_min', 'vout', 'dropout_mv')):
            at = float(e.get('dropout_at_ma', rating))
            do = float(e['dropout_mv']) / 1000 * (max(1.0, tot / at) if at else 1.0)
            head = float(e['vin_min']) - float(e['vout']) - do
            gd = 'pass' if head >= 0.2 else 'warn' if head >= 0 else 'fail'
            T['headroom'] = head
            T['checks'].append((gd, f'ドロップアウト: 入力最低 {e["vin_min"]} V - 出力 {e["vout"]} V - dropout {do * 1000:.0f} mV (@{at:.0f} mA 最大値) = 余裕 {head:.2f} V (基準 0.2 V 以上)'))
        else:
            T['checks'].append(('warn', 'vin_min / vout / dropout_mv が無い (ドロップアウト未確認)'))
        if all(k in e for k in ('vin_max', 'vout', 'theta_ja')):
            pd = (float(e['vin_max']) - float(e['vout'])) * tot / 1000
            ta = float(e.get('ta_max', 50)); tjm = float(e.get('tj_max', 125))
            tj = ta + pd * float(e['theta_ja'])
            gt = 'pass' if tj <= tjm - 25 else 'warn' if tj <= tjm else 'fail'
            T['pd'] = pd; T['tj'] = tj
            T['checks'].append((gt, f'発熱: ({e["vin_max"]} - {e["vout"]}) V × {tot:.1f} mA = {pd:.3f} W、Tj = {ta:.0f} + {pd:.3f} × {e["theta_ja"]} = {tj:.0f} ℃ (基準 {tjm - 25:.0f} ℃ 以下 = 上限 {tjm:.0f} - 25)'))
        else:
            T['checks'].append(('warn', 'vin_max / theta_ja が無い (発熱未確認)'))
        # レール上の IC が loads に載っているか
        names = {l['ref'] for l in loads}
        net_ics = set()
        for n, nodes in nl.nets.items():
            if re.sub(r'^/', '', n) != rail.lstrip('/'):
                continue
            for nd in nodes:
                if nd['ref'].startswith('U') and nd['ref'] != reg and nd['type'].startswith('power_in'):
                    net_ics.add(nd['ref'])
        miss = sorted(net_ics - names)
        if miss:
            T['checks'].append(('warn', f'{rail} に電源ピンがあるのに予算表に無い IC: {", ".join(miss)}'))
        for gg, msg in T['checks']:
            g = worst(g, gg)
            if gg != 'pass':
                rows.append(f'{rail}: {msg}')
        rows.append(f'{rail}: ' + ' / '.join(msg.split(':')[0] + ' ' + gg for gg, msg in T['checks'][:3]))
        tables.append(T)
    return g, rows, tables


def to_md(T):
    e = T['entry']
    L = [f"### {T['rail']} ({e.get('regulator', '?')} {e.get('mpn', '')})", '',
         f"- 定格 {e.get('rating_ma')} mA、出力 {e.get('vout')} V、入力 {e.get('vin_min')}〜{e.get('vin_max')} V、dropout 最大 {e.get('dropout_mv')} mV @{e.get('dropout_at_ma')} mA、"
         f"θJA {e.get('theta_ja')} ℃/W、Tj 上限 {e.get('tj_max', 125)} ℃、周囲 {e.get('ta_max', 50)} ℃。出典: {e.get('source', '-')}", '',
         '| 負荷 | 最大電流 (mA) | 根拠 |', '|---|---|---|']
    for l in e.get('loads', []):
        L.append(f"| {l['ref']} | {float(l['ma']):g} | {l.get('note', '')} |")
    L.append(f"| **合計 (最悪)** | **{T['total_ma']:.1f}** | |")
    L += ['', '| 判定 | 項目 |', '|---|---|'] + [f'| {g} | {m} |' for g, m in T['checks']]
    return '\n'.join(L) + '\n'


def main():
    from common import Netlist, export_netlist
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir'); ap.add_argument('--md')
    a = ap.parse_args()
    pdir = os.path.abspath(a.project_dir)
    name = os.path.splitext(os.path.basename(glob.glob(os.path.join(pdir, '*.kicad_pro'))[0]))[0]
    cfg = {}
    if os.path.exists(os.path.join(pdir, 'review.toml')):
        import tomllib
        cfg = tomllib.load(open(os.path.join(pdir, 'review.toml'), 'rb')).get('config', {})
    x = f'/tmp/power-{name}.xml'
    ok, err = export_netlist(os.path.join(pdir, name + '.kicad_sch'), x)
    if not ok:
        print(err, file=sys.stderr); return 2
    g, rows, tables = evaluate(Netlist(x), cfg)
    print(g); print('\n'.join(rows))
    if a.md:
        open(a.md, 'w').write(f'## 電源予算 ({name}、tools/review/power_budget.py で生成)\n\n' + '\n'.join(to_md(T) for T in tables)); print('wrote', a.md)
    return 1 if g == 'fail' else 0


if __name__ == '__main__':
    sys.exit(main())
