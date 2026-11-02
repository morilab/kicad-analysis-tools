#!/usr/bin/env python3
"""gerber-lvs の回帰テスト。元プロジェクトは読むだけ (gerber はコピーを改変、IPC-D-356 は一時コピーから)。
  python3 tests/run_tests.py [--quick] [--json out.json] [--only units normal old inject input synth]
  高速スイートだけ: python3 tests/test_units.py (KiCad 不要、約 2 s)
  1. 正常 4 基板の今の gerber → error 0 / warning 0
  2. FT2232H の refill 前 gerber → 補助検査 (NPTH 上の銅、外形 clearance) で検出、short/open は 0
  3. 故障注入 (2 層 FT2232H、4 層 fpga-voice-recorder): 配線削除→OPEN、銅追加→SHORT、via 削除→MISSING_HOLE、部品穴削除→MISSING_HOLE
     配線削除は「消した区間の中心線がほかの銅で全部覆われている」ものを幾何的につながったままとして先に除外し、理由を記録する
  3b. 入力の整合 (4 層 fpga-voice-recorder): 層の欠落・入れ替え、drill の単位、pad の flash 削除
  4. 合成: 裏面 SMD (部品を裏返したコピー、2 層/4 層)、内部切り欠き (KiCad で正しく作ったもの / Edge_Cuts だけ足した古い gerber)
データ: tests/data/ (tests/testdata.py)。gerber/drill は git に入れず、無ければ tests/regen_testdata_gerbers.sh で自動で出し直す (refill なし)
環境変数: GLVS_DATA (tests/data の代わり)、GLVS_PROJECTS (既定 tests/data/current = 4 基板の控え。手元の案件で回すならそのディレクトリ),
  GLVS_OLD_FT (既定 tests/data/pre-refill-20261009/usb-jtag-ft2232h)、GLVS_NO_REGEN=1 (自動で出し直さない)、GLVS_APPRUN"""
import os, sys, re, json, math, glob, random, shutil, subprocess, tempfile, time, argparse
HERE = os.path.dirname(os.path.abspath(__file__)); TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
import gerberlib as gl, gerber_lvs as gv
import numpy as np

from testdata import PROJ, OLD_FT, BOARDS, gerber_dir, ensure_gerbers  # 4 基板の控え (案件は作業中に動くので) と refill 前の FT2232H
APPRUN = os.environ.get('GLVS_APPRUN', '')   # KiCad 同梱 python の起動コマンド (make_synth.py を自分で用意した場合だけ使う)
SYNTH = os.environ.get('GLVS_MAKE_SYNTH', os.path.join(HERE, 'make_synth.py'))
CLI_ENV = dict(os.environ, HOME=os.environ.get('KICAD_CLI_HOME', os.path.expanduser('~')))
rows = []


def rec(group, case, ok, detail=''):
    rows.append(dict(group=group, case=case, ok=bool(ok), detail=detail))
    print(f"{'PASS' if ok else 'FAIL'} [{group}] {case} {detail}", flush=True)


def lvs(gdir, d356, pro=None, aux=None):
    return gv.LVS(gdir, d356, gv.read_rules(pro), aux=aux, log=lambda *a: None).run()


def errs(l, code=None):
    return [f for f in l.findings if f['level'] == 'error' and (code is None or f['code'] == code)]


# ---------------------------------------------------------------- 1. 正常
def test_normal(tmp):
    for b, _ in BOARDS:
        P = os.path.join(PROJ, b); out = os.path.join(tmp, 'normal', b)
        r = subprocess.run([sys.executable, os.path.join(TOOL, 'gerber_lvs.py'), P, b, '--gerber-dir', gerber_dir(P), '--out-dir', out, '-q'], capture_output=True, text=True, env=CLI_ENV)
        res = json.load(open(os.path.join(out, 'gerber_lvs.json'))) if os.path.exists(os.path.join(out, 'gerber_lvs.json')) else {}
        s = res.get('summary', {})
        rec('normal', b, r.returncode == 0 and s.get('error') == 0 and s.get('warning') == 0,
            f"exit={r.returncode} error={s.get('error')} warning={s.get('warning')} info={s.get('info')} {res.get('time_s', {}).get('total')}s")


# ---------------------------------------------------------------- 2. 古い FT2232H
def test_old_ft(tmp):
    if not os.path.isdir(OLD_FT):
        rec('old-ft2232h', 'data', False, f'控えが無い: {OLD_FT}'); return
    out = os.path.join(tmp, 'old')
    r = subprocess.run([sys.executable, os.path.join(TOOL, 'gerber_lvs.py'), OLD_FT, 'usb-jtag-ft2232h', '--gerber-dir', gerber_dir(OLD_FT), '--out-dir', out, '-q', '--png', '--aux'], capture_output=True, text=True, env=CLI_ENV)  # 補助検査は --aux (旧動作) のときだけ
    res = json.load(open(os.path.join(out, 'gerber_lvs.json')))
    fs = [f for f in res['findings'] if f['level'] == 'error']
    over = {(f['layer'], tuple(f['at'])) for f in fs if f['code'] == 'COPPER_OVER_NPTH'}
    edge = [f for f in fs if f['code'] == 'EDGE_CLEARANCE']
    net = [f for f in fs if f['code'] in ('SHORT', 'OPEN')]
    rec('old-ft2232h', 'exit=1', r.returncode == 1, f'exit={r.returncode}')
    rec('old-ft2232h', 'COPPER_OVER_NPTH J1 φ0.65 x2 x F/B', len(over) == 4, str(sorted(over)))
    rec('old-ft2232h', 'EDGE_CLEARANCE < 0.1 mm', bool(edge) and min(f['dist'] for f in edge) < 0.1,
        f"{len(edge)} 件, 最小 {min(f['dist'] for f in edge) if edge else None} mm")
    rec('old-ft2232h', 'short/open なし (ベタが NPTH を覆ってもネットは変わらない)', not net, f'{len(net)} 件')


# ---------------------------------------------------------------- 3. 故障注入
def copper_files(gdir):
    return gv.discover(gdir)[0]


def segment_covered(gfile, idx_line, a, b, net, R0):
    """消す区間の中心線が、それ以外の銅 (同じ層) で全部覆われているか (= 消しても幾何的につながったまま)"""
    g = gl.Gerber(gfile)
    m = 0.4
    x0, x1 = min(a[0], b[0]) - m, max(a[0], b[0]) + m; y0, y1 = min(a[1], b[1]) - m, max(a[1], b[1]) + m
    R = gl.Raster(x0, y0, x1, y1, 0.01)
    objs = [o for o in g.objs if not (o[1] == 'stroke' and {o[2][1], o[2][2]} == {a, b})]
    g.objs = objs
    im = np.asarray(gl.render(g, R)) > 127
    L = math.dist(a, b); n = max(2, int(L / 0.02))
    for k in range(n + 1):
        x = a[0] + (b[0] - a[0]) * k / n; y = a[1] + (b[1] - a[1]) * k / n
        c, r = R.xy2pix(x, y)
        if not im[int(round(r)), int(round(c))]: return False
    return True


def inject(board, nl, d356, tmp, n_open, n_short, n_via, rnd):
    src = gerber_dir(os.path.join(PROJ, board)); grp = f'inject-{nl}L {board}'
    pts = gl.parse_d356(d356)
    padcnt = {}
    for p in pts: padcnt[p['net'].upper()] = padcnt.get(p['net'].upper(), 0) + 1
    files = copper_files(src)
    work = os.path.join(tmp, 'inj', board)

    def fresh():
        shutil.rmtree(work, ignore_errors=True); shutil.copytree(src, work); return work

    def same(n1, n2): return n1.upper() == n2.upper()
    # --- OPEN: 表/裏の信号配線の区間を 1 本消す
    cands = []
    for li in (0, len(files) - 1):
        lines = open(files[li]).read().split('\n'); net = None; prev = None
        for i, l in enumerate(lines):
            m = re.match(r'%TO\.N,(.*)\*%', l)
            if m: net = m.group(1)
            elif l.startswith('%TD'): net = None
            m = re.match(r'X(-?\d+)Y(-?\d+)D0([12])\*', l)
            if m:
                p = (int(m.group(1)) / 1e6, int(m.group(2)) / 1e6)
                if m.group(3) == '1' and net and prev and math.dist(prev, p) > 0.3 and padcnt.get(net.upper(), 0) <= 8 \
                        and not net.upper().startswith(('UNCONNECTED', 'GND', '+', 'VBUS')):
                    cands.append((li, i, net, prev, p))
                prev = p
    rnd.shuffle(cands); done = 0; excluded = []
    for li, i, net, a, b in cands:
        if done >= n_open: break
        if segment_covered(files[li], i, a, b, net, None):
            excluded.append(f'{net} {a}->{b}'); continue
        d = fresh(); f = os.path.join(d, os.path.basename(files[li]))
        L = open(f).read().split('\n'); L[i] = 'G04 injected: deleted segment*'; open(f, 'w').write('\n'.join(L))
        r = lvs(d, d356); op = [x for x in errs(r, 'OPEN') if same(x['net'], net)]
        other = [x for x in errs(r) if x not in op]
        rec(grp, f'配線削除→OPEN {gv.lname(files[li])} {net} ({a[0]:.2f},{a[1]:.2f})-({b[0]:.2f},{b[1]:.2f})', op and not other,
            f'OPEN {len(op)} / 他 error {len(other)}'); done += 1
    rec(grp, f'配線削除の除外 (中心線がほかの銅で全部覆われ幾何的につながったまま) {len(excluded)} 件', True, '; '.join(excluded[:5]))
    # --- SHORT: 近い別ネット pad 間を 0.15 mm 線 / 丸い銅で結ぶ
    sides = {1: 0, nl: len(files) - 1}
    pads = [p for p in pts if p['acc'] in sides and p['net'] != 'N/C']
    pairs = [(p, q) for i, p in enumerate(pads) for q in pads[i + 1:]
             if p['acc'] == q['acc'] and not same(p['net'], q['net']) and 0.3 < math.dist((p['x'], p['y']), (q['x'], q['y'])) < 1.5]
    rnd.shuffle(pairs)
    for k, (p, q) in enumerate(pairs[:n_short]):
        d = fresh(); f = os.path.join(d, os.path.basename(files[sides[p['acc']]])); t = open(f).read()
        P = lambda v: round(v * 1e6)
        if k % 2 == 0:
            kind = '線'; blob = f"%ADD999C,0.150000*%\nD999*\nX{P(p['x'])}Y{P(p['y'])}D02*\nX{P(q['x'])}Y{P(q['y'])}D01*\n"
        else:
            kind = '丸'; dd = math.dist((p['x'], p['y']), (q['x'], q['y']))
            blob = f"%ADD998C,{dd + 0.1:.4f}*%\nD998*\nX{P((p['x'] + q['x']) / 2)}Y{P((p['y'] + q['y']) / 2)}D03*\n"
        open(f, 'w').write(t.replace('M02*', blob + 'M02*', 1))
        r = lvs(d, d356)
        sh = [x for x in errs(r, 'SHORT') if any(same(n, p['net']) for n in x['nets']) and any(same(n, q['net']) for n in x['nets'])]
        loc = sh[0].get('contacts', [{}])[0].get('at') if sh else None
        rec(grp, f"銅追加({kind})→SHORT {p['ref']}-{p['pin']}[{p['net']}] / {q['ref']}-{q['pin']}[{q['net']}]", bool(sh),
            f'SHORT {len(errs(r, "SHORT"))} 件, 接触点 {loc}')
    # --- via / 部品穴の削除
    vias = [p for p in pts if p['ref'] == 'VIA']; tht = [p for p in pts if p['type'] == '317' and p['ref'] != 'VIA' and p['plated']]
    rnd.shuffle(vias); rnd.shuffle(tht)
    cases = [(p, m) for p in vias[:n_via] for m in ('drill', 'drill+pads')] + [(p, 'drill') for p in tht[:2]]
    for p, mode in cases:
        d = fresh(); dr = glob.glob(os.path.join(d, '*.drl'))
        for f in dr:
            txt = open(f).read().split('\n')
            keep = []
            for l in txt:
                m = re.match(r'X(-?[\d.]+)Y(-?[\d.]+)$', l)
                if m and math.dist((float(m.group(1)), float(m.group(2))), (p['x'], p['y'])) < 0.02: continue
                keep.append(l)
            open(f, 'w').write('\n'.join(keep))
        if mode == 'drill+pads':
            for g in copper_files(d):
                gl_ = open(g).read().split('\n')
                gl_ = ['G04 injected*' if (m := re.match(r'X(-?\d+)Y(-?\d+)D03\*', l)) and math.dist((int(m.group(1)) / 1e6, int(m.group(2)) / 1e6), (p['x'], p['y'])) < 0.02 else l for l in gl_]
                open(g, 'w').write('\n'.join(gl_))
        r = lvs(d, d356)
        mh = [x for x in errs(r, 'MISSING_HOLE') if same(x['net'], p['net'])]
        op = [x for x in errs(r, 'OPEN') if same(x['net'], p['net'])]
        nm = f"VIA({p['x']:.2f},{p['y']:.2f})" if p['ref'] == 'VIA' else f"{p['ref']}-{p['pin']}"
        rec(grp, f"{'via' if p['ref'] == 'VIA' else '部品穴'}削除({mode})→MISSING_HOLE {nm} [{p['net']}]", bool(mh),
            f"MISSING_HOLE {len(mh)}, 同ネット OPEN {len(op)}" + ('' if op else (' (ほかの via でつながるのでネットは切れない)' if p['ref'] == 'VIA' else ' (表裏の pad の銅は残り、配線がある側でつながったまま)')))


# ---------------------------------------------------------------- 3b. 入力の整合 (実基板の gerber を壊す)
def inject_input(board, d356, tmp, rnd):
    src = gerber_dir(os.path.join(PROJ, board)); grp = f'input {board}'
    work = os.path.join(tmp, 'inp', board)
    def fresh():
        shutil.rmtree(work, ignore_errors=True); shutil.copytree(src, work); return work
    def run(d):
        try: return lvs(d, d356), None
        except gl.Unsupported as e: return None, f'Unsupported {e}'
        except Exception as e: return None, f'{type(e).__name__}: {e}'
    files = [os.path.basename(f) for f in copper_files(src)]
    d = fresh(); os.remove(os.path.join(d, files[1]))
    r, e = run(d); rec(grp, f'内層 {files[1]} を丸ごと削除 → LAYER_MISSING', r and errs(r, 'LAYER_MISSING'), e or str(sorted({f['code'] for f in errs(r)})))
    d = fresh(); os.remove(os.path.join(d, files[-1]))
    r, e = run(d); rec(grp, f'B.Cu を丸ごと削除 → LAYER_MISSING', r and errs(r, 'LAYER_MISSING'), e or str(sorted({f['code'] for f in errs(r)})))
    d = fresh(); a, b = os.path.join(d, files[1]), os.path.join(d, files[2]); os.rename(a, a + '.t'); os.rename(b, a); os.rename(a + '.t', b)
    r, e = run(d); rec(grp, f'内層 2 枚の中身を入れ替え (ファイル名はそのまま) → LAYER_NAME_MISMATCH', r and errs(r, 'LAYER_NAME_MISMATCH'), e or str(sorted({f['code'] for f in errs(r)})))
    # 片層だけ原点ずれ: In1 の座標を +0.30 mm ずらす → LAYER_OFFSET
    d = fresh(); f = os.path.join(d, files[1]); txt = open(f).read()
    txt = re.sub(r'^X(-?\d+)Y(-?\d+)', lambda m: f'X{int(m.group(1)) + 300000}Y{m.group(2)}', txt, flags=re.M); open(f, 'w').write(txt)
    r, e = run(d); o = errs(r, 'LAYER_OFFSET') if r else []
    rec(grp, f'内層 {files[1]} だけ +0.30 mm ずらす → LAYER_OFFSET', len(o) == 1 and o[0]['offset'] == [0.3, 0.0], e or str([(f['layer'], f['offset']) for f in o]))
    # .gbrjob: 銅層を 1 枚消す → GBRJOB_MISMATCH も出る
    d = fresh(); os.remove(os.path.join(d, files[2]))
    r, e = run(d); rec(grp, f'{files[2]} を削除 → GBRJOB_MISMATCH (job file にあるのに無い)', r and errs(r, 'GBRJOB_MISMATCH'), e or str(sorted({f['code'] for f in errs(r)})))
    # kicad-cli が IPC-D-356 を出せない (壊れた .kicad_pcb) → RuntimeError (exit 2)
    bp = os.path.join(tmp, 'broken.kicad_pcb'); open(bp, 'w').write('(kicad_pcb (version 1) broken')
    try: gv.export_d356(bp, None); ok = False; why = '例外なし'
    except RuntimeError as e: ok = 'ipcd356 失敗' in str(e); why = str(e)[:80]
    rec(grp, '壊れた .kicad_pcb → kicad-cli 失敗を RuntimeError で報告', ok, why)
    # 単位: drill を正しく INCH に書き直す → error 0 / INCH の数値のまま METRIC と宣言 → 実行できない (exit 2)
    for mode in ('inch-ok', 'inch-as-mm'):
        d = fresh()
        for f in glob.glob(os.path.join(d, '*.drl')):
            out = []
            for l in open(f).read().split('\n'):
                m = re.match(r'^(T\d+C)([\d.]+)$', l) or re.match(r'^X(-?[\d.]+)Y(-?[\d.]+)(.*)$', l)
                if l == 'METRIC': out.append('INCH' if mode == 'inch-ok' else 'METRIC'); continue
                if m and l.startswith('T'): out.append(f'{m.group(1)}{float(m.group(2)) / 25.4:.5f}'); continue
                if m:
                    rest = re.sub(r'([XY])(-?[\d.]+)', lambda q: f'{q.group(1)}{float(q.group(2)) / 25.4:.5f}', m.group(3))  # G85 の終点も
                    out.append(f'X{float(m.group(1)) / 25.4:.5f}Y{float(m.group(2)) / 25.4:.5f}{rest}'); continue
                out.append(l)
            open(f, 'w').write('\n'.join(out))
        r, e = run(d)
        if mode == 'inch-ok': rec(grp, 'drill をインチで正しく書き直す → error 0', r and not errs(r), e or str(sorted({f['code'] for f in errs(r)})))
        else: rec(grp, 'drill の数値がインチなのに METRIC 宣言 → 実行できない (exit 2 相当)', r is None, e or 'error を出さずに通った')
    # pad の flash を 1 個消す (配線の端が残る場合も PAD_COPPER_INCOMPLETE で拾う)
    pts = [p for p in gl.parse_d356(d356) if p['type'] == '327' and p['acc'] == 1 and min(p['sx'], p['sy']) > 0.4 and not p['net'].upper().startswith('UNCONNECTED')]
    rnd.shuffle(pts); hit = 0; excl = []
    for p in pts:
        if hit >= 3: break
        d = fresh(); f = os.path.join(d, files[0]); L = open(f).read().split('\n'); k = None
        for i, l in enumerate(L):
            m = re.match(r'X(-?\d+)Y(-?\d+)D03\*', l)
            if m and math.dist((int(m.group(1)) / 1e6, int(m.group(2)) / 1e6), (p['x'], p['y'])) < 0.005: k = i; break
        if k is None: continue
        L[k] = 'G04 injected: pad deleted*'; open(f, 'w').write('\n'.join(L))
        # ベタ (solid 接続) が pad の場所を覆っていれば、消しても銅は残る → 除外 (理由を記録)
        hs = min(p['sx'], p['sy']) / 2 / math.sqrt(2); Rw = gl.Raster(p['x'] - hs, p['y'] - hs, p['x'] + hs, p['y'] + hs, 0.01)
        if (np.asarray(gl.render(gl.Gerber(f), Rw)) > 127).mean() >= 0.9:
            excl.append(f"{p['ref']}-{p['pin']}[{p['net']}]"); continue
        hit += 1
        r, e = run(d); nm = f"{p['ref']}-{p['pin']}"
        got = [f['code'] for f in r.findings if f.get('pad') == nm or (f['code'] == 'OPEN' and any(nm in g for g in f.get('groups', [])))] if r else []
        rec(grp, f"pad 削除 {nm} [{p['net']}] → NO_COPPER_AT_PAD / OPEN / PAD_COPPER_INCOMPLETE", bool(got), e or str(got))
    rec(grp, f'pad 削除の除外 (ベタが pad の場所を覆い、消しても銅が残る) {len(excl)} 件', hit == 3, '; '.join(excl[:5]))


# ---------------------------------------------------------------- 4. 合成 (裏面 SMD / 切り欠き)
def kicad_out(pcb, nl, gdir):
    L = 'F.Cu,B.Cu' if nl == 2 else 'F.Cu,In1.Cu,In2.Cu,B.Cu'
    os.makedirs(gdir, exist_ok=True)
    run = lambda *a: subprocess.run(['kicad-cli', *a], capture_output=True, text=True, env=CLI_ENV, check=True)
    run('pcb', 'drc', '--refill-zones', '--save-board', '-o', os.path.join(gdir, '..', 'drc.json'), '--format', 'json', pcb)
    run('pcb', 'export', 'gerbers', '-l', L + ',F.Paste,B.Paste,Edge.Cuts', '--use-drill-file-origin', '-o', gdir + '/', pcb)
    run('pcb', 'export', 'drill', '--drill-origin', 'plot', '--format', 'excellon', '--excellon-units', 'mm', '-o', gdir + '/', pcb)
    run('pcb', 'export', 'ipcd356', '-o', os.path.join(gdir, '..', 'ref.d356'), pcb)
    return os.path.join(gdir, '..', 'ref.d356')


def copy_project(board, dst):
    os.makedirs(dst, exist_ok=True)
    for ext in ('kicad_pcb', 'kicad_pro'):
        shutil.copy2(os.path.join(PROJ, board, f'{board}.{ext}'), os.path.join(dst, f'{board}.{ext}'))
    return os.path.join(dst, f'{board}.kicad_pcb')


def synth(tmp):
    if not os.path.exists(SYNTH):
        rec('synth', 'make_synth.py', True, '同梱していない (KiCad の pcbnew を使うため) ので合成テストを省略'); return
    for board, nl, ref in (('usb-jtag-ft2232h', 2, 'C1'), ('rp2040-keypad10', 4, 'C1')):
        d = os.path.join(tmp, 'synth', f'flip-{board}'); pcb = copy_project(board, d)
        subprocess.run([*APPRUN.split(), SYNTH, pcb, 'flip', ref], capture_output=True, check=True)
        d356 = kicad_out(pcb, nl, os.path.join(d, 'gerber'))
        pts = [p for p in gl.parse_d356(d356) if p['ref'] == ref]
        r = lvs(os.path.join(d, 'gerber'), d356, pro=pcb.replace('.kicad_pcb', '.kicad_pro'))
        acc = sorted({p['acc'] for p in pts}); refpads = {f'{ref}-{p["pin"]}' for p in pts}
        nocu = [f for f in errs(r, 'NO_COPPER_AT_PAD')]
        bad_open = [f for f in errs(r, 'OPEN') if not any(x in refpads for g in f['groups'] for x in g)]
        rec('synth', f'裏面 SMD {nl}L ({board} の {ref} を裏返したコピー): access コード {acc} = {nl} 層目、裏面で pad が見つかる',
            acc == [nl] and not nocu and not errs(r, 'SHORT') and not bad_open,
            f"NO_COPPER_AT_PAD {len(nocu)}, SHORT {len(errs(r, 'SHORT'))}, OPEN {len(errs(r, 'OPEN'))} (いずれも {ref} の pad を含む: {not bad_open})")
    # 切り欠き: 銅も穴も配線も無い場所 (ベタだけ) に 1.5 mm 角
    board, nl = 'usb-jtag-ft2232h', 2
    src = gerber_dir(os.path.join(PROJ, board))
    l0 = lvs(src, export(board), pro=os.path.join(PROJ, board, board + '.kicad_pro'))
    aux = gv.aux_origin(os.path.join(PROJ, board, board + '.kicad_pcb'))
    strokes = [o for g in l0.gs for o in g.objs if o[1] in ('stroke', 'flash')]
    best = None
    for gx in np.arange(3, 51, 0.5):
        for gy in np.arange(3, 33, 0.5):
            dmin = min([math.dist((gx, gy), (p['x'], p['y'])) for p in l0.pts] + [math.dist((gx, gy), (h.x, h.y)) for h in l0.holes] +
                       [min(math.dist((gx, gy), o[2][1]), math.dist((gx, gy), o[2][-1] if o[1] == 'stroke' else o[2][1])) for o in strokes])
            if best is None or dmin > best[0]: best = (dmin, gx, gy)
    _, gx, gy = best; w = 1.5
    kx, ky = aux[0] + gx - w / 2, aux[1] - gy - w / 2
    d = os.path.join(tmp, 'synth', 'cutout'); pcb = copy_project(board, d)
    subprocess.run([*APPRUN.split(), SYNTH, pcb, 'cutout', str(kx), str(ky), str(w), str(w)], capture_output=True, check=True)
    d356 = kicad_out(pcb, nl, os.path.join(d, 'gerber'))
    r = lvs(os.path.join(d, 'gerber'), d356, pro=pcb.replace('.kicad_pcb', '.kicad_pro'))
    rec('synth', f'内部切り欠き (KiCad で refill した正しい出力、gerber ({gx},{gy}) に {w} mm 角)', r.n_cutouts == 1 and not errs(r),
        f'切り欠き {r.n_cutouts}, error {len(errs(r))}')
    # 古い gerber: 銅は元のまま、Edge_Cuts だけ切り欠き入り → 切り欠きの中に銅
    d2 = os.path.join(tmp, 'synth', 'cutout-stale'); shutil.rmtree(d2, ignore_errors=True); shutil.copytree(src, d2)
    shutil.copy2(glob.glob(os.path.join(d, 'gerber', '*Edge_Cuts*'))[0], glob.glob(os.path.join(d2, '*Edge_Cuts*'))[0])
    r = lvs(d2, d356)
    rec('synth', '切り欠きの中に銅 (Edge_Cuts だけ新しい) → COPPER_OUTSIDE_BOARD', errs(r, 'COPPER_OUTSIDE_BOARD') and r.n_cutouts == 1,
        f"COPPER_OUTSIDE_BOARD {len(errs(r, 'COPPER_OUTSIDE_BOARD'))}, EDGE_CLEARANCE {len(errs(r, 'EDGE_CLEARANCE'))}")


_d356 = {}


def export(board):
    if board not in _d356:
        _d356[board], _ = gv.export_d356(os.path.join(PROJ, board, board + '.kicad_pcb'), os.path.join(PROJ, board, board + '.kicad_pro'))
    return _d356[board]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--quick', action='store_true'); ap.add_argument('--json'); ap.add_argument('--keep', action='store_true')
    ap.add_argument('--only', nargs='*', default=['units', 'normal', 'old', 'inject', 'input', 'synth'])
    a = ap.parse_args()
    os.environ['HOME'] = CLI_ENV['HOME']
    tmp = tempfile.mkdtemp(prefix='glvs-test-'); t0 = time.time(); rnd = random.Random(20261009)
    try:
        if set(a.only) & {'normal', 'old', 'inject', 'input', 'synth'}:
            miss = ensure_gerbers()
            if miss: rec('data', 'gerber/drill がある (tests/regen_testdata_gerbers.sh で出し直す)', False, ', '.join(miss))
        if 'units' in a.only:  # 高速スイート (KiCad 不要) も同じ集計に入れる
            r = subprocess.run([sys.executable, os.path.join(HERE, 'test_units.py')], capture_output=True, text=True)
            for ln in r.stdout.splitlines():
                m = re.match(r'^(PASS|FAIL) \[([^\]]+)\] (.*)$', ln)
                if m: rec('units/' + m.group(2), m.group(3), m.group(1) == 'PASS')
        if 'normal' in a.only: test_normal(tmp)
        if 'old' in a.only: test_old_ft(tmp)
        if 'inject' in a.only:
            n = (3, 3, 1) if a.quick else (8, 8, 3)
            inject('usb-jtag-ft2232h', 2, export('usb-jtag-ft2232h'), tmp, *n, rnd)
            inject('fpga-voice-recorder', 4, export('fpga-voice-recorder'), tmp, *n, rnd)
        if 'input' in a.only:
            inject_input('fpga-voice-recorder', export('fpga-voice-recorder'), tmp, rnd)
        if 'synth' in a.only: synth(tmp)
    finally:
        if not a.keep: shutil.rmtree(tmp, ignore_errors=True)
        else: print('作業ディレクトリ:', tmp)
    npass = sum(r['ok'] for r in rows)
    print(f'\n== {npass}/{len(rows)} PASS ({time.time() - t0:.0f} s)')
    if a.json: json.dump(rows, open(a.json, 'w'), ensure_ascii=False, indent=1)
    return 0 if npass == len(rows) else 1


if __name__ == '__main__':
    sys.exit(main())
