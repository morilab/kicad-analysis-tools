#!/usr/bin/env python3
"""board_rules — 作者の基板標準ルール (2026-10-14) の検査。kreview P22 / P23 から呼ぶ (単体でも動く)。

  python3 board_rules.py <x.kicad_pcb | project_dir> [--json out.json]

P22 基板外周からの距離 (design_rules.json board_edge):
  - 外周 (Edge.Cuts の外周と切り欠き) から copper_mm (既定 1 mm) 以内の銅箔 (トラック・ゾーンの塗り・via・パッド) は禁止。
  - 部品: courtyard (F/B.CrtYd、無ければ部品外形 = Fab とパッドの外接矩形) と外周の最短距離が part_mm 未満は禁止。
    part_mm は tools/gerber-drc/rules/user/morilab.yaml の user.edge_keepout_parts (value) から読む。
    ファイル・キーが無ければ既定 3 mm で判定し、その旨を注意 (notes) に出す。
    例外: フットプリント名 Connector*:* / MountingHole:* / TestPoint:*、リファレンス J*、YAML の except.footprints、
    review.toml [config.board_edge] exempt の個別指定。
  - 銅箔の例外: 参照記号 (J/P…) かフットプリント名 (design_rules.json exempt_ref / exempt_fp) と review.toml exempt。
    例外部品のパッドと、その courtyard の中の配線・via・塗りは銅箔の規則から外す。
P23 シルクの重なり (design_rules.json silk_overlap):
  - TEXT_TEXT: 表示シルク文字どうし (同じ面) の外接矩形 (回転考慮) の重なり (リファレンス文字どうしを含む)。
  - TEXT_PAD : シルク文字とパッド (同じ面の銅・mask 開口) の重なり (pcb_interfere.own_silk を使う)。
  - TEXT_BODY: シルク文字と他部品の本体 (Fab 外形、無ければパッド外形) の重なり (実装後に文字が隠れる)。
  - LINE_PAD : シルクの線と他部品・自部品のパッドの重なり。
  文字外形は KiCad ストロークフォントの概算 (kpcb.Text.poly)。正確な silk_over_copper / silk_overlap は kicad-cli DRC の結果を併記する。
P24 取付穴の有無 (design_rules.json mounting_holes、2026-10-14):
  - 取付穴 = フットプリント名が MountingHole* の部品、または全パッドが NPTH で穴径 2 mm 以上の部品。1 個も無ければ warn
    (穴が要らない基板は review.toml [waive] P24_mounting_holes = "理由" で明記)。位置・穴径・メッキ・角からの距離・ピッチを報告する。
"""
import argparse, fnmatch, glob, json, math, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import KB
sys.path.insert(0, os.path.join(KB, 'tools', 'pcb-score'))
import kpcb
import pcb_interfere as pi


def rules():
    d = json.load(open(os.path.join(HERE, 'design_rules.json')))
    return d['board_edge'], d['silk_overlap']


# ---- 幾何 (numpy で点 × 線分の最短距離)
def _pts_to_segs(P, A, B):
    import numpy as np
    P = np.asarray(P, float).reshape(-1, 2); A = np.asarray(A, float).reshape(-1, 2); B = np.asarray(B, float).reshape(-1, 2)
    if not len(P) or not len(A):
        return np.full(len(P), np.inf)
    d = B - A; L = (d ** 2).sum(1); L[L == 0] = 1e-12
    t = (((P[:, None, :] - A[None]) * d[None]).sum(2) / L[None]).clip(0, 1)
    q = A[None] + t[..., None] * d[None]
    return np.sqrt(((P[:, None, :] - q) ** 2).sum(2)).min(1)


class Edge:
    def __init__(s, b):
        outer, holes, _ = b.outline()
        s.outer, s.holes = outer, holes or []
        A, B = [], []
        for poly in ([outer] if outer else []) + s.holes:
            for i in range(len(poly)):
                A.append(poly[i - 1]); B.append(poly[i])
        s.A, s.B = A, B
        s.V = [p for poly in ([outer] if outer else []) + s.holes for p in poly]

    def inside(s, x, y):
        if not s.outer:
            return True
        return kpcb.point_in_poly(x, y, s.outer) and not any(kpcb.point_in_poly(x, y, h) for h in s.holes)

    def dist_path(s, pts, closed=False):
        """折れ線 / 多角形と外周の最短距離。外周の外 (または切り欠きの中) に出ている点があれば 0"""
        if not s.A or not pts:
            return math.inf
        if any(not s.inside(*p) for p in pts[:: max(1, len(pts) // 64)]):
            return 0.0
        segA = pts[:-1] + ([pts[-1]] if closed else []); segB = pts[1:] + ([pts[0]] if closed else [])
        d1 = _pts_to_segs(pts, s.A, s.B).min()
        d2 = _pts_to_segs(s.V, segA, segB).min() if segA else math.inf
        return float(min(d1, d2))


def _exempt(f, be, cfg_ex):
    if f.ref in cfg_ex:
        return f'review.toml で個別指定: {cfg_ex[f.ref] or "(理由なし)"}'
    if re.search(be['exempt_ref'], f.ref):
        return f'参照記号 {f.ref} ({be["exempt_ref"]})'
    fpn = f.lib.split(':')[-1]
    if re.search(be['exempt_fp'], fpn):
        return f'フットプリント名 {fpn}'
    return None


# ---- P22 部品 3 mm 禁止 (2026-10-14 作者 指示): しきい値は gerber-drc のユーザールール YAML から読む
PARTS_RULE_YAML = os.path.join(KB, 'tools', 'gerber-drc', 'rules', 'user', 'morilab.yaml')
PARTS_RULE_ID = 'user.edge_keepout_parts'
PARTS_DEFAULT_MM = 3.0
# 例外: フットプリント名 (lib:name) が Connector*:* / MountingHole:* / TestPoint:*、またはリファレンスが J*
PARTS_EXEMPT = ['Connector*:*', 'MountingHole:*', 'TestPoint:*', 'J*']


def parts_rule(path=None):
    """-> (part_mm, 例外パターン list, 注意 list)。ファイル・キーが無ければ既定 3 mm と注意を返す"""
    path = path or PARTS_RULE_YAML
    notes = []
    pats = list(PARTS_EXEMPT)
    rel = os.path.relpath(path, KB) if path.startswith(KB) else path
    if not os.path.exists(path):
        return PARTS_DEFAULT_MM, pats, [f'{rel} が無い → 部品の外周距離は既定 {PARTS_DEFAULT_MM:g} mm で判定']
    try:
        import yaml
        d = yaml.safe_load(open(path, encoding='utf-8')) or {}
    except Exception as e:
        return PARTS_DEFAULT_MM, pats, [f'{rel} を読めない ({type(e).__name__}: {e}) → 既定 {PARTS_DEFAULT_MM:g} mm で判定']
    r = next((x for x in (d.get('rules') or []) if isinstance(x, dict) and x.get('id') == PARTS_RULE_ID), None)
    if r is None or r.get('value') is None:
        return PARTS_DEFAULT_MM, pats, [f'{rel} に {PARTS_RULE_ID} (value) が無い → 既定 {PARTS_DEFAULT_MM:g} mm で判定']
    try:
        v = float(r['value'])
    except (TypeError, ValueError):
        return PARTS_DEFAULT_MM, pats, [f'{rel} の {PARTS_RULE_ID} の値 {r["value"]!r} が数値でない → 既定 {PARTS_DEFAULT_MM:g} mm で判定']
    for q in ((r.get('except') or {}).get('footprints') or []):
        if q not in pats:
            pats.append(str(q))
    return v, pats, notes


def _parts_exempt(f, pats, cfg_ex):
    """部品 3 mm の例外。':' を含むパターンはフットプリント名 (lib:name)、含まないものはリファレンスと照合"""
    if f.ref in cfg_ex:
        return f'review.toml で個別指定: {cfg_ex[f.ref] or "(理由なし)"}'
    name = f.lib.split(':')[-1]
    for q in pats:
        if ':' in q and fnmatch.fnmatchcase(f.lib, q):
            return f'フットプリント名 {f.lib} ({q})'
        if ':' not in q and fnmatch.fnmatchcase(f.ref, q):
            return f'リファレンス {f.ref} ({q})'
    # 標準ライブラリのフットプリントをプロジェクトのライブラリへ写したもの (例 myproj:MountingHole_3.2mm_M3) は、ライブラリ名が変わっても
    # フットプリント名が標準ライブラリ名で始まる (KiCad の命名)。'MountingHole:*' なら名前 'MountingHole*' も同じ例外にする (2026-10-16 修正、
    # 実基板の H1〜H4 が P22 の部品違反に出ていた)
    for q in pats:
        if ':' in q:
            lp, np_ = q.split(':', 1)
            if lp.strip('*') and fnmatch.fnmatchcase(name, lp + '*') and fnmatch.fnmatchcase(name, np_ if np_ != '*' else '*'):
                return f'フットプリント名 {f.lib} (ライブラリの写し、{q} と同じ名前規則)'
    return None


def _part_outline(f):
    """P22 部品の判定に使う外形: courtyard (F/B.CrtYd) の多角形 → courtyard の外接矩形 → 部品外形 (Fab とパッド) の外接矩形"""
    ps = f.courtyard()
    if ps:
        return ps, 'courtyard'
    bb = f.courtyard_bbox()
    how = 'courtyard (外接矩形)'
    if not bb:
        bbs = [x for x in (f.fab_bbox(), f.pads_bbox()) if x]
        bb = (min(x[0] for x in bbs), min(x[1] for x in bbs), max(x[2] for x in bbs), max(x[3] for x in bbs)) if bbs else None
        how = '部品外形 (外接矩形)'
    if not bb:
        return [], None
    x0, y0, x1, y1 = bb
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]], how


def _body_poly(f, which='CrtYd'):
    ps = f.polys(which)[0] if which != 'CrtYd' else f.courtyard()
    if ps:
        return ps, which
    bb = f.courtyard_bbox() if which == 'CrtYd' else f.fab_bbox()
    if bb:
        x0, y0, x1, y1 = bb
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]], which + ' (外接矩形)'
    bb = f.pads_bbox()
    if bb:
        x0, y0, x1, y1 = bb
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]], 'パッド外形'
    return [], None


def edge_checks(b, cfg=None):
    """-> dict(copper=[...], parts=[...], exempt=[...]) 各要素は dict(kind, ref/net, layer, d, msg)"""
    be, _ = rules(); cfg = cfg or {}
    c = cfg.get('board_edge', {}) or {}
    cu_mm = float(c.get('copper_mm', be['copper_mm']))
    part_mm, ppats, notes = parts_rule(c.get('parts_rule_yaml'))
    if 'part_mm' in c:
        part_mm = float(c['part_mm']); notes.append(f'review.toml [config.board_edge] part_mm = {part_mm:g} で上書き')
    ex = c.get('exempt', {}) or {}
    if isinstance(ex, list):
        ex = {r: '' for r in ex}
    E = Edge(b)
    if not E.A:
        return dict(copper=[], parts=[], exempt=[], error='基板外形 (Edge.Cuts) が無い', cu_mm=cu_mm, part_mm=part_mm, notes=notes)
    exempt = {}; ex_boxes = []
    for f in b.footprints:
        why = _exempt(f, be, ex)
        if why:
            exempt[f.ref] = why
            bb = f.courtyard_bbox() or f.pads_bbox()
            if bb:
                ex_boxes.append((bb[0] - 0.3, bb[1] - 0.3, bb[2] + 0.3, bb[3] + 0.3))
    in_ex = lambda x, y: any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in ex_boxes)
    cu = []
    near = cu_mm + 5.0
    ob = kpcb.bbox(E.V)
    def maybe_near(pts):
        bb = kpcb.bbox(pts)
        return bb[0] - ob[0] < near or bb[1] - ob[1] < near or ob[2] - bb[2] < near or ob[3] - bb[3] < near or E.holes
    for t in b.tracks + b.arcs:
        if not t['layer'].endswith('.Cu') or not maybe_near(t['pts']):
            continue
        d = E.dist_path(t['pts']) - t['w'] / 2
        if d < cu_mm:
            mid = t['pts'][len(t['pts']) // 2]
            if all(in_ex(*p) for p in t['pts']):
                continue
            cu.append(dict(kind='track', net=t['net'], layer=t['layer'], d=max(0.0, d), at=mid,
                           msg=f"配線 {t['net'] or '(net なし)'} {t['layer']} @({mid[0]:.2f},{mid[1]:.2f}) が外周から {max(0.0, d):.2f} mm"))
    for v in b.vias:
        d = E.dist_path([tuple(v['at'])]) - v['size'] / 2
        if d < cu_mm and not in_ex(*v['at']):
            cu.append(dict(kind='via', net=v['net'], layer='via', d=max(0.0, d), at=tuple(v['at']),
                           msg=f"via {v['net']} @({v['at'][0]:.2f},{v['at'][1]:.2f}) が外周から {max(0.0, d):.2f} mm"))
    for f in b.footprints:
        if f.ref in exempt:
            continue
        for p in f.pads:
            if not p.copper_layers(b.copper):
                continue
            d = E.dist_path(p.poly(), closed=True)
            if d < cu_mm:
                cu.append(dict(kind='pad', ref=f.ref, net=p.net, layer='/'.join(p.copper_layers(b.copper)[:2]), d=d, at=(p.x, p.y),
                               msg=f"パッド {f.ref}.{p.num} @({p.x:.2f},{p.y:.2f}) が外周から {d:.2f} mm"))
    zworst = {}
    for z in kpcb.kids(b.root, 'zone'):
        if kpcb.kid(z, 'keepout') is not None:
            continue
        net = str(kpcb.val(z, 'net_name', default='') or kpcb.val(z, 'net', default='') or '')
        for fp_ in kpcb.kids(z, 'filled_polygon'):
            L = str(kpcb.val(fp_, 'layer', default=''))
            pts = [(float(q[1]), float(q[2])) for q in (kpcb.kid(fp_, 'pts') or ['pts'])[1:] if isinstance(q, list) and q[0] == 'xy']
            if len(pts) < 3:
                continue
            import numpy as np
            P = np.array(pts)
            dv = _pts_to_segs(P, E.A, E.B)
            bad = [i for i in range(len(pts)) if dv[i] < cu_mm and not in_ex(*pts[i])]
            if not bad:
                continue
            i = min(bad, key=lambda k: dv[k])
            k = (net, L)
            if k not in zworst or dv[i] < zworst[k]['d']:
                zworst[k] = dict(kind='zone', net=net, layer=L, d=float(dv[i]), at=pts[i], n=len(bad),
                                 msg=f"ゾーン {net} {L} の塗りが外周から {float(dv[i]):.2f} mm @({pts[i][0]:.2f},{pts[i][1]:.2f}) (頂点 {len(bad)} 個)")
    cu += list(zworst.values())
    parts = []; pexempt = {}
    for f in b.footprints:
        why = _parts_exempt(f, ppats, ex)
        if why:
            pexempt[f.ref] = why
            continue
        if 'board_only' in f.attr and not f.pads:
            continue
        polys, how = _part_outline(f)
        if not polys:
            continue
        d = min(E.dist_path(pp, closed=True) for pp in polys)
        if d < part_mm:
            parts.append(dict(kind='part', ref=f.ref, d=d, at=(f.x, f.y), how=how,
                              msg=f"{f.ref} ({f.lib.split(':')[-1]}) の {how} が外周から {d:.2f} mm (< {part_mm:g} mm)"))
    cu.sort(key=lambda x: x['d']); parts.sort(key=lambda x: x['d'])
    return dict(copper=cu, parts=parts, exempt=sorted(f'{r}: {w}' for r, w in exempt.items()), cu_mm=cu_mm, part_mm=part_mm,
                parts_exempt=sorted(f'{r}: {w}' for r, w in pexempt.items()), notes=notes)


def mounting_holes(b, cfg=None):
    """取付穴の一覧 (P24)。戻り値 dict(holes=[...], min_count, outline_bbox)。holes の各要素は ref/x/y/drill/plated/fp/corner_mm。"""
    r = json.load(open(os.path.join(HERE, 'design_rules.json')))['mounting_holes']
    rx = re.compile(r['fp_match'])
    holes = []
    for f in b.footprints:
        pads = list(f.pads)
        dr = max((p.drill for p in pads), default=0.0)
        npth_only = bool(pads) and all(p.type == 'np_thru_hole' for p in pads)
        if rx.search(f.lib) or (npth_only and dr >= r['min_drill_mm']):
            holes.append(dict(ref=f.ref, x=round(f.x, 3), y=round(f.y, 3), drill=dr, fp=f.lib.split(':')[-1],
                              plated=any(p.type == 'thru_hole' for p in pads)))
    bb = None
    try:
        ol = b.outline()[0]
        if ol:
            bb = (min(p[0] for p in ol), min(p[1] for p in ol), max(p[0] for p in ol), max(p[1] for p in ol))
    except Exception:
        bb = None
    if bb:
        for h in holes:   # 一番近い角からの x / y 距離
            dx = min(h['x'] - bb[0], bb[2] - h['x']); dy = min(h['y'] - bb[1], bb[3] - h['y'])
            h['corner_mm'] = (round(dx, 2), round(dy, 2))
    return dict(holes=holes, min_count=int((cfg or {}).get('mounting_holes_min', r['min_count'])), outline_bbox=bb)


def pad_shape(p, n=16):
    """パッドの外形 (circle は多角形、oval は長円、roundrect/rect は矩形)。回転を考慮"""
    if p.shape == 'circle':
        r = p.w / 2
        return [(p.x + r * math.cos(2 * math.pi * k / n), p.y + r * math.sin(2 * math.pi * k / n)) for k in range(n)]
    if p.shape == 'oval' and abs(p.w - p.h) > 1e-6:
        r = min(p.w, p.h) / 2; L = (max(p.w, p.h) - 2 * r) / 2
        pts = []
        for k in range(n + 1):
            a = -math.pi / 2 + math.pi * k / n
            pts.append((L + r * math.cos(a), r * math.sin(a)))
        for k in range(n + 1):
            a = math.pi / 2 + math.pi * k / n
            pts.append((-L + r * math.cos(a), r * math.sin(a)))
        if p.h > p.w:
            pts = [(-y, x) for x, y in pts]
        return [(p.x + dx, p.y + dy) for dx, dy in (kpcb.rot(x, y, p.angle) for x, y in pts)]
    if p.shape == 'oval':
        return pad_shape(type('P', (), dict(shape='circle', w=p.w, h=p.h, x=p.x, y=p.y))(), n)
    return p.poly()


def _silk_layer(L):
    return L.endswith(('SilkS', 'Silkscreen'))


def silk_checks(b, cfg=None):
    """-> list of dict(kind, a, b, layer, at, msg)"""
    cfg = cfg or {}
    tol = float(rules()[1].get('min_overlap_mm', 0.02))
    ign = (cfg.get('silk', {}) or {}).get('ignore', {}) or {}
    if isinstance(ign, list):
        ign = {r: '' for r in ign}
    texts = [t for t in b.texts if not t.hidden and _silk_layer(t.layer) and t.text.strip() and not t.text.startswith('${')
             and not (t.owner and t.owner.ref in ign)]
    # ${REFERENCE} などの表示は実際の文字に置き換えて幅を見積もる
    for t in b.texts:
        if not t.hidden and _silk_layer(t.layer) and t.text.strip().upper() == '${REFERENCE}' and t.owner and t.owner.ref not in ign:
            t.text = t.owner.ref; texts.append(t)
    name = lambda t: (f"{t.owner.ref} の {('Reference' if t.kind == 'property' and t.text == t.owner.ref else t.kind)}「{t.text}」" if t.owner else f"文字「{t.text[:20]}」")
    res = []
    polys = [(t, t.poly()) for t in texts]
    bbs = [kpcb.bbox(p) for _, p in polys]
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            ti, tj = polys[i][0], polys[j][0]
            if ti.layer != tj.layer or not kpcb.bbox_overlap(bbs[i], bbs[j]):
                continue
            if ti.owner is not None and ti.owner is tj.owner and ti.text == tj.text:
                continue
            if kpcb.poly_intersect(polys[i][1], polys[j][1]):
                refs = ti.kind == 'property' and tj.kind == 'property'
                res.append(dict(kind='TEXT_TEXT', a=name(ti), b=name(tj), layer=ti.layer, at=(ti.x, ti.y),
                                refs=[x.owner.ref for x in (ti, tj) if x.owner],
                                msg=f"{name(ti)} と {name(tj)} が重なる ({ti.layer})" + (' [リファレンス文字どうし]' if refs else '')))
    for s_ in pi.own_silk(b):
        if s_['owner'] in ign:
            continue
        res.append(dict(kind='TEXT_PAD', a=s_['text'], b=','.join(s_['pads'][:4]), layer=s_['layer'], at=tuple(s_['at']), refs=[s_['owner']] if s_['owner'] else [],
                        msg=f"{(s_['owner'] + ' の ') if s_['owner'] else ''}文字「{s_['text'][:20]}」がパッド {', '.join(s_['pads'][:4])} に掛かる ({s_['layer']})"))
    bodies = []
    for f in b.footprints:
        if f.ref in ign:
            continue
        side = f.side
        ps, how = _body_poly(f, 'Fab')
        bodies += [(f, side, p, how) for p in ps]
    for t, tp in polys:
        side = t.layer[0]
        tb = kpcb.bbox(tp)
        for f, fs, bp, how in bodies:
            if fs != side or f is t.owner or not kpcb.bbox_overlap(tb, kpcb.bbox(bp)):
                continue
            if kpcb.poly_intersect(tp, bp):
                res.append(dict(kind='TEXT_BODY', a=name(t), b=f.ref, layer=t.layer, at=(t.x, t.y), refs=[x for x in (t.owner.ref if t.owner else None, f.ref) if x],
                                msg=f"{name(t)} が {f.ref} の本体 ({how}) に重なる (実装後に隠れる)"))
                break
    # シルクの線 × パッド (空間ハッシュで絞る)
    grid = {}
    G = 2.0
    for f in b.footprints:
        for p in f.pads:
            pp = pad_shape(p); bb = kpcb.bbox(pp)
            for gx in range(int(bb[0] // G), int(bb[2] // G) + 1):
                for gy in range(int(bb[1] // G), int(bb[3] // G) + 1):
                    grid.setdefault((gx, gy), []).append((f, p, pp))
    seen = set()
    sources = [(f, L, it) for f in b.footprints if f.ref not in ign for L, its in f.graphics.items() if _silk_layer(L) for it in its]
    sources += [(None, L, it) for L, its in b.gr.items() if _silk_layer(L) for it in its]
    for f, L, (poly, paths, w) in sources:
        side = L[0]
        w = float(w or 0.12)
        segs = [(a, c) for path in paths for a, c in zip(path, path[1:])]
        for a, c in segs:
            dx, dy = c[0] - a[0], c[1] - a[1]; ln = math.hypot(dx, dy)
            if ln == 0:
                continue
            hw = max(0.01, w / 2 - tol)        # 許容 (design_rules.json silk_overlap.min_overlap_mm、既定 0.02 mm。接する・かすめるだけは重ならない扱い)
            nx, ny = -dy / ln * hw, dx / ln * hw
            rect = [(a[0] + nx, a[1] + ny), (c[0] + nx, c[1] + ny), (c[0] - nx, c[1] - ny), (a[0] - nx, a[1] - ny)]
            bb = kpcb.bbox(rect)
            cand = {id(x[1]): x for gx in range(int(bb[0] // G), int(bb[2] // G) + 1) for gy in range(int(bb[1] // G), int(bb[3] // G) + 1) for x in grid.get((gx, gy), ())}
            for g, p, pp in cand.values():
                if not any(Lp in (f'{side}.Cu', f'{side}.Mask', '*.Cu', '*.Mask', 'F&B.Cu') for Lp in p.layers):
                    continue
                key = (f.ref if f else '-', g.ref, p.num, L)
                if key in seen:
                    continue
                if kpcb.poly_intersect(rect, pp):
                    seen.add(key)
                    res.append(dict(kind='LINE_PAD', a=(f.ref if f else '基板') + ' のシルク線', b=f'{g.ref}.{p.num}', layer=L, at=(p.x, p.y), refs=[x for x in (f.ref if f else None, g.ref) if x],
                                    msg=f"{(f.ref + ' の') if f else '基板の'}シルク線がパッド {g.ref}.{p.num} に掛かる ({L})"))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('target'); ap.add_argument('--json')
    a = ap.parse_args()
    p = a.target
    if os.path.isdir(p):
        p = glob.glob(os.path.join(p, '*.kicad_pcb'))[0]
    b = kpcb.Board(p)
    E = edge_checks(b); S = silk_checks(b)
    print(f"P22 外周 {E['cu_mm']:g} mm 以内の銅箔 {len(E['copper'])} 件 / {E['part_mm']:g} mm 以内の部品 {len(E['parts'])} 件 (例外 {len(E['exempt'])} 部品)")
    for x in E['copper'][:20] + E['parts'][:20]:
        print('  ' + x['msg'])
    for x in E['exempt']:
        print('  例外: ' + x)
    for x in E.get('notes') or []:
        print('  注意: ' + x)
    print(f"P23 シルク重なり {len(S)} 件")
    for x in S:
        print(f"  {x['kind']}: {x['msg']}")
    if a.json:
        json.dump(dict(edge=E, silk=S), open(a.json, 'w'), ensure_ascii=False, indent=1, default=str)


if __name__ == '__main__':
    sys.exit(main())
