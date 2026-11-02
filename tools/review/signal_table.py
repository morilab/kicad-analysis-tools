#!/usr/bin/env python3
"""signal_table — ネットごとの信号特性表 (ドライバ部品とピン、tr、最大周波数、直列抵抗、出典、確度) を作る。

  python3 signal_table.py <project_dir> [--save] [--out FILE]

- 部品の特性: data/signal/parts/<LCSC>.yaml (値オブジェクト、熱データと同じ書き方) → 無ければ data/signal/families.yaml の推定。
- ドライバ: ネット上の IC (U*) の output / tri_state / bidirectional ピン。抵抗 (R*) を 2 段までたどり (電源・GND に行く抵抗は除く)、
  抵抗の先のドライバなら直列抵抗 = 経路の抵抗の和。候補が複数なら tr が最も速いもの (リターンパスの最悪)。
  抵抗 >2.2 kΩ は pull とみなしてたどらない。VREF/VCCIO/REF (静的) は表に入れない。発振ピン (OSC/XTAL) は crystal。スイッチ (SW*) のネットは switch_input。
  USB D+/D- は HS 部品 (design_rules.json hs_usb_parts) なら usb_hs、それ以外は usb_fs。ドライバが無くコネクタだけ = external。
- 実効 tr = √(tr² + (2.2 × R直列 × C負荷)²)。C負荷は families.yaml defaults.c_load_pf (既定 10 pF)。
- 確度: tr と f の basis がどちらも datasheet / design なら「正本」、それ以外は「仮」。
- --save: data/signal/boards/<基板>.yaml に書く (既存ファイルの overrides: は残し、最優先で使う)。
"""
import argparse, glob, json, math, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import KB, is_gnd, is_power, parse_value

SIG = os.path.join(KB, 'data', 'signal')


def _yaml():
    try:
        import yaml
    except ImportError:
        sys.exit('PyYAML が要る (pip install pyyaml)')
    return yaml


def V(x):
    return None if x is None else (x.get('value') if isinstance(x, dict) else x)


def src_text(x, ds=None):
    if not isinstance(x, dict):
        return '-'
    b = x.get('basis')
    if b == 'datasheet':
        s = x.get('src', {})
        d = f"{ds.get('title', '')} {ds.get('doc', '')} {ds.get('rev', '')}".strip() if ds else ''
        return f"{d} p.{s.get('page')} {s.get('table', '')}".strip()
    if b == 'assumed':
        return f"仮定: {x.get('reason', '')} ({x.get('calc', '')})"
    return f"{b}: {x.get('from', '')}"


def certain(x):
    return isinstance(x, dict) and x.get('basis') in ('datasheet', 'design')


class Library:
    def __init__(s):
        y = _yaml()
        s.parts = {}
        for f in glob.glob(os.path.join(SIG, 'parts', '*.yaml')):
            d = y.safe_load(open(f))
            s.parts[str(d.get('lcsc'))] = d
        F = y.safe_load(open(os.path.join(SIG, 'families.yaml')))
        s.fam = F['families']; s.defaults = F.get('defaults', {})

    def family_for(s, text):
        for k, f in s.fam.items():
            if f.get('match') and re.search(f['match'], text, re.I):
                return k
        return 'generic_cmos33'

    def spec(s, nl, ref, pin):
        """-> dict(tr, f, rout, src_tr, src_f, part, kind)"""
        c = nl.comps[ref]
        lcsc = nl.field(ref, 'LCSC'); text = ' '.join([c['value'], c['lib'], c['part'], nl.field(ref, 'MPN'), ref])
        P = s.parts.get(lcsc)
        if P:
            sg = P.get('signal', {}); e = dict(sg.get('default', {}))
            pe = (sg.get('pins') or {}).get(str(pin))
            if pe and pe.get('kind'):
                e = dict(s.fam[pe['kind']], **{k: v for k, v in pe.items() if k != 'kind'}); kind = pe['kind']
            elif pe:
                e.update(pe); kind = 'part'
            else:
                kind = 'part'
            ds = P.get('datasheet')
            return dict(tr=e.get('tr_ns'), f=e.get('f_max_mhz'), rout=e.get('r_out_ohm'), ds=ds if kind == 'part' else None,
                        where=f"data/signal/parts/{lcsc}.yaml" + (f" (pin {pin} → {kind})" if kind != 'part' else ''))
        k = s.family_for(text); e = s.fam[k]
        return dict(tr=e.get('tr_ns'), f=e.get('f_max_mhz'), rout=e.get('r_out_ohm'), ds=None, where=f'families.yaml {k} (部品 YAML なし)')


NOT_SIGNAL = r'VREF|VCCIO|(^|-)REF\)?$|VBAT|AVDD|AVCC'   # 基準電圧・アナログ電源 (静的) は除く
SERIES_MAX = 2200      # これより大きい抵抗は直列でなく pull とみなしてたどらない
OSC_PIN = r'OSC|XTAL|XI$|XO$|XIN|XOUT'
DRV = ('output', 'tri_state', 'bidirectional', 'open_collector', 'open_emitter', 'power_out')


def build(nl, name, rules=None, board_nets=None):
    L = Library()
    rules = rules or json.load(open(os.path.join(HERE, 'design_rules.json')))
    hs = any(re.search(rules['hs_usb_parts'], c['value'] + ' ' + c['part'], re.I) for c in nl.comps.values())
    cl = V(L.defaults['c_load_pf']) * 1e-12
    pin_net = nl.pin_net
    nets = {}
    for n, nodes in nl.nets.items():
        if not n or is_gnd(n) or is_power(n) or (board_nets is not None and n not in board_nets):
            continue
        short = n.split('/')[-1]
        if re.search(NOT_SIGNAL, short, re.I):
            continue
        if re.search(r'USB_?D[PNM]|(^|_)D[+-]', short, re.I):
            k = 'usb_hs' if hs else 'usb_fs'
            drv = [nd for nd in nodes if nd['ref'].startswith('U') and nd['type'] in DRV]
            e = L.fam[k]
            nets[n] = dict(driver=f"{drv[0]['ref']}.{drv[0]['pin']} ({drv[0]['func']})" if drv else '-', tr=e['tr_ns'], f=e.get('f_max_mhz'),
                           series=None, where=f'families.yaml {k}', ds=None)
            continue
        if any(nd['ref'].startswith('SW') for nd in nodes):
            e = L.fam['switch_input']
            nets[n] = dict(driver=f"スイッチ {', '.join(sorted({nd['ref'] for nd in nodes if nd['ref'].startswith('SW')}))}", tr=e['tr_ns'], f=e.get('f_max_mhz'),
                           series=None, where='families.yaml switch_input', ds=None)
            continue
        # 抵抗を 2 段までたどる
        cands = []
        seen = {n}; frontier = [(n, 0.0, [])]
        for depth in range(3):
            nxt = []
            for net, rs, path in frontier:
                for nd in nl.nets.get(net, []):
                    r = nd['ref']
                    if r.startswith(('U', 'Y', 'X')) and (nd['type'] in DRV or r[0] in 'YX'):
                        if r[0] in 'YX' or re.search(OSC_PIN, nd['func'] or '', re.I):
                            e = L.fam['crystal']; sp = dict(tr=e['tr_ns'], f=e.get('f_max_mhz'), rout=None, ds=None, where='families.yaml crystal')
                        else:
                            sp = L.spec(nl, r, nd['pin'])
                        cands.append((r, nd['pin'], nd['func'], rs, path, sp))
                    elif r.startswith('R') and depth < 2:
                        other = [pin_net.get((r, p)) for p in ('1', '2') if pin_net.get((r, p)) != net]
                        if len(other) == 1 and other[0] and other[0] not in seen and not is_gnd(other[0]) and not is_power(other[0]):
                            rv = parse_value(nl.comps[r]['value'].split()[0]) or 0
                            if rv > SERIES_MAX:
                                continue
                            seen.add(other[0]); nxt.append((other[0], rs + rv, path + [f"{r} {nl.comps[r]['value']}"]))
            if cands:
                break
            frontier = nxt
        if not cands:
            if any(nd['ref'].startswith(('J', 'P', 'CN')) for nd in nodes):
                e = L.fam['external']
                nets[n] = dict(driver='コネクタの先 (外部)', tr=e['tr_ns'], f=None, series=None, where='families.yaml external', ds=None)
            continue

        def eff(c):
            tr = V(c[5]['tr']) or 2.0
            return math.sqrt(tr ** 2 + (2.2 * c[3] * cl * 1e9) ** 2)
        c = min(cands, key=eff)
        nets[n] = dict(driver=f"{c[0]}.{c[1]} ({c[2]})", tr=c[5]['tr'], f=c[5]['f'], rout=c[5].get('rout'),
                       series=dict(ohm=c[3], via=c[4]) if c[3] > 0 else None, where=c[5]['where'], ds=c[5]['ds'])
    out = {}
    for n, e in nets.items():
        tr = V(e['tr']); rs = (e['series'] or {}).get('ohm', 0)
        tre = math.sqrt(tr ** 2 + (2.2 * rs * cl * 1e9) ** 2)
        out[n] = dict(driver=e['driver'], tr_ns=e['tr'], tr_eff_ns=round(tre, 3), f_max_mhz=e['f'],
                      series_r=({'ohm': rs, 'path': ' → '.join(e['series']['via'])} if e['series'] else None),
                      source=f"{e['where']}: tr {src_text(e['tr'], e['ds'])}" + (f" / f {src_text(e['f'], e['ds'])}" if e['f'] else ''),
                      certainty='正本' if certain(e['tr']) and (e['f'] is None or certain(e['f'])) else '仮')
    return out, dict(c_load_pf=L.defaults['c_load_pf'], v_mm_per_ns=L.defaults['v_mm_per_ns'], hs_usb=hs)


def merge_overrides(table, name):
    p = os.path.join(SIG, 'boards', f'{name}.yaml')
    if not os.path.exists(p):
        return table, None
    y = _yaml(); d = y.safe_load(open(p)) or {}
    for n, e in (d.get('overrides') or {}).items():
        if n in table:
            table[n].update(e); table[n]['certainty'] = e.get('certainty', '正本' if certain(e.get('tr_ns')) else '仮'); table[n]['source'] = 'overrides: ' + src_text(e.get('tr_ns'))
            tr = V(table[n]['tr_ns']); rs = (table[n].get('series_r') or {}).get('ohm', 0)
            table[n]['tr_eff_ns'] = round(math.sqrt(tr ** 2 + (2.2 * rs * 10e-12 * 1e9) ** 2), 3)
    return table, d.get('overrides')


def save(table, meta, name, overrides=None):
    y = _yaml()
    p = os.path.join(SIG, 'boards', f'{name}.yaml')
    doc = {'board': name, 'generated_by': 'tools/review/signal_table.py (手で直す値は overrides: に書く。nets: は再生成で上書き)',
           'defaults': {k: v for k, v in meta.items()}, 'overrides': overrides or {}, 'nets': table}
    with open(p, 'w') as f:
        f.write('# ネットごとの信号特性 (リターンパス P20/P21 の点数に使う)。形式は data/signal/README.md\n')
        y.safe_dump(doc, f, allow_unicode=True, sort_keys=False, width=200)
    return p


def table_for(nl, name, board=None):
    bn = None
    if board is not None:
        bn = {t['net'] for t in board.tracks + board.arcs} | {v['net'] for v in board.vias}
    t, meta = build(nl, name, board_nets=bn)
    t, ov = merge_overrides(t, name)
    return t, meta, ov


def main():
    from common import Netlist, export_netlist
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir'); ap.add_argument('--save', action='store_true'); ap.add_argument('--out')
    a = ap.parse_args()
    pdir = os.path.abspath(a.project_dir)
    name = os.path.splitext(os.path.basename(glob.glob(os.path.join(pdir, '*.kicad_pro'))[0]))[0]
    x = f'/tmp/signal-{name}.xml'
    ok, err = export_netlist(os.path.join(pdir, name + '.kicad_sch'), x)
    if not ok:
        print(err, file=sys.stderr); return 2
    sys.path.insert(0, os.path.join(KB, 'tools', 'pcb-score')); import kpcb
    b = kpcb.Board(os.path.join(pdir, name + '.kicad_pcb'))
    t, meta, ov = table_for(Netlist(x), name, b)
    for n, e in sorted(t.items()):
        print(f"{n:28} {e['driver'][:34]:34} tr {V(e['tr_ns'])} → {e['tr_eff_ns']} ns  Rs {(e['series_r'] or {}).get('ohm', '-')}  {e['certainty']}")
    if a.save:
        print('wrote', save(t, meta, name, ov))
    if a.out:
        _yaml().safe_dump(t, open(a.out, 'w'), allow_unicode=True, sort_keys=False)


if __name__ == '__main__':
    sys.exit(main())
