"""実装性チェック (P18b・P26〜P33) の幾何部分。numpy と matplotlib.path だけを使う (scipy 不要)。

- 基板は読むだけ (kpcb の S 式)。距離・面積は mm。
- 値の根拠と「仮」の区別は mount_check_items (notes/2026-10-16-mount-check-items.md) と各定数のコメント。
"""
import math, re
import numpy as np
from matplotlib.path import Path
import kpcb

# ---- 工程の値 (出典 URL は 2026-10-16 確認)
T_PCB_MM = 1.6            # 板厚 (setup の stackup が無いときの既定)
STENCIL_MM = 0.12         # 仮: JLC 標準厚の 1 つ https://jlcpcb.com/capabilities/pcb-stencil-manufacturing
TRANSFER = 0.90           # 仮: ペーストの転写率
METAL_VOL = 0.50          # ペーストの金属体積率 50%: EPC AN029 https://epc-co.com/epc/portals/0/epc/documents/application-notes/AN029%20Solder%20Stencil%20Design%20Guidelines%20for%20Reliable%20Assembly%20of%20PQFN%20GaN.pdf
DRILL_TOL_PLUS = 0.13     # JLC 穴径公差 +0.13/-0.08 https://jlcpcb.com/capabilities/pcb-capabilities
TENT_MIN = 0.10           # 仮: テントが効くとみなす 穴縁/ランド縁 → マスク開口縁。JLC soldermask bridge 0.10 mm (同上) を準用
EP_AREA = 4.0             # P18/P19 と同じ: これ以上のパッドは EP・タブ (thermal via は意図的)

# JLC のサイズ別スポーク推奨 (表示のみ): https://jlcpcb.com/blog/thermal-relief-pad-design
JLC_SPOKE = {
    'passive': dict(label='0402/0603 受動部品', spoke=(0.10, 0.20), gap=(0.20, 0.30)),
    'smd': dict(label='一般 SMD (0805・SOT-23・SOIC・コネクタ)', spoke=(0.20, 0.30), gap=(0.25, 0.40)),
}
JLC_SPOKE_URL = 'https://jlcpcb.com/blog/thermal-relief-pad-design'

SIZE_RE = re.compile(r'_(01005|0201|0402|0603|0805|1206|1210)_')
CHIP_RE = re.compile(r'^(R|C|L|FB|F|D|LED)\d')
RF_RE = re.compile(r'RF_Module|Antenna|WROOM|WROVER|ESP32|nRF5|CC1101|SX12', re.I)
SIZE_ORDER = ['01005', '0201', '0402', '0603', '0805', '1206', '1210']
ZC = {'0': 'none', '1': 'thermal', '2': 'solid', '3': 'thru_hole_only'}


def chip_size(f):
    m = SIZE_RE.search(f.lib)
    return m.group(1) if m else None


def smd_pads(f):
    return [p for p in f.pads if p.type == 'smd' and p.num]


def is_chip(f):
    return bool(CHIP_RE.match(f.ref)) and len(smd_pads(f)) == 2 and chip_size(f) is not None


def is_connector(f):
    return f.ref.startswith('J') and len(smd_pads(f)) >= 2


def board_thickness(b):
    g = kpcb.kid(b.root, 'general')
    try:
        return float(kpcb.val(g, 'thickness', default=T_PCB_MM)) if g is not None else T_PCB_MM
    except Exception:
        return T_PCB_MM


def setup_info(b):
    s = kpcb.kid(b.root, 'setup')
    def yes(name):
        n = kpcb.kid(s, name) if s is not None else None
        if n is None:
            return False
        return any(str(kpcb.val(n, side, default='no')) == 'yes' for side in ('front', 'back')) if any(isinstance(x, list) for x in n[1:]) else str(n[1]) == 'yes'
    mask = float(kpcb.val(s, 'pad_to_mask_clearance', default=0) or 0) if s is not None else 0.0
    paste = float(kpcb.val(s, 'pad_to_paste_clearance', default=0) or 0) if s is not None else 0.0
    ratio = float(kpcb.val(s, 'pad_to_paste_clearance_ratio', default=0) or 0) if s is not None else 0.0
    return dict(mask=mask, paste=paste, ratio=ratio, filled=yes('plugging') or yes('filling') or yes('capping'))


def zone_fills(b):
    """{(layer, net): [多角形]} (filled_polygon)"""
    out = {}
    for z in kpcb.kids(b.root, 'zone'):
        net = str(kpcb.val(z, 'net_name', default='') or kpcb.val(z, 'net', default='') or '')
        for fp_ in kpcb.kids(z, 'filled_polygon'):
            L = str(kpcb.val(fp_, 'layer', default=''))
            pts = kpcb.kid(fp_, 'pts')
            poly = [(float(c[1]), float(c[2])) for c in (pts or ['pts'])[1:] if isinstance(c, list) and c[0] == 'xy']
            if len(poly) >= 3:
                out.setdefault((L, net), []).append(poly)
    return out


def zone_settings(b):
    """[{net, layers, connect: solid/thermal/none/thru_hole_only, clearance, gap, spoke}] (keepout は除く)"""
    out = []
    for z in kpcb.kids(b.root, 'zone'):
        if kpcb.kid(z, 'keepout') is not None:
            continue
        net = str(kpcb.val(z, 'net_name', default='') or kpcb.val(z, 'net', default='') or '')
        cp = kpcb.kid(z, 'connect_pads') or ['connect_pads']
        atoms = [str(x) for x in cp[1:] if not isinstance(x, list)]
        mode = {'yes': 'solid', 'no': 'none', 'thru_hole_only': 'thru_hole_only'}.get(atoms[0] if atoms else '', 'thermal')
        fill = kpcb.kid(z, 'fill') or ['fill']
        layers = [str(x) for x in (kpcb.kid(z, 'layers') or kpcb.kid(z, 'layer') or ['l'])[1:]]
        out.append(dict(net=net, layers=layers, connect=mode, clearance=float(kpcb.val(cp, 'clearance', default=0) or 0),
                        gap=float(kpcb.val(fill, 'thermal_gap', default=0.5) or 0.5), spoke=float(kpcb.val(fill, 'thermal_bridge_width', default=0.5) or 0.5)))
    return out


def pad_node(f, num):
    for p in kpcb.kids(f.node, 'pad'):
        if str(p[1]) == num:
            return p


def resolve_zone_connection(b, f, p, zs, dru_text=''):
    """優先順位: パッド > フットプリント > .kicad_dru の zone_connection > ゾーン。
    戻り値 dict(mode, src, detail, spoke, gap) (spoke/gap はパッド > フットプリント > ゾーンの順)"""
    pn = pad_node(f, p.num)
    def num(node, name):
        v = kpcb.val(node, name, default=None) if node is not None else None
        return float(v) if v is not None else None
    sw = num(pn, 'thermal_bridge_width') or num(f.node, 'thermal_bridge_width')
    sg = num(pn, 'thermal_gap') or num(f.node, 'thermal_gap')
    zone = next((z for z in zs if z['net'] == p.net and p.net and any(L in z['layers'] for L in p.copper_layers(b.copper))), None)
    if zone:
        sw = sw or zone['spoke']; sg = sg or zone['gap']
    for where, node in (('pad', pn), ('footprint', f.node)):
        v = kpcb.val(node, 'zone_connect', default=None) if node is not None else None
        if v is not None:
            return dict(mode=ZC.get(str(v), str(v)), src=where, detail=f'(zone_connect {v})', spoke=sw, gap=sg, zone=bool(zone))
    if 'zone_connection' in dru_text:
        return dict(mode='custom-rule?', src='kicad_dru', detail='.kicad_dru に zone_connection 規則あり (条件は手で確認)', spoke=sw, gap=sg, zone=bool(zone))
    if zone:
        m = zone['connect']
        if m == 'thru_hole_only':
            m = 'none' if p.type == 'smd' else 'solid'
        return dict(mode=m, src='zone', detail=f"zone {zone['net']}: connect_pads={zone['connect']}", spoke=sw, gap=sg, zone=True)
    return dict(mode='n/a', src='-', detail='同ネットのゾーンなし', spoke=None, gap=None, zone=False)


def mask_paste(f, p, st):
    """マスク開口・ペースト開口 (mm)。パッド > フットプリント > 基板 setup"""
    pn = pad_node(f, p.num)
    def get(name, default):
        for node in (pn, f.node):
            v = kpcb.val(node, name, default=None) if node is not None else None
            if v is not None:
                return float(v)
        return default
    mm = get('solder_mask_margin', st['mask'])
    pm = get('solder_paste_margin', st['paste'])
    pr = get('solder_paste_margin_ratio', None)
    if pr is None:
        pr = get('solder_paste_ratio', st['ratio'])
    paste_layer = any(L.endswith('.Paste') for L in p.layers)
    g = pm + pr * min(p.w, p.h)
    pw = max(0.0, p.w + 2 * g) if paste_layer else 0.0
    ph = max(0.0, p.h + 2 * g) if paste_layer else 0.0
    return dict(mask_margin=mm, paste_w=pw, paste_h=ph, paste_layer=paste_layer)


class CopperIndex:
    """層・ネットごとのゾーン塗り (matplotlib Path) と配線"""
    def __init__(s, b, fills=None):
        s.fills = fills if fills is not None else zone_fills(b)
        s.paths = {k: [Path(p) for p in v] for k, v in s.fills.items()}
        s.tracks = {}
        for t in b.tracks + b.arcs:
            for a, c in zip(t['pts'], t['pts'][1:]):
                s.tracks.setdefault((t['layer'], t['net']), []).append((a, c, t['w']))

    def in_pour(s, layer, net, pts):
        m = np.zeros(len(pts), bool)
        for P in s.paths.get((layer, net), []):
            m |= P.contains_points(pts)
        return m

    def in_track(s, layer, net, pts):
        m = np.zeros(len(pts), bool)
        X, Y = pts[:, 0], pts[:, 1]
        for a, c, w in s.tracks.get((layer, net), []):
            dx, dy = c[0] - a[0], c[1] - a[1]; L = dx * dx + dy * dy
            t = np.zeros_like(X) if L == 0 else np.clip(((X - a[0]) * dx + (Y - a[1]) * dy) / L, 0, 1)
            m |= np.hypot(X - a[0] - t * dx, Y - a[1] - t * dy) <= w / 2
        return m

    def tracks_touching(s, layer, net, p):
        n = 0
        for a, c, w in s.tracks.get((layer, net), []):
            for e in (a, c):
                if rect_dist(e[0], e[1], p) <= w / 2 + 1e-6:
                    n += 1; break
        return n


def rect_dist(x, y, p):
    lx, ly = kpcb.rot(x - p.x, y - p.y, -p.angle)
    return math.hypot(max(0.0, abs(lx) - p.w / 2), max(0.0, abs(ly) - p.h / 2))


def _ring(p, d, step=0.01):
    """パッド矩形の外側 d の輪 (角は円弧)。戻り値 (点 [N,2], 辺番号 [N] (0..3 = 直線部、-1 = 角))"""
    hw, hh = p.w / 2, p.h / 2
    pts, eid = [], []
    def add_line(x0, y0, x1, y1, e):
        n = max(2, int(math.hypot(x1 - x0, y1 - y0) / step))
        for k in range(n):
            t = (k + 0.5) / n; pts.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)); eid.append(e)
    def add_arc(cx, cy, a0):
        n = max(2, int(d * math.pi / 2 / step))
        for k in range(n):
            a = math.radians(a0 + 90 * (k + 0.5) / n); pts.append((cx + d * math.cos(a), cy + d * math.sin(a))); eid.append(-1)
    add_line(-hw, -hh - d, hw, -hh - d, 0); add_arc(hw, -hh, -90)
    add_line(hw + d, -hh, hw + d, hh, 1); add_arc(hw, hh, 0)
    add_line(hw, hh + d, -hw, hh + d, 2); add_arc(-hw, hh, 90)
    add_line(-hw - d, hh, -hw - d, -hh, 3); add_arc(-hw, -hh, 180)
    P = np.array([kpcb.rot(x, y, p.angle) for x, y in pts]) + np.array([p.x, p.y])
    return P, np.array(eid)


def pad_ring(b, f, p, CI, zc):
    """パッドの縁の銅のつながり。
    - pour_frac: 縁の外 0.05 mm の輪のうちベタ (同ネットの塗り) にある割合
    - cu_frac / exit_w: 同 (ベタ + 配線)、exit_w = cu_frac × 周長 (mm)
    - md_edges: マスク定義の辺の数 (辺の直線部の ≥50% がベタ・配線に続く = 濡れ境界がマスク端、50% は仮)
    - spokes: 隙間の中央 (gap/2) の輪でベタの塗りを横切る数 (= スポーク本数、配線は数えない)
    - tracks: パッドに端点が接する同ネットの配線の本数
    """
    L = next((x for x in p.copper_layers(b.copper)), f.layer.split('.')[0] + '.Cu')
    P, e = _ring(p, 0.05)
    pour = CI.in_pour(L, p.net, P) if p.net else np.zeros(len(P), bool)
    cu = pour | (CI.in_track(L, p.net, P) if p.net else np.zeros(len(P), bool))
    md = sum(1 for k in range(4) if (e == k).any() and cu[e == k].mean() >= 0.5)
    spokes = None
    if zc.get('mode') == 'thermal' and zc.get('gap'):
        R, _ = _ring(p, zc['gap'] / 2)
        inz = CI.in_pour(L, p.net, R)
        spokes = 0 if not inz.any() else (len(R) if inz.all() else int(np.sum(inz & ~np.roll(inz, 1))))
    per = 2 * (p.w + p.h)
    return dict(layer=L, pour_frac=round(float(pour.mean()), 3), cu_frac=round(float(cu.mean()), 3), exit_w=round(float(cu.mean()) * per, 2),
                md_edges=md, spokes=spokes, tracks=CI.tracks_touching(L, p.net, p) if p.net else 0)


def _disc_rect_overlap(cx, cy, r, p, grow=0.0, n=40):
    xs = np.linspace(-r, r, n); X, Y = np.meshgrid(xs, xs); m = X * X + Y * Y <= r * r
    lx, ly = kpcb.rot(cx - p.x, cy - p.y, -p.angle)
    inside = (np.abs(X + lx) <= p.w / 2 + grow) & (np.abs(Y + ly) <= p.h / 2 + grow)
    return float((inside & m).sum()) / max(1, m.sum())


def aperture_pads(f, p, side='F', tol=0.02):
    """フットプリントに足した開口用パッド (番号なし・銅なし、マスク/ペースト層だけ) のうち p に接するもの。
    開口用パッドでパッドのマスク・ペースト開口を延長する作り (2026-10-16 J2 の +0.3 mm) に対応"""
    out = []
    for q in f.pads:
        if q is p or q.num or q.type != 'smd' or any(L.endswith('.Cu') for L in q.layers):
            continue
        if not any(L in (f'{side}.Mask', f'{side}.Paste') for L in q.layers):
            continue
        if any(rect_dist(x, y, p) <= tol for x, y in q.poly()) or any(rect_dist(x, y, q) <= tol for x, y in p.poly()):
            out.append(q)
    return out


def via_relations(b, p, mask_margin, reach=0.3, extras=()):
    """パッドのマスク開口の近くの via。drill_edge / land_edge = 穴縁 / ランド縁 → 開口縁 (負は開口にかかる)、f_open = 穴のうち開口内の割合。
    extras = マスク層を持つ開口用パッド (aperture_pads)。開口 = パッド ∪ extras (各 + mask_margin)"""
    rows = []
    shapes = [p] + [q for q in extras if any(L.endswith('.Mask') for L in q.layers)]
    for v in b.vias:
        dc = min(rect_dist(v['at'][0], v['at'][1], q) for q in shapes)
        land = dc - v['size'] / 2 - mask_margin
        if land > reach:
            continue
        r = v['drill'] / 2
        f_open = min(1.0, sum(_disc_rect_overlap(v['at'][0], v['at'][1], r, q, grow=mask_margin) for q in shapes)) if dc - r - mask_margin < 0 else 0.0
        rows.append(dict(at=tuple(v['at']), net=v['net'], drill=v['drill'], size=v['size'], center_in_pad=dc == 0,
                         drill_edge=round(dc - r - mask_margin, 3), land_edge=round(land, 3), f_open=round(f_open, 3)))
    return rows


def solder_mm3(mp, stencil=STENCIL_MM, transfer=TRANSFER):
    return mp['paste_w'] * mp['paste_h'] * stencil * transfer * METAL_VOL


def wicking(vr, solder, t_pcb, filled=False):
    """吸い込み比 = Σ(穴が開口にかかる via の穴容積 πd²/4·t) / パッドのはんだ体積 (上限: 穴全体へ流れる)。
    lower = 開口にかかる割合で按分した下限。穴埋め (IPC-4761 Type V〜VII) なら 0"""
    ex = [v for v in vr if v['f_open'] > 0]
    vol = sum(math.pi * (v['drill'] / 2) ** 2 * t_pcb for v in ex)
    low = sum(math.pi * (v['drill'] / 2) ** 2 * t_pcb * v['f_open'] for v in ex)
    if filled or not solder:
        return dict(exposed=ex, ratio=0.0, lower=0.0, hole_mm3=round(vol, 4))
    return dict(exposed=ex, ratio=round(vol / solder, 3), lower=round(low / solder, 3), hole_mm3=round(vol, 4))


def is_ep_or_tab(f, p):
    """EP・放熱タブ: 面積 ≥4 mm² (P18/P19 と同じ)、または部品で一番大きく 2 番目の 2 倍以上の SMD パッド (チップ・コネクタ以外)"""
    a = p.w * p.h
    if a >= EP_AREA:
        return True
    if is_chip(f) or is_connector(f):
        return False
    areas = sorted({round(q.w * q.h, 4) for q in smd_pads(f)}, reverse=True)
    return len(areas) >= 2 and abs(a - areas[0]) < 1e-6 and areas[0] >= 2 * areas[1]


def courtyard_gaps(b, targets):
    out = {}
    for f in targets:
        c1 = f.courtyard_bbox()
        if not c1:
            continue
        rows = []
        for g in b.footprints:
            if g is f or g.side != f.side:
                continue
            c2 = g.courtyard_bbox()
            if c2:
                rows.append((round(kpcb.bbox_dist(c1, c2), 3), g.ref))
        rows.sort()
        out[f.ref] = rows
    return out
