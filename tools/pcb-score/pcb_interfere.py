#!/usr/bin/env python3
"""pcb_interfere — 組立てできるか (干渉) を見る検査。DRC が「0」でも見落とすものを拾う。

  python3 pcb_interfere.py board.kicad_pcb [--out DIR] [--png] [--no-drc]

1. rules   : .kicad_pro の DRC 規則の重大度を監査 (必須規則が ignore / warning になっていないか)
2. drc     : プロジェクトを /tmp にコピーし、必須規則を error にして kicad-cli pcb drc --severity-all (元ファイルは触らない)
3. silk    : 表示されている silk 文字が pad (同じ面) に掛かっていないか (文字外形は概算。正確な判定は 2 の silk_over_copper)
4. body    : courtyard が無い / Fab 外形が courtyard からはみ出す / 3D モデルが無い / 3D 外形が courtyard からはみ出す
5. module  : modules.json の実外形・keepout (antenna 等) と他部品の courtyard の重なり、説明文と品種の食い違い、取り違え時の干渉
6. mating  : connectors.json の嵌合方向で、相手 plug の占有領域が基板外にあるか / 他部品と重ならないか、前面が基板端から奥すぎないか
結果: <out>/interfere.json, interfere.txt, (--png) <out>/png/*.png (指摘箇所の拡大図)。終了コード 0 = error 0 件。
"""
import argparse, datetime, glob, json, math, os, re, shutil, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kpcb

HERE = os.path.dirname(os.path.abspath(__file__))
KICAD_HOME = os.environ.get('KICAD_CLI_HOME', os.path.expanduser('~'))

# 組立て・製造の観点で error にすべき規則 (rules/pcb-layout-review.md §3 と同期)
REQUIRED = {
    'courtyards_overlap': 'error', 'missing_courtyard': 'error', 'malformed_courtyard': 'error',
    'silk_over_copper': 'error', 'silk_overlap': 'warning', 'silk_edge_clearance': 'warning',
    'text_height': 'warning', 'text_thickness': 'warning',
    'lib_footprint_mismatch': 'warning', 'lib_footprint_issues': 'warning', 'footprint_type_mismatch': 'warning',
    'npth_inside_courtyard': 'error', 'pth_inside_courtyard': 'error', 'copper_edge_clearance': 'error',
    'unconnected_items': 'error', 'clearance': 'error', 'shorting_items': 'error',
}
# KiCad 10 の既定 (規則が .kicad_pro に無いときの値、主要なものだけ)
KICAD_DEFAULT = {'missing_courtyard': 'ignore', 'silk_over_copper': 'warning', 'silk_overlap': 'warning', 'courtyards_overlap': 'error',
                 'silk_edge_clearance': 'warning', 'text_height': 'warning', 'text_thickness': 'warning',
                 'lib_footprint_mismatch': 'warning', 'lib_footprint_issues': 'warning', 'footprint_type_mismatch': 'ignore'}
SEV = {'ignore': 0, 'warning': 1, 'error': 2}


def audit_rules(pro):
    d = json.load(open(pro))
    rs = d.get('board', {}).get('design_settings', {}).get('rule_severities', {})
    rules = d.get('board', {}).get('design_settings', {}).get('rules', {})
    out = []
    for k, need in REQUIRED.items():
        cur = rs.get(k); eff = cur or KICAD_DEFAULT.get(k, 'error')
        out.append(dict(rule=k, current=cur or f'(未設定→既定 {eff})', effective=eff, required=need, ok=SEV[eff] >= SEV[need]))
    ignored = sorted(k for k, v in rs.items() if v == 'ignore')
    return out, ignored, {k: rules.get(k) for k in ('min_silk_clearance', 'min_text_height', 'min_text_thickness', 'min_copper_edge_clearance')}


def run_drc(board, workdir, strict, parity=False):
    """プロジェクト一式を workdir にコピーして DRC。strict なら REQUIRED を適用 (ignore→指定値、弱いものは引き上げ)。
    parity なら回路図 (同名 .kicad_sch、階層シートの *.kicad_sch も全部コピー) との照合 --schematic-parity も付ける (2026-10-16)"""
    src = board.dir; name = board.name
    os.makedirs(workdir, exist_ok=True)
    for f in os.listdir(src):
        p = os.path.join(src, f)
        if f.startswith(name + '.') and os.path.isfile(p) and not f.endswith(('.log', '.kicad_prl')) or f in ('fp-lib-table', 'sym-lib-table'):
            shutil.copy2(p, workdir)
        elif f == 'lib' and os.path.isdir(p):
            shutil.copytree(p, os.path.join(workdir, 'lib'), dirs_exist_ok=True)
        elif parity and f.endswith('.kicad_sch') and os.path.isfile(p):
            shutil.copy2(p, workdir)
    par = parity and os.path.exists(os.path.join(workdir, name + '.kicad_sch'))
    pro = os.path.join(workdir, name + '.kicad_pro')
    if strict and os.path.exists(pro):
        d = json.load(open(pro))
        rs = d.setdefault('board', {}).setdefault('design_settings', {}).setdefault('rule_severities', {})
        for k, need in REQUIRED.items():
            if SEV.get(rs.get(k, KICAD_DEFAULT.get(k, 'error')), 2) < SEV[need]:
                rs[k] = need
        json.dump(d, open(pro, 'w'), indent=2, ensure_ascii=False)
    out = os.path.join(workdir, 'drc.json')
    env = dict(os.environ, HOME=KICAD_HOME)
    r = subprocess.run(['kicad-cli', 'pcb', 'drc', '--severity-all', '--format', 'json', '--units', 'mm', '-o', out]
                       + (['--schematic-parity'] if par else []) + [os.path.join(workdir, name + '.kicad_pcb')], capture_output=True, text=True, env=env, timeout=600)
    if not os.path.exists(out):
        return {'error': r.stderr[-2000:] or r.stdout[-2000:]}
    j = json.load(open(out))
    by = {}
    for v in j.get('violations', []):
        by.setdefault(v['type'], []).append(v)
    return {'counts': {k: len(v) for k, v in sorted(by.items())}, 'violations': by,
            'unconnected': len(j.get('unconnected_items', [])), 'file': out,
            'parity': j.get('schematic_parity', []) if par else [], 'parity_checked': par}


def own_silk(board):
    """表示 silk 文字 × 同じ面の pad (mask 開口のある銅)"""
    res = []
    for t in board.texts:
        if t.hidden or not t.layer.endswith('SilkS') or not t.text.strip():
            continue
        side = t.layer[0]
        tp = t.poly(); hits = []
        for f in board.footprints:
            for p in f.pads:
                if not any(L in (f'{side}.Cu', f'{side}.Mask', '*.Cu', '*.Mask', 'F&B.Cu') for L in p.layers):
                    continue
                if kpcb.poly_intersect(tp, p.poly()):
                    hits.append(f'{f.ref}.{p.num}')
        if hits:
            res.append(dict(text=t.text, kind=t.kind, owner=t.owner.ref if t.owner else None, layer=t.layer, bbox=kpcb.bbox(tp),
                            at=[round(t.x, 2), round(t.y, 2)], size=t.size, pads=hits, n_pads=len(hits)))
    return res


def ref_checks(board):
    """表示されている Reference が基板外にある / 自分の courtyard から遠く、他部品の方が近い (取り違えの元)"""
    outer, holes, _ = board.outline()
    res = []
    for f in board.footprints:
        mine = f.courtyard()
        for t in f.texts:
            if t.kind != 'property' or t.hidden or not t.layer.endswith('SilkS') or t.text != f.ref:
                continue
            tp = t.poly()
            out_pts = [q for q in tp if outer and not kpcb.point_in_poly(q[0], q[1], outer)]
            if out_pts:
                res.append(dict(ref=f.ref, sev='warning', msg=f'Reference 文字が基板外形の外に出ている @({t.x:.2f},{t.y:.2f}) (silk が切れる)'))
            if not mine:
                continue
            d_own = min(kpcb.poly_dist(tp, p) for p in mine)
            near = None
            for g in board.footprints:
                if g is f:
                    continue
                for p in g.courtyard():
                    d = kpcb.poly_dist(tp, p)
                    if near is None or d < near[1]:
                        near = (g.ref, d)
            if d_own > 1.5 and near and near[1] < d_own + 0.5:
                res.append(dict(ref=f.ref, sev='warning', msg=f'Reference 文字が自分から {d_own:.1f} mm、{near[0]} から {near[1]:.1f} mm (どちらの部品か紛らわしい)'))
    return res


def body_checks(board):
    res = []
    for f in board.footprints:
        cy = f.courtyard(); cyb = f.courtyard_bbox()
        if not cy:
            res.append(dict(ref=f.ref, fp=f.lib, issue='courtyard 無し (閉じた courtyard が無い)', sev='error'))
        fab = f.fab_bbox()
        if cyb and fab and (fab[0] < cyb[0] - 0.01 or fab[1] < cyb[1] - 0.01 or fab[2] > cyb[2] + 0.01 or fab[3] > cyb[3] + 0.01):
            res.append(dict(ref=f.ref, fp=f.lib, issue='Fab (本体) 外形が courtyard からはみ出す', sev='error'))
        vis = [m for m in f.models if not m['hide']]
        mech = f.pads and all(p.type == 'np_thru_hole' for p in f.pads)   # 取付穴など NPTH だけの部品は本体が無い (3D 不要、2026-10-14)
        if not vis and 'board_only' not in f.attr and f.pads and not mech:
            res.append(dict(ref=f.ref, fp=f.lib, issue='3D モデル指定なし', sev='warning'))
        for m in vis:
            mf = kpcb.resolve_model(board, m['path'])
            if not mf:
                res.append(dict(ref=f.ref, fp=f.lib, issue=f'3D モデルのファイルが無い: {m["path"]} (3D 表示・干渉確認ができない)', sev='warning'))
                continue
            mb = kpcb.model_bbox_local(mf, m)
            if not mb:
                continue
            (x0, y0, x1, y1), (z0, z1) = mb
            # courtyard (ローカル) と比べる
            if cy:
                loc = [f.to_local(*q) for p in cy for q in p]
                c = kpcb.bbox(loc)
                over = max(c[0] - x0, c[1] - y0, x1 - c[2], y1 - c[3])
                if over > 0.3:
                    res.append(dict(ref=f.ref, fp=f.lib, issue=f'3D モデル外形が courtyard から {over:.2f} mm はみ出す (モデルの向き・offset 誤り or courtyard 不足)',
                                    sev='warning', model=os.path.basename(mf), model_bbox_local=[round(v, 2) for v in (x0, y0, x1, y1)],
                                    courtyard_local=[round(v, 2) for v in c], height=round(z1, 2)))
    return res


def module_checks(board, table):
    res = []
    for f in board.footprints:
        for m in table:
            if not re.search(m['match'], f.lib):
                continue
            body = kpcb.rect_local(f, m['body'])
            fab = f.fab_bbox()
            item = dict(ref=f.ref, fp=f.lib, module=m['name'], source=m.get('source'), body_abs=[[round(x, 2), round(y, 2)] for x, y in body],
                        findings=[])
            if fab:
                loc = kpcb.bbox([f.to_local(x, y) for x, y in ((fab[0], fab[1]), (fab[2], fab[3]))])
                diff = max(abs(a - b) for a, b in zip(loc, m['body']))
                item['fab_vs_datasheet_mm'] = round(diff, 2)
                if diff > 0.3:
                    item['findings'].append(dict(sev='warning', msg=f'Fab 外形がデータシート外形と {diff:.2f} mm 違う'))
            cyb = f.courtyard_bbox()
            if cyb:
                cyl = kpcb.bbox([f.to_local(x, y) for x, y in ((cyb[0], cyb[1]), (cyb[2], cyb[3]))])
                if cyl[0] > m['body'][0] or cyl[1] > m['body'][1] or cyl[2] < m['body'][2] or cyl[3] < m['body'][3]:
                    item['findings'].append(dict(sev='error', msg='courtyard が実外形より小さい'))
            desc = (f.props.get('Description', '') + ' ' + f.props.get('Value', '')).lower()
            for w in m.get('desc_forbid', []):
                if w.lower() in desc:
                    item['findings'].append(dict(sev='warning', msg=f'Description/Value に "{w}" とある (品種 {m["name"]} と食い違い。BOM/発注の取り違えの元)'))
            zones = [('実外形', body, 'error')]
            if 'keepout' in m:
                zones.append((m.get('keepout_name', 'keepout'), kpcb.rect_local(f, m['keepout']), 'error'))
            if 'confusable' in m:
                c = m['confusable']
                zones.append((c['name'], kpcb.rect_local(f, c['body']), 'info'))
            nb = []
            for g in board.footprints:
                if g is f:
                    continue
                for gp in g.courtyard() or []:
                    for zname, zp, sev in zones:
                        d = kpcb.poly_dist(gp, zp)
                        if d == 0:
                            nb.append(dict(ref=g.ref, zone=zname, sev=sev, overlap_mm2=round(kpcb.overlap_area(gp, zp), 2)))
                        elif zname == '実外形' and d < 1.0:
                            nb.append(dict(ref=g.ref, zone=zname, sev='info', gap_mm=round(d, 2)))
            item['neighbors'] = nb
            # 本体 (pad では無く courtyard でも無く) から近い部品の実距離 (Fab/pads から)
            for n in nb:
                if n['sev'] == 'error':
                    item['findings'].append(dict(sev='error', msg=f"{n['ref']} の courtyard が {m['name']} の {n['zone']} と重なる ({n.get('overlap_mm2')} mm2)"))
                elif 'overlap_mm2' in n:
                    item['findings'].append(dict(sev='info', msg=f"{n['ref']} は「{n['zone']}」と重なる ({n['overlap_mm2']} mm2)"))
            res.append(item)
    return res


def ray_exit(outer, holes, p, d, maxlen=40.0, step=0.05):
    """p から d 方向に進んで基板 (outer - holes) の外に出るまでの距離。p が既に外なら負 (外→中に入るまで)"""
    def inside(x, y):
        return kpcb.point_in_poly(x, y, outer) and not any(kpcb.point_in_poly(x, y, h) for h in holes)
    if inside(*p):
        t = 0.0
        while t < maxlen and inside(p[0] + d[0] * t, p[1] + d[1] * t):
            t += step
        return t
    t = 0.0
    while t < maxlen and not inside(p[0] - d[0] * t, p[1] - d[1] * t):
        t += step
    return -t


def mating_checks(board, table):
    outer, holes, _ = board.outline()
    res = []
    for f in board.footprints:
        lcsc = f.props.get('LCSC', '') or f.props.get('LCSC Part', '')
        rule = None
        for c in table:
            if (c['match'].startswith('lcsc:C') and lcsc and c['match'][5:] == lcsc) or (not c['match'].startswith('lcsc:C') and re.search(c['match'], f.lib)):
                rule = c; break
        is_conn = f.lib.startswith('Connector') or f.ref.startswith(('J', 'P', 'CN')) or 'CONN' in f.lib.upper() or 'TF-' in f.lib
        if not rule:
            if is_conn:
                res.append(dict(ref=f.ref, fp=f.lib, sev='warning', status='unknown',
                                msg='嵌合方向が connectors.json に無い (3D/データシートで確認して表に足す)'))
            continue
        cy = f.courtyard()
        cyl = kpcb.bbox([f.to_local(*q) for p in cy for q in p]) if cy else None
        it = dict(ref=f.ref, fp=f.lib, entry=rule['entry'], note=rule.get('note'), lcsc=lcsc or None)
        if rule['entry'] == 'top':
            m = rule.get('margin', 0.5)
            fab = f.fab_bbox()
            base = kpcb.bbox([f.to_local(x, y) for x, y in ((fab[0], fab[1]), (fab[2], fab[3]))]) if fab else (
                (cyl[0] + 0.25, cyl[1] + 0.25, cyl[2] - 0.25, cyl[3] - 0.25) if cyl else None)
            if base is None:
                continue
            zone = kpcb.rect_local(f, (base[0] - m, base[1] - m, base[2] + m, base[3] + m))
            hits = [g.ref for g in board.footprints if g is not f and any(kpcb.poly_intersect(gp, zone) for gp in g.courtyard())]
            it.update(keepout=[[round(x, 2), round(y, 2)] for x, y in zone], hits=hits,
                      sev='warning' if hits else 'ok', status='top-entry',
                      msg=(f'上挿し。plug + 指の余白 {m} mm に {hits} の courtyard (背の高い部品ならロック操作の邪魔)' if hits else
                           f'上挿し。plug + 指の余白 {m} mm に他部品なし (基板端からの距離は嵌合に無関係)'))
            res.append(it); continue
        d = rule['dir']; ang = f.angle
        dx, dy = kpcb.rot(d[0], d[1], ang)
        # 前面
        if 'front' in rule:
            front = rule['front']
        else:
            front = (cyl[2] if d[0] > 0 else -cyl[0] if d[0] < 0 else cyl[3] if d[1] > 0 else -cyl[1]) - 0.25
        cx = ((cyl[0] + cyl[2]) / 2) if cyl else 0.0
        cyc = ((cyl[1] + cyl[3]) / 2) if cyl else 0.0
        if d[1] != 0:
            fl = (cx, front * d[1])
            w = rule.get('plug_w') or ((cyl[2] - cyl[0]) if cyl else 5)
            kz = (cx - w / 2, min(fl[1], fl[1] + d[1] * rule['plug_len']), cx + w / 2, max(fl[1], fl[1] + d[1] * rule['plug_len']))
        else:
            fl = (front * d[0], cyc)
            w = rule.get('plug_w') or ((cyl[3] - cyl[1]) if cyl else 5)
            kz = (min(fl[0], fl[0] + d[0] * rule['plug_len']), cyc - w / 2, max(fl[0], fl[0] + d[0] * rule['plug_len']), cyc + w / 2)
        fp_abs = f.tf(*fl)
        inset = ray_exit(outer, holes, fp_abs, (dx, dy))
        zone = kpcb.rect_local(f, kz)
        hits = [g.ref for g in board.footprints if g is not f and any(kpcb.poly_intersect(gp, zone) for gp in g.courtyard())]
        bad = inset > rule['max_inset'] + 1e-6
        it.update(dir_abs=[round(dx, 3), round(dy, 3)], front_abs=[round(fp_abs[0], 2), round(fp_abs[1], 2)], inset_mm=round(inset, 2),
                  max_inset=rule['max_inset'], keepout=[[round(x, 2), round(y, 2)] for x, y in zone], hits=hits, status='side-entry',
                  sev='error' if (bad or hits) else 'ok',
                  msg=(f"横挿し。前面が基板端から {inset:.2f} mm 内側 (許容 {rule['max_inset']} mm)" if inset >= 0 else
                       f"横挿し。前面が基板端から {-inset:.2f} mm はみ出し") + (f'、plug 領域に {hits}' if hits else '') +
                      (' → plug が基板/部品に当たる' if (bad or hits) else ' → OK'))
        res.append(it)
    return res


# ---------------------------------------------------------------- PNG (指摘箇所の拡大図)
def draw_zoom(board, center_bb, out, title, extra=(), scale=40, pad=4.0):
    from PIL import Image, ImageDraw, ImageFont
    x0, y0, x1, y1 = center_bb[0] - pad, center_bb[1] - pad, center_bb[2] + pad, center_bb[3] + pad
    minw = 760 / scale
    if x1 - x0 < minw:
        cx = (x0 + x1) / 2; x0, x1 = cx - minw / 2, cx + minw / 2
    W, H = int((x1 - x0) * scale), int((y1 - y0) * scale) + 40
    im = Image.new('RGB', (W, H), (18, 32, 24)); d = ImageDraw.Draw(im, 'RGBA')
    try:
        font = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 16)
        fs = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 12)
    except Exception:
        font = fs = ImageFont.load_default()
    P = lambda p: ((p[0] - x0) * scale, (p[1] - y0) * scale + 40)
    outer, holes, _ = board.outline()
    if outer:
        d.polygon([P(p) for p in outer], fill=(40, 70, 45), outline=(230, 230, 0))
    for t in board.tracks + board.arcs:
        if t['layer'] in ('F.Cu', 'B.Cu'):
            col = (190, 80, 60, 110) if t['layer'] == 'F.Cu' else (70, 110, 200, 90)
            d.line([P(p) for p in t['pts']], fill=col, width=max(1, int(t['w'] * scale)))
    for f in board.footprints:
        for p in f.pads:
            d.polygon([P(q) for q in p.poly()], fill=(200, 160, 60, 220))
        for cp in f.courtyard():
            d.line([P(q) for q in cp + [cp[0]]], fill=(255, 0, 255), width=1)
        for poly, paths, w in f.layer_shapes('Fab'):
            for pa in paths:
                d.line([P(q) for q in pa], fill=(160, 160, 255), width=1)
        cb = f.courtyard_bbox()
        if cb and cb[2] > x0 and cb[0] < x1 and cb[3] > y0 and cb[1] < y1:
            d.text(P((cb[0], cb[1] - 0.1)), f.ref, fill=(255, 120, 255), font=fs, anchor='lb')
    for t in board.texts:
        if t.hidden or not t.layer.endswith('SilkS'):
            continue
        tp = t.poly()
        d.polygon([P(q) for q in tp], outline=(255, 255, 255), fill=(255, 255, 255, 60))
    for poly, col, label in extra:
        d.polygon([P(q) for q in poly], outline=col, fill=col[:3] + (50,))
        if label:
            d.text(P(poly[0]), label, fill=col, font=fs, anchor='lt')
    d.rectangle([0, 0, W, 38], fill=(0, 0, 0))
    d.text((6, 4), title, fill=(255, 255, 255), font=font)
    d.text((W - 6, H - 4), f'1 mm = {scale} px | 桃: courtyard, 青: Fab, 白: silk 文字 (概算), 黄: pad', fill=(220, 220, 220), font=fs, anchor='rb')
    im.save(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pcb')
    ap.add_argument('--out', help='出力ディレクトリ (既定 /tmp/pcbcheck/<名前>-<日時>)')
    ap.add_argument('--no-drc', action='store_true')
    ap.add_argument('--png', action='store_true', help='指摘箇所の拡大図 (PNG) を出す')
    a = ap.parse_args()
    b = kpcb.Board(a.pcb)
    out = a.out or f"/tmp/pcbcheck/{b.name}-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    os.makedirs(out, exist_ok=True)
    R = {'board': b.name, 'file': b.path, 'out': out}
    pro = os.path.join(b.dir, b.name + '.kicad_pro')
    if os.path.exists(pro):
        R['rules'], R['ignored_rules'], R['rule_values'] = audit_rules(pro)
    if not a.no_drc:
        R['drc_project'] = run_drc(b, os.path.join(out, 'drc-project'), strict=False)
        R['drc_strict'] = run_drc(b, os.path.join(out, 'drc-strict'), strict=True)
    R['silk_over_pad'] = own_silk(b)
    R['ref_text'] = ref_checks(b)
    R['body'] = body_checks(b)
    R['module'] = module_checks(b, json.load(open(os.path.join(HERE, 'modules.json')))['modules'])
    R['mating'] = mating_checks(b, json.load(open(os.path.join(HERE, 'connectors.json')))['connectors'])
    # ---- text
    L = [f'== {b.name}  ({b.path})', f'   出力: {out}']
    if 'rules' in R:
        bad = [r for r in R['rules'] if not r['ok']]
        L.append(f"[rules] 必須規則 {len(R['rules'])} 件中 {len(bad)} 件が弱い: " + ', '.join(f"{r['rule']}={r['effective']}(要 {r['required']})" for r in bad))
        L.append(f"        ignore 指定: {R['ignored_rules']}   値: {R['rule_values']}")
    n_err = 0
    if 'drc_strict' in R:
        p, s = R['drc_project'], R['drc_strict']
        if 'error' in s:
            L.append(f"[drc] 失敗: {s['error'][:300]}")
        else:
            L.append(f"[drc] プロジェクト設定のまま: {p.get('counts')} 未接続 {p.get('unconnected')}")
            L.append(f"[drc] 必須規則を有効にすると: {s['counts']} 未接続 {s['unconnected']}")
            for k, vs in s['violations'].items():
                if k in p.get('counts', {}) and p['counts'][k] == len(vs):
                    continue
                for v in vs[:12]:
                    items = '; '.join(f"{i.get('description', '')[:70]} @({i['pos']['x']:.2f},{i['pos']['y']:.2f})" for i in v.get('items', [])[:2])
                    L.append(f"   {v['severity']:7} {k}: {items}")
                if len(vs) > 12:
                    L.append(f'   ... {k} ほか {len(vs) - 12} 件')
            n_err += sum(1 for vs in s['violations'].values() for v in vs if v['severity'] == 'error')
    L.append(f"[silk] 表示文字が pad に掛かる (概算): {len(R['silk_over_pad'])} 件")
    R['silk_over_pad'].sort(key=lambda x: -x['n_pads'])
    for s_ in R['silk_over_pad']:
        L.append(f"   '{s_['text']}' ({s_['kind']}{' of ' + s_['owner'] if s_['owner'] else ''}, {s_['layer']} @{s_['at']}) → pad {s_['n_pads']} 個: {s_['pads'][:8]}")
    L.append(f"[ref] Reference 文字の位置: {len(R['ref_text'])} 件")
    for x in R['ref_text']:
        L.append(f"   {x['sev']:7} {x['ref']}: {x['msg']}")
    L.append(f"[body] {len(R['body'])} 件")
    for x in R['body']:
        L.append(f"   {x['sev']:7} {x['ref']} ({x['fp']}): {x['issue']}")
        n_err += x['sev'] == 'error'
    for m in R['module']:
        L.append(f"[module] {m['ref']} {m['module']} (Fab とデータシート外形の差 {m.get('fab_vs_datasheet_mm')} mm) 出典: {m['source']}")
        for fnd in m['findings']:
            L.append(f"   {fnd['sev']:7} {fnd['msg']}")
            n_err += fnd['sev'] == 'error'
        near = [n for n in m['neighbors'] if 'gap_mm' in n]
        if near:
            L.append('   近い部品 (実外形まで 1 mm 未満): ' + ', '.join(f"{n['ref']} {n['gap_mm']} mm" for n in near))
    L.append('[mating]')
    for x in R['mating']:
        L.append(f"   {x['sev']:7} {x['ref']} ({x['fp']}): {x['msg']}")
        n_err += x['sev'] == 'error'
    R['n_error'] = n_err
    L.append(f'error 合計 (DRC strict + 独自検査): {n_err}')
    # ---- png
    if a.png:
        pd = os.path.join(out, 'png'); os.makedirs(pd, exist_ok=True); R['png'] = []
        for m in R['module']:
            ex = [(m['body_abs'] and [tuple(p) for p in m['body_abs']], (0, 255, 255, 255), m['module'] + ' 実外形')]
            f = b.fp(m['ref']); tb = json.load(open(os.path.join(HERE, 'modules.json')))['modules']
            mt = [t for t in tb if re.search(t['match'], f.lib)][0]
            if 'confusable' in mt:
                ex.append((kpcb.rect_local(f, mt['confusable']['body']), (255, 80, 80, 255), mt['confusable']['name']))
            if 'keepout' in mt:
                ex.append((kpcb.rect_local(f, mt['keepout']), (255, 80, 80, 255), mt.get('keepout_name')))
            bb = kpcb.bbox([q for e in ex for q in e[0]])
            R['png'].append(draw_zoom(b, bb, os.path.join(pd, f"{b.name}_{m['ref']}_module.png"), f"{b.name} {m['ref']} {m['module']}: 実外形と周辺部品", ex, scale=30, pad=3))
        for x in R['mating']:
            if 'keepout' not in x:
                continue
            f = b.fp(x['ref'])
            ex = [([tuple(p) for p in x['keepout']], (0, 255, 255, 255), 'plug 領域')]
            bb = kpcb.bbox([q for q in ex[0][0]] + [q for p in f.courtyard() for q in p])
            R['png'].append(draw_zoom(b, bb, os.path.join(pd, f"{b.name}_{x['ref']}_mating.png"), f"{b.name} {x['ref']}: {x['msg'][:60]}", ex, scale=30, pad=6))
        for s_ in R['silk_over_pad'][:6]:
            bb = s_['bbox']
            safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', s_['text'])[:20]
            R['png'].append(draw_zoom(b, bb, os.path.join(pd, f"{b.name}_silk_{safe}.png"), f"{b.name} silk '{s_['text']}' が pad {s_['n_pads']} 個に掛かる", (), scale=40, pad=2))
        L.append('[png] ' + ' '.join(R['png']))
    txt = '\n'.join(L)
    open(os.path.join(out, 'interfere.txt'), 'w').write(txt + '\n')
    for k in ('drc_project', 'drc_strict'):   # JSON には件数だけ (詳細は drc.json)
        if k in R and 'violations' in R[k]:
            R[k] = {kk: vv for kk, vv in R[k].items() if kk != 'violations'}
    json.dump(R, open(os.path.join(out, 'interfere.json'), 'w'), indent=1, ensure_ascii=False)
    print(txt)
    sys.exit(1 if n_err else 0)


if __name__ == '__main__':
    main()
