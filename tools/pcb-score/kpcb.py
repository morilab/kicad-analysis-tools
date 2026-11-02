"""kpcb — .kicad_pcb (KiCad 10) を S 式として直接読む小さなライブラリ (pcbnew SWIG 不要、system python3 + numpy + Pillow)。

座標は mm、y は下向き (KiCad と同じ)。footprint 内の座標は (at x y θ) で回して平行移動する:
  x' = x cosθ + y sinθ,  y' = -x sinθ + y cosθ   (KiCad の RotatePoint と同じ。θ>0 は画面上で反時計回り)
pad / text の角度はファイル上で既に絶対角 (footprint の回転込み)。
"""
import math, os, re

# ---------------------------------------------------------------- S 式
_tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text):
    stack, cur = [], []
    for m in _tok.finditer(text):
        t = m.group(0)
        if t == '(':
            stack.append(cur); cur = []
        elif t == ')':
            done = cur; cur = stack.pop(); cur.append(done)
        elif t[0] == '"':
            cur.append(Str(t[1:-1].replace('\\"', '"').replace('\\\\', '\\')))
        else:
            cur.append(t)
    return cur[0]


class Str(str):
    """引用符付き文字列 (atom と区別したいとき用)"""


def head(n):
    return n[0] if isinstance(n, list) and n else None


def kids(n, name):
    return [c for c in n[1:] if isinstance(c, list) and c and c[0] == name]


def kid(n, name):
    for c in n[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def val(n, name, i=1, default=None, conv=None):
    c = kid(n, name)
    if c is None or len(c) <= i:
        return default
    return conv(c[i]) if conv else c[i]


def xy(n, name):
    c = kid(n, name)
    return (float(c[1]), float(c[2])) if c is not None else None


def is_yes(n, name):
    """(hide yes) / (hide) / 素の hide atom"""
    c = kid(n, name)
    if c is not None:
        return len(c) == 1 or c[1] in ('yes', 'true')
    return name in [x for x in n[1:] if not isinstance(x, list)]


# ---------------------------------------------------------------- 幾何
def rot(x, y, deg):
    if not deg:
        return x, y
    a = math.radians(deg); c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def arc_points(start, mid, end, seg=1.0 / 8):
    """3 点円弧を点列に (端点含む)"""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))

    def norm(a):
        while a < 0: a += 2 * math.pi
        return a
    # a1 -> a3 を a2 を通る向きで
    ccw = norm(a2 - a1) < norm(a3 - a1)
    sweep = norm(a3 - a1) if ccw else -norm(a1 - a3)
    n = max(2, int(abs(sweep) * r / 0.2) + 1)
    return [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]


def arc_length(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def poly_area(p):
    return abs(sum(p[i][0] * p[i - 1][1] - p[i - 1][0] * p[i][1] for i in range(len(p)))) / 2


def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_union(a, b):
    if a is None: return b
    if b is None: return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def bbox_overlap(a, b, gap=0.0):
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def bbox_dist(a, b):
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def point_in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]; xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def chain_loops(segs, tol=0.01):
    """端点がつながる線分 (点列) を閉ループにまとめる。戻り値: [ループの点列], 余った開いた鎖の数"""
    segs = [list(s) for s in segs if len(s) >= 2]
    loops, open_n = [], 0
    while segs:
        cur = segs.pop(0)
        changed = True
        while changed and math.dist(cur[0], cur[-1]) > tol:
            changed = False
            for i, s in enumerate(segs):
                if math.dist(cur[-1], s[0]) <= tol:
                    cur += s[1:]; segs.pop(i); changed = True; break
                if math.dist(cur[-1], s[-1]) <= tol:
                    cur += s[::-1][1:]; segs.pop(i); changed = True; break
                if math.dist(cur[0], s[-1]) <= tol:
                    cur = s[:-1] + cur; segs.pop(i); changed = True; break
                if math.dist(cur[0], s[0]) <= tol:
                    cur = s[::-1][:-1] + cur; segs.pop(i); changed = True; break
        if math.dist(cur[0], cur[-1]) <= tol and len(cur) >= 4:
            loops.append(cur[:-1])
        else:
            open_n += 1
    return loops, open_n


def shape_paths(n, tf=lambda x, y: (x, y)):
    """gr_*/fp_* の図形 1 個 -> (閉じた多角形 or None, 線の点列のリスト, 線幅)"""
    h = n[0].split('_', 1)[1]
    st = kid(n, 'stroke')
    w = float(val(st, 'width', default=0) or 0) if st else float(val(n, 'width', default=0) or 0)
    fill = val(n, 'fill', default='no')
    filled = fill in ('yes', 'solid')
    if h == 'line':
        return None, [[tf(*xy(n, 'start')), tf(*xy(n, 'end'))]], w
    if h == 'arc':
        pts = arc_points(xy(n, 'start'), xy(n, 'mid'), xy(n, 'end'))
        return None, [[tf(*p) for p in pts]], w
    if h == 'rect':
        (x1, y1), (x2, y2) = xy(n, 'start'), xy(n, 'end')
        p = [tf(x1, y1), tf(x2, y1), tf(x2, y2), tf(x1, y2)]
        return p, [p + [p[0]]], w
    if h == 'circle':
        c, e = xy(n, 'center'), xy(n, 'end'); r = math.dist(c, e)
        p = [tf(c[0] + r * math.cos(2 * math.pi * i / 48), c[1] + r * math.sin(2 * math.pi * i / 48)) for i in range(48)]
        return p, [p + [p[0]]], w
    if h == 'poly':
        pts = kid(n, 'pts')
        p = []
        for c in pts[1:]:
            if c[0] == 'xy':
                p.append(tf(float(c[1]), float(c[2])))
            elif c[0] == 'arc':
                p += [tf(*q) for q in arc_points(xy(c, 'start'), xy(c, 'mid'), xy(c, 'end'))]
        return p, [p + [p[0]]], w
    return None, [], w


# ---------------------------------------------------------------- PCB モデル
class Pad:
    def __init__(s, fp, n):
        s.fp = fp
        s.num = str(n[1]); s.type = n[2]; s.shape = n[3]
        at = kid(n, 'at')
        lx, ly = float(at[1]), float(at[2])
        s.angle = float(at[3]) if len(at) > 3 else 0.0
        s.x, s.y = fp.tf(lx, ly)
        sz = kid(n, 'size'); s.w, s.h = float(sz[1]), float(sz[2] if len(sz) > 2 else sz[1])
        s.layers = [str(x) for x in (kid(n, 'layers') or ['layers'])[1:]]
        dn = kid(n, 'drill') or []   # (drill 3.2) / (drill oval 1.0 2.0)。穴なし (SMD) は 0
        nums = []
        for x in dn[1:]:
            try:
                nums.append(float(str(x)))
            except ValueError:
                pass
        s.drill = min(nums) if nums else 0.0
        s.net = val(n, 'net', default='')
        if s.net and not isinstance(s.net, Str) and len(kid(n, 'net')) > 2:   # 旧形式 (net N "name")
            s.net = kid(n, 'net')[2]
        s.net = str(s.net or '')

    def poly(s):
        hw, hh = s.w / 2, s.h / 2
        return [(s.x + dx, s.y + dy) for dx, dy in (rot(-hw, -hh, s.angle), rot(hw, -hh, s.angle), rot(hw, hh, s.angle), rot(-hw, hh, s.angle))]

    def copper_layers(s, copper):
        out = []
        for L in s.layers:
            if L == '*.Cu':
                out += copper
            elif L == 'F&B.Cu':
                out += ['F.Cu', 'B.Cu']
            elif L.endswith('.Cu'):
                out.append(L)
        return out


class Text:
    def __init__(s, kind, text, x, y, angle, layer, size, thick, hidden, justify, owner=None):
        s.kind, s.text, s.x, s.y, s.angle, s.layer = kind, text, x, y, angle, layer
        s.size, s.thick, s.hidden, s.justify, s.owner = size, thick, hidden, justify, owner

    def poly(s):
        """KiCad 既定ストロークフォントの外形の概算 (幅 ≈ 文字数 × 0.9 × 文字幅、高さ ≈ 文字高 + 線幅)。
        正確な判定は DRC (silk_over_copper) に任せ、ここは注釈・概算用。"""
        lines = s.text.split('\\n') if '\\n' in s.text else s.text.split('\n')
        n = max(len(l) for l in lines) if lines else 0
        w = n * s.size[1] * 0.9 + s.thick
        h = len(lines) * s.size[0] * 1.4 - s.size[0] * 0.4 + s.thick
        x0, x1 = -w / 2, w / 2
        y0, y1 = -h / 2, h / 2
        if 'left' in s.justify: x0, x1 = 0, w
        if 'right' in s.justify: x0, x1 = -w, 0
        if 'top' in s.justify: y0, y1 = 0, h
        if 'bottom' in s.justify: y0, y1 = -h, 0
        a = s.angle
        return [(s.x + dx, s.y + dy) for dx, dy in (rot(x0, y0, a), rot(x1, y0, a), rot(x1, y1, a), rot(x0, y1, a))]


def _text_from(n, kind, tf, owner, fp_angle=0.0):
    if kind == 'property':
        text = str(n[2])
    else:
        text = str(n[2] if n[0] == 'fp_text' else n[1])
    at = kid(n, 'at')
    lx, ly = float(at[1]), float(at[2])
    ang = float(at[3]) if len(at) > 3 else 0.0
    x, y = tf(lx, ly)
    layer = str(val(n, 'layer', default=''))
    eff = kid(n, 'effects') or ['effects']
    font = kid(eff, 'font') or ['font']
    sz = kid(font, 'size'); size = (float(sz[1]), float(sz[2])) if sz else (1.27, 1.27)
    thick = float(val(font, 'thickness', default=0.15) or 0.15)
    just = kid(eff, 'justify'); just = [str(x) for x in just[1:]] if just else []
    hidden = is_yes(n, 'hide') or is_yes(eff, 'hide')
    if n[0] == 'fp_text' and len(n) > 1 and 'hide' in n[3:6]:
        hidden = True
    return Text(kind, text, x, y, ang, layer, size, thick, hidden, just, owner)


class Footprint:
    def __init__(s, n):
        s.node = n
        s.lib = str(n[1])
        s.layer = str(val(n, 'layer'))
        at = kid(n, 'at'); s.x, s.y = float(at[1]), float(at[2]); s.angle = float(at[3]) if len(at) > 3 else 0.0
        s.props = {str(p[1]): str(p[2]) for p in kids(n, 'property')}
        s.ref = s.props.get('Reference', '?'); s.value = s.props.get('Value', '')
        s.attr = [str(x) for x in (kid(n, 'attr') or ['attr'])[1:]]
        s.pads = [Pad(s, p) for p in kids(n, 'pad')]
        s.graphics = {}           # layer -> [(poly|None, [paths], width)]
        for c in n[1:]:
            if isinstance(c, list) and c and c[0] in ('fp_line', 'fp_arc', 'fp_rect', 'fp_circle', 'fp_poly'):
                L = str(val(c, 'layer', default=''))
                s.graphics.setdefault(L, []).append(shape_paths(c, s.tf))
        s.texts = [_text_from(p, 'property', s.tf, s) for p in kids(n, 'property') if kid(p, 'at') is not None]
        s.texts += [_text_from(t, 'fp_text', s.tf, s) for t in kids(n, 'fp_text')]
        s.models = []
        for m in kids(n, 'model'):
            def x3(name):
                c = kid(m, name); c = kid(c, 'xyz') if c else None
                return tuple(float(v) for v in c[1:4]) if c else (0.0, 0.0, 0.0)
            s.models.append(dict(path=str(m[1]), offset=x3('offset'), scale=x3('scale') or (1, 1, 1), rotate=x3('rotate'),
                                 hide=is_yes(m, 'hide')))
        s.side = 'B' if s.layer.startswith('B.') else 'F'

    def tf(s, lx, ly):
        dx, dy = rot(lx, ly, s.angle)
        return s.x + dx, s.y + dy

    def to_local(s, x, y):
        return rot(x - s.x, y - s.y, -s.angle)

    def layer_shapes(s, which):
        """which: 'CrtYd' / 'Fab' / 'SilkS' ... (表裏どちらでも)"""
        out = []
        for L, items in s.graphics.items():
            if L.endswith('.' + which):
                out += items
        return out

    def polys(s, which):
        """その層の図形から閉じた多角形を作る (rect/poly/circle はそのまま、line/arc は鎖をつなぐ)"""
        items = s.layer_shapes(which)
        polys = [p for p, paths, w in items if p]
        segs = []
        for p, paths, w in items:
            if not p:
                segs += paths
        loops, open_n = chain_loops(segs)
        return polys + loops, open_n

    def courtyard(s):
        polys, open_n = s.polys('CrtYd')
        return polys

    def courtyard_bbox(s):
        ps = s.courtyard()
        if ps:
            return bbox([q for p in ps for q in p])
        segs = [q for p, paths, w in s.layer_shapes('CrtYd') for path in paths for q in path]
        return bbox(segs) if segs else None

    def fab_bbox(s):
        pts = [q for p, paths, w in s.layer_shapes('Fab') for path in paths for q in path]
        return bbox(pts) if pts else None

    def pads_bbox(s):
        pts = [q for p in s.pads for q in p.poly()]
        return bbox(pts) if pts else None


class Board:
    def __init__(s, path):
        s.path = os.path.abspath(path)
        s.dir = os.path.dirname(s.path)
        s.name = os.path.splitext(os.path.basename(path))[0]
        s.root = parse_sexpr(open(path, encoding='utf-8').read())
        r = s.root
        s.copper = [str(L[1]) for L in (kid(r, 'layers') or ['layers'])[1:] if isinstance(L, list) and str(L[1]).endswith('.Cu')]
        s.footprints = [Footprint(n) for n in kids(r, 'footprint')]
        s.tracks, s.arcs, s.vias = [], [], []
        for n in kids(r, 'segment'):
            s.tracks.append(dict(a=xy(n, 'start'), b=xy(n, 'end'), w=float(val(n, 'width')), layer=str(val(n, 'layer')),
                                 net=str(val(n, 'net', default='')), pts=[xy(n, 'start'), xy(n, 'end')]))
        for n in kids(r, 'arc'):
            pts = arc_points(xy(n, 'start'), xy(n, 'mid'), xy(n, 'end'))
            s.arcs.append(dict(a=pts[0], b=pts[-1], w=float(val(n, 'width')), layer=str(val(n, 'layer')),
                               net=str(val(n, 'net', default='')), pts=pts))
        for n in kids(r, 'via'):
            s.vias.append(dict(at=xy(n, 'at'), size=float(val(n, 'size')), drill=float(val(n, 'drill', default=0) or 0),
                               net=str(val(n, 'net', default='')), layers=[str(x) for x in (kid(n, 'layers') or ['l'])[1:]]))
        s.zones = []
        for n in kids(r, 'zone'):
            layers = [str(x) for x in (kid(n, 'layers') or kid(n, 'layer') or ['l'])[1:]]
            outline = kid(kid(n, 'polygon') or ['polygon'], 'pts')
            pts = [(float(c[1]), float(c[2])) for c in (outline or ['pts'])[1:] if c[0] == 'xy']
            s.zones.append(dict(net=str(val(n, 'net_name', default='') or val(n, 'net', default='')), layers=layers, outline=pts,
                                keepout=kid(n, 'keepout') is not None, name=str(val(n, 'name', default=''))))
        s.gr = {}
        for c in r[1:]:
            if isinstance(c, list) and c and c[0] in ('gr_line', 'gr_arc', 'gr_rect', 'gr_circle', 'gr_poly'):
                L = str(val(c, 'layer', default=''))
                s.gr.setdefault(L, []).append(shape_paths(c))
        s.texts = [_text_from(t, 'gr_text', lambda x, y: (x, y), None) for t in kids(r, 'gr_text')]
        for f in s.footprints:
            s.texts += f.texts

    # ---- 外形
    def outline(s):
        """Edge.Cuts -> (外周の多角形, [穴の多角形], 開いた鎖の数)"""
        items = list(s.gr.get('Edge.Cuts', []))
        for f in s.footprints:
            items += f.graphics.get('Edge.Cuts', [])
        polys = [p for p, paths, w in items if p]
        segs = [path for p, paths, w in items if not p for path in paths]
        loops, open_n = chain_loops(segs)
        allp = polys + loops
        if not allp:
            return None, [], open_n
        allp.sort(key=poly_area, reverse=True)
        return allp[0], allp[1:], open_n

    def fp(s, ref):
        for f in s.footprints:
            if f.ref == ref:
                return f
        return None

    def nets(s):
        d = {}
        for f in s.footprints:
            for p in f.pads:
                if p.net:
                    d.setdefault(p.net, []).append(p)
        return d


def resolve_model(board, path):
    """3D モデルのパスを解決 (${KICAD10_3DMODEL_DIR} など、プロジェクト相対)。見つからなければ None"""
    env = {'KIPRJMOD': board.dir}
    share = os.environ.get('KICAD_SHARE', '/usr/share/kicad')
    for k in ('KICAD10_3DMODEL_DIR', 'KICAD9_3DMODEL_DIR', 'KICAD8_3DMODEL_DIR', 'KICAD7_3DMODEL_DIR', 'KICAD6_3DMODEL_DIR'):
        env[k] = os.environ.get(k, share + '/3dmodels')
    p = re.sub(r'\$\{(\w+)\}', lambda m: env.get(m.group(1), m.group(0)), path)
    cands = [p] if os.path.isabs(p) else [os.path.join(board.dir, p)]
    root, ext = os.path.splitext(cands[0])
    cands += [root + e for e in ('.step', '.stp', '.wrl', '.STEP', '.WRL') if root + e != cands[0]]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def model_bbox_local(model_file, m):
    """3D モデルの外形 (x,y の bbox、footprint ローカル座標、mm) と高さ。STEP は CARTESIAN_POINT、WRL は point[] を集める概算。
    KiCad はモデルの +Y を基板の -Y (上) に置く。offset/scale/rotate(z のみ) を適用。"""
    txt = open(model_file, errors='ignore').read()
    pts = []
    if model_file.lower().endswith(('.step', '.stp')):
        cp = {i: (float(a), float(b), float(c)) for i, a, b, c in re.findall(
            r"#(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*([-\d.E+e]+)\s*,\s*([-\d.E+e]+)\s*,\s*([-\d.E+e]+)", txt)}
        # 形状の頂点 (VERTEX_POINT) だけを使う。AXIS2_PLACEMENT の原点 (面の基準点) は形状の外にあることがあり、
        # 外形を大きく見誤る (usb-jtag-ft2232h J2 の LCSC モデルで +3.7 mm、2026-10-09)
        vp = re.findall(r"VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#(\d+)", txt)
        pts = [cp[i] for i in vp if i in cp] or list(cp.values())
    else:   # VRML: KiCad は 1 単位 = 0.1 inch
        for blk in re.findall(r'point\s*\[(.*?)\]', txt, re.S):
            nums = [float(v) for v in re.findall(r'[-\d.eE+]+', blk)]
            pts += [(nums[i] * 2.54, nums[i + 1] * 2.54, nums[i + 2] * 2.54) for i in range(0, len(nums) - 2, 3)]
    if not pts:
        return None
    sx, sy, sz = m['scale']; ox, oy, oz = m['offset']; rz = m['rotate'][2]
    out = []
    for x, y, z in pts:
        x, y, z = x * sx, y * sy, z * sz
        # モデル座標系 (y 上向き) で z 回転 -> 基板座標 (y 下向き) に
        a = math.radians(rz)
        xr, yr = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
        out.append((xr + ox, -(yr + oy), z + oz))
    xs = [p[0] for p in out]; ys = [p[1] for p in out]; zs = [p[2] for p in out]
    return (min(xs), min(ys), max(xs), max(ys)), (min(zs), max(zs))


# ---------------------------------------------------------------- ラスタ (面積の和集合を数える)
class Raster:
    def __init__(s, bb, res=0.05):
        import numpy as np
        from PIL import Image, ImageDraw
        s.np, s.Image, s.ImageDraw = np, Image, ImageDraw
        s.x0, s.y0 = bb[0] - 1, bb[1] - 1
        s.res = res
        s.W = int((bb[2] - bb[0] + 2) / res) + 1
        s.H = int((bb[3] - bb[1] + 2) / res) + 1

    def new(s):
        im = s.Image.new('1', (s.W, s.H), 0)
        return im, s.ImageDraw.Draw(im)

    def px(s, p):
        return ((p[0] - s.x0) / s.res, (p[1] - s.y0) / s.res)

    def poly(s, d, pts, v=1):
        if len(pts) >= 3:
            d.polygon([s.px(p) for p in pts], fill=v, outline=v)

    def line(s, d, pts, w, v=1):
        r = max(w / 2 / s.res, 0.5)
        P = [s.px(p) for p in pts]
        d.line(P, fill=v, width=max(1, int(round(w / s.res))))
        for x, y in (P[0], P[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=v)

    def circle(s, d, c, r, v=1):
        x, y = s.px(c); rr = r / s.res
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=v)

    def arr(s, im):
        return s.np.array(im, dtype=bool)

    def area(s, a):
        return float(a.sum()) * s.res * s.res


# ---------------------------------------------------------------- 多角形の交差・距離 (単純多角形、凹でも可)
def _seg_inter(p1, p2, p3, p4):
    def o(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    o1, o2, o3, o4 = o(p1, p2, p3), o(p1, p2, p4), o(p3, p4, p1), o(p3, p4, p2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def poly_intersect(a, b):
    """内部が重なるか (辺が接するだけは重ならない扱い)"""
    if not bbox_overlap(bbox(a), bbox(b)):
        return False
    for i in range(len(a)):
        for j in range(len(b)):
            if _seg_inter(a[i - 1], a[i], b[j - 1], b[j]):
                return True
    ca = (sum(p[0] for p in a) / len(a), sum(p[1] for p in a) / len(a))
    cb = (sum(p[0] for p in b) / len(b), sum(p[1] for p in b) / len(b))
    return point_in_poly(ca[0], ca[1], b) or point_in_poly(cb[0], cb[1], a)


def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def poly_dist(a, b):
    """最短距離 (重なれば 0)"""
    if poly_intersect(a, b):
        return 0.0
    d = min(_pt_seg(p, b[j - 1], b[j]) for p in a for j in range(len(b)))
    return min(d, min(_pt_seg(p, a[j - 1], a[j]) for p in b for j in range(len(a))))


def overlap_area(a, b, res=0.02):
    """2 多角形の重なり面積 (小さなラスタで概算)"""
    ba, bb_ = bbox(a), bbox(b)
    ib = (max(ba[0], bb_[0]), max(ba[1], bb_[1]), min(ba[2], bb_[2]), min(ba[3], bb_[3]))
    if ib[0] >= ib[2] or ib[1] >= ib[3]:
        return 0.0
    r = Raster(ib, res)
    i1, d1 = r.new(); r.poly(d1, a)
    i2, d2 = r.new(); r.poly(d2, b)
    return r.area(r.arr(i1) & r.arr(i2))


def rect_local(fp, r):
    """footprint ローカルの矩形 [x0,y0,x1,y1] -> 絶対座標の 4 点"""
    x0, y0, x1, y1 = r
    return [fp.tf(x0, y0), fp.tf(x1, y0), fp.tf(x1, y1), fp.tf(x0, y1)]
