"""段階 1: 回路図 (ERC + schlint + フィールド・DNP)"""
import collections, json, os, re, subprocess, sys
from common import Metric, judge, cli, KB

SCHLINT = os.path.join(KB, 'tools', 'kicad-schlint', 'schlint.py')
NON_PARTS = re.compile(r'^(#|PWR|FLG)')


def run(ctx):
    M = []; S = 'sch'
    sch = ctx['sch']; out = ctx['out']
    # --- ERC
    ej = os.path.join(out, 'erc.json')
    cli(['sch', 'erc', '--severity-all', '--format', 'json', '-o', ej, sch])
    if os.path.exists(ej):
        e = json.load(open(ej))
        vs = [v for s in e.get('sheets', []) for v in s.get('violations', [])]
        ne = sum(v['severity'] == 'error' for v in vs); nw = sum(v['severity'] == 'warning' for v in vs)
        c = collections.Counter(v['type'] for v in vs)
        M.append(Metric(S, 'S01_erc', 'ERC 違反 (error / warning)', f'{ne}/{nw}', 'fail' if ne else 'warn' if nw else 'pass',
                        'error 0 かつ warning 0', [f'{k}: {n}' for k, n in c.items()], 'kicad-cli sch erc'))
    else:
        M.append(Metric(S, 'S01_erc', 'ERC', None, 'skip', '', ['ERC 実行失敗'], 'kicad-cli'))
    pro = json.load(open(ctx['pro'])) if os.path.exists(ctx['pro']) else {}
    ign = sorted(k for k, v in pro.get('erc', {}).get('rule_severities', {}).items() if v == 'ignore')
    M.append(Metric(S, 'S02_erc_ignored', 'ERC 規則の ignore 指定数', len(ign), 'pass' if not ign else 'warn',
                    '0 (ignore は理由を review.toml に記録)', ign, '.kicad_pro'))
    # --- schlint
    sj = os.path.join(out, 'schlint.json')
    subprocess.run([sys.executable, SCHLINT, sch, '--json', sj], capture_output=True, text=True)
    if not os.path.exists(sj):
        M.append(Metric(S, 'S03_schlint', 'schlint', None, 'skip', '', ['schlint 実行失敗'], 'schlint')); return M
    j = json.load(open(sj)); f = j['findings']
    cnt = collections.Counter(x['code'] for x in f)
    def codes(*cs): return sum(cnt[c] for c in cs)
    def ex(*cs, n=6): return [f"{x['code']}: {x['message'][:90]}" for x in f if x['code'] in cs][:n]
    M.append(Metric(S, 'S03_schlint_errors', 'schlint error 数', j['summary']['error'], judge(j['summary']['error'], 0, 0),
                    '0', ex(*[x['code'] for x in f if x['severity'] == 'error']), 'schlint'))
    lo = j['stats'].get('label_only', {})
    worst = max((v['ratio'] for v in lo.values()), default=None)
    M.append(Metric(S, 'S04_label_only_ratio', 'ラベルだけで接続するピンの比率 (シート最大)', None if worst is None else round(worst, 3),
                    judge(worst, 0.30, 0.50), '≤0.30 pass / ≤0.50 warn', [f"{k}: {v['label_only']}/{v['label_only'] + v['wired']}" for k, v in lo.items()], 'schlint'))
    M.append(Metric(S, 'S05_wire_crosses_symbol', 'ワイヤがシンボル・ピンを横切る', codes('WIRE_CROSSES_SYMBOL', 'WIRE_CROSSES_PIN'),
                    'fail' if cnt['WIRE_CROSSES_SYMBOL'] else judge(cnt['WIRE_CROSSES_PIN'], 0, 3), 'シンボル横断 0、ピン重なり ≤3 warn',
                    ex('WIRE_CROSSES_SYMBOL', 'WIRE_CROSSES_PIN'), 'schlint'))
    n = codes('JUNCTION_MISSING', 'PIN_ON_WIRE_MIDDLE', 'WIRE_DANGLING', 'WIRE_ISOLATED', 'LABEL_UNCONNECTED')
    M.append(Metric(S, 'S06_connectivity_traps', 'T 分岐の接続点なし・ピンがワイヤ途中・浮きワイヤ/ラベル', n, judge(n, 0, 0), '0',
                    ex('JUNCTION_MISSING', 'PIN_ON_WIRE_MIDDLE', 'WIRE_DANGLING', 'WIRE_ISOLATED', 'LABEL_UNCONNECTED'), 'schlint'))
    n = codes('GRID_PIN', 'GRID_SYMBOL', 'GRID_WIRE', 'GRID_LABEL', 'GRID_OTHER')
    M.append(Metric(S, 'S07_off_grid', 'グリッド外の接続点', n, judge(n, 0, 5), '0 pass / ≤5 warn', ex('GRID_PIN', 'GRID_WIRE', 'GRID_LABEL'), 'schlint'))
    n = cnt['POWER_ORIENTATION']
    M.append(Metric(S, 'S08_power_orientation', '電源が上向き・GND が下向きでない記号', n, judge(n, 0, 10), '0 pass / ≤10 warn', ex('POWER_ORIENTATION'), 'schlint'))
    n = codes('FLOW_IO_REVERSED', 'FLOW_SOURCE_RIGHT')
    M.append(Metric(S, 'S09_signal_flow', '左→右の流れに反する配置 (推定)', n, judge(n, 0, 3), '0 pass / ≤3 warn', ex('FLOW_IO_REVERSED', 'FLOW_SOURCE_RIGHT'), 'schlint'))
    n = sum(1 for x in f if x['code'] == 'TITLE_EMPTY' and x['severity'] == 'warning')
    M.append(Metric(S, 'S10_title_block', 'タイトル欄の空き (title/date/rev/company)', n, judge(n, 0, 0), '0', ex('TITLE_EMPTY'), 'schlint'))
    n = codes('BLOCK_NO_FRAME', 'BLOCK_FRAME_NO_TEXT', 'BLOCK_SYMBOL_STRADDLE')
    M.append(Metric(S, 'S11_block_frames', 'ブロック枠・説明の欠け / 枠またぎ', n, judge(n, 0, 2), '0 pass / ≤2 warn', ex('BLOCK_NO_FRAME', 'BLOCK_FRAME_NO_TEXT', 'BLOCK_SYMBOL_STRADDLE'), 'schlint'))
    n = sum(1 for x in f if x['code'] in ('FIELD_OVERLAP', 'FIELD_HIDDEN', 'TEXT_OVERLAP') and x['severity'] == 'warning')
    M.append(Metric(S, 'S12_field_text_overlap', '部品番号・定数・注記の重なり / 非表示', n, judge(n, 0, 5), '0 pass / ≤5 warn', ex('FIELD_OVERLAP', 'FIELD_HIDDEN', 'TEXT_OVERLAP'), 'schlint'))
    n = codes('PAPER_OCCUPANCY_LOW', 'PAPER_OVERFLOW')
    M.append(Metric(S, 'S13_paper', '用紙占有率・はみ出し', n, judge(n, 0, 0), '占有率 25〜85%、はみ出し 0', ex('PAPER_OCCUPANCY_LOW', 'PAPER_OVERFLOW', 'PAPER_DENSE'), 'schlint'))
    n = codes('TEXT_KANA_STROKE', 'TEXT_HALFWIDTH_KANA')
    M.append(Metric(S, 'S14_kana', '既定フォントで崩れるカナ', n, 'info' if not cnt['TEXT_HALFWIDTH_KANA'] else 'warn', '参考 (半角カナは warn)', ex('TEXT_KANA_STROKE', 'TEXT_HALFWIDTH_KANA', n=3), 'schlint'))
    # --- フィールドの揃い (ネットリスト)
    nl = ctx.get('netlist')
    if nl:
        parts = [c for r, c in nl.comps.items() if not NON_PARTS.match(r) and not c['exclude_bom']]
        need = ('Value', 'Footprint', 'LCSC', 'MPN')
        miss = []
        for c in parts:
            for k in need:
                v = c['value'] if k == 'Value' else c['footprint'] if k == 'Footprint' else (c['fields'].get(k) or c['props'].get(k))
                if not v or v in ('~', '?'):
                    if k in ('LCSC', 'MPN') and c['dnp']:
                        continue          # 手はんだの DNP 部品は LCSC/MPN 無しでも可
                    miss.append(f"{c['ref']}: {k}")
        tot = len(parts) * len(need)
        ratio = 1 - len(miss) / tot if tot else None
        M.append(Metric(S, 'S15_field_completeness', 'シンボルのフィールド (Value/Footprint/LCSC/MPN) の充足率', None if ratio is None else round(ratio, 3),
                        judge(ratio, 1.0, 0.95, high=True), '100% pass / ≥95% warn (DNP の LCSC/MPN は免除)', miss[:12], 'netlist'))
        # DNP の整合: 回路図 dnp と PCB の attr dnp、回路図の注記 "DNP:" の部品番号
        sdnp = {r for r, c in nl.comps.items() if c['dnp']}
        pdnp = {f.ref for f in ctx['board'].footprints if 'dnp' in f.attr} if ctx.get('board') else set()
        txt = open(sch, encoding='utf-8').read()
        noted = set()
        for m in re.finditer(r'DNP\s*[:：]\s*([^"\n]+)', txt):
            seg = m.group(1)
            for a_, n1, n2 in re.findall(r'\b([A-Z]{1,3})(\d+)\s*[-~〜～]\s*(?:[A-Z]{1,3})?(\d+)\b', seg):
                noted |= {f'{a_}{i}' for i in range(int(n1), int(n2) + 1)}
            seg = re.sub(r'\b([A-Z]{1,3})(\d+)\s*[-~〜～]\s*(?:[A-Z]{1,3})?(\d+)\b', '', seg)
            noted |= set(re.findall(r'\b([A-Z]{1,3}\d+)\b', seg))
        bad = []
        if ctx.get('board') and sdnp != pdnp:
            bad.append(f'回路図 DNP {sorted(sdnp)} と PCB の dnp {sorted(pdnp)} が違う')
        if noted and noted != sdnp:
            bad.append(f'注記の DNP {sorted(noted)} と属性 {sorted(sdnp)} が違う')
        M.append(Metric(S, 'S16_dnp_consistency', 'DNP の整合 (回路図属性 / PCB / 注記)', len(bad), 'fail' if bad else 'pass', '一致',
                        bad or [f'DNP: {sorted(sdnp)}'], 'netlist+pcb'))
    return M
