#!/usr/bin/env python3
"""gerber-drc の回帰テスト。python3 tests/run_tests.py [--quick]
  1. 正常 4 基板 → error 0 / warning 0
  2. refill 前の FT2232H → 12 件 (COPPER_OVER_NPTH 4 + EDGE_CLEARANCE 8)
  3. 移設の同一性: gerber-lvs --aux の補助検査と、検出 (level, code, layer, 座標) が完全に一致 (正常 4 枚 + 旧 FT2232H)
  4. 合成 (gerber-lvs の tests/synthboard.py で手書き、KiCad 不要): NPTH 上の銅・NPTH clearance・外形 clearance ちょうど/未満・外形外の銅・paste 下の銅無し・外形が開いている
  5. ルール YAML (層マージ・緩め警告・backend 振り分け・外周 1mm ユーザールール・rules2dru)、6. kicad-cli が .kicad_dru を読む (全件のみ)
--quick は 4・5 だけ (約 2 s)。環境変数 GLVS_PROJECTS / GLVS_OLD_FT / GERBER_LVS_DIR は gerber-lvs のテストと同じ"""
import os, sys, json, shutil, subprocess, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
import gerber_drc as gd
gv, gl = gd.gv, gd.gl
sys.path.insert(0, os.path.join(gd.LVS_DIR, 'tests'))
import synthboard as sb
import testdata as td  # gerber-lvs/tests/testdata.py: 見本基板の控えの場所と gerber_dir() / ensure_gerbers()

PROJ, OLD_FT = td.PROJ, td.OLD_FT
BOARDS = [b for b, _ in td.BOARDS]
AUX = {'COPPER_OVER_NPTH', 'NPTH_CLEARANCE', 'EDGE_CLEARANCE', 'COPPER_OUTSIDE_BOARD', 'PASTE_WITHOUT_COPPER', 'OUTLINE_OPEN'}
rows = []


def rec(group, case, ok, detail=''):
    rows.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'} [{group}] {case} {detail}", flush=True)


def cli(P, name, out, *extra):
    r = subprocess.run([sys.executable, os.path.join(TOOL, 'gerber_drc.py'), P, name, '--out-dir', out, '-q', *extra], capture_output=True, text=True)
    j = os.path.join(out, 'gerber_drc.json')
    return r.returncode, (json.load(open(j)) if os.path.exists(j) else {})


def key(f): return (f['level'], f['code'], f.get('layer'), tuple(f.get('at', ())))


def same_as_lvs(P, name, tmp, grp):
    """gerber-lvs (--aux、旧動作) の補助検査と gerber-drc の検出が同じか"""
    out = os.path.join(tmp, 'lvs-' + name)
    r = subprocess.run([sys.executable, os.path.join(gd.LVS_DIR, 'gerber_lvs.py'), P, name, '--gerber-dir', td.gerber_dir(P), '--out-dir', out, '-q', '--aux'], capture_output=True, text=True,
                       env=dict(os.environ, HOME=os.environ.get('KICAD_CLI_HOME', os.path.expanduser('~'))))
    lj = json.load(open(os.path.join(out, 'gerber_lvs.json')))
    a = sorted(key(f) for f in lj['findings'] if f['code'] in AUX)
    _, dj = cli(P, name, os.path.join(tmp, 'drc-' + name), '--gerber-dir', td.gerber_dir(P))
    b = sorted(key(f) for f in dj['findings'])
    rec(grp, f'{name}: gerber-lvs --aux の補助検査と一致', a == b and lj.get('origin_offset') == [0.0, 0.0], f'lvs {len(a)} 件 / drc {len(b)} 件')


def test_real(tmp):
    # gerber は git 管理外。無ければ gerber-lvs の regen_testdata_gerbers.sh で出し直す (tests/data の下だけ、refill なし)
    miss = td.ensure_gerbers([os.path.join(PROJ, b) for b in BOARDS] + [OLD_FT])
    if miss:
        return rec('real', f'テストデータの gerber が無い → bash {td.REGEN} で出し直す', False, ', '.join(miss))
    for b in BOARDS:
        rc, j = cli(os.path.join(PROJ, b), b, os.path.join(tmp, 'n-' + b), '--gerber-dir', td.gerber_dir(os.path.join(PROJ, b)))
        s = j.get('summary', {})
        rec('normal', b, rc == 0 and s.get('error') == 0 and s.get('warning') == 0, f"exit={rc} {s}")
        same_as_lvs(os.path.join(PROJ, b), b, tmp, 'same')
    rc, j = cli(OLD_FT, 'usb-jtag-ft2232h', os.path.join(tmp, 'old'), '--png', '--gerber-dir', td.gerber_dir(OLD_FT))
    fs = [f for f in j['findings'] if f['level'] == 'error']
    c = {k: sum(1 for f in fs if f['code'] == k) for k in ('COPPER_OVER_NPTH', 'EDGE_CLEARANCE')}
    rec('old-ft2232h', 'exit=1、12 件 (COPPER_OVER_NPTH 4 + EDGE_CLEARANCE 8)', rc == 1 and len(fs) == 12 and c == {'COPPER_OVER_NPTH': 4, 'EDGE_CLEARANCE': 8}, f'exit={rc} {c}')
    rec('old-ft2232h', 'PNG を出す', len([f for f in fs if 'png' in f]) == 12)
    same_as_lvs(OLD_FT, 'usb-jtag-ft2232h', tmp, 'same')


def test_synth(tmp):
    G = 'synth'
    def run(b, nm, paste=None, edge_open=False):
        d = os.path.join(tmp, 's-' + nm); shutil.rmtree(d, ignore_errors=True); b.write(d)
        if paste is not None:
            body = ''.join(f'X{sb.C(x)}Y{sb.C(y)}D03*\n' for x, y in paste)
            sb.gerber(os.path.join(d, 'sb-F_Paste.gtp'), 'Paste,Top', 'D10*\n' + body, '%ADD10R,0.8X0.8*%\n')
        if edge_open:
            sb.gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', '%ADD10C,0.05*%\nD10*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y12000000D01*\nX0Y12000000D01*\n')
        return gd.DRC(d, gv.read_rules(None), log=lambda *a: None).run()
    def codes(r, lv=None): return sorted({f['code'] for f in r.findings if lv is None or f['level'] == lv})
    r = run(sb.base2(), 'ok'); rec(G, '正常 (2 層合成) → 0 件', not r.findings, str(codes(r)))
    r = run(sb.base4_blind(), 'ok4'); rec(G, '正常 (4 層、ブラインド/ベリード via) → 0 件', not r.findings, str(codes(r)))
    b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=1, x=18.5, y=2, w=1.2, h=1.2))
    r = run(b, 'npth'); rec(G, 'NPTH の上に銅 → COPPER_OVER_NPTH', 'COPPER_OVER_NPTH' in codes(r, 'error'), str(codes(r)))
    b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=1, x=18.5 + 0.5 + 0.1 + 0.25, y=2, w=0.5, h=0.5))
    r = run(b, 'npthclr'); rec(G, 'NPTH の穴縁から 0.1 mm (< 0.2) → NPTH_CLEARANCE', 'NPTH_CLEARANCE' in codes(r, 'warning') and 'COPPER_OVER_NPTH' not in codes(r), str(codes(r)))
    b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=1, x=10, y=12 - 0.2 - 0.5, w=1, h=1))
    r = run(b, 'edge-eq'); rec(G, '外形 clearance ちょうど 0.2 mm → 出さない', 'EDGE_CLEARANCE' not in codes(r), str(codes(r)))
    b.pads[-1]['y'] = 12 - 0.15 - 0.5
    r = run(b, 'edge-lt'); rec(G, '外形 clearance 0.15 mm → EDGE_CLEARANCE', 'EDGE_CLEARANCE' in codes(r, 'error'), str(codes(r)))
    b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=2, x=21, y=6, w=1, h=1))
    r = run(b, 'out'); rec(G, '外形の外に銅 → COPPER_OUTSIDE_BOARD', 'COPPER_OUTSIDE_BOARD' in codes(r, 'error'), str(codes(r)))
    r = run(sb.base2(), 'paste', paste=[(3, 4), (6, 5.5)])
    rec(G, 'paste 開口の下に銅が無い → PASTE_WITHOUT_COPPER 1 件', [f['code'] for f in r.findings] == ['PASTE_WITHOUT_COPPER'], str(codes(r)))
    r = run(sb.base2(), 'open', edge_open=True); rec(G, '外形が閉じていない → OUTLINE_OPEN (外形外の検査は省略)', codes(r) == ['OUTLINE_OPEN'], str(codes(r)))
    rc = gd.main(['--gerber-dir', os.path.join(tmp, 's-out'), '--out-dir', os.path.join(tmp, 'cli'), '-q', '--edge-clearance', '0.1'])
    rec(G, 'CLI --gerber-dir: error ありで exit 1', rc == 1)
    rc = gd.main(['--gerber-dir', os.path.join(tmp, 'nothing'), '--out-dir', os.path.join(tmp, 'cli'), '-q'])
    rec(G, 'CLI: gerber が無ければ exit 2', rc == 2)
    # 2026-10-14 カバレッジで見つけたシナリオ不足
    d = os.path.join(tmp, 's-ok')
    sb.gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', 'G36*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y12000000D01*\nX0Y12000000D01*\nX0Y0D01*\nG37*\n')
    r = gd.DRC(d, gv.read_rules(None), log=lambda *a: None).run(); rec(G, '外形が region (G36) → 閉路 1、0 件', len(r.loops) == 1 and not r.findings, str(codes(r)))
    o = os.path.join(tmp, 'cli2'); gd.main(['--gerber-dir', os.path.join(tmp, 's-out'), '--out-dir', o, '-q', '--png']); gd.main(['--gerber-dir', os.path.join(tmp, 's-out'), '--out-dir', o, '-q', '--png'])
    rec(G, 'PNG を 2 回出しても古い PNG は残らない', len([f for f in os.listdir(os.path.join(o, 'gerber_drc')) if f.endswith('.png')]) == 1)
    bad = os.path.join(tmp, 's-ok', 'sb-F_Cu.gbr'); txt = open(bad).read(); open(bad, 'w').write(txt.replace('M02*', 'FOO*\nM02*'))
    rec(G, 'CLI: 未対応構文 → exit 2', gd.main(['--gerber-dir', d, '--out-dir', o, '-q']) == 2)
    r2 = subprocess.run([sys.executable, os.path.join(TOOL, 'gerber_drc.py'), '--gerber-dir', d], capture_output=True, text=True, cwd=tmp,
                        env=dict(os.environ, GERBER_LVS_DIR=os.path.join(tmp, 'no-lvs')))
    rec(G, 'gerber-lvs が見つからない → exit 1 で理由を出す', r2.returncode == 1 and 'gerber-lvs が見つからない' in r2.stderr, r2.stderr.strip()[:80])


def test_rules(tmp):
    """2026-10-14 ルール YAML (rules.py / rules2dru.py / gerber_drc.py --rules)"""
    G = 'rules'
    import rules as rl, rules2dru as r2d
    RD = os.path.join(TOOL, 'rules')
    rs = rl.load([os.path.join(RD, 'project', 'example.yaml')])
    rec(G, '層マージ: factory→user→project、project の上書きが勝つ', rs.rules['edge.copper_clearance_vcut']['enabled'] is True and rs.rules['silk.min_text_height']['value'] == 0.8
        and [f['layer'] for f in rs.files] == ['factory', 'user', 'project'])
    rec(G, '工場値を緩めると warning を記録', any('silk.min_text_height' in w for w in rs.warnings), str(rs.warnings))
    ml = rl.load([os.path.join(RD, 'factory', 'jlcpcb-multilayer.yaml')])
    rec(G, '多層 factory は標準の差分 (0.09、2 層の最大寸法は無効)', ml.rules['track.min_width']['value'] == 0.09 and not ml.rules['board.max_size_2l']['enabled'] and not ml.warnings)
    gb = {r['id'] for r in rs.for_backend('gerber_drc')}; kd = {r['id'] for r in rs.for_backend('kicad_dru')}
    rec(G, 'backend 振り分け (外形 1mm は両方、部品 3mm は kicad_dru だけ、paste は gerber だけ)',
        'user.edge_keepout_copper' in gb & kd and 'user.edge_keepout_parts' in kd - gb and 'fab.paste_without_copper' in gb - kd)
    mil = os.path.join(tmp, 'mil.yaml'); open(mil, 'w').write('file: {layer: project, extends: [factory/jlcpcb-standard]}\nrules:\n  - {id: edge.copper_clearance, value: 20, unit: mil}\n')
    rec(G, 'unit: mil を mm に換算', abs(rl.load([mil]).rules['edge.copper_clearance']['value'] - 0.508) < 1e-9)
    p = gd.rules_params(rs)
    rec(G, 'rules_params: 基本 0.2 + 追加 user 1.0 mm、NPTH 0.2 warning', p['edge_clr'] == 0.2 and ('user.edge_keepout_copper', 1.0, 'error') in p['extra_edge']
        and p['hole_clr'] == 0.2 and p['sev'].get('NPTH_CLEARANCE') == 'warning', str(p['extra_edge']))
    # 外周 1mm ユーザールール (合成基板、外形 20x12、上辺 y=12)
    def board(gap):
        b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=1, x=10, y=12 - gap - 0.5, w=1, h=1))
        d = os.path.join(tmp, f'r-{gap}'); shutil.rmtree(d, ignore_errors=True); b.write(d); return d
    user = os.path.join(RD, 'user', 'morilab.yaml')
    def run(d, *extra):
        o = os.path.join(d, 'o'); rc = gd.main(['--gerber-dir', d, '--out-dir', o, '-q', *extra])
        return rc, json.load(open(os.path.join(o, 'gerber_drc.json')))
    def ec(j): return sorted((f.get('rule_id') or 'base', f['level']) for f in j['findings'] if f['code'] == 'EDGE_CLEARANCE')
    d = board(0.5)
    rc0, j0 = run(d); rc1, j1 = run(d, '--rules', user)
    rec(G, 'YAML 無し: 外形 0.5 mm は従来どおり 0 件', rc0 == 0 and not j0['findings'] and 'yaml' not in j0['rules'])
    rec(G, '外周 1mm (user): 0.5 mm → EDGE_CLEARANCE rule_id=user.edge_keepout_copper', rc1 == 1 and ec(j1) == [('user.edge_keepout_copper', 'error')], str(ec(j1)))
    d = board(0.15); _, j2 = run(d, '--rules', user)
    rec(G, '0.15 mm → 工場 0.2 と user 1.0 の両方を出す', ec(j2) == [('base', 'error'), ('user.edge_keepout_copper', 'error')], str(ec(j2)))
    d = board(1.2); rc3, j3 = run(d, '--rules', user)
    rec(G, '1.2 mm → 出さない', rc3 == 0 and not ec(j3), str(ec(j3)))
    pj = os.path.join(tmp, 'p.yaml'); open(pj, 'w').write('file: {layer: project, extends: [user/morilab]}\nrules:\n  - {id: user.edge_keepout_copper, severity: warning}\n  - {id: fab.copper_outside_board, severity: warning}\n')
    _, j4 = run(board(0.5), '--rules', pj)
    rec(G, 'project で重大度を上書き (error → warning)、exit 0', j4['status'] == 'ok' and ec(j4) == [('user.edge_keepout_copper', 'warning')], str(ec(j4)))
    _, j5 = run(board(0.15), '--rules', user, '--edge-clearance', '0.1')
    rec(G, 'CLI --edge-clearance が YAML より優先', j5['rules']['edge_clearance'] == 0.1 and ec(j5) == [('user.edge_keepout_copper', 'error')], str(ec(j5)))
    rec(G, '未実装の check は notes に出す', any('pth.track_clearance' in n and '未実装' in n for n in run(board(1.2), '--rules', os.path.join(RD, 'project', 'example.yaml'))[1]['notes']))
    # 穴径 (2026-10-14): ドリルファイルの径で工場の最小/最大と project の Φ2 縛り
    def hb(nm, via_d=0.3, npth_d=1.0, big=None):
        b = sb.base2(); b.holes[0]['d'] = via_d; b.npth[0] = (18.5, 2, npth_d)
        if big: b.holes.append(dict(net='GND', x=5, y=10, d=big, ring=big + 0.6, span=(1, 2)))
        d = os.path.join(tmp, 'h-' + nm); shutil.rmtree(d, ignore_errors=True); b.write(d); return d
    def hs(j): return sorted((f['code'], f['rule_id'], f['drill'], f['count']) for f in j['findings'] if f['code'].startswith('HOLE_'))
    fac = os.path.join(RD, 'factory', 'jlcpcb-standard.yaml')
    _, j = run(hb('ok'), '--rules', fac); rec(G, '穴径: 工場値の範囲内 → 0 件', not hs(j), str(hs(j)))
    _, j = run(hb('small', via_d=0.1, npth_d=0.4, big=7.0), '--rules', fac)
    rec(G, '穴径: PTH φ0.1 < 0.15、NPTH φ0.4 < 0.5、φ7 > 6.3 → HOLE_SIZE 3 件', hs(j) == [('HOLE_SIZE', 'hole.max_drill', 7.0, 1), ('HOLE_SIZE', 'hole.min_drill', 0.1, 1), ('HOLE_SIZE', 'npth.min_drill', 0.4, 1)]
        and j['status'] == 'fail', str(hs(j)))
    def pcb(d, x, y, rot, px, py, fid='MountingHole:MountingHole_2mm', ref='H1'):
        f = os.path.join(d, 'x.kicad_pcb')
        open(f, 'w').write(f'(kicad_pcb (version 20240108)\n (setup (aux_axis_origin 0 0))\n (footprint "{fid}" (layer "F.Cu") (at {x} {y} {rot})\n'
                           f'  (property "Reference" "{ref}" (at 0 0 0))\n  (pad "" np_thru_hole circle (at {px} {py} {rot}) (size 2 2) (drill 2) (layers "*.Cu"))))\n')
        return f
    stud = os.path.join(tmp, 'stud.yaml'); open(stud, 'w').write(
        'file: {layer: project, extends: [factory/jlcpcb-standard]}\nrules:\n  - {id: project.stud_hole_d2, check: hole_diameter_set, scope: {hole: npth, footprints: ["MountingHole:*", "H*"]}, value: {allowed: [2.0], tol: 0.05}}\n')
    d = hb('stud1', npth_d=1.0); _, j = run(d, '--rules', stud, '--pcb', pcb(d, 18.5, -2, 0, 0, 0))
    rec(G, 'スタッド穴 Φ2 縛り: MountingHole の NPTH が φ1.0 → HOLE_DIAMETER_SET', hs(j) == [('HOLE_DIAMETER_SET', 'project.stud_hole_d2', 1.0, 1)], str(hs(j)))
    d = hb('stud2', npth_d=2.0); _, j = run(d, '--rules', stud, '--pcb', pcb(d, 17.5, -2, 90, 0, 1))
    rec(G, 'スタッド穴 φ2.0 (footprint 90° 回転、pad オフセット) → 0 件', not hs(j) and not any('stud' in n for n in j['notes']), str(hs(j)) + str(j['notes']))
    d = hb('stud3', npth_d=1.0); _, j = run(d, '--rules', stud, '--pcb', pcb(d, 5, -5, 0, 0, 0))
    rec(G, 'footprint の位置に穴が無ければ対象外 (別の NPTH φ1.0 は縛らない)', not hs(j), str(hs(j)))
    _, j = run(hb('stud4', npth_d=1.0), '--rules', stud)
    rec(G, '.kicad_pcb が無ければ footprint 条件付きルールは見ず notes に出す', not hs(j) and any('.kicad_pcb が無い' in n for n in j['notes']))
    # rules2dru --project (make_out.sh から呼ぶ)
    P = os.path.join(tmp, 'proj'); os.makedirs(P)
    rec(G, 'rules2dru --project: YAML が無ければ何もしない', r2d.project(P, 'b', log=lambda m: None) == 'none' and not os.path.exists(os.path.join(P, 'b.kicad_dru')))
    shutil.copy(os.path.join(RD, 'project', 'example.yaml'), os.path.join(P, 'gerber_drc_rules.yaml'))
    st = [r2d.project(P, 'b', log=lambda m: None), r2d.project(P, 'b', log=lambda m: None)]
    rec(G, 'rules2dru --project: 生成 → 2 回目は最新なので書かない', st == ['written', 'same'] and r2d.MARK in open(os.path.join(P, 'b.kicad_dru')).read(), str(st))
    hand = '(version 1)\n(rule "mine" (constraint clearance (min 0.3mm)))\n'; open(os.path.join(P, 'b.kicad_dru'), 'w').write(hand)
    msgs = []; st = r2d.project(P, 'b', log=msgs.append)
    rec(G, 'rules2dru --project: 手書き .kicad_dru は上書きせず警告、生成物は out/*.generated', st == 'manual' and open(os.path.join(P, 'b.kicad_dru')).read() == hand
        and os.path.exists(os.path.join(P, 'out', 'b.kicad_dru.generated')) and any('警告' in m for m in msgs))
    open(os.path.join(P, 'gerber_drc_rules.yaml'), 'w').write('file: {layer: bogus}\n'); os.remove(os.path.join(P, 'b.kicad_dru'))
    rec(G, 'rules2dru --project: YAML が壊れていれば exit 2 (make_out.sh は止まる)', r2d.main(['--project', P, 'b']) == 2)
    # make_out.sh (出力スクリプト) は本リポに含めない。環境変数 GDRC_MAKE_OUT で場所を渡したときだけ確かめる
    mo = os.environ.get('GDRC_MAKE_OUT')
    if mo and os.path.exists(mo):
        t = open(mo).read()
        rec(G, f'make_out.sh ({mo}): 最初の kicad-cli pcb drc の前に rules2dru --project', 'rules2dru.py' in t and '--project . "$N"' in t
            and t.index('--project . "$N"') < t.index('kicad-cli pcb drc'))
    # rules2dru
    txt = r2d.to_dru(rs)
    rec(G, 'rules2dru: 外形 1mm・部品 3mm (例外 J*)・Φ2・pad 3mm を書き、マスクは書かない',
        '(rule "user.edge_keepout_copper"\n  (constraint edge_clearance (min 1mm))' in txt and "A.memberOfFootprint('J*')" in txt
        and '(constraint hole_size (min 1.95mm) (max 2.05mm))' in txt and '"mask.min_bridge"' not in txt and txt.startswith('(version 1)'))
    return txt


def test_dru_kicad(tmp, txt):
    """box の kicad-cli で .kicad_dru が読まれ、ルール名が違反に出る (約 2 分)"""
    b = 'rp2040-keypad10'; src = os.path.join(PROJ, b); d = os.path.join(tmp, 'dru')
    if not shutil.which('kicad-cli') or not os.path.isdir(src): return rec('dru', 'kicad-cli か基板が無いので省略', True)
    os.makedirs(d); [shutil.copy(os.path.join(src, b + e), d) for e in ('.kicad_pcb', '.kicad_pro')]
    open(os.path.join(d, b + '.kicad_dru'), 'w').write(txt)
    out = os.path.join(d, 'drc.json')
    r = subprocess.run(['kicad-cli', 'pcb', 'drc', '--severity-all', '--format', 'json', '-o', out, os.path.join(d, b + '.kicad_pcb')], capture_output=True, text=True,
                       env=dict(os.environ, HOME=os.environ.get('KICAD_CLI_HOME', os.path.expanduser('~'))), timeout=900)
    v = json.load(open(out))['violations'] if os.path.exists(out) else []
    hit = {i for i in ('user.edge_keepout_copper', 'user.edge_keepout_parts', 'pth.track_clearance', 'silk.min_text_height') if any(f"'{i}'" in x['description'] for x in v)}
    rec('dru', f'kicad-cli pcb drc ({b}) が .kicad_dru を読み、ルール名で違反を出す', r.returncode == 0 and len(hit) == 4, f'rc={r.returncode} {sorted(hit)} {r.stderr[-200:]}')


def main():
    tmp = tempfile.mkdtemp(prefix='gdrc-test-')
    try:
        test_synth(tmp)
        txt = test_rules(tmp)
        if '--quick' not in sys.argv: test_real(tmp); test_dru_kicad(tmp, txt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'\n== {sum(rows)}/{len(rows)} PASS'); return 0 if all(rows) else 1


if __name__ == '__main__':
    sys.exit(main())
