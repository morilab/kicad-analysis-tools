"""段階 2: 設計・BOM (LCSC、Basic/Extended、品種の食い違い、ピン数、デカップリング、pull-up、水晶の負荷容量、
逆刺し・ピンずれ (DR-01)、電源予算 (DR-02))"""
import collections, glob, json, math, os, re
from common import Metric, judge, KB, HERE, is_power, is_gnd, parse_value
import misinsert, power_budget, derating

NON_PARTS = re.compile(r'^(#|PWR|FLG)')
SPLIT = re.compile(r'[-_/\s]+')
UNIT_SUFFIX = re.compile(r'^(V|MM|MHZ|PF|NF|UF|K|R|OHM)$', re.I)


def variant_conflicts(names):
    """names: {出所: 文字列}。共通の先頭トークンの次で「片方が他方の接頭辞」(1 と 1U など) なら食い違い"""
    toks = {k: [t.upper() for t in SPLIT.split(v) if t] for k, v in names.items() if v}
    out = []
    keys = list(toks)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = toks[keys[i]], toks[keys[j]]
            n = 0
            while n < min(len(a), len(b)) and a[n] == b[n]:
                n += 1
            if n == 0 or sum(len(t) for t in a[:n]) < 4 or n >= min(len(a), len(b)):
                continue
            x, y = a[n], b[n]
            s, l = (x, y) if len(x) < len(y) else (y, x)
            if s != l and l.startswith(s) and l[len(s):].isalpha() and not UNIT_SUFFIX.match(l[len(s):]):
                out.append(f'{keys[i]}="{names[keys[i]]}" と {keys[j]}="{names[keys[j]]}" ({x} ≠ {y})')
    return out


THT_FP = re.compile(r'PinHeader|PinSocket|TerminalBlock|THT|_Vertical$|_Horizontal$', re.I)
PITCH = re.compile(r'_P(\d+(?:\.\d+)?)mm')


def dnp_policy(comps, board_attr=None, min_pitch=2.54):
    """DR-04: DNP にしてよいのは スルーホール (THT) かつ ピッチ ≥2.54mm だけ。DNP 理由は部品表 (フィールド DNP理由) に書く。
    comps: {ref: {footprint, dnp, fields}}。board_attr: {ref: attr 文字列 (smd / through_hole)} があれば footprint 名より優先。
    返り値 [(ref, 種類, 内容)]。種類 = smd / pitch / reason"""
    out = []
    for r, c in sorted(comps.items()):
        if not c.get('dnp'):
            continue
        fp = c.get('footprint', '')
        a = (board_attr or {}).get(r, '')
        if 'smd' in a:
            tht = False
        elif 'through_hole' in a:
            tht = True
        else:
            tht = bool(THT_FP.search(fp)) and 'SMD' not in fp.upper()
        m = PITCH.search(fp)
        pitch = float(m.group(1)) if m else None
        if not tht:
            out.append((r, 'smd', f'{r} ({fp or "footprint なし"}): SMD の DNP。JLC で実装する (手はんだしない)。例外は実装費と別手配の送料を比べた理由を DNP理由 に'))
        elif pitch is not None and pitch < min_pitch - 1e-6:
            out.append((r, 'pitch', f'{r} ({fp}): THT だがピッチ {pitch}mm < {min_pitch}mm。手はんだしにくいので実装するか 2.54mm 品に'))
        if not (c.get('fields') or {}).get('DNP理由'):
            out.append((r, 'reason', f'{r}: 部品表に DNP理由 が無い (回路図のフィールド DNP理由 に書く)'))
    return out


def run(ctx):
    M = []; S = 'design'
    nl = ctx.get('netlist'); b = ctx.get('board'); cfg = ctx['cfg']
    rules = json.load(open(os.path.join(HERE, 'design_rules.json')))
    if not nl:
        return [Metric(S, 'D00', 'ネットリスト', None, 'skip', '', ['ネットリスト出力失敗'])]
    parts = {r: c for r, c in nl.comps.items() if not NON_PARTS.match(r)}
    bom = {r: c for r, c in parts.items() if not c['dnp'] and not c['exclude_bom']}
    # D01 LCSC
    miss = [r for r in bom if not nl.field(r, 'LCSC')]
    cov = 1 - len(miss) / len(bom) if bom else None
    M.append(Metric(S, 'D01_lcsc_coverage', 'BOM 部品の LCSC 番号の充足率', None if cov is None else round(cov, 3), judge(cov, 1.0, 1.0, high=True),
                    '100% (DNP と exclude_from_bom は除く)', miss[:12], 'netlist'))
    # D02 Basic / Extended
    cls = collections.defaultdict(set)
    for r in bom:
        cls[nl.field(r, 'JLCPCB') or '(未記入)'].add(nl.field(r, 'LCSC') or r)
    ext = sorted(cls.get('Extended', set()))
    p, w = cfg.get('extended_pass', 5), cfg.get('extended_warn', 10)
    fee = len(ext) * rules['extended_fee_usd']
    M.append(Metric(S, 'D02_extended_parts', 'Extended 部品の品種数 (部品ロード料)', len(ext), judge(len(ext), p, w),
                    f'≤{p} pass / ≤{w} warn (review.toml で変更可)',
                    [f'Basic {len(cls.get("Basic", ()))} 品種 / Extended {len(ext)} 品種 / 未記入 {len(cls.get("(未記入)", ()))}',
                     f'Extended ロード料の目安 ${fee:.0f} / 1 発注 (${rules["extended_fee_usd"]}/品種、要確認)'] +
                    [f"{r}: {nl.comps[r]['value']} {nl.field(r, 'LCSC')}" for r in bom if nl.field(r, 'JLCPCB') == 'Extended'][:12], 'netlist'))
    pe = sorted({f"{r} {nl.comps[r]['value']} ({nl.field(r, 'LCSC')})" for r in bom
                 if nl.field(r, 'JLCPCB') == 'Extended' and re.match(rules['passive_prefix'], r)})
    M.append(Metric(S, 'D03_extended_passives', 'Extended の受動部品 (Basic に置き換えられる候補)', len(pe), judge(len(pe), 0, 2),
                    '0 pass / ≤2 warn', pe[:12], 'netlist'))
    unk = sorted(r for r in bom if not nl.field(r, 'JLCPCB'))
    M.append(Metric(S, 'D04_jlc_class_field', 'JLCPCB (Basic/Extended) フィールド未記入', len(unk), judge(len(unk), 0, 0), '0', unk[:12], 'netlist'))
    M.append(Metric(S, 'D05_stock', 'LCSC 在庫確認', None, 'skip', 'オンライン時のみ (TODO)', ['オフライン実行。発注前に JLCPCB の部品検索で在庫を確認'], ''))
    # D06 品種の食い違い (symbol / Value / MPN / footprint / Description)
    mods = json.load(open(os.path.join(KB, 'tools', 'pcb-score', 'modules.json')))['modules']
    bad = []
    for r, c in parts.items():
        names = {'symbol': c['part'], 'Value': c['value'], 'MPN': nl.field(r, 'MPN'),
                 'footprint': c['footprint'].split(':')[-1]}
        for x in variant_conflicts(names):
            bad.append(f'{r}: {x}')
        for m in mods:
            if re.search(m['match'], c['footprint']):
                d = (c['description'] + ' ' + c['value']).lower()
                for wd in m.get('desc_forbid', []):
                    if wd.lower() in d:
                        bad.append(f'{r}: Description に "{wd}" (footprint は {m["name"]})')
    M.append(Metric(S, 'D06_variant_mismatch', '品種の食い違い (symbol / Value / MPN / footprint / Description) の部品数', len({x.split(':')[0] for x in bad}), 'fail' if bad else 'pass', '0',
                    bad[:12], 'netlist+modules.json'))
    # D07 シンボルのピン数と footprint の pad 数
    if b:
        bad = []
        for r, c in parts.items():
            f = b.fp(r)
            if not f or c['exclude_board']:
                continue
            sp = set(nl.pins(r))
            fp = {p.num for p in f.pads if p.num and p.type != 'np_thru_hole'}
            if not sp:
                continue
            miss_fp = sorted(sp - fp, key=lambda s: (len(s), s))
            extra = sorted(fp - sp, key=lambda s: (len(s), s))
            if miss_fp:
                bad.append(('fail', f'{r}: シンボルのピン {miss_fp[:8]} に対応する pad が無い'))
            if extra:
                # pad に net が付いていない余り pad (取付穴など) は warn
                bad.append(('warn', f'{r}: footprint の pad {extra[:8]} がシンボルに無い (シンボル {len(sp)} / pad {len(fp)})'))
        g = 'fail' if any(s == 'fail' for s, _ in bad) else 'warn' if bad else 'pass'
        M.append(Metric(S, 'D07_pin_pad_match', 'シンボルのピン番号と footprint の pad 番号', len(bad), g, '全一致 (pad の余りは warn)', [m for _, m in bad][:12], 'netlist+pcb'))
    # D08 デカップリング (IC の電源ピンの net に GND への C があるか)
    caps = collections.defaultdict(list)
    for r, c in parts.items():
        if r.startswith('C') and not c['dnp']:
            ns = [nl.pin_net.get((r, p)) for p in nl.pins(r)] or [n for (rr, p), n in nl.pin_net.items() if rr == r]
            ns = [n for n in ns if n]
            if len(ns) == 2 and any(is_gnd(n) for n in ns):
                caps[[n for n in ns if not is_gnd(n)][0] if not all(is_gnd(n) for n in ns) else ''].append(r)
    rows = []; worst = 'pass'
    for r, c in parts.items():
        if not r.startswith('U') or c['dnp']:
            continue
        pw = collections.Counter()
        for pnum, pi in nl.pins(r).items():
            n = nl.pin_net.get((r, pnum))
            if pi['type'] == 'power_in' and n and not is_gnd(n):
                pw[n] += 1
        for n, k in pw.items():
            nc = len(caps.get(n, []))
            g = 'fail' if nc == 0 else 'warn' if nc < math.ceil(k / 2) else 'pass'
            if g != 'pass':
                rows.append(f'{r} {n}: 電源ピン {k} 本 / C {nc} 個')
            worst = max(worst, g, key=['pass', 'warn', 'fail'].index)
    M.append(Metric(S, 'D08_decoupling', 'IC 電源ピンの net ごとのデカップリング C', len(rows), worst, 'C ≥ 1 かつ C ≥ ピン数/2 (net 単位)',
                    rows[:12], 'netlist'))
    # D09 pull-up (I2C, reset)
    def has_pull(n):
        for (rr, p), nn in nl.pin_net.items():
            if nn == n and rr.startswith('R'):
                other = [nl.pin_net.get((rr, q)) for q in ('1', '2') if q != p]
                if other and other[0] and is_power(other[0]):
                    return rr
        return None
    rows = []; worst = 'pass'
    i2c = [n for n in nl.nets if re.search(r'(^|/|_)(I2C_)?(SDA|SCL)\d*$', n, re.I) and len(nl.nets[n]) >= 2]
    for n in i2c:
        if not has_pull(n):
            rows.append(f'I2C {n}: pull-up 抵抗なし'); worst = 'fail'
    rp = re.compile(rules['reset_pin_names'], re.I)
    for r, c in parts.items():
        if not r.startswith('U'):
            continue
        for pnum, pi in nl.pins(r).items():
            if not pi['name'] or not rp.match(pi['name']):
                continue
            n = nl.pin_net.get((r, pnum))
            if not n or is_power(n) or n.startswith('unconnected-'):
                if n and n.startswith('unconnected-'):
                    if not any(re.search(ip['part'], c['part'] + c['value']) and pi['name'] in ip['pins'] for ip in rules['internal_pullup']):
                        rows.append(f'{r}.{pi["name"]}: 未接続 (内部 pull-up の記録なし)'); worst = max(worst, 'warn', key=['pass', 'warn', 'fail'].index)
                continue
            if not has_pull(n):
                ok = any(re.search(ip['part'], c['part'] + c['value']) and pi['name'] in ip['pins'] for ip in rules['internal_pullup'])
                if not ok:
                    rows.append(f'{r}.{pi["name"]} ({n}): pull-up 抵抗なし'); worst = max(worst, 'warn', key=['pass', 'warn', 'fail'].index)
    M.append(Metric(S, 'D09_pullups', 'I2C (SDA/SCL) と reset/EN の pull-up', len(rows), worst, 'I2C は必須 (fail)、reset は内部 pull-up の記録が無ければ warn',
                    rows[:12] or [f'I2C net {len(i2c)} 本'], 'netlist+design_rules.json'))
    # D10 水晶の負荷容量
    rows = []; worst = 'pass'; any_x = False
    cst = cfg.get('crystal', {}).get('cstray_pf', rules['cstray_pf'])
    for r, c in parts.items():
        if not (r.startswith('Y') or 'crystal' in (c['part'] + c['description']).lower()) or 'osc' in c['part'].lower():
            continue
        any_x = True
        mpn = nl.field(r, 'MPN')
        cl = None; src = ''
        tcl = cfg.get('crystal', {}).get(r, {}).get('cl_pf')
        if tcl:
            cl, src = tcl, 'review.toml'
        elif nl.field(r, 'CL'):
            cl, src = parse_value(nl.field(r, 'CL') + ('' if 'p' in nl.field(r, 'CL').lower() else 'p')) * 1e12, 'シンボルの CL フィールド'
        else:
            for x in rules['crystals']:
                if mpn and re.search(x['match_mpn'], mpn):
                    cl, src = x['cl_pf'], 'design_rules.json'
        sig = sorted({nl.pin_net.get((r, p)) for p in nl.pins(r)} - {None}, key=str)
        sig = [n for n in sig if not is_gnd(n)]
        lc = []
        for n in sig:
            cs = [cr for cr in caps.get(n, [])]
            v = [parse_value(nl.comps[cr]['value']) for cr in cs]
            v = [x for x in v if x]
            lc.append(v[0] * 1e12 if v else None)
        if cl is None or len(lc) != 2 or None in lc:
            rows.append(f'{r} {mpn}: CL 未記録 or 負荷 C 不明 (負荷 C {lc}) → design_rules.json / CL フィールドに記録すること')
            worst = 'warn' if worst == 'pass' else worst
            continue
        calc = lc[0] * lc[1] / (lc[0] + lc[1]) + cst
        d = abs(calc - cl)
        g = judge(d, 2.0, 4.0)
        worst = max(worst, g, key=['pass', 'warn', 'fail'].index)
        rows.append(f'{r} {mpn}: C={lc[0]:.0f}/{lc[1]:.0f}pF → {lc[0]:.0f}·{lc[1]:.0f}/({lc[0]:.0f}+{lc[1]:.0f})+{cst:.0f} = {calc:.1f}pF, CL={cl}pF ({src}), 差 {d:.1f}pF')
    M.append(Metric(S, 'D10_crystal_load', '水晶の負荷容量 C1·C2/(C1+C2)+Cstray と CL の差', None if not any_x else len(rows), worst if any_x else 'skip',
                    '|差| ≤2pF pass / ≤4pF warn (CL 未記録は warn)', rows or ['水晶なし (発振器は対象外)'], 'netlist+design_rules.json'))
    # D11 逆刺し・ピンずれ (DR-01): キーの無いピンヘッダ/ソケットの全ケース。キー付きは「キーで物理的に防止」= pass
    refs, skipped = misinsert.targets(nl, cfg)
    mi = cfg.get('misinsert', {})
    rows = []; worst = 'pass'; nbad = 0
    for r in refs:
        res = misinsert.analyze(nl, r, cfg.get('connector', {}).get(r), mi.get('vmax', 3.6), mi.get('imax_pass_ma', 25.0), mi.get('imax_warn_ma', 50.0))
        g = {'ok': 'pass', 'warn': 'warn', 'fail': 'fail'}[res['grade']]
        if res['roles_from'] != 'review.toml' and g == 'pass':
            g = 'warn'   # 推定の役割は人が確認しにくい。G1 では review.toml に roles を書く (DR-01 検証手順 1)
        worst = max(worst, g, key=['pass', 'warn', 'fail'].index)
        bad = [(c['name'], x) for c in res['cases'] for x in c['rows'] if x[6] != 'ok']
        nbad += sum(1 for _, x in bad if x[6] == 'fail')
        rows.append(f"{r} ({res['value']}): {g} (役割 {res['roles_from']}) " + ', '.join(f"{c['name']}={c['grade']}" for c in res['cases']))
        rows += [f"  {r} {cn}: 自 {x[0]} {x[1]} ↔ 相手 {x[3]} {x[4]}: {x[7]}" for cn, x in bad if x[6] == 'fail'][:6]
    keyed = misinsert.n_prevented(skipped)   # キー付き (review.toml keyed = 理由、または極性付き footprint) は「キーで物理的に防止」= ok
    M.append(Metric(S, 'D11_connector_misinsert', '逆刺し・±1 ピンずれで壊れないピン配置 (DR-01)', nbad if (refs or keyed) else None,
                    worst if (refs or keyed) else 'skip',
                    '単一の誤り (逆刺し・±1 ずれ) で 電源-GND 短絡 0・制限なしの出力衝突 0、直列 R で ≤25 mA pass / ≤50 mA warn。二重の誤りは warn まで',
                    (rows + skipped)[:16] or ['対象コネクタなし'], 'netlist+review.toml (misinsert.py で全ケース表)'))
    # D12 電源予算 (DR-02): 定格 ≥ 最悪 × 2、ドロップアウト、発熱
    g, rows, tables = power_budget.evaluate(nl, cfg)
    M.append(Metric(S, 'D12_power_budget', '電源予算: 余裕 200% 以上・ドロップアウト・発熱 (DR-02)',
                    min((round(T['margin'], 2) for T in tables if T.get('margin')), default=None), g,
                    '定格/最悪 ≥2.0 pass (≥1.5 warn)、vin_min-vout-dropout ≥0.2 V、Tj ≤ 上限-25 ℃。予算は review.toml [config.power]',
                    rows[:12], 'netlist+review.toml (power_budget.py で予算表)'))
    # D13 DNP の方針 (DR-04): SMD は DNP にしない、DNP は THT ≥2.54mm だけ、DNP 理由を部品表に
    battr = {f.ref: ' '.join(f.attr) if isinstance(f.attr, (list, tuple, set)) else str(f.attr) for f in b.footprints} if b else None
    dp = dnp_policy(parts, battr)
    ndnp = sum(1 for c in parts.values() if c['dnp'])
    M.append(Metric(S, 'D13_dnp_policy', 'DNP は THT ≥2.54mm だけ・SMD は JLC 実装・DNP 理由を部品表に (DR-04)', len(dp) if ndnp else None,
                    ('warn' if dp else 'pass') if ndnp else 'skip',
                    'DNP の SMD 0、2.54mm 未満の THT 0、DNP理由 の空欄 0 (あれば warn)',
                    [x[2] for x in dp][:12] or [f'DNP {ndnp} 個、全部 THT ≥2.54mm で DNP理由 あり' if ndnp else 'DNP なし'], 'netlist+pcb'))
    # D14 降格判定 (ディレーティング): C の耐圧・DC バイアス、R の電力、LDO の損失と Tj (温度マップがあれば使う)
    g, table, env = derating.evaluate(nl, cfg, ctx['dir'])
    open(os.path.join(ctx['out'], 'derating.md'), 'w').write(derating.to_md(ctx['name'], table, env))
    bad = [t for t in table if t['grade'] in ('warn', 'fail')]
    nest = sum(1 for t in table if '推定' in t['msg'])
    M.append(Metric(S, 'D14_derating', '降格判定: C 耐圧 (セラミック ≤50%・電解 ≤80%)・DC バイアス、R 電力 ≤50%、LDO 損失・Tj 余裕',
                    len(bad), g, 'C: セラミック V/Vr ≤0.5 pass ≤0.8 warn、電解 ≤0.8/≤0.9、タンタル ≤0.5/≤0.67、DC バイアス後 <50% warn。R: P/P定格 ≤0.5 pass ≤0.8 warn (70℃超は降格)。Tj 余裕 ≥25℃ pass ≥0 warn',
                    [t['msg'] for t in sorted(bad, key=lambda t: t['grade'] != 'fail')][:12] + [env, f'定格・容量変化に推定を含む行 {nest} 件 (表: derating.md)'], 'netlist+review.toml (derating.py)'))
    return M
