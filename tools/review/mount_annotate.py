#!/usr/bin/env python3
"""実装性チェック (P18b・P26〜P33) の注釈画像: 点数・合否と、問題の場所を項目ごとの色の丸で示す。

  python3 mount_annotate.py <out>/kreview.json BOARD.kicad_pcb OUT.png      # kreview.py からも自動で呼ぶ (<out>/mount_check.png)

- 上段: 基板全体 (F 面の銅・ベタ・パッド・via、シルクは濃い紺、Edge.Cuts は太い黒線)。丸の色 = 項目、実線 = fail・破線 = warn、番号 = 下の一覧。
- 下段: 番号の小さい (fail 優先) 箇所の拡大 (白破線 = マスク開口、赤点線 = 穴 +0.13 mm)。
"""
import json, math, os, sys
import numpy as np
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager as fm
from matplotlib.patches import Circle, Polygon as MPoly, Rectangle
from matplotlib.lines import Line2D
from matplotlib.collections import PolyCollection, LineCollection
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'pcb-score'))
import kpcb
import mount_geom as mg

MOUNT_IDS = ['P18b_via_near_pad', 'P26_zone_connect_audit', 'P27_thermal_balance', 'P28_pad_pour_balance', 'P29_reflow_timing',
             'P30_rework_clearance', 'P31_connector_land', 'P32_paste_vs_coplanarity', 'P33_solder_wicking']
COL = {'P18b_via_near_pad': '#ff7f00', 'P27_thermal_balance': '#e7298a', 'P28_pad_pour_balance': '#7b3294', 'P29_reflow_timing': '#1b9e77',
       'P30_rework_clearance': '#1f78b4', 'P31_connector_land': '#a6761d', 'P32_paste_vs_coplanarity': '#666666', 'P33_solder_wicking': '#d7191c'}
PRIO = ['P33_solder_wicking', 'P28_pad_pour_balance', 'P27_thermal_balance', 'P18b_via_near_pad', 'P30_rework_clearance',
        'P31_connector_land', 'P32_paste_vs_coplanarity', 'P29_reflow_timing', 'P26_zone_connect_audit']   # 番号と拡大の順 (fail が先、同じ判定なら はんだ量に効くもの)
FONT = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'


def mount_score(ms):
    c = {g: sum(1 for m in ms if m['grade'] == g) for g in ('pass', 'warn', 'fail')}
    n = sum(c.values())
    sc = round((c['pass'] + 0.5 * c['warn']) / n * 100) if n else None
    grade = 'A' if c['fail'] == 0 and c['warn'] <= 1 else 'B' if c['fail'] == 0 else 'C' if c['fail'] <= 2 else 'D'
    return sc, grade, ('合格' if c['fail'] == 0 else '不合格'), c


def _seg_poly(a, b_, w):
    dx, dy = b_[0] - a[0], b_[1] - a[1]; L = math.hypot(dx, dy) or 1e-9; nx, ny = -dy / L * w / 2, dx / L * w / 2
    return [(a[0] + nx, a[1] + ny), (b_[0] + nx, b_[1] + ny), (b_[0] - nx, b_[1] - ny), (a[0] - nx, a[1] - ny)]


def _circ(c, r, n=32):
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.c_[c[0] + r * np.cos(t), c[1] + r * np.sin(t)]


def draw_board(ax, b, fills, outline, box, side='F', detail=True):
    """銅・パッド・via・シルクを描く。2026-10-16 高速化: 1 個ずつ add_patch せず PolyCollection / LineCollection にまとめ、
    拡大図 (box が基板より小さい) では box の外 (+1 mm) の図形を描かない。見た目は同じ (円は 32 角形)"""
    L = f'{side}.Cu'; m_ = 1.0
    def vis(bb):
        return bb[2] >= box[0] - m_ and bb[0] <= box[2] + m_ and bb[3] >= box[1] - m_ and bb[1] <= box[3] + m_
    def pc(polys, z, **kw):
        if polys:
            ax.add_collection(PolyCollection(polys, closed=True, zorder=z, **kw), autolim=False)
    ax.set_facecolor('#9a9a9a'); ax.add_patch(MPoly(outline, closed=True, fc='#1f5a2a', ec='none', zorder=0))
    gnd, oth = [], []
    for (lay, net), polys in fills.items():
        if lay == L:
            for p in polys:
                if vis(kpcb.bbox(p)):
                    (gnd if 'GND' in net.upper() else oth).append(p)
    pc(gnd, 1, facecolors='#c98f6b', edgecolors='none'); pc(oth, 1, facecolors='#b8b06b', edgecolors='none')
    tr = []
    for t in b.tracks + b.arcs:
        if t['layer'] == L:
            for a, c in zip(t['pts'], t['pts'][1:]):
                if vis((min(a[0], c[0]), min(a[1], c[1]), max(a[0], c[0]), max(a[1], c[1]))):
                    tr.append(_seg_poly(a, c, t['w']))
    pc(tr, 2, facecolors='#c9a227', edgecolors='none')
    npth, pads, labels = [], [], []
    for f in b.footprints:
        for p in f.pads:
            if not vis((p.x - 3, p.y - 3, p.x + 3, p.y + 3)):
                continue
            if p.type == 'np_thru_hole':
                npth.append(_circ((p.x, p.y), p.drill / 2)); continue
            if L not in p.copper_layers(b.copper):
                continue
            pads.append(p.poly())
            if detail and box[0] < p.x < box[2] and box[1] < p.y < box[3]:
                labels.append((p.x, p.y, f'{f.ref}.{p.num}'))
    pc(npth, 4, facecolors='#202020', edgecolors='none')
    pc(pads, 3, facecolors='#e8c15a', edgecolors='#333', linewidths=0.4)
    if detail:
        pc(pads, 8, facecolors='none', edgecolors='w', linestyles=[(0, (2, 2))], linewidths=0.8)
        for x, y, t in labels:
            ax.text(x, y, t, ha='center', va='center', fontsize=6.5, zorder=9, clip_on=True, weight='bold')
    vs = [v for v in b.vias if vis((v['at'][0] - 1, v['at'][1] - 1, v['at'][0] + 1, v['at'][1] + 1))]
    pc([_circ(v['at'], v['size'] / 2) for v in vs], 5, facecolors='#b0b0b0', edgecolors='#555', linewidths=0.3)
    pc([_circ(v['at'], v['drill'] / 2) for v in vs], 6, facecolors='#111', edgecolors='none')
    if detail:
        pc([_circ(v['at'], (v['drill'] + mg.DRILL_TOL_PLUS) / 2) for v in vs], 7, facecolors='none', edgecolors='#ff4040', linestyles=':', linewidths=0.9)
    silk = []
    for f in b.footprints:
        if f.side != side:
            continue
        for p_, paths, w in f.layer_shapes('SilkS'):
            for path in paths:
                if path and vis(kpcb.bbox(path)):
                    silk.append(path)
    if silk:
        ax.add_collection(LineCollection(silk, colors='#0b0b3b', linewidths=1.5 if detail else 1.1, zorder=7), autolim=False)
    if not detail:
        for f in b.footprints:
            if f.side == side:
                bb = f.pads_bbox()
                if bb:
                    ax.text((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2, f.ref, fontsize=5, ha='center', va='center', color='#0b0b3b', zorder=9, clip_on=True, weight='bold')
    xs, ys = zip(*(outline + [outline[0]])); ax.plot(xs, ys, color='#000', lw=4, zorder=10)
    ax.set_xlim(box[0], box[2]); ax.set_ylim(box[3], box[1]); ax.set_aspect('equal'); ax.tick_params(labelsize=7); ax.grid(alpha=0.2, lw=0.4)


def render(R, b, out_png, max_zoom=8):
    if not any(f.fname == FONT for f in fm.fontManager.ttflist):   # キャッシュに無いときだけ (addfont は CJK の ttc を読むのに 10 s かかる)
        fm.fontManager.addfont(FONT)
    plt.rcParams['font.family'] = fm.FontProperties(fname=FONT).get_name()
    ms = [m for m in R['metrics'] if m['id'] in MOUNT_IDS]
    sc, grade, verdict, cnt = mount_score(ms)
    pcb = R.get('stages', {}).get('pcb', {})
    fills = mg.zone_fills(b); outline, holes, _ = b.outline()
    items = []
    for m in ms:
        for l in m.get('locs', []) or []:
            items.append(dict(l, id=m['id']))
    items.sort(key=lambda x: (0 if x['grade'] == 'fail' else 1, PRIO.index(x['id'])))
    for i, x in enumerate(items, 1):
        x['no'] = i
    xs = [p[0] for p in outline]; ys = [p[1] for p in outline]
    box = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
    # 拡大: fail 優先、同じ項目は最大 2 つ
    zoom = []; per = {}
    for x in items:
        if per.get(x['id'], 0) >= 2:
            continue
        zoom.append(x); per[x['id']] = per.get(x['id'], 0) + 1
        if len(zoom) >= max_zoom:
            break
    cols = 4; zrows = math.ceil(len(zoom) / cols)
    fig = plt.figure(figsize=(18, 10 + 4.4 * zrows), dpi=100)
    gs = fig.add_gridspec(1 + zrows, cols, height_ratios=[10] + [4.4] * zrows, hspace=0.35, wspace=0.18)
    ax = fig.add_subplot(gs[0, :3]); axl = fig.add_subplot(gs[0, 3]); axl.axis('off')
    draw_board(ax, b, fills, outline, box, detail=False)
    for x in items:
        c = COL.get(x['id'], '#000')
        ax.add_patch(Circle((x['x'], x['y']), max(0.6, x['r']), fc='none', ec=c, lw=2.6 if x['grade'] == 'fail' else 1.8, ls='-' if x['grade'] == 'fail' else '--', zorder=12))
        ax.text(x['x'] + max(0.6, x['r']) * 0.75, x['y'] - max(0.6, x['r']) * 0.75, str(x['no']), color='w', fontsize=7, zorder=13, weight='bold',
                bbox=dict(fc=c, ec='none', pad=0.6), clip_on=True)
    ax.set_title(f"{R.get('project')} 実装性 (P18b・P26〜P33) {sc} 点 / {grade} / {verdict}  —  PCB 段 {pcb.get('score')} 点 {pcb.get('grade')}  (F 面)", fontsize=13)
    lines = [f"pass {cnt['pass']} / warn {cnt['warn']} / fail {cnt['fail']} (info・skip は点に入れない)", '']
    for m in ms:
        lines.append(f"{m['grade'].upper():5} {m['id']}  {m['value']}")
    lines += ['', '番号 (fail が先):']
    lines += [f"{x['no']:>2} {x['grade']} {x['label']}" for x in items[:40]]
    if len(items) > 40:
        lines.append(f'… ほか {len(items) - 40} 件 (kreview.md)')
    axl.text(0, 1, '\n'.join(lines), va='top', ha='left', fontsize=7.6, family=plt.rcParams['font.family'], transform=axl.transAxes)
    leg = [Line2D([], [], marker='o', ls='', mfc='none', mec=COL[k], mew=2, ms=10, label=k) for k in COL]
    leg += [Line2D([], [], color='k', lw=2.6, label='実線 = fail'), Line2D([], [], color='k', lw=1.8, ls='--', label='破線 = warn'),
            Line2D([], [], color='#0b0b3b', lw=1.5, label='シルク'), Line2D([], [], color='#000', lw=4, label='Edge.Cuts'),
            Rectangle((0, 0), 1, 1, fc='#c98f6b', label='GND ベタ (F.Cu)')]
    ax.legend(handles=leg, loc='upper center', bbox_to_anchor=(0.5, -0.05), ncol=5, fontsize=8)
    for k, x in enumerate(zoom):
        a = fig.add_subplot(gs[1 + k // cols, k % cols])
        h = max(1.3, min(4.0, x['r'] * 1.8))
        f = b.fp(x['ref']) if x.get('ref') else None
        draw_board(a, b, fills, outline, (x['x'] - h, x['y'] - h, x['x'] + h, x['y'] + h), side=f.side if f else 'F')
        c = COL.get(x['id'], '#000')
        a.add_patch(Circle((x['x'], x['y']), max(0.5, x['r']), fc='none', ec=c, lw=2.6, ls='-' if x['grade'] == 'fail' else '--', zorder=12))
        a.set_title(f"{x['no']}. {x['grade'].upper()} {x['label']}", fontsize=9, color=c)
    fig.savefig(out_png, bbox_inches='tight'); plt.close(fig)
    return out_png


if __name__ == '__main__':
    R = json.load(open(sys.argv[1])); b = kpcb.Board(sys.argv[2])
    print(render(R, b, sys.argv[3]))
