#!/usr/bin/env python3
"""課題の場所が分かる注釈画像を作る (KiCad 基板 + DRC JSON + kreview.md/.json)。

使い方:
  (board.json は銅箔・シルク・外形の JSON。作る dump_board.py は KiCad の pcbnew を使うため本リポに含めない)
  python3 annotate_issues.py board.json DRC.json OUT.png [--kreview kreview.md] \
      [--changed "x,y,r,説明" ...] [--islands "C9.2,R15.2,..."]   # PIL のある python で描く

- DRC の未配線はラッツネスト (太い線)、違反はその場所を赤丸 + 番号。
- kreview.md の WARN/FAIL 行に出てくる「REF.PAD」と「(x, y)」を赤丸 + 番号 (P18/P20/P21 など)。
- --changed で今回変えた場所を青丸で示す。
- 問題に関わる Cu / シルクは目立つ色、ほかは薄く。右端に凡例と番号一覧 (日本語, Noto Sans CJK)。
- 小さい箇所 (半径 < 1.5mm) とシルク・外形の課題は下段に拡大図 (対象が枠の半分程度になる倍率、外形は太線、外周 1 mm 帯は薄赤、外形の外は灰色、
  課題のシルクは濃い黄 + 黒縁、タイトルに部品番号)。dump_board.py の silk / outline が要る。
"""
import sys, re, json, math, argparse
from PIL import Image, ImageDraw, ImageFont
ap = argparse.ArgumentParser()
ap.add_argument('board'); ap.add_argument('drc'); ap.add_argument('out')
ap.add_argument('--kreview'); ap.add_argument('--changed', action='append', default=[])
ap.add_argument('--islands', default='', help='GND 島のパッド (REF.PAD をカンマ区切り)')
ap.add_argument('--scale', type=float, default=24)
A = ap.parse_args()
FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
f14 = ImageFont.truetype(FONT, 14); f18 = ImageFont.truetype(FONT, 18); f12 = ImageFont.truetype(FONT, 12)
B = json.load(open(A.board)); X0, Y0, X1, Y1 = B['edge']
S = A.scale; PAD = 20
pads = {f"{p['ref']}.{p['num']}": p for p in B['pads']}
issues = []
D = json.load(open(A.drc))
for u in D.get('unconnected_items', []):
    it = u['items']; net = it[0]['description'].split('[')[1].split(']')[0] if '[' in it[0]['description'] else ''
    pts = [(i['pos']['x'], i['pos']['y']) for i in it]
    if 'Zone' in it[0]['description']: continue  # ベタ同士 (位置なし) は --islands で示す
    cx = sum(p[0] for p in pts) / len(pts); cy = sum(p[1] for p in pts) / len(pts)
    r = max(0.8, max(math.hypot(p[0] - cx, p[1] - cy) for p in pts) + 0.5)
    issues.append(dict(kind='未配線', x=cx, y=cy, r=r, text=f'未配線 {net}: ' + ' ↔ '.join(i['description'][:26] for i in it), nets={net}, rats=pts))
SILK = {q['uuid']: q for q in B.get('silk', [])}
OUTL = B.get('outline') or [[(X0, Y0), (X1, Y0), (X1, Y1), (X0, Y1)]]
def seg_d(px, py, a, c):
    dx, dy = c[0] - a[0], c[1] - a[1]; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / L))
    return math.hypot(px - a[0] - t * dx, py - a[1] - t * dy)
def inside(px, py):
    n = False
    for o in OUTL:
        for a, c in zip(o, o[1:] + o[:1]):
            if (a[1] > py) != (c[1] > py) and px < a[0] + (py - a[1]) * (c[0] - a[0]) / (c[1] - a[1]): n = not n
    return n
def edge_dist(pts):
    """外形からの符号付き最小距離 (内側 +、外へはみ出すと -)"""
    best = None
    for px, py in pts:
        dd = min(seg_d(px, py, a, c) for o in OUTL for a, c in zip(o, o[1:] + o[:1])) * (1 if inside(px, py) else -1)
        best = dd if best is None else min(best, dd)
    return best
for v in D.get('violations', []):
    if v['type'] in ('lib_footprint_issues', 'track_dangling'): continue
    its = [i for i in v['items'] if 'Edge.Cuts' not in i['description']] or v['items']
    pts = [(i['pos']['x'], i['pos']['y']) for i in its]
    uu = {i.get('uuid') for i in v['items'] if i.get('uuid') in SILK}
    refs = set(re.findall(r' of ([A-Z]+[0-9]+)', ' '.join(i['description'] for i in v['items'])))
    sp = [q for u in uu for poly in SILK[u]['polys'] for q in poly]
    if sp: pts = pts + sp
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2; cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    r = max(0.8, max(math.hypot(p[0] - cx, p[1] - cy) for p in pts) + 0.4)
    extra = ''
    if v['type'].startswith('silk') and sp:
        ed = edge_dist(sp)
        extra = f' / 端から {ed:.2f} mm' if ed >= 0 else f' / 外形の外へ {-ed:.2f} mm はみ出し'
    what = ', '.join(sorted(refs)) + ' ' if refs else ''
    issues.append(dict(kind='DRC', type=v['type'], x=cx, y=cy, r=r, text=f"DRC {v['severity']} {v['type']}: {what}{its[0]['description'][:30]}{extra}",
                       nets=set(re.findall(r'\[([^\]]+)\]', ' '.join(i['description'] for i in v['items']))) - {'GND'}, uuids=uu, title=f"{what}{v['type']}", edge=v['type'].startswith('silk')))
for rp in [q for q in A.islands.split(',') if q]:
    if rp in pads:
        p = pads[rp]; issues.append(dict(kind='GND島', x=p['x'], y=p['y'], r=0.8, text=f'GND の島 {rp} (GND ビアが届かない)', nets=set(), refs={rp}, title=f'{rp} GND 島'))
if A.kreview:
    for line in open(A.kreview, encoding='utf-8'):
        m = re.match(r'\|\s*(WARN|FAIL)\s*\|\s*(\w+?)_', line)
        if not m: continue
        cid = m.group(2); cells = line.split('|'); det = cells[-2] if len(cells) > 2 else ''
        for xy in re.findall(r'\((\d+\.?\d*),\s*(\d+\.?\d*)\)', det)[:6]:
            issues.append(dict(kind=cid, x=float(xy[0]), y=float(xy[1]), r=0.8, text=f'{m.group(1)} {cid} ({xy[0]}, {xy[1]})', nets=set(), title=f'{cid} ({xy[0]}, {xy[1]})', vpt=(float(xy[0]), float(xy[1]))))
        for rp in re.findall(r'\b([A-Z]+\d+\.\d+)\b', det)[:8]:
            if rp in pads:
                p = pads[rp]; issues.append(dict(kind=cid, x=p['x'], y=p['y'], r=0.7, text=f'{m.group(1)} {cid} {rp}', nets=set(), refs={rp}, title=f'{rp} {cid}', vpad=rp))
changed = []
for c in A.changed:
    x, y, r, t = c.split(',', 3); changed.append(dict(x=float(x), y=float(y), r=float(r), text=t))
hl_nets = set().union(*[i['nets'] for i in issues]) if issues else set()
hl_refs = set().union(*[i.get('refs', set()) for i in issues]) if issues else set()
hl_silk = set().union(*[i.get('uuids', set()) for i in issues]) if issues else set()
def pad_bbox(k):
    q = [c for poly in pads[k]['poly'].values() for c in poly] or [(pads[k]['x'], pads[k]['y'])]
    return min(c[0] for c in q), min(c[1] for c in q), max(c[0] for c in q), max(c[1] for c in q)
hl_vias = set()
for i in issues:
    for n, v in enumerate(B['vias']):
        if i.get('vpad') and i['vpad'] in pads:
            x0, y0, x1, y1 = pad_bbox(i['vpad'])
            if x0 - v['d'] / 2 <= v['x'] <= x1 + v['d'] / 2 and y0 - v['d'] / 2 <= v['y'] <= y1 + v['d'] / 2: hl_vias.add(n); i.setdefault('vias', set()).add(n)
        if i.get('vpt') and math.hypot(v['x'] - i['vpt'][0], v['y'] - i['vpt'][1]) < 0.35: hl_vias.add(n); i.setdefault('vias', set()).add(n)
W = int((X1 - X0) * S) + 2 * PAD; H = int((Y1 - Y0) * S) + 2 * PAD
small = [i for i in issues if i['r'] < 1.5 or i.get('edge')][:18]
ZW = 280; LEG = 560; ZC = max(1, (W + LEG - 10) // (ZW + 10)); ZR = (len(small) + ZC - 1) // ZC
img = Image.new('RGB', (W + LEG, H + (ZR * (ZW + 34) + 10 if small else 0)), 'white'); d = ImageDraw.Draw(img, 'RGBA')
def P(x, y, ox=0, oy=0, s=S, x0=X0, y0=Y0): return (ox + PAD + (x - x0) * s, oy + PAD + (y - y0) * s)
def draw_board(ox, oy, s, x0, y0, w, h, dd, zoom=False):
    pp = lambda x, y: (ox + (x - x0) * s, oy + (y - y0) * s)
    if zoom:  # 外周 1 mm の銅箔禁止帯 (薄い赤)。外形の外は灰色で後から塗る
        for o in OUTL:
            dd.line([pp(*q) for q in o + o[:1]], fill=(255, 120, 120, 70), width=max(2, int(2.0 * s)))
    for t in B['tracks']:
        hl = t['net'] in hl_nets
        col = (255, 0, 255, 255) if hl else ((220, 60, 60, 45 if not zoom else 80) if t['l'] == 'F' else (60, 60, 220, 45 if not zoom else 80))
        dd.line([pp(t['x1'], t['y1']), pp(t['x2'], t['y2'])], fill=col, width=max(1, int(t['w'] * s)))
    for n, v in enumerate(B['vias']):
        r = v['d'] / 2 * s; c = pp(v['x'], v['y'])
        hl = v['net'] in hl_nets or n in hl_vias
        dd.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=(255, 0, 255, 255) if hl else (150, 150, 150, 80 if not zoom else 140),
                   outline=(120, 0, 120, 255) if hl and zoom else None, width=2 if hl and zoom else 0)
    for key, p in pads.items():
        hl = key in hl_refs or p['net'] in hl_nets
        for poly in p['poly'].values():
            dd.polygon([pp(*q) for q in poly], fill=(255, 140, 0, 235) if hl else (200, 170, 90, 60 if not zoom else 110), outline=(150, 70, 0, 255) if hl and zoom else None)
    for q in B.get('silk', []):  # シルク: ふつうは薄い灰、課題のシルクは濃い黄 + 黒縁
        hl = q['uuid'] in hl_silk
        for poly in q['polys']:
            if len(poly) < 3: continue
            if hl:
                dd.polygon([pp(*c) for c in poly], fill=(200, 150, 0, 255), outline=(0, 0, 0, 255), width=2 if zoom else 1)
            else:
                dd.polygon([pp(*c) for c in poly], fill=(120, 120, 120, 60 if not zoom else 110))
    refs = {k.split('.')[0] for k in hl_refs}
    for rf in refs:
        xs = [q[0] for k, p in pads.items() if k.split('.')[0] == rf for poly in p['poly'].values() for q in poly]
        ys = [q[1] for k, p in pads.items() if k.split('.')[0] == rf for poly in p['poly'].values() for q in poly]
        if xs: dd.rectangle([pp(min(xs) - .2, min(ys) - .2), pp(max(xs) + .2, max(ys) + .2)], outline=(0, 170, 0, 255), width=2)
    for o in OUTL:
        dd.line([pp(*q) for q in o + o[:1]], fill=(0, 0, 0, 255), width=4 if zoom else 1)
draw_board(PAD, PAD, S, X0, Y0, W, H, d)
for k, i in enumerate(issues, 1):
    for a, c in zip(i.get('rats', [])[:-1], i.get('rats', [])[1:]): d.line([P(*a), P(*c)], fill=(255, 0, 0, 255), width=4)
    c = P(i['x'], i['y']); r = i['r'] * S
    d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=(230, 0, 0, 255), width=3)
    d.text((c[0] + r * .7, c[1] - r - 16), str(k), fill=(230, 0, 0), font=f14)
for k, i in enumerate(changed, 1):
    c = P(i['x'], i['y']); r = i['r'] * S
    d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=(0, 90, 255, 255), width=3)
    d.text((c[0] - r, c[1] + r), f'C{k}', fill=(0, 90, 255), font=f14)
# 凡例と番号一覧
x = W + 10; y = 10
d.text((x, y), '凡例', fill='black', font=f18); y += 26
for col, t in (((230, 0, 0), '赤丸 = 残った課題 (番号は下の一覧)'), ((0, 90, 255), '青丸 = 今回変えた場所 (C 番号)'), ((255, 0, 0), '太い赤線 = 未配線のラッツネスト'),
               ((255, 0, 255), 'マゼンタ = 課題に関わる配線・ビア'), ((255, 140, 0), '橙 = 課題に関わるパッド'), ((200, 150, 0), '濃い黄 + 黒縁 = 課題のシルク'), ((0, 170, 0), '緑枠 = 課題に関わる部品 (外形の目安)')):
    d.rectangle([x, y + 4, x + 14, y + 16], fill=col); d.text((x + 20, y), t, fill='black', font=f12); y += 18
y += 8; d.text((x, y), '番号一覧', fill='black', font=f18); y += 26
for k, i in enumerate(issues, 1):
    if y > img.height - 20: d.text((x, y), f'… ほか {len(issues) - k + 1} 件', fill='black', font=f12); break
    d.text((x, y), f'{k}. {i["text"]}'[:60], fill=(160, 0, 0), font=f12); y += 16
for k, i in enumerate(changed, 1):
    if y > img.height - 20: break
    d.text((x, y), f'C{k}. {i["text"]}'[:60], fill=(0, 60, 200), font=f12); y += 16
# 拡大図: 対象 (課題のシルク・パッド・ビア・ラッツネスト) が枠の半分くらいになる倍率。外形は太い黒線、外周 1 mm 帯は薄い赤、外形の外は灰色
def obj_pts(i):
    q = [(i['x'], i['y'])] + list(i.get('rats', []))
    for u in i.get('uuids', ()):
        q += [c for poly in SILK[u]['polys'] for c in poly]
    for k in i.get('refs', ()):
        if k in pads: x0, y0, x1, y1 = pad_bbox(k); q += [(x0, y0), (x1, y1)]
    for n in i.get('vias', ()):
        v = B['vias'][n]; q += [(v['x'] - v['d'] / 2, v['y'] - v['d'] / 2), (v['x'] + v['d'] / 2, v['y'] + v['d'] / 2)]
    return q
if small:
    zy0 = H + 20
    for n, i in enumerate(small):
        ox = 10 + (n % ZC) * (ZW + 10); zy = zy0 + (n // ZC) * (ZW + 34)
        q = obj_pts(i); bx0, by0, bx1, by1 = min(c[0] for c in q), min(c[1] for c in q), max(c[0] for c in q), max(c[1] for c in q)
        size = max(bx1 - bx0, by1 - by0, 0.4); zs = max(15, min(220, ZW / 2 / size))
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2; half = ZW / zs / 2
        sub = Image.new('RGB', (ZW, ZW), 'white'); sd = ImageDraw.Draw(sub, 'RGBA')
        draw_board(0, 0, zs, cx - half, cy - half, ZW, ZW, sd, zoom=True)
        # 外形の外を灰色に
        mask = Image.new('L', (ZW, ZW), 0); md = ImageDraw.Draw(mask)
        for o in OUTL: md.polygon([((c[0] - cx + half) * zs, (c[1] - cy + half) * zs) for c in o], fill=255)
        sub.paste(Image.new('RGB', (ZW, ZW), (205, 205, 205)), (0, 0), Image.eval(mask, lambda a: 255 - a))
        sd = ImageDraw.Draw(sub, 'RGBA')
        for o in OUTL:
            sd.line([((c[0] - cx + half) * zs, (c[1] - cy + half) * zs) for c in o + o[:1]], fill=(0, 0, 0), width=4)
        for u in i.get('uuids', ()):  # 課題のシルクは灰色の上でも見えるよう最後にもう一度
            for poly in SILK[u]['polys']:
                if len(poly) >= 3: sd.polygon([((c[0] - cx + half) * zs, (c[1] - cy + half) * zs) for c in poly], fill=(200, 150, 0), outline=(0, 0, 0), width=2)
        for a, c in zip(i.get('rats', [])[:-1], i.get('rats', [])[1:]):
            sd.line([((a[0] - cx + half) * zs, (a[1] - cy + half) * zs), ((c[0] - cx + half) * zs, (c[1] - cy + half) * zs)], fill=(255, 0, 0), width=5)
        rr = max(i['r'] * zs, size * zs * 0.6, 20); ic = ((i['x'] - cx + half) * zs, (i['y'] - cy + half) * zs)
        sd.ellipse([ic[0] - rr, ic[1] - rr, ic[0] + rr, ic[1] + rr], outline=(230, 0, 0), width=3)
        sbar = 1.0 if zs * 1.0 < ZW / 3 else 0.5 if zs * 0.5 < ZW / 3 else 0.2   # 目盛り
        sd.line([(ZW - 10 - sbar * zs, ZW - 10), (ZW - 10, ZW - 10)], fill=(0, 0, 0), width=3); sd.text((ZW - 10 - sbar * zs, ZW - 28), f'{sbar:g} mm', fill=(0, 0, 0), font=f12)
        img.paste(sub, (ox, zy + 22)); d.rectangle([ox, zy + 22, ox + ZW, zy + 22 + ZW], outline='black')
        d.text((ox + 2, zy + 2), f"拡大 {issues.index(i) + 1}: {i.get('title', i['kind'])}"[:34], fill=(230, 0, 0), font=f14)
img.save(A.out); print('issues', len(issues), 'changed', len(changed), A.out)
