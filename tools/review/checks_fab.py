"""段階 4: 製造出力 (fill の鮮度、gerber/drill の揃い・鮮度、BOM/CPL、回転、外形、LVS フック)"""
import csv, glob, hashlib, io, json, os, re, shutil, subprocess, zipfile
from common import Metric, judge, cli, copy_project, KB, HERE
import kpcb


def fill_hash(path):
    r = kpcb.parse_sexpr(open(path, encoding='utf-8').read())
    zs = []; nzones = 0; nfilled = 0
    for z in kpcb.kids(r, 'zone'):
        if kpcb.kid(z, 'keepout') is not None:
            continue
        nzones += 1
        net = str(kpcb.val(z, 'net_name', default=''))
        for fp in kpcb.kids(z, 'filled_polygon'):
            nfilled += 1
            pts = kpcb.kid(fp, 'pts')
            co = tuple((round(float(c[1]), 3), round(float(c[2]), 3)) for c in pts[1:] if c[0] == 'xy')
            zs.append((net, str(kpcb.val(fp, 'layer', default='')), co))
    zs.sort()
    return hashlib.sha1(repr(zs).encode()).hexdigest()[:12], nzones, nfilled, zs


def norm_gerber(data):
    out = []
    for ln in data.decode('utf-8', 'replace').splitlines():
        if ln.startswith(('G04', '%TF.CreationDate', '%TF.GenerationSoftware', ';', '; ')) or 'CreationDate' in ln:
            continue
        out.append(ln)
    return '\n'.join(out)


# ---- 製造出力の探索 (2026-10-14: JLC 向けの実名 <board>_gerber_drill.zip / _bom_jlc.csv / out/jlcpcb/ などにも対応)
ZIP_PATS = [r'gerber.*\.zip$', r'\.zip$']
BOM_PATS = [r'bom.*jlc.*\.csv$', r'jlc.*bom.*\.csv$', r'_bom\.csv$', r'bom.*\.csv$']
CPL_PATS = [r'(cpl|pos).*jlc.*\.csv$', r'jlc.*(cpl|pos).*\.csv$', r'_(cpl|pos)(_mm)?\.csv$', r'(cpl|pos).*\.csv$']
NOT_FINAL = re.compile(r'raw|review|kicad|notes', re.I)


def _verkey(path):
    """版フォルダの自然順 (rev0.10 > rev0.2 > rev0.1)"""
    return [int(x) for x in re.findall(r'\d+', os.path.basename(path))] or [0]


def find_outputs(pdir, name):
    """out/ 以下 (深さ 3 まで) から gerber zip・JLC 用 BOM・CPL を探す -> dict(dir, zip, bom, cpl)。
    zip: 名前のパターン順 → 基板名で始まる → 版フォルダの新しい順 → mtime。BOM/CPL: zip と同じフォルダ → 親 → out/ 全体"""
    top = os.path.join(pdir, 'out')
    files = []
    for root, dirs, fs in os.walk(top):
        if root[len(top):].count(os.sep) >= 3:
            dirs[:] = []
        files += [os.path.join(root, f) for f in fs]
    def pick(pats, near=None):
        for pat in pats:
            c = [f for f in files if re.search(pat, os.path.basename(f), re.I) and not NOT_FINAL.search(os.path.basename(f).replace(name, ''))]
            if not c:
                continue
            if near:
                for d in (near, os.path.dirname(near)):
                    cc = [f for f in c if os.path.dirname(f) == d]
                    if cc:
                        return sorted(cc, key=lambda f: (os.path.basename(f).startswith(name), os.path.getmtime(f)))[-1]
            return sorted(c, key=lambda f: (os.path.basename(f).startswith(name), _verkey(os.path.dirname(f)), os.path.getmtime(f)))[-1]
        return None
    zp = pick(ZIP_PATS)
    d = os.path.dirname(zp) if zp else top
    return dict(dir=d, zip=zp, bom=pick(BOM_PATS, d), cpl=pick(CPL_PATS, d))


LAYER_EXT = {'gtl': 'F_Cu', 'gbl': 'B_Cu', 'gts': 'F_Mask', 'gbs': 'B_Mask', 'gto': 'F_Silkscreen', 'gbo': 'B_Silkscreen',
             'gtp': 'F_Paste', 'gbp': 'B_Paste', 'gm1': 'Edge_Cuts', 'gko': 'Edge_Cuts'}


def gerber_key(fn, copper=()):
    """zip 内のファイル名 -> 層の鍵 (F_Cu / In1_Cu / F_Mask ... / drl:PTH / drl:NPTH / drl:ALL)。
    層名付き (<board>-In1_Cu-GND.g1)・Protel 拡張子 (.gtl, .g1) の両方を認識。対象外 (job, drill map) と不明は None"""
    b = os.path.basename(fn)
    if b.endswith('.gbrjob') or re.search(r'drl_map|-map\.', b):
        return None
    if b.lower().endswith(('.drl', '.xln', '.exc')):
        m = re.search(r'[-_](N?PTH)([-_.]|$)', b, re.I)
        return 'drl:' + (m.group(1).upper() if m else 'ALL')
    for L in [c.replace('.', '_') for c in copper] + ['F_Cu', 'B_Cu', 'F_Mask', 'B_Mask', 'F_Silkscreen', 'B_Silkscreen', 'F_SilkS', 'B_SilkS',
                                                       'F_Paste', 'B_Paste', 'Edge_Cuts'] + [f'In{i}_Cu' for i in range(1, 31)]:
        if re.search(rf'[-_]{L}([-_.]|$)', b):
            return L.replace('SilkS', 'Silkscreen')
    ext = b.rsplit('.', 1)[-1].lower()
    m = re.match(r'g(\d+)$', ext)
    if m:
        return f'In{m.group(1)}_Cu'
    return LAYER_EXT.get(ext)


def _col(header, pats):
    """CSV のヘッダから列名を探す (大文字小文字・空白・記号を無視)。pats は正規表現の候補 (先に一致したもの)"""
    norm = {h: re.sub(r'[^a-z0-9#]', '', (h or '').lower()) for h in header}
    for pat in pats:
        for h, n in norm.items():
            if re.fullmatch(pat, n):
                return h
    return None


def read_csv(path):
    """BOM/CPL の CSV を csv モジュールで読む (引用符内のカンマ "C1,C3" を正しく扱う。BOM 付き UTF-8 も可) -> (header, rows)"""
    txt = open(path, encoding='utf-8-sig', errors='replace').read()
    try:
        dia = csv.Sniffer().sniff(txt[:4096], delimiters=',;\t')
    except csv.Error:
        dia = csv.excel
    r = csv.DictReader(io.StringIO(txt), dialect=dia)
    rows = list(r)
    return r.fieldnames or [], rows


DESIG_COL = [r'designator', r'designators', r'reference', r'references', r'refdes', r'ref']
LCSC_COL = [r'lcsc', r'lcscpart#?', r'lcscpartnumber', r'lcsc#', r'jlcpcbpart#?', r'jlcpcbpartnumber', r'lcsc.*', r'jlc.*part.*']
ROT_COL = [r'rotation', r'rot', r'angle']


def lvs_board_name(pdir, name=None):
    """F09: LVS に渡す基板名。.kicad_pro のファイル名 (コピー先のフォルダ名が違っても正しい名前)"""
    if name and os.path.exists(os.path.join(pdir, name + '.kicad_pro')):
        return name
    pros = sorted(glob.glob(os.path.join(pdir, '*.kicad_pro')))
    return os.path.splitext(os.path.basename(pros[0]))[0] if pros else name


SI_NET = re.compile(r'USB_D[PN]|USB_D[+-]|D\+|D-$', re.I)


def si_records(nl, pdir, S='fab'):
    """F10: 差動線のインピーダンス検討の記録が review/ にあるか。
    ② 2D 電界計算 (tools/si/zdiff2d.py): review/ の *zdiff*.md・sweep.csv、または本文に zdiff2d を含む md
    ③ SPICE (tools/si/usb_hs_spice.py): review/ の results.json に eye_height を含む
    ④ openEMS は任意 (あれば明細に出す)。①近似式は kreview P15 が自動で出す"""
    nets = sorted(n for n in (nl.nets if nl else {}) if SI_NET.search(n.split('/')[-1]))
    if not nets:
        return Metric(S, 'F10_si_records', 'インピーダンス検討の記録 (②2D 計算・③SPICE)', None, 'skip', '高速差動のネットがある時だけ', ['対象ネットなし'], 'review/')
    rv = os.path.join(pdir, 'review')
    md = glob.glob(os.path.join(rv, '**', '*.md'), recursive=True)
    f2 = [f for f in glob.glob(os.path.join(rv, '**', '*'), recursive=True)
          if re.search(r'zdiff.*\.md$|sweep\.csv$', os.path.basename(f))]
    f2 += [f for f in md if f not in f2 and 'zdiff2d' in open(f, encoding='utf-8', errors='replace').read()]
    f3 = [f for f in glob.glob(os.path.join(rv, '**', 'results.json'), recursive=True)
          if 'eye_height' in open(f, encoding='utf-8', errors='replace').read()]
    f4 = [f for f in md if re.search(r'openEMS', open(f, encoding='utf-8', errors='replace').read()) and re.search(r'openEMS[^\n]*Ω', open(f, encoding='utf-8', errors='replace').read())]
    rel = lambda fs: ', '.join(os.path.relpath(f, pdir) for f in fs[:3]) or 'なし'
    miss = [x for x, f in (('② 2D 計算', f2), ('③ SPICE', f3)) if not f]
    return Metric(S, 'F10_si_records', 'インピーダンス検討の記録 (②2D 計算・③SPICE、skills/usb-diff-2layer)', len(miss), 'warn' if miss else 'pass',
                  '② と ③ の結果ファイルが review/ にある (④ openEMS は任意)',
                  [f'対象ネット: {", ".join(nets[:6])}', f'② {rel(f2)}', f'③ {rel(f3)}', f'④ {rel(f4)} (任意)'] + [f'無い: {m}' for m in miss], 'review/')


def run(ctx):
    M = []; S = 'fab'
    b = ctx['board']; name = ctx['name']; pdir = ctx['dir']; out = ctx['out']; cfg = ctx['cfg']
    fo = find_outputs(pdir, name)
    od = fo['dir']
    # F10 インピーダンス検討の記録 (skills/usb-diff-2layer の流れ ②③。高速差動 (USB D+/D- など) がある時だけ)
    M.append(si_records(ctx.get('netlist'), pdir, S))
    # F01 外形が閉じている
    outer, holes, open_n = b.outline()
    M.append(Metric(S, 'F01_outline_closed', '基板外形 (Edge.Cuts) が閉じている', open_n, 'pass' if outer and open_n == 0 else 'fail', '外周 1 つ・開いた鎖 0',
                    [f'外周 {"あり" if outer else "なし"}, 穴 {len(holes)}, 開いた鎖 {open_n}'], 'kpcb'))
    # F02 fill の鮮度: /tmp コピーで refill+save して filled_polygon を比べる
    h0, nz, nf, z0 = fill_hash(b.path)
    tmp = copy_project(pdir, name, os.path.join(out, 'refill'))
    tp = os.path.join(tmp, name + '.kicad_pcb')
    cli(['pcb', 'drc', '--refill-zones', '--save-board', '-o', os.path.join(tmp, 'drc_refill.json'), '--format', 'json', tp])
    h1, _, nf1, z1 = fill_hash(tp)
    if nz == 0:
        M.append(Metric(S, 'F02_fill_fresh', 'zone の fill が最新 (refill 後と一致)', None, 'skip', '', ['zone なし'], ''))
    else:
        g = 'pass' if h0 == h1 and nf > 0 else 'fail'
        det = [f'zone {nz}, 保存済み filled_polygon {nf} → refill 後 {nf1}, hash {h0} → {h1}']
        if g == 'fail':
            a = {(n, l) for n, l, _ in z0}; c = {(n, l) for n, l, _ in z1}
            det.append('差のある zone (net, 層): ' + ', '.join(f'{n}@{l}' for n, l in sorted({(n, l) for n, l, p in set(z0) ^ set(z1)}))[:300])
            det.append('対処: make_out.sh (refill + save 済み) で出し直す。KiCad で B キー → 保存でも可')
        M.append(Metric(S, 'F02_fill_fresh', 'zone の fill が最新 (refill 後と一致)', 'same' if h0 == h1 else 'stale', g, 'refill 前後で filled_polygon が同じ', det, 'kicad-cli --refill-zones'))
    # F03 gerber/drill の揃い
    zp = fo['zip']
    if not zp:
        M.append(Metric(S, 'F03_gerber_set', 'gerber/drill 一式', None, 'fail', 'out/ 以下の *gerber*.zip (版フォルダ・jlcpcb/ も探す)', ['zip が無い (make_out.sh 未実行)'], ''))
        return M
    z = zipfile.ZipFile(zp); names = z.namelist()
    keys = {x: gerber_key(x, b.copper) for x in names}
    have = set(keys.values())
    need = [L.replace('.', '_') for L in b.copper] + ['F_Mask', 'B_Mask', 'F_Silkscreen', 'B_Silkscreen', 'Edge_Cuts']
    smd_side = {f.side for f in b.footprints if 'smd' in f.attr and 'dnp' not in f.attr}
    need += [f'{s_}_Paste' for s_ in sorted(smd_side)]
    miss = [n for n in need if n not in have]
    drl = [x for x in names if (keys[x] or '').startswith('drl:')]
    if not drl:
        miss.append('drill (.drl)')
    drl_kind = '統合 drl' if 'drl:ALL' in have else 'PTH/NPTH 分割' if drl else 'なし'
    M.append(Metric(S, 'F03_gerber_set', 'JLCPCB 向け gerber/drill 一式 (銅 各層・mask・silk・paste・外形・drill)', len(miss), 'pass' if not miss else 'fail', '欠け 0',
                    (miss or [f'{len(names)} files ({os.path.relpath(zp, pdir)}、drill = {drl_kind}): ' +
                              ', '.join(f'{keys[n] or "-"}={n.split("/")[-1]}' for n in sorted(names))]), 'zip'))
    # F04 drill の穴数 = PCB の穴数 (PTH pad + NPTH pad + via)
    nh = sum(1 for f in b.footprints for p in f.pads if p.type in ('thru_hole', 'np_thru_hole')) + len(b.vias)
    hits = 0
    for d in drl:
        for ln in z.read(d).decode('utf-8', 'replace').splitlines():
            if re.match(r'^X-?[\d.]+Y-?[\d.]+', ln):
                hits += 1
    M.append(Metric(S, 'F04_drill_count', 'drill ファイルの穴数 = PCB の穴数 (pad + via)', f'{hits}/{nh}', 'pass' if hits == nh else 'fail', '一致',
                    [f'drill {hits}, PCB {nh} (via {len(b.vias)})'], 'zip+kpcb'))
    # F05 gerber の鮮度: 現在の PCB から /tmp に出し直して比べる (make_out.sh と同じ指定)
    gd = os.path.join(out, 'gerber'); os.makedirs(gd, exist_ok=True)
    layers = ','.join(b.copper + ['F.Paste', 'B.Paste', 'F.Silkscreen', 'B.Silkscreen', 'F.Mask', 'B.Mask', 'Edge.Cuts'])
    cli(['pcb', 'export', 'gerbers', '-l', layers, '--subtract-soldermask', '--use-drill-file-origin', '-o', gd + '/', b.path])
    # drill は統合 (1 ファイル) と PTH/NPTH 分割の両方を出し、zip 側の形式に合わせて比べる
    cli(['pcb', 'export', 'drill', '--drill-origin', 'plot', '--format', 'excellon', '--excellon-units', 'mm', '-o', gd + '/', b.path])
    gds = os.path.join(gd, 'split'); os.makedirs(gds, exist_ok=True)
    cli(['pcb', 'export', 'drill', '--drill-origin', 'plot', '--format', 'excellon', '--excellon-units', 'mm', '--excellon-separate-th', '-o', gds + '/', b.path])
    regen = {}
    for p in sorted(glob.glob(os.path.join(gd, '*'))) + sorted(glob.glob(os.path.join(gds, '*'))):
        k = gerber_key(p, b.copper)
        if k and os.path.isfile(p):
            regen.setdefault(k, p)
    diff = []
    for x in names:
        k = keys[x]
        if not k:
            continue
        p = regen.get(k)
        if not p:
            diff.append(f'{x}: 出し直しに無い (層 {k})'); continue
        if norm_gerber(z.read(x)) != norm_gerber(open(p, 'rb').read()):
            diff.append(f'{x} ≠ {os.path.relpath(p, gd)}')
    M.append(Metric(S, 'F05_gerber_fresh', 'out/ の gerber が現在の PCB と一致 (出し直して比較)', len(diff), 'pass' if not diff else 'fail', '差 0 (日付行は除外)',
                    diff[:10] or [f'一致 (drill = {drl_kind})'], 'kicad-cli export'))
    # F06 出力の時刻: 回路図由来 (erc/bom/schematic.pdf) は .kicad_sch より、その他は .kicad_pcb より新しいこと
    st, pt = os.path.getmtime(ctx['sch']), os.path.getmtime(ctx['pcb'])
    old = []
    for p in glob.glob(os.path.join(od, '*')) + glob.glob(os.path.join(od, 'png', '*')) + glob.glob(os.path.join(od, 'gerber', '*')):
        if not os.path.isfile(p):
            continue
        fn = os.path.basename(p)
        ref_t = st if (fn.startswith('erc') or '_bom' in fn or 'schematic' in fn) else max(st, pt)
        if os.path.getmtime(p) < ref_t - 1:
            old.append(os.path.relpath(p, pdir))
    M.append(Metric(S, 'F06_outputs_newer', 'out/ の出力が元ファイル (.kicad_sch / .kicad_pcb) より新しい', len(old), judge(len(old), 0, 0), '古い出力 0', old[:10], 'mtime'))
    # F07 BOM / CPL
    nl = ctx.get('netlist')
    bomf, cplf = fo['bom'], fo['cpl']
    if bomf and cplf:
        bom_refs = {}; no_lcsc = []
        bh, brows = read_csv(bomf)
        dcol, lcol = _col(bh, DESIG_COL), _col(bh, LCSC_COL)
        for r in brows:
            for ref in re.split(r'[,\s]+', r.get(dcol) or ''):
                if ref:
                    bom_refs[ref] = r
                    if not lcol or not re.match(r'^\s*C\d+', r.get(lcol) or ''):
                        no_lcsc.append(ref)
        ch, crows = read_csv(cplf)
        cdcol, rcol = _col(ch, DESIG_COL), _col(ch, ROT_COL)
        cpl = {r[cdcol]: dict(r, Rotation=r.get(rcol) or 0) for r in crows if cdcol and r.get(cdcol)}
        dnp_s = {r for r, c in nl.comps.items() if c['dnp']} if nl else set()
        dnp_p = {f.ref for f in b.footprints if 'dnp' in f.attr}
        dnp = dnp_s | dnp_p
        pcb_parts = {f.ref for f in b.footprints if 'board_only' not in f.attr and not f.ref.startswith('#')}
        smd = {f.ref for f in b.footprints if 'smd' in f.attr and 'dnp' not in f.attr and 'exclude_from_pos_files' not in f.attr}
        rows = []; g = 'pass'
        def bad(msg, sev='fail'):
            nonlocal g
            rows.append(msg); g = 'fail' if sev == 'fail' or g == 'fail' else 'warn'
        x = sorted(set(bom_refs) - set(cpl)); x and bad(f'BOM にあって CPL に無い: {x[:10]}')
        x = sorted(set(cpl) - set(bom_refs)); x and bad(f'CPL にあって BOM に無い: {x[:10]}')
        x = sorted(dnp & (set(bom_refs) | set(cpl))); x and bad(f'DNP が BOM/CPL に入っている: {x}')
        x = sorted(smd - set(cpl)); x and bad(f'SMD 部品が CPL に無い: {x[:10]}')
        x = sorted(set(cpl) - pcb_parts); x and bad(f'CPL の部品番号が PCB に無い: {x[:10]}')
        no_lcsc and bad(f'BOM 行に LCSC 番号なし: {no_lcsc[:10]}')
        tht = sorted(r for r in bom_refs if (b.fp(r) and 'through_hole' in b.fp(r).attr))
        if tht:
            rows.append(f'THT 部品が BOM に {len(tht)} 個 {tht[:8]} (JLC で THT 実装を頼むか手はんだか確認)')
        M.append(Metric(S, 'F07_bom_cpl', 'BOM / CPL の整合 (部品番号・DNP 除外・SMD 全数)', len([r for r in rows if not r.startswith('THT')]), g, '食い違い 0',
                        (rows or [f'BOM {len(bom_refs)} / CPL {len(cpl)} / DNP {sorted(dnp)}']) + [f'BOM {os.path.relpath(bomf, pdir)} (列 {dcol} / {lcol})、CPL {os.path.relpath(cplf, pdir)} (列 {cdcol} / {rcol})'], 'out/*.csv'))
        # F08 回転
        rot = json.load(open(os.path.join(HERE, 'jlc_rotation.json')))
        hits = []
        for ref in cpl:
            f = b.fp(ref)
            if not f:
                continue
            fpn = f.lib.split(':')[-1]
            for ru in rot['rules']:
                if re.search(ru['match'], fpn) and ru['offset'] % 360 and not f.lib.startswith('lcsc:'):
                    hits.append(f"{ref} {fpn}: KiCad {float(cpl[ref]['Rotation']):.0f}° → JLC 推定 {(float(cpl[ref]['Rotation']) + ru['offset']) % 360:.0f}° ({ru['status']})")
                    break
        verified = cfg.get('rotation_verified', [])
        open_ = [h for h in hits if h.split()[0] not in verified]
        M.append(Metric(S, 'F08_cpl_rotation', 'CPL 回転の要確認部品 (JLC と KiCad で 0° が違う品種)', len(open_), 'pass' if not open_ else 'warn',
                        '0 (JLC の部品プレビューで確認したら review.toml rotation_verified に記録)', open_[:12], 'jlc_rotation.json'))
    else:
        M.append(Metric(S, 'F07_bom_cpl', 'BOM / CPL', None, 'fail', '', [f'BOM または CPL が無い (BOM {bomf}, CPL {cplf}。out/ 以下の *bom*jlc*.csv / *cpl*jlc*.csv などを探した)'], ''))
    # F09 LVS (gerber ↔ ネットリスト) フック
    # 優先順: KREVIEW_LVS_CMD > ../gerber-lvs/gerber_lvs.py (KREVIEW_LVS_PATH で変更可)
    #          > 旧位置 tools/gerber-lvs/{gerber_lvs.py|lvs.py|main.py}
    lvs = os.environ.get('KREVIEW_LVS_CMD')
    entry = None
    cands = [os.environ.get('KREVIEW_LVS_PATH', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gerber-lvs', 'gerber_lvs.py'))] + \
            [os.path.join(KB, 'tools', 'gerber-lvs', c) for c in ('gerber_lvs.py', 'lvs.py', 'main.py')]
    for p in cands:
        if p and os.path.exists(p):
            entry = p; break
    if lvs or entry:
        bn = lvs_board_name(pdir, name)
        cmd = (lvs.split() if lvs else ['python3', entry]) + ['--json', os.path.join(out, 'lvs.json'), pdir]
        if 'gerber_lvs' in ' '.join(cmd[:3]):
            # 基板名は .kicad_pro から取って必ず渡す (KREVIEW_LVS_CMD 経由でも。コピー先のフォルダ名から推測させない)。txt/PNG は lvs.json と同じ out/ に出る
            cmd += [bn, '--png', '-q']
        try:
            os.remove(os.path.join(out, 'lvs.json'))
        except OSError:
            pass
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=dict(os.environ, KREVIEW_BOARD_NAME=bn))
        try:
            j = json.load(open(os.path.join(out, 'lvs.json')))
            sh, op = len(j.get('shorts', [])), len(j.get('opens', []))
            ok = sh == op == 0 and j.get('status', 'ok') in ('ok', 'pass')
            det = [str(x) for x in (j.get('shorts', []) + j.get('opens', []))]
            det += [f"{f.get('code')}: {f.get('msg')}" for f in j.get('findings', []) if f.get('level') == 'error' and f.get('code') not in ('SHORT', 'OPEN')]
            M.append(Metric(S, 'F09_gerber_lvs', 'gerber ↔ ネットリスト LVS (short / open、ほかの error は詳細に)', f'{sh}/{op}', 'pass' if ok else 'fail',
                            'short 0 / open 0 / error 0', det[:8], ' '.join(cmd[:2])))
        except Exception as e:
            M.append(Metric(S, 'F09_gerber_lvs', 'gerber ↔ ネットリスト LVS', None, 'skip', '', [f'LVS の JSON が読めない: {e}', r.stderr[-200:]], ' '.join(cmd[:2])))
    else:
        M.append(Metric(S, 'F09_gerber_lvs', 'gerber ↔ ネットリスト LVS', None, 'skip', 'short 0 / open 0',
                        ['gerber-lvs が見つからない (KREVIEW_LVS_CMD / KREVIEW_LVS_PATH を設定)'], ''))
    # F11 製造データ DRC (gerber/drill の幾何: NPTH 上の銅・NPTH/外形 clearance・外形外の銅・paste 下の銅・外形が閉じていない)
    # 2026-10-14 に gerber-lvs の補助検査を tools/gerber-drc へ移した。KREVIEW_DRC_CMD > KREVIEW_DRC_PATH > tools/gerber-drc/gerber_drc.py
    dcmd = os.environ.get('KREVIEW_DRC_CMD')
    dentry = os.environ.get('KREVIEW_DRC_PATH', os.path.join(KB, 'tools', 'gerber-drc', 'gerber_drc.py'))
    if dcmd or os.path.exists(dentry):
        cmd = (dcmd.split() if dcmd else ['python3', dentry]) + ['--json', os.path.join(out, 'gerber_drc.json'), pdir]
        if not dcmd: cmd += [name, '--png', '-q']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        try:
            j = json.load(open(os.path.join(out, 'gerber_drc.json')))
            fs = j.get('findings', [])
            ne = sum(1 for f in fs if f.get('level') == 'error'); nw = sum(1 for f in fs if f.get('level') == 'warning')
            det = [f"{f.get('code')}: {f.get('msg')}" for f in fs if f.get('level') in ('error', 'warning')]
            M.append(Metric(S, 'F11_gerber_drc', '製造データ DRC (NPTH 上の銅・clearance・外形外の銅・paste、error / warning)', f'{ne}/{nw}',
                            'fail' if ne or j.get('status') == 'fail' else ('warn' if nw else 'pass'), 'error 0 / warning 0', det[:8], ' '.join(cmd[:2])))
        except Exception as e:
            M.append(Metric(S, 'F11_gerber_drc', '製造データ DRC', None, 'skip', '', [f'gerber-drc の JSON が読めない: {e}', r.stderr[-200:]], ' '.join(cmd[:2])))
    else:
        M.append(Metric(S, 'F11_gerber_drc', '製造データ DRC', None, 'skip', 'error 0 / warning 0',
                        ['gerber-drc が見つからない (tools/gerber-drc/gerber_drc.py、または KREVIEW_DRC_CMD / KREVIEW_DRC_PATH を設定)'], ''))
    return M
