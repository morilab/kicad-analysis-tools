#!/usr/bin/env python3
"""kreview — KiCad プロジェクトの定量レビュー (回路図 → 設計/BOM → PCB → 製造出力) を 1 回で回す。

  python3 kreview.py <project_dir> [--out DIR] [--gate G1|G2|G3 | --stages sch,design,pcb,fab] [--compare prev/kreview.json] [--quiet]

- --gate: board-hotl のゲート。G1 = sch,design / G2 = sch,design,pcb / G3 = 全段 (既定)
- --compare: 前の版の kreview.json。判定が悪化した指標を列挙し (JSON の regressions、md の「悪化」節)、悪化があれば終了コード 1

- プロジェクトは読むだけ。書き込みが要る処理 (refill、DRC 規則の強化、gerber の出し直し) は --out (/tmp) のコピーで行う。
- 結果: <out>/kreview.json, <out>/kreview.md, 標準出力に採点表。終了コード: 0 = fail 0 件、1 = fail あり、2 = 実行エラー。
- 例外 (waiver) と設定は <project_dir>/review.toml (任意):
    [waive]                       # 指標 ID = "理由 (日付・確認者)"。理由の無い waiver は無効
    P09_3d_missing = "KiCad 10 に WROOM-1U の STEP が無い (2026-10-09 mori 確認)"
    [config]
    reserve = ["F.SilkS"]         # pcb_score の予約層 (OLED の下など)
    hs_usb = true                 # USB HS 判定を強制
    extended_pass = 5             # Extended 品種数のしきい値
    extended_warn = 10
    via_in_pad_ok = true
    rotation_verified = ["U1"]    # JLC プレビューで回転確認済みの部品番号
    [config.mount]                # 実装性 P18b・P26〜P33 (値はすべて仮、仕様 notes/2026-10-16-mount-check-items.md)
    wick_fail = 0.25              # 吸い込み比 fail (warn 0.10、EP・タブは 0.50 まで warn)。穴埋めを頼むなら via_filled = true
    process = { J2 = "hand" }     # 工程。hand だけ P27 を fail まで (既定 reflow は warn 止まり)。exclude = {"R1" = "理由"} で個別除外
    gerber = true                 # ペースト開口を gerber 実データ (kicad-cli 出力) で見る (P32/P33)
    [config.crystal]
    cstray_pf = 4.0
    Y1 = { cl_pf = 20 }
    [config.connector.J2]         # DR-01 逆刺し (D11): キー無しピンヘッダのピンの役割。keyed = "理由" = キーで物理的に防止 (ok)
    roles = { "1" = "gnd", "2" = "out", "3" = "vref" }
    [config.power."+3V3"]         # DR-02 電源予算 (D12): 書式は power_budget.py
    regulator = "U3"
    [config.derating]             # D14 降格判定 (derating.py)
    ta = 50                       # 周囲温度の仮定 (℃)
    thermal_map = "review/thermal/map.json"   # 熱解析 bot の温度マップ (任意)
    net_v = { "VREF_JTAG" = 3.3 }  # ネット名で決まらない電圧
    [config.stackup]
    h = 1.53                      # 表層 → GND 面 (mm)
    er = 4.5
規則と指標の意味: skills/design-review/SKILL.md
"""
import argparse, datetime, glob, json, os, sys, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import Metric, KB, Netlist, export_netlist
sys.path.insert(0, os.path.join(KB, 'tools', 'pcb-score'))
import kpcb
import checks_sch, checks_design, checks_pcb, checks_fab

VERSION = '0.1 (2026-10-09)'
STAGES = [('sch', '回路図', checks_sch), ('design', '設計・BOM', checks_design), ('pcb', 'PCB', checks_pcb), ('fab', '製造出力', checks_fab)]
MARK = {'pass': 'PASS', 'warn': 'WARN', 'fail': 'FAIL', 'skip': 'skip', 'info': 'info', 'waived': 'waiv'}


def load_cfg(pdir):
    p = os.path.join(pdir, 'review.toml')
    if not os.path.exists(p):
        return {}, {}
    import tomllib
    d = tomllib.load(open(p, 'rb'))
    return d.get('config', {}), d.get('waive', {})


def stage_grade(ms):
    c = {g: sum(1 for m in ms if m['grade'] == g) for g in ('pass', 'warn', 'fail', 'skip', 'info', 'waived')}
    n = c['pass'] + c['warn'] + c['fail']
    score = round((c['pass'] + 0.5 * c['warn']) / n * 100) if n else None
    grade = 'A' if c['fail'] == 0 and c['warn'] <= 1 else 'B' if c['fail'] == 0 else 'C' if c['fail'] <= 2 else 'D'
    if n == 0:
        grade = '-'
    return dict(counts=c, score=score, grade=grade)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir')
    ap.add_argument('--out')
    ap.add_argument('--stages', default=None)
    ap.add_argument('--gate', choices=['G1', 'G2', 'G3'])
    ap.add_argument('--compare')
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()
    pdir = os.path.abspath(a.project_dir)
    pros = glob.glob(os.path.join(pdir, '*.kicad_pro'))
    if not pros:
        print('kicad_pro が無い', file=sys.stderr); return 2
    name = os.path.splitext(os.path.basename(pros[0]))[0]
    out = a.out or f'/tmp/kreview/{name}-{datetime.datetime.now():%Y%m%d-%H%M%S}'
    os.makedirs(out, exist_ok=True)
    cfg, waive = load_cfg(pdir)
    ctx = dict(dir=pdir, name=name, out=out, cfg=cfg, pro=pros[0], sch=os.path.join(pdir, name + '.kicad_sch'), pcb=os.path.join(pdir, name + '.kicad_pcb'))
    ok, err = export_netlist(ctx['sch'], os.path.join(out, 'netlist.xml'))
    ctx['netlist'] = Netlist(os.path.join(out, 'netlist.xml')) if ok else None
    ctx['board'] = kpcb.Board(ctx['pcb']) if os.path.exists(ctx['pcb']) else None
    want = (a.stages or {'G1': 'sch,design', 'G2': 'sch,design,pcb'}.get(a.gate, 'sch,design,pcb,fab')).split(',')
    allm = []; timing = {}
    for sid, title, mod in STAGES:
        if sid not in want:
            continue
        t0 = time.time()
        try:
            ms = mod.run(ctx)
        except Exception as e:
            ms = [Metric(sid, f'{sid}_error', f'{title} の検査が例外で停止', None, 'fail', '', traceback.format_exc().splitlines()[-6:], 'kreview')]
        timing[sid] = round(time.time() - t0, 1)
        allm += ms
    for m in allm:
        if m['id'] in waive and m['grade'] in ('warn', 'fail'):
            reason = str(waive[m['id']]).strip()
            if reason:
                m['detail'].insert(0, f"waiver: {reason} (元の判定 {m['grade']})"); m['waived_from'] = m['grade']; m['grade'] = 'waived'
    stages = {}
    for sid, title, _ in STAGES:
        ms = [m for m in allm if m['stage'] == sid]
        if ms:
            stages[sid] = dict(title=title, **stage_grade(ms))
    order = '-ABCD'
    overall = max((s['grade'] for s in stages.values()), key=order.index) if stages else '-'
    scs = [s['score'] for s in stages.values() if s['score'] is not None]
    R = dict(tool='kreview', version=VERSION, project=name, dir=pdir, out=out, date=datetime.datetime.now().isoformat(timespec='seconds'),
             overall=dict(grade=overall, score=round(sum(scs) / len(scs)) if scs else None,
                          fail=sum(1 for m in allm if m['grade'] == 'fail'), warn=sum(1 for m in allm if m['grade'] == 'warn')),
             stages=stages, metrics=allm, timing_s=timing, waivers=waive, gate=a.gate)
    if a.compare:
        P = json.load(open(a.compare)); prev = {m['id']: m for m in P['metrics']}
        rank = {'pass': 0, 'info': 0, 'skip': 0, 'waived': 0, 'warn': 1, 'fail': 2}
        R['regressions'] = [dict(id=m['id'], before=f"{prev[m['id']]['grade']} {prev[m['id']]['value']}", after=f"{m['grade']} {m['value']}")
                            for m in allm if m['id'] in prev and rank[m['grade']] > rank[prev[m['id']]['grade']]]
        R['compared_with'] = a.compare
    if ctx.get('board') is not None and any(m['id'] == 'P33_solder_wicking' for m in allm):
        try:   # 実装性 (P18b・P26〜P33) の注釈画像 (mount_annotate.py)
            import mount_annotate
            R['mount_png'] = mount_annotate.render(R, ctx['board'], os.path.join(out, 'mount_check.png'))
        except Exception as e:
            R['mount_png'] = f'失敗: {e}'
    json.dump(R, open(os.path.join(out, 'kreview.json'), 'w'), indent=1, ensure_ascii=False, default=str)
    md = report_md(R)
    open(os.path.join(out, 'kreview.md'), 'w').write(md)
    print(scorecard(R, verbose=not a.quiet))
    print(f'\n出力: {out}/kreview.json, kreview.md' + (f", {R['mount_png']}" if R.get('mount_png') else ''))
    if R.get('regressions'):
        print('悪化 (前の版より判定が悪い): ' + ', '.join(f"{x['id']} {x['before']} → {x['after']}" for x in R['regressions']))
    return 1 if R['overall']['fail'] or R.get('regressions') else 0


def scorecard(R, verbose=True):
    L = [f"== kreview {R['project']}  総合 {R['overall']['grade']} ({R['overall']['score']} 点)  fail {R['overall']['fail']} / warn {R['overall']['warn']}"]
    L.append(f"{'段階':10} {'評価':4} {'点':>4}  pass warn fail skip/info/waived")
    for sid, s in R['stages'].items():
        c = s['counts']
        L.append(f"{s['title']:10} {s['grade']:4} {str(s['score']):>4}  {c['pass']:4} {c['warn']:4} {c['fail']:4} {c['skip']}/{c['info']}/{c['waived']}")
    if verbose:
        for m in R['metrics']:
            if m['grade'] in ('fail', 'warn'):
                d = ('  ' + m['detail'][0][:110]) if m['detail'] else ''
                L.append(f"  {MARK[m['grade']]} {m['id']:26} {str(m['value']):>8}  {m['title'][:40]}{d}")
    return '\n'.join(L)


def esc(s):
    return str(s).replace('|', '\\|').replace('\n', ' ')


def report_md(R):
    L = [f"# kreview: {R['project']}", '', f"- 日時: {R['date']} / kreview {R['version']}", f"- 対象: `{R['dir']}` (読み取りのみ。書き込みは `{R['out']}` のコピー)",
         f"- **総合 {R['overall']['grade']} ({R['overall']['score']} 点)**、fail {R['overall']['fail']} / warn {R['overall']['warn']}"] + ([f"- 実装性の画像: `{R['mount_png']}`"] if R.get('mount_png') else []) + ['',
         '| 段階 | 評価 | 点 | pass | warn | fail | skip | info | waived |', '|---|---|---|---|---|---|---|---|---|']
    for sid, s in R['stages'].items():
        c = s['counts']
        L.append(f"| {s['title']} | {s['grade']} | {s['score']} | {c['pass']} | {c['warn']} | {c['fail']} | {c['skip']} | {c['info']} | {c['waived']} |")
    for sid, s in R['stages'].items():
        L += ['', f"## {s['title']}", '', '| 判定 | ID | 項目 | 値 | しきい値 | 主な詳細 |', '|---|---|---|---|---|---|']
        for m in [m for m in R['metrics'] if m['stage'] == sid]:
            L.append(f"| {MARK[m['grade']]} | {m['id']} | {esc(m['title'])} | {esc(m['value'])} | {esc(m['threshold'])} | {esc('; '.join(m['detail'][:3]))[:300]} |")
    if 'regressions' in R:
        L += ['', f"## 悪化 (前の版 `{R['compared_with']}` との比較)", '']
        L += [f"- {x['id']}: {x['before']} → {x['after']} (理由: 未記入)" for x in R['regressions']] or ['- なし']
    L += ['', '## 詳細 (warn / fail)', '']
    for m in R['metrics']:
        if m['grade'] in ('warn', 'fail', 'waived') and m['detail']:
            L.append(f"### {MARK[m['grade']]} {m['id']} {m['title']}")
            L += [f'- {esc(d)}' for d in m['detail'][:20]] + ['']
    return '\n'.join(L) + '\n'


if __name__ == '__main__':
    sys.exit(main())
