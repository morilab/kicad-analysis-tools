#!/usr/bin/env python3
"""misinsert — 逆刺し・ピンずれの全ケース表 (設計ルール DR-01、skills/design-rules/SKILL.md)。

  python3 misinsert.py <project_dir> [--ref J2,J3] [--md out.md] [--vmax 3.6]

キーの無いピンヘッダ・ピンソケット (逆刺し・1 ピンずれが物理的に可能なもの) について、相手 (ケーブル側) が
「同じピン配置の信号」を運んでくる前提で、逆刺し・±1 ずれ (単一の誤り) と 逆刺し+±1 (二重の誤り) の
全ピンの組を表にして判定する。kreview の D11 もこのモジュールを使う。

ピンの役割 (自基板側):
  gnd   GND
  sup   自基板が出す電源 (+3V3、VBUS など)
  vref  相手の電源を受ける基準電圧 (VREF / VTREF。自基板は駆動しない)
  out   自基板の出力 (相手の入力)          in    自基板の入力 (相手の出力)
  od    自基板のオープンドレイン出力         bidir 双方向 (相手も駆動しうる → 相手=出力 とみなす)
  nc    未接続
役割は review.toml の [config.connector.<ref>] roles = {"1"="gnd", ...} で与える (推奨)。無ければネットリストから推定する。
直列抵抗 (コネクタのピンと IC の間の R) の値はネットリストから読む。
"""
import argparse, glob, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import is_gnd, is_power, parse_value

HEADER_FP = re.compile(r'PinHeader|PinSocket|Pin_Header|Socket_Strip|Pin_Socket', re.I)
KEYED_FP = re.compile(r'Box|Shrouded|IDC|JST|Molex|USB|Hirose|Wurth_WR-BHD', re.I)
VREF_NET = re.compile(r'(^|/)(VREF|VTREF|VTARGET|VT_REF|VIO_REF|VREF_\w+|\w+_VREF)$', re.I)
# 相手 (ケーブル側) がそのピンに出すもの: 自基板の役割の相補
PEER = {'gnd': 'gnd', 'sup': 'sup', 'vref': 'sup', 'out': 'in', 'od': 'in', 'in': 'out', 'bidir': 'out', 'nc': 'nc'}
ROLE_JA = {'gnd': 'GND', 'sup': '電源(出)', 'vref': 'VREF(受)', 'out': '出力', 'od': 'OD出力', 'in': '入力', 'bidir': '双方向', 'nc': 'NC'}
PEER_JA = {'gnd': 'GND', 'sup': '電源', 'in': '入力', 'out': '出力', 'nc': 'NC'}
RANK = {'ok': 0, 'warn': 1, 'fail': 2}
DOUBLE = ('逆刺し +1 ずれ', '逆刺し -1 ずれ', '逆刺し 列 +1 ずれ', '逆刺し 列 -1 ずれ')


def geometry(npin, rows):
    """ケース名 -> 写像 (自基板のピン番号 -> 相手のピン番号 or None)。rows=1: 1xN、rows=2: 2xN (KiCad Odd_Even 番号)"""
    N = npin
    if rows == 1:
        f = {'逆刺し': lambda i: N + 1 - i, '+1 ずれ': lambda i: i + 1, '-1 ずれ': lambda i: i - 1,
             '逆刺し +1 ずれ': lambda i: N + 2 - i, '逆刺し -1 ずれ': lambda i: N - i}
    else:
        f = {'逆刺し (180 度)': lambda i: N + 1 - i, '列 +1 ずれ': lambda i: i + 2, '列 -1 ずれ': lambda i: i - 2,
             '行ずれ (片列だけ嵌合)': lambda i: i + 1 if i % 2 else None,
             '逆刺し 列 +1 ずれ': lambda i: N + 3 - i, '逆刺し 列 -1 ずれ': lambda i: N - 1 - i}
    C = {}
    for k, fn in f.items():
        C[k] = {}
        for i in range(1, N + 1):
            j = fn(i)
            C[k][i] = j if j is not None and 1 <= j <= N else None
    return C


def judge_pair(a, t, r_ohm, vmax, ipass, iwarn):
    """自基板の役割 a と、相手がそこに出すもの t -> (判定 ok/warn/fail, 理由)"""
    if a == 'nc' or t == 'nc':
        return 'ok', ''
    if a == 'gnd':
        if t == 'sup':
            return 'fail', '電源-GND 短絡'
        if t == 'out':
            return 'fail', '相手の出力を GND に短絡 (電流制限なし)'
        return 'ok', ''
    if a == 'sup':
        if t == 'gnd':
            return 'fail', '電源-GND 短絡'
        if t == 'out':
            return 'fail', '相手の出力と自基板の電源が衝突 (電流制限なし)'
        if t == 'sup':
            return 'warn', '電源同士の接続 (逆流のおそれ)'
        return 'warn', '相手の入力に自基板の電源 (保護ダイオードへ注入、制限なし)'
    if a == 'vref':
        if t == 'out':
            return 'warn', '相手の出力で VREF を給電 (相手出力の負荷)'
        if t == 'gnd':
            return 'ok', 'VREF = 0 V → 変換 IC 停止 (Hi-Z)'
        return 'ok', ''
    if a == 'in':
        if t in ('in', 'out'):
            return 'ok', '入力同士' if t == 'in' else ''
        if t == 'gnd':
            return 'ok', '入力が GND (電流なし)'
        if r_ohm:
            return 'ok', f'入力に相手の電源、直列 {r_ohm:g} ohm で {vmax / r_ohm * 1000:.0f} mA 以下'
        return 'warn', '入力に相手の電源 (直列抵抗なし)'
    # 自基板の出力 (out / od / bidir)
    if t == 'in':
        return 'ok', ''
    if r_ohm:
        i = vmax / r_ohm * 1000
        g = 'ok' if i <= ipass else 'warn' if i <= iwarn else 'fail'
        what = {'gnd': 'GND', 'sup': '相手の電源', 'out': '相手の出力'}[t]
        return g, f'{what}と衝突、直列 {r_ohm:g} ohm で {i:.0f} mA 以下'
    return 'fail', {'gnd': '出力が GND に短絡', 'sup': '出力が相手の電源に短絡', 'out': '出力同士の衝突'}[t] + ' (直列抵抗なし)'


def series_r(nl, net, ref):
    """コネクタのピン net に直列に入る R (他端が電源/GND/VREF でない R) の最小値 [ohm] と他端の net"""
    best = None; far = []
    for nd in nl.nets.get(net, []):
        r = nd['ref']
        if r == ref or not re.match(r'^R\d', r):
            continue
        other = [nl.pin_net.get((r, p)) for p in ('1', '2') if p != nd['pin']]
        o = other[0] if other else None
        if not o or is_power(o) or is_gnd(o) or VREF_NET.search(o):
            continue    # pull-up / pull-down
        v = parse_value(nl.comps[r]['value'].split()[0])
        if v and (best is None or v < best):
            best = v
        far.append(o)
    return best, far


def guess_role(nl, ref, pin):
    net = nl.pin_net.get((ref, pin))
    if not net or net.startswith('unconnected-') or len(nl.nets.get(net, [])) <= 1:
        return 'nc'
    if is_gnd(net):
        return 'gnd'
    if VREF_NET.search(net):
        return 'vref'
    if is_power(net):
        return 'sup'
    r, far = series_r(nl, net, ref)
    types = set()
    for n in (far or [net]):
        for nd in nl.nets.get(n, []):
            if nd['ref'] == ref or re.match(r'^(R|C|L|FB|D)\d', nd['ref']):
                continue
            if re.match(r'^Q\d', nd['ref']):
                types.add('od'); continue
            types.add(nd['type'].split('+')[0])
    if 'od' in types:
        return 'od'
    if types & {'output', 'tri_state', 'power_out'}:
        return 'out'
    if 'bidirectional' in types or 'passive' in types or not types:
        return 'bidir'
    return 'in'


def analyze(nl, ref, conf=None, vmax=3.6, ipass=25.0, iwarn=50.0):
    conf = conf or {}
    pins = [p for p in nl.pins(ref)] or sorted({p for (r, p) in nl.pin_net if r == ref})
    pins = sorted([p for p in pins if str(p).isdigit()], key=int)
    fp = nl.comps[ref]['footprint']
    m = re.search(r'_(\d)x(\d+)', fp) or re.search(r'_(\d)x(\d+)', nl.comps[ref]['part'])
    rows = int(m.group(1)) if m else 1
    roles_cfg = {str(k): v for k, v in conf.get('roles', {}).items()}
    roles, rr, names = {}, {}, {}
    for p in pins:
        net = nl.pin_net.get((ref, p)) or ''
        names[p] = re.sub(r'^/', '', net) if net and not net.startswith('unconnected-') else '(NC)'
        roles[p] = roles_cfg.get(p) or guess_role(nl, ref, p)
        rr[p] = series_r(nl, net, ref)[0] if net and roles[p] in ('out', 'od', 'in', 'bidir') else None
    dnp = bool(nl.comps[ref].get('dnp'))   # 未実装 (拡張端子など): 電源-GND 短絡以外の fail は warn
    cases = []; worst = 'ok'
    for cname, mp in geometry(len(pins), rows).items():
        double = cname in DOUBLE
        out = []
        for p in pins:
            q = mp[int(p)]
            if q is None:
                out.append((p, names[p], roles[p], '-', '(空き)', 'nc', 'ok', '')); continue
            q = str(q)
            t = PEER[roles[q]]
            g, why = judge_pair(roles[p], t, rr[p], vmax, ipass, iwarn)
            if double and g == 'fail':
                g = 'warn'; why += ' [二重の誤り]'
            elif dnp and g == 'fail' and '電源-GND' not in why:
                g = 'warn'; why += ' [未実装 DNP: 手はんだした時だけ起きる]'   # 電源-GND 短絡は DNP でも fail のまま
            out.append((p, names[p], roles[p], q, names[q], t, g, why))
            worst = max(worst, g, key=RANK.get)
        cases.append(dict(name=cname, double=double, rows=out, grade=max((r[6] for r in out), key=RANK.get)))
    return dict(ref=ref, value=nl.comps[ref]['value'], footprint=fp, pins=pins, roles=roles, series_r=rr, names=names,
                cases=cases, grade=worst, vmax=vmax, ipass=ipass, iwarn=iwarn, roles_from='review.toml' if roles_cfg else '推定', dnp=dnp)


PREVENTED = 'キーで物理的に防止'


def targets(nl, cfg):
    """検査対象: footprint がピンヘッダ/ソケットでキー付きでないもの (または review.toml に roles があるもの)。
    keyed = "理由" (シュラウド付き box header、JST などの極性付き、規格コネクタ) は全ケース表を作らず「キーで物理的に防止」= ok とする"""
    cc = cfg.get('connector', {})
    out, skipped = [], []
    for r, c in sorted(nl.comps.items()):
        if not re.match(r'^(J|P|CN|CON)\d', r):
            continue
        conf = cc.get(r, {})
        if conf.get('keyed'):
            skipped.append(f"{r}: {PREVENTED} ({conf['keyed']})"); continue
        if conf.get('roles') or (HEADER_FP.search(c['footprint']) and not KEYED_FP.search(c['footprint'])):
            out.append(r)
        elif KEYED_FP.search(c['footprint']):
            skipped.append(f"{r}: {PREVENTED} (嵌合方向が決まる footprint: {c['footprint'].split(':')[-1]})")
        else:
            skipped.append(f"{r}: 対象外 (ピンヘッダ以外: {c['footprint'].split(':')[-1]})")
    return out, skipped


def n_prevented(skipped):
    """targets() の skipped のうち、キーで物理的に防止 (ok) と判定したものの数"""
    return sum(1 for x in skipped if PREVENTED in x)


def to_md(res):
    L = [f"### {res['ref']} ({res['value']}, {res['footprint'].split(':')[-1]}): **{res['grade']}**", '',
         f"- 役割の出所: {res['roles_from']}。判定: 電源-GND 短絡なし、GND/電源と相手の出力の衝突なし、出力の衝突は直列抵抗で "
         f"{res['vmax']:g} V / R ≤ {res['ipass']:g} mA (≤ {res['iwarn']:g} mA は warn)。二重の誤り (逆刺し+ずれ) の fail は warn 扱い。"
         + (" **未実装 (DNP)**: 電源-GND 短絡以外の fail は warn 扱い (手はんだした時だけ起きる)。" if res.get('dnp') else ''), '',
         '| pin | 信号 | 役割 | 直列 R |', '|---|---|---|---|']
    for p in res['pins']:
        r = res['series_r'][p]
        L.append(f"| {p} | {res['names'][p]} | {ROLE_JA[res['roles'][p]]} | {('%g ohm' % r) if r else '-'} |")
    L += ['', '| ケース | 判定 | 要注意の組 |', '|---|---|---|']
    for c in res['cases']:
        bad = [f"自 {p} {n} ↔ 相手 {q} {qn}: {why}" for p, n, a, q, qn, t, g, why in c['rows'] if g != 'ok']
        L.append(f"| {c['name']}{' (二重)' if c['double'] else ''} | {c['grade']} | {'<br>'.join(bad) or '-'} |")
    for c in res['cases']:
        L += ['', f"#### {c['name']}{' (二重の誤り)' if c['double'] else ''}: {c['grade']}", '',
              '| 自 pin | 自信号 (役割) | 相手 pin | 相手信号 (相手が出すもの) | 判定 | 理由 |', '|---|---|---|---|---|---|']
        for p, n, a, q, qn, t, g, why in c['rows']:
            L.append(f"| {p} | {n} ({ROLE_JA[a]}) | {q} | {qn} ({PEER_JA.get(t, t)}) | {g} | {why} |")
    return '\n'.join(L) + '\n'


def run_project(pdir, refs=None, vmax=None):
    from common import Netlist, export_netlist
    name = os.path.splitext(os.path.basename(glob.glob(os.path.join(pdir, '*.kicad_pro'))[0]))[0]
    cfg = {}
    if os.path.exists(os.path.join(pdir, 'review.toml')):
        import tomllib
        cfg = tomllib.load(open(os.path.join(pdir, 'review.toml'), 'rb')).get('config', {})
    x = f'/tmp/misinsert-{name}.xml'
    ok, err = export_netlist(os.path.join(pdir, name + '.kicad_sch'), x)
    if not ok:
        raise SystemExit(err)
    nl = Netlist(x)
    t, skipped = targets(nl, cfg)
    mi = cfg.get('misinsert', {})
    vm = vmax or mi.get('vmax', 3.6)
    res = [analyze(nl, r, cfg.get('connector', {}).get(r), vm, mi.get('imax_pass_ma', 25.0), mi.get('imax_warn_ma', 50.0))
           for r in (refs or t)]
    return name, res, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir'); ap.add_argument('--ref'); ap.add_argument('--md'); ap.add_argument('--vmax', type=float)
    a = ap.parse_args()
    name, res, skipped = run_project(os.path.abspath(a.project_dir), a.ref.split(',') if a.ref else None, a.vmax)
    md = [f'## 逆刺し・ピンずれの全ケース表 ({name}、tools/review/misinsert.py で生成)', '']
    md += [f'- {s}' for s in skipped]
    worst = 'ok'
    for r in res:
        worst = max(worst, r['grade'], key=RANK.get)
        md += ['', to_md(r)]
        print(f"{r['ref']}: {r['grade']}  " + ', '.join(f"{c['name']}={c['grade']}" for c in r['cases']))
    if a.md:
        open(a.md, 'w').write('\n'.join(md) + '\n'); print('wrote', a.md)
    return {'ok': 0, 'warn': 0, 'fail': 1}[worst]


if __name__ == '__main__':
    sys.exit(main())
