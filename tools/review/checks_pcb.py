"""段階 3: PCB (配置の詰まり、DRC 必須規則、干渉、3D、USB 差動、デカップリング距離、水晶、via-in-pad)"""
import collections, json, math, os, re, sys
from common import Metric, judge, KB, HERE, is_power, is_gnd
sys.path.insert(0, os.path.join(KB, 'tools', 'pcb-score'))
import kpcb, pcb_score, pcb_interfere as pi
import return_path
import board_rules

ORDER = ['pass', 'warn', 'fail']
def worse(a, b): return max(a, b, key=ORDER.index)

# 既定の積層 (JLCPCB 標準、表層から最寄りの GND 面までの誘電体厚 mm と εr)
STACKUP = {2: dict(h=1.53, er=4.5, note='JLC 2 層 1.6 mm (FR-4)'), 4: dict(h=0.2104, er=4.4, note='JLC 4 層 1.6 mm JLC04161H-7628 (PP 0.2104 mm)')}
T_CU = 0.035


def zdiff_ms(w, s, h, er, t=T_CU):
    """IPC-2141 の edge-coupled microstrip 近似 (GND ベタの共平面効果は入れない → 2 層では高めに出る)"""
    z0 = 87 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))
    return 2 * z0 * (1 - 0.48 * math.exp(-0.96 * s / h)), z0


def _seg_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def usb_pairs(b):
    names = {t['net'] for t in b.tracks + b.arcs}
    pairs = []
    for n in names:
        m = re.match(r'^(.*?)(D\+|DP|_P)(_\w+)?$', n.split('/')[-1])
        if not m or not re.search(r'USB|^D\+$|^DP$', n.split('/')[-1], re.I):
            continue
        for neg in ('D-', 'DN', 'DM', '_N'):
            cand = n[:len(n) - len(n.split('/')[-1])] + m.group(1) + neg + (m.group(3) or '')
            if cand in names:
                pairs.append((n, cand)); break
    return sorted(pairs)


def run(ctx):
    M = []; S = 'pcb'
    b = ctx['board']; cfg = ctx['cfg']; out = ctx['out']; nl = ctx.get('netlist')
    if not b:
        return [Metric(S, 'P00', 'PCB', None, 'skip', '', ['.kicad_pcb なし'])]
    # --- 配置の詰まり (pcb_score)
    R = pcb_score.score(b.path, 0.05, tuple(cfg.get('reserve', [])))
    ctx['pcb_score'] = R
    G = R.get('grades', {})
    sc = R.get('score')
    M.append(Metric(S, 'P01_layout_score', 'pcb_score 合成点 (density/empty/far_empty/margin/detour/via)', sc, judge(sc, 70, 50, high=True), '≥70 pass / ≥50 warn',
                    [f"{k}={v['value']} ({v['grade']})" for k, v in G.items()] + [f"縮小目安: 密度 40% なら {R.get('area_at_target')} mm² (現 {R.get('board_area')} mm²)"] +
                    ([f"予約層 {cfg.get('reserve')}"] if cfg.get('reserve') else []), 'pcb_score'))
    for k, title in (('density', '部品密度 (courtyard 和 / 外形)'), ('far_empty_ratio', '2 mm 以上離れた空きの割合 (間延び)')):
        if k in G:
            t = pcb_score.THRESHOLDS[k]
            M.append(Metric(S, f'P02_{k}', title, G[k]['value'], G[k]['grade'], f"{'≥' if t[0] == 'high' else '≤'}{t[1]} pass / {t[2]} warn", [], 'pcb_score'))
    # --- DRC (必須規則を適用した /tmp コピー)
    rules, ignored, vals = pi.audit_rules(ctx['pro']) if os.path.exists(ctx['pro']) else ([], [], {})
    weak = [f"{r['rule']}={r['effective']} (要 {r['required']})" for r in rules if not r['ok']]
    M.append(Metric(S, 'P03_rule_audit', '必須 DRC 規則の重大度 (.kicad_pro)', len(weak), 'fail' if weak else 'pass', '弱いもの 0',
                    weak + ([f'ignore: {ignored}'] if ignored else []), 'pcb_interfere.audit_rules'))
    d = pi.run_drc(b, os.path.join(out, 'drc-strict'), strict=True, parity=True)
    ctx['drc'] = d
    if 'error' in d:
        M.append(Metric(S, 'P04_drc', 'DRC', None, 'skip', '', [d['error'][:200]], 'kicad-cli'))
    else:
        cnt = collections.Counter(); sev = collections.Counter()
        for k, vs in d['violations'].items():
            for v in vs:
                cnt[(k, v['severity'])] += 1; sev[v['severity']] += 1
        for v in d.get('parity', []):
            cnt[('parity:' + v.get('type', '?'), v.get('severity', 'error'))] += 1; sev[v.get('severity', 'error')] += 1
        detail = [f'{k} ({s}): {n}' for (k, s), n in sorted(cnt.items(), key=lambda x: -x[1])]
        detail.append(f"回路図との照合 (--schematic-parity): {len(d.get('parity', []))} 件" if d.get('parity_checked') else '回路図との照合: 未実施 (.kicad_sch なし)')
        detail += [f"parity {v.get('type')}: {v.get('description', '')[:60]} {', '.join(i.get('description', '')[:30] for i in v.get('items', [])[:2])}" for v in d.get('parity', [])[:8]]
        M.append(Metric(S, 'P04_drc', 'DRC 違反 (必須規則適用・回路図との照合込み、error / warning)', f"{sev['error']}/{sev['warning']}",
                        'fail' if sev['error'] else 'warn' if sev['warning'] else 'pass', 'error 0 / warning 0', detail, 'kicad-cli pcb drc'))
        sop = len(d['violations'].get('silk_over_copper', []))
        M.append(Metric(S, 'P05_silk_over_copper', 'silk が pad (銅・mask 開口) に掛かる', sop, judge(sop, 0, 0), '0',
                        [f"{', '.join(i.get('description', '')[:50] for i in v.get('items', [])[:2])}" for v in d['violations'].get('silk_over_copper', [])[:8]], 'DRC silk_over_copper'))
        cy = sum(len(d['violations'].get(k, [])) for k in ('courtyards_overlap', 'missing_courtyard', 'malformed_courtyard', 'pth_inside_courtyard', 'npth_inside_courtyard'))
        M.append(Metric(S, 'P06_courtyard', 'courtyard 重なり・欠落', cy, judge(cy, 0, 0), '0', [], 'DRC'))
        M.append(Metric(S, 'P07_unconnected', '未配線 (unconnected_items)', d['unconnected'], judge(d['unconnected'], 0, 0), '0', [], 'DRC'))
    # --- 干渉 (pcb_interfere)
    refs = pi.ref_checks(b)
    M.append(Metric(S, 'P08_refdes_placement', 'Reference 文字の位置 (基板外 / 他部品の方が近い)', len(refs), judge(len(refs), 0, 2), '0 pass / ≤2 warn',
                    [f"{x['ref']}: {x['msg']}" for x in refs][:10], 'pcb_interfere.ref_checks'))
    body = pi.body_checks(b)
    nomodel = [x for x in body if '3D モデル' in x['issue'] and 'はみ出す' not in x['issue']]
    over = [x for x in body if 'はみ出す' in x['issue'] and '3D' in x['issue']]
    cyiss = [x for x in body if x['sev'] == 'error']
    M.append(Metric(S, 'P09_3d_missing', '3D モデルが無い部品 (3D の干渉確認ができない)', len(nomodel), judge(len(nomodel), 0, 3), '0 pass / ≤3 warn',
                    sorted({f"{x['ref']} ({x['fp'].split(':')[-1]})" for x in nomodel})[:12], 'pcb_interfere.body_checks'))
    M.append(Metric(S, 'P10_3d_vs_courtyard', '3D モデル外形が courtyard からはみ出す (向き・offset 誤り)', len(over), judge(len(over), 0, 0), '0 (>0.3 mm を検出)',
                    [f"{x['ref']}: {x['issue']}" for x in over][:10], 'pcb_interfere.body_checks'))
    M.append(Metric(S, 'P11_fab_vs_courtyard', 'Fab 外形が courtyard からはみ出す / courtyard 無し', len(cyiss), judge(len(cyiss), 0, 0), '0',
                    [f"{x['ref']}: {x['issue']}" for x in cyiss][:10], 'pcb_interfere.body_checks'))
    mods = pi.module_checks(b, json.load(open(os.path.join(KB, 'tools', 'pcb-score', 'modules.json')))['modules'])
    mf = [(fnd['sev'], f"{m['ref']} {m['module']}: {fnd['msg']}") for m in mods for fnd in m['findings']]
    g = 'fail' if any(s == 'error' for s, _ in mf) else 'warn' if mf else ('pass' if mods else 'skip')
    M.append(Metric(S, 'P12_module_keepout', 'モジュール実外形・keepout と他部品 / 説明文の品種', len(mf), g, 'error 0、warning 0',
                    [x for _, x in mf] or [f"{m['ref']} {m['module']} OK" for m in mods] or ['modules.json に該当なし'], 'pcb_interfere.module_checks'))
    mt = pi.mating_checks(b, json.load(open(os.path.join(KB, 'tools', 'pcb-score', 'connectors.json')))['connectors'])
    bad = [x for x in mt if x['sev'] == 'error']
    M.append(Metric(S, 'P13_connector_mating', 'コネクタの嵌合方向 (相手 plug の領域)', len(bad), ('fail' if bad else 'pass') if mt else 'skip', '0',
                    [f"{x['ref']}: {x['msg']}" for x in (bad or mt)][:8], 'pcb_interfere.mating_checks'))
    # --- USB 差動
    pairs = usb_pairs(b)
    hs = bool(nl and any(re.search(json.load(open(os.path.join(HERE, 'design_rules.json')))['hs_usb_parts'], c['value'] + c['part'], re.I) for c in nl.comps.values()))
    hs = cfg.get('hs_usb', hs)
    st = dict(STACKUP.get(len(b.copper), STACKUP[2])); st.update(cfg.get('stackup', {}))
    rows_l = []; rows_z = []; gl = 'pass'; gz = 'pass'
    for p, n in pairs:
        tp = [t for t in b.tracks + b.arcs if t['net'] == p]; tn = [t for t in b.tracks + b.arcs if t['net'] == n]
        lp = sum(kpcb.arc_length(t['pts']) for t in tp); ln = sum(kpcb.arc_length(t['pts']) for t in tn)
        dl = abs(lp - ln)
        lp_, lw_ = (1.0, 2.5) if hs else (5.0, 10.0)
        g = judge(dl, lp_, lw_); gl = worse(gl, g)
        rows_l.append(f'{p}/{n}: {lp:.2f} / {ln:.2f} mm, 差 {dl:.2f} mm ({g})')
        # 結合部の gap と幅 (P の track 上の点から同層の N track までの距離)
        gaps = []; coupled = 0.0; total = 0.0
        for t in tp:
            for a, c in zip(t['pts'], t['pts'][1:]):
                L = math.hypot(c[0] - a[0], c[1] - a[1]); k = max(1, int(L / 0.1))
                for i in range(k):
                    q = (a[0] + (c[0] - a[0]) * (i + .5) / k, a[1] + (c[1] - a[1]) * (i + .5) / k)
                    dd = [(_seg_dist(q, u, v) - (t['w'] + s['w']) / 2) for s in tn if s['layer'] == t['layer'] for u, v in zip(s['pts'], s['pts'][1:])]
                    total += L / k
                    if dd and min(dd) < 0.5:
                        gaps.append(min(dd)); coupled += L / k
        if not gaps:
            rows_z.append(f'{p}/{n}: 結合区間なし'); continue
        gaps.sort(); s_ = gaps[len(gaps) // 2]
        w = collections.Counter(round(t['w'], 3) for t in tp).most_common(1)[0][0]
        zd, z0 = zdiff_ms(w, s_, st['h'], st['er'])
        dev = abs(zd - 90) / 90
        g = judge(dev, 0.10, 0.20)
        if not hs:
            g = 'info'
        elif g == 'fail' and coupled < cfg.get('usb_short_mm', 15):
            g = 'warn'          # 短い (立上り 500 ps の 1/5 程度以下) なら実害小
        if g in ORDER:
            gz = worse(gz, g)
        rows_z.append(f'{p}/{n}: w={w} gap={s_:.3f} mm 結合 {coupled:.1f}/{total:.1f} mm → Zdiff≈{zd:.0f} Ω (Z0 {z0:.0f}, {st["note"]}) ({g})')
    if pairs:
        M.append(Metric(S, 'P14_usb_length_skew', 'USB D+/D- の配線長の差', len(rows_l), gl, 'HS: ≤1.0 mm pass / ≤2.5 mm warn、FS: ≤5 / ≤10 mm', rows_l + [f"{'HS' if hs else 'FS'} として判定"], 'kpcb'))
        M.append(Metric(S, 'P15_usb_zdiff', 'USB 差動インピーダンス概算 (IPC-2141 microstrip、HS のみ判定)', len(rows_z), gz if hs else 'info',
                        '90 Ω ±10% pass / ±20% warn (HS のみ。結合 <15 mm は warn 止まり。GND 共平面は未考慮 = 2 層は高めに出る)',
                        rows_z + [f"HS USB 部品: {'あり' if hs else 'なし (FS は参考値)'}"], 'kpcb'))
    else:
        M.append(Metric(S, 'P14_usb_length_skew', 'USB D+/D- の配線長の差', None, 'skip', '', ['USB 差動 net が見つからない'], ''))
    # --- デカップリング C の距離、水晶の距離、via-in-pad、EP の thermal via
    if nl:
        caps_by_net = collections.defaultdict(list)
        for f in b.footprints:
            if re.match(r'^C\d', f.ref) and 'dnp' not in f.attr:
                nets = {p.net for p in f.pads}
                if len(nets) == 2 and any(is_gnd(x) for x in nets):
                    for p in f.pads:
                        if not is_gnd(p.net):
                            caps_by_net[p.net].append((f.ref, p))
        rows = []; worst = 'pass'; npins = 0
        for f in b.footprints:
            if not f.ref.startswith('U') or f.ref not in nl.comps:
                continue
            pins = nl.pins(f.ref)
            for p in f.pads:
                pi_ = pins.get(p.num)
                if not pi_ or pi_['type'] != 'power_in' or not p.net or is_gnd(p.net):
                    continue
                cs = caps_by_net.get(p.net, [])
                npins += 1
                if not cs:
                    continue          # C が無いのは D08 で fail にしている
                dist, cref = min((math.hypot(cp.x - p.x, cp.y - p.y), cr) for cr, cp in cs)
                g = judge(dist, cfg.get('decap_pass_mm', 3.0), cfg.get('decap_warn_mm', 6.0))
                worst = worse(worst, g)
                if g != 'pass':
                    rows.append(f'{f.ref}.{p.num} ({pi_["name"]}, {p.net}): 最寄り {cref} {dist:.1f} mm ({g})')
        M.append(Metric(S, 'P16_decap_distance', 'IC 電源ピン → 最寄りのデカップリング C の pad 中心距離', len(rows), worst if npins else 'skip',
                        '≤3 mm pass / ≤6 mm warn (ピンごと、最悪値で判定)', rows[:12] or [f'電源ピン {npins} 本すべて ≤3 mm'], 'kpcb+netlist'))
        rows = []; worst = 'pass'; anyx = False
        for f in b.footprints:
            if not f.ref.startswith('Y'):
                continue
            anyx = True
            for p in f.pads:
                if not p.net or is_gnd(p.net):
                    continue
                ics = [(math.hypot(q.x - p.x, q.y - p.y), g_.ref, q.num) for g_ in b.footprints if g_.ref.startswith('U') for q in g_.pads if q.net == p.net]
                if not ics:
                    continue
                dmin = min(ics)
                g = judge(dmin[0], 5.0, 10.0); worst = worse(worst, g)
                rows.append(f'{f.ref}.{p.num} → {dmin[1]}.{dmin[2]} {dmin[0]:.1f} mm ({g})')
        if anyx:
            M.append(Metric(S, 'P17_crystal_distance', '水晶 pad → IC の発振ピンの距離', len(rows), worst, '≤5 mm pass / ≤10 mm warn', rows, 'kpcb'))
    vip = []; ep_rows = []; ep_warn = False
    for f in b.footprints:
        for p in f.pads:
            if p.type != 'smd' or not p.num:
                continue
            poly = p.poly()
            inside = [v for v in b.vias if kpcb.point_in_poly(v['at'][0], v['at'][1], poly)]
            area = p.w * p.h
            if area >= 4.0:          # exposed pad など: thermal via は意図的
                if p.net and is_gnd(p.net) and f.ref.startswith('U'):
                    ep_rows.append(f'{f.ref}.{p.num} ({p.w:.1f}×{p.h:.1f} mm, {p.net}): via {len(inside)} 個')
                    if not inside:
                        ep_warn = True
            elif inside:
                vip.append(f'{f.ref}.{p.num} ({p.net}): via {len(inside)} 個')
    # P18 の pad は穴がマスク開口にかかるので P33_solder_wicking (checks_mount) で量を判定する。二重に減点しないよう、実装性チェックが有効なら info
    mount_on = (cfg.get('mount', {}) or {}).get('enable', True)
    M.append(Metric(S, 'P18_via_in_pad', '小 pad の via-in-pad (はんだ吸い込み / 埋め工程が必要)', len(vip),
                    ('info' if vip else 'pass') if mount_on else ('pass' if not vip else ('waived' if cfg.get('via_in_pad_ok') else 'warn')),
                    '0 (埋め・蓋をする前提なら review.toml via_in_pad_ok)' + ('。判定は P33_solder_wicking (吸い込み比)' if mount_on else ''), vip[:10], 'kpcb'))
    M.append(Metric(S, 'P19_ep_thermal_vias', 'IC の exposed pad (GND) の thermal via', len(ep_rows), 'warn' if ep_warn else ('pass' if ep_rows else 'skip'),
                    'GND の EP に via ≥1', ep_rows[:10] or ['EP なし'], 'kpcb'))
    # --- P22/P23 基板標準ルール (2026-10-14 作者): 外周からの距離、シルクの重なり (board_rules.py、design_rules.json)
    E = board_rules.edge_checks(b, cfg)
    if E.get('error'):
        M.append(Metric(S, 'P22_board_edge', '外周からの距離 (銅箔・部品)', None, 'skip', '', [E['error']], 'board_rules'))
    else:
        n = len(E['copper']) + len(E['parts'])
        dl = [x['msg'] for x in E['copper'][:8]] + [x['msg'] for x in E['parts'][:8]] + [f'例外 (銅箔): {x}' for x in E['exempt'][:10]] \
            + [f'例外 (部品): {x}' for x in E.get('parts_exempt', [])[:10]] + [f'注意: {x}' for x in E.get('notes') or []]
        if ctx.get('drc') and 'violations' in ctx['drc']:
            dl.append(f"参考: DRC copper_edge_clearance {len(ctx['drc']['violations'].get('copper_edge_clearance', []))} 件 (基板設定の値で判定、1 mm とは限らない)")
        M.append(Metric(S, 'P22_board_edge', f"外周から {E['cu_mm']:g} mm 以内の銅箔 / {E['part_mm']:g} mm 以内の部品 (2026-10-14 基板標準)", f"{len(E['copper'])}/{len(E['parts'])}",
                        'fail' if n else 'pass', f"銅箔 0 / 部品 0 (部品は courtyard と外形の最短距離、しきい値は gerber-drc user.edge_keepout_parts。例外: Connector*:*・MountingHole:*・TestPoint:*・J*、review.toml [config.board_edge] exempt)", dl, 'board_rules.edge_checks'))
    SL = board_rules.silk_checks(b, cfg)
    import collections as _c
    kc = _c.Counter(x['kind'] for x in SL)
    dl = [f"{k} {v}" for k, v in sorted(kc.items())] + [f"{x['kind']}: {x['msg']}" for x in sorted(SL, key=lambda x: ['TEXT_TEXT', 'TEXT_PAD', 'TEXT_BODY', 'LINE_PAD'].index(x['kind']))[:14]]
    if ctx.get('drc') and 'violations' in ctx['drc']:
        dl.append(f"参考: DRC silk_overlap {len(ctx['drc']['violations'].get('silk_overlap', []))} / silk_over_copper {len(ctx['drc']['violations'].get('silk_over_copper', []))} 件")
    M.append(Metric(S, 'P23_silk_overlap', 'シルクの重なり (文字どうし・文字と部品/パッド・線とパッド、2026-10-14 基板標準で必須)', len(SL), 'fail' if SL else 'pass',
                    '0 (文字は回転を考慮した外接矩形の概算)', dl, 'board_rules.silk_checks'))
    # --- P24 取付穴の有無 (2026-10-14 作者基板標準。穴が要らない基板は review.toml [waive] P24_mounting_holes = "理由")
    MH = board_rules.mounting_holes(b, cfg)
    hs = MH['holes']
    dl = [f"{h['ref']} ({h['fp']}): ({h['x']:.2f}, {h['y']:.2f}) mm、穴径 {h['drill']:g} mm、{'メッキあり' if h['plated'] else 'NPTH'}"
          + (f"、角から x {h['corner_mm'][0]:g} / y {h['corner_mm'][1]:g} mm" if 'corner_mm' in h else '') for h in hs]
    if len(hs) >= 2:
        xs = sorted({h['x'] for h in hs}); ys = sorted({h['y'] for h in hs})
        dl.append(f"穴中心の外接矩形 {xs[-1] - xs[0]:.2f} x {ys[-1] - ys[0]:.2f} mm" + ('、矩形 (4 隅、x・y 各 2 値)' if len(hs) == 4 and len(xs) == 2 and len(ys) == 2 else ''))
    if not hs:
        dl.append('取付穴なし。ケース・スペーサで固定するなら四隅に M3 (MountingHole_3.2mm_M3) などを追加。不要なら review.toml [waive] P24_mounting_holes に理由')
    M.append(Metric(S, 'P24_mounting_holes', '取付穴の数 (2026-10-14 基板標準: 無ければ warn)', len(hs),
                    'pass' if len(hs) >= MH['min_count'] else 'warn', f"≥{MH['min_count']} (不要なら review.toml の waiver に理由)", dl, 'board_rules.mounting_holes'))
    # --- P25 uuid の重複と回路図シンボルとの対応 (uuid_check.py、2026-10-16 作者 決定)
    import uuid_check, glob as _g
    U = uuid_check.check(b.path, sorted(_g.glob(os.path.join(ctx['dir'], '*.kicad_sch'))), os.path.join(out, 'netlist.xml') if nl else None)
    M.append(Metric(S, 'P25_uuid_unique', 'uuid の重複 (本体/子要素) と footprint path ↔ 回路図シンボルの対応', U['value'], U['grade'],
                    '本体重複・回路図シンボル重複・path の空/重複/対応なし 0 (fail)、子要素・本体以外の重複と部品番号/値の食い違い 0 (warn)。値は 本体/子要素/path',
                    uuid_check.detail(U), 'uuid_check.check'))
    # --- P20/P21 リターンパスの点数 (return_path.py。tr は data/signal の信号特性表。絵と表は <out>/return_path_*.png / return_path.md)
    if not return_path.filled_planes(b):
        M.append(Metric(S, 'P20_return_path_gap', 'リターンパス: GND ベタの切れ目またぎの点数', None, 'skip', '', ['GND / 電源ベタの塗り (filled_polygon) が無い (zone なし or 未 fill)'], ''))
    elif not nl:
        M.append(Metric(S, 'P20_return_path_gap', 'リターンパス: GND ベタの切れ目またぎの点数', None, 'skip', '', ['ネットリストが無い (信号特性表を作れない)'], ''))
    else:
        items, pngs, rst = return_path.run(b, nl, ctx['name'], ctx['out'], cfg)
        ls_ = rst['layers']
        rp_cov = [f"{L}: 信号配線 {v['tracks']} 本 = GND 参照 {v['gnd']} / 電源ベタ参照 {v['power']} (仮定: 電源ベタを帰路とみなす{'、' + '; '.join(v['refs']) if v['power'] else ''}) / 評価不能 {v['none']} ({v['none_len']:.0f} mm)"
                  for L, v in ls_.items() if v['tracks']]
        nnone = sum(v['none'] for v in ls_.values())
        vs = rst['vias']
        rp_via = [f"信号 via: 前後の参照が同じ (GND via 不要、0 点) {vs['same_ref']} / 同ネット別層 (縫い via) {vs['stitch_via']} / 別ネット (ステッチング C) {vs['stitch_cap']} / 前後の層不明 (従来の GND via 距離) {vs['legacy']} / 参照なし {vs['no_ref']}"]
        ign = (cfg.get('return_path', {}) or {}).get('ignore_nets', {}) or {}
        if isinstance(ign, list):
            ign = {n: '' for n in ign}
        for mid, kind, title in (('P20_return_path_gap', 'cross', 'リターンパス: GND ベタの切れ目またぎの点数 (1000×D/(tr×v)、D = max(またぐ長さ, 迂回))'),
                                 ('P21_return_via', 'via', 'リターンパス: 層を変える信号 via の点数 (前後の参照が同じなら 0、同ネット別層は縫い via、別ネットはステッチング C までの距離 d、D = 2d)')):
            xs_all = [i for i in items if i['kind'] == kind]
            # P20 のみ: review.toml [config.return_path] ignore_nets のネットは採点から外す (理由付き。絵・表には残る)
            waived = []
            xs = xs_all
            if kind == 'cross' and ign:
                waived = [i for i in xs_all if i.get('net') in ign and i['grade'] in ('fail', 'warn')]
                xs = [i for i in xs_all if i.get('net') not in ign or i['grade'] == 'pass']
            nf = sum(1 for i in xs if i['grade'] == 'fail'); nw = sum(1 for i in xs if i['grade'] == 'warn')
            top = max((i['score'] for i in xs), default=0)
            g = 'fail' if nf else 'warn' if nw else 'pass' if xs_all else 'skip'
            rows = [f"fail {nf} / warn {nw} / 全 {len(xs_all)} 区間、最大 {top:.0f} 点 (番号は絵と return_path.md の表)"]
            if waived:
                rows.append(f"ignore_nets で除外 {len(waived)} 区間 (fail/warn): " + ', '.join(sorted({f"{i['net']} ({ign[i['net']]})" for i in waived})))
            rows += (rp_cov + ([f'評価不能 (隣に GND も電源ベタも無い) {nnone} 本'] if nnone else [])) if kind == 'cross' else rp_via
            rows += [f"#{i['no']} {i['score']:.0f} 点 {i['net']} {i['layer']} ({i['x']:.1f}, {i['y']:.1f}) tr_eff {i['tr_eff']:g} ns ({i['e']['certainty']}): {i['what']}"
                     for i in xs if i['grade'] != 'pass'][:12]
            rows += ['絵: ' + ', '.join(pngs)]
            M.append(Metric(S, mid, title, f"{nf}/{nw}/{top:.0f}", g,
                            f'区間の点数 ≥{return_path.TH_FAIL} fail / ≥{return_path.TH_WARN} warn (2026-10-14 作者確定（運用で見直しあり）、SKILL.md §2.6)。値 = fail 数/warn 数/最大点', rows, 'kpcb+netlist+data/signal'))
    # --- P18b・P26〜P33 実装性 (checks_mount.py、2026-10-16 作者 承認、しきい値は仮。review.toml [config.mount] enable = false で止める)
    if (cfg.get('mount', {}) or {}).get('enable', True):
        import checks_mount
        M += checks_mount.run(ctx)
    return M
