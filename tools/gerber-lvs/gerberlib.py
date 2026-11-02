"""gerber-lvs の部品: Gerber / Excellon / IPC-D-356 の読み込み、ラスタ化、run 単位の連結成分。
依存は numpy と Pillow だけ (pcbnew SWIG・shapely・scipy は使わない)。KiCad 10.0.7 の出力で確認。"""
import math, os, re, glob
import numpy as np
from PIL import Image, ImageDraw


class Unsupported(Exception):
    """対応していない Gerber/Excellon 構文 (終了コード 2 にする)"""


# ---------------------------------------------------------------- Gerber
class Gerber:
    """RS-274X を描画命令の列 (objs) にする。objs: (polarity 'D'/'C', kind, data, net)
    kind = 'flash' (ap, (x,y)) / 'stroke' (ap, (x0,y0), (x1,y1)) / 'region' [contour, ...]"""

    def __init__(self, path):
        self.path = path
        self.fs = None
        self.unit = None
        self.macros, self.aps, self.objs = {}, {}, []
        self.file_function = None
        self._parse(open(path, encoding='utf-8', errors='replace').read())

    def _coord(self, s):
        if self.unit is None: raise Unsupported(f'{os.path.basename(self.path)}: %MO (単位) より前に座標がある')
        return int(s) / 10 ** self.fs[1] * self.unit

    def _parse(self, txt):
        toks = re.findall(r'%[^%]*%|[^%*]+\*', txt, re.S)
        x = y = 0.0
        ap = None; interp = 1; quadrant = 'multi'; region = False; pol = 'D'; net = None
        contour, contours = [], []
        for t in toks:
            t = t.strip()
            if t.startswith('%'):
                for blk in self._split_ext(t[1:-1].strip()):
                    pol, net = self._ext(blk, pol, net)
                continue
            c = t[:-1].strip()
            if not c or c.startswith('G04'):
                continue
            if c in ('M02', 'M00'):
                break
            if c == 'G74':
                quadrant = 'single'; continue
            if c == 'G75':
                quadrant = 'multi'; continue
            m = re.match(r'^(?:G0?([0-9]+))?(?:X([+-]?\d+))?(?:Y([+-]?\d+))?(?:I([+-]?\d+))?(?:J([+-]?\d+))?(?:D0?([0-9]+))?$', c)
            if not m:  # G54D10 (古い aperture 選択)・G70/G71/G90 も上の正規表現で読むので、ここに来るのは知らない命令だけ
                raise Unsupported(f'{os.path.basename(self.path)}: unknown command {c!r}')
            g, sx, sy, si, sj, d = m.groups()
            if g is not None:
                g = int(g)
                if g == 91: raise Unsupported('incremental coordinates (G91)')
                if g in (70, 71):  # 古い単位指定 (%MO が無いときだけ使う)
                    if self.unit is None: self.unit = 25.4 if g == 70 else 1.0
                elif g not in (1, 2, 3, 36, 37, 54, 55, 90):
                    raise Unsupported(f'{os.path.basename(self.path)}: unknown G code G{g:02d}')
                if g in (1, 2, 3):
                    interp = g
                elif g == 36:
                    region = True; contour, contours = [], []
                    continue
                elif g == 37:
                    if len(contour) > 2: contours.append(contour)
                    if contours: self.objs.append((pol, 'region', contours, net))
                    region = False; contour, contours = [], []
                    continue
            if d is not None and int(d) >= 10:
                ap = int(d); continue
            if self.fs is None and (sx or sy):
                raise Unsupported('coordinates before %FS')
            nx = self._coord(sx) if sx is not None else x
            ny = self._coord(sy) if sy is not None else y
            if d is None:
                if sx is None and sy is None: continue
                d = 1  # 古い書き方: 直前の D01 を引き継ぐ
            d = int(d)
            if d == 1:
                if interp == 1:
                    pts = [(nx, ny)]
                else:
                    if quadrant == 'single': raise Unsupported('single-quadrant arcs (G74)')
                    pts = arc_pts(x, y, nx, ny, self._coord(si or '0'), self._coord(sj or '0'), interp == 2)
                if region:
                    if not contour: contour = [(x, y)]
                    contour += pts
                else:
                    if ap is None: raise Unsupported('D01 without aperture')
                    prev = (x, y)
                    for p in pts:
                        self.objs.append((pol, 'stroke', (ap, prev, p), net)); prev = p
            elif d == 2:
                if region and len(contour) > 2: contours.append(contour)
                contour = []
            elif d == 3:
                if ap is None: raise Unsupported('D03 without aperture')
                self.objs.append((pol, 'flash', (ap, (nx, ny)), net))
            x, y = nx, ny

    @staticmethod
    def _split_ext(body):
        # AM は '*' を含む 1 塊。ほかは '*' ごとに分ける
        if body.startswith('AM'):
            return [body]
        return [b for b in body.split('*') if b.strip()]

    def _ext(self, b, pol, net):
        b = b.strip()
        if b.startswith('FS'):
            m = re.search(r'X(\d)(\d)Y(\d)(\d)', b)
            if not m or 'I' in b[2:4] or 'T' in b[2:4]: raise Unsupported(f'FS {b} (incremental / trailing-zero 省略は未対応)')
            self.fs = (int(m.group(1)), int(m.group(2)))
        elif b.startswith('MO'):
            self.unit = 25.4 if 'IN' in b else 1.0
        elif b.startswith('AM'):
            parts = [p.strip() for p in b[2:].split('*') if p.strip()]
            self.macros[parts[0]] = [p for p in parts[1:] if not re.match(r'^0(\s|$)', p) and p != '0']
        elif b.startswith('AD'):
            m = re.match(r'ADD(\d+)([A-Za-z_.$][A-Za-z0-9_.$-]*),?(.*)$', b)
            if not m: raise Unsupported(f'AD {b}')
            params = [float(v) for v in m.group(3).split('X')] if m.group(3) else []
            self.aps[int(m.group(1))] = (m.group(2), params)
        elif b.startswith('LP'):
            pol = b[2]
        elif b.startswith('TF.FileFunction'):
            self.file_function = b.split(',')[1:]
        elif b.startswith('TO.N'):
            net = b.split(',', 1)[1] if ',' in b else None
        elif b.startswith('TD'):
            if b in ('TD', 'TD.N'): net = None
        elif b[:2] in ('LM', 'LR', 'LS'):
            if b not in ('LMN', 'LR0', 'LR0.0', 'LS1', 'LS1.0'): raise Unsupported(b)
        elif b.startswith('SR'):
            if b not in ('SR', 'SRX1Y1I0J0', 'SRX1Y1I0.0J0.0'): raise Unsupported('step and repeat (SR)')
        elif b.startswith(('IP', 'MI', 'OF', 'SF', 'IR', 'AS')):
            if b not in ('IPPOS', 'MIA0B0', 'OFA0B0', 'SFA1B1', 'IR0', 'ASAXBY'): raise Unsupported(b)
        return pol, net

    def bbox(self):
        xs, ys = [], []
        for _, k, d, _ in self.objs:
            if k == 'flash': xs.append(d[1][0]); ys.append(d[1][1])
            elif k == 'stroke': xs += [d[1][0], d[2][0]]; ys += [d[1][1], d[2][1]]
            else:
                for c in d: xs += [p[0] for p in c]; ys += [p[1] for p in c]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def arc_pts(x0, y0, x1, y1, i, j, cw, tol=0.0005):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0 = math.atan2(y0 - cy, x0 - cx); a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        if a1 >= a0 - 1e-12: a1 -= 2 * math.pi
    else:
        if a1 <= a0 + 1e-12: a1 += 2 * math.pi
    step = 2 * math.acos(max(-1.0, 1 - tol / max(r, 1e-9))) or 0.1
    n = max(2, int(abs(a1 - a0) / step) + 1)
    pts = [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n)]
    return pts + [(x1, y1)]


def circle_poly(cx, cy, r, step=0.01):
    n = max(16, int(2 * math.pi * r / step))
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def rot(pts, deg):
    if not deg: return list(pts)
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(x * c - y * s, x * s + y * c) for x, y in pts]


def _rect(w, h, cx=0.0, cy=0.0):
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def macro_shapes(prims, params):
    """aperture macro → [(exposure 1/0, polygon)] (aperture 座標)"""
    v = {i + 1: p for i, p in enumerate(params)}

    def ev(e):
        e = e.strip().replace('x', '*').replace('X', '*')
        e = re.sub(r'\$(\d+)', lambda m: repr(v.get(int(m.group(1)), 0.0)), e)
        if not re.fullmatch(r'[0-9.+\-*/() eE]*', e): raise Unsupported(f'macro expression {e!r}')
        return float(eval(e, {'__builtins__': {}})) if e else 0.0

    out = []
    for p in prims:
        if p.startswith('$'):
            k, e = p.split('=', 1); v[int(k[1:])] = ev(e); continue
        a = [ev(t) for t in p.split(',')]
        code = int(a[0])
        if code == 1:
            exp, d, cx, cy = a[1:5]; r = a[5] if len(a) > 5 else 0
            out.append((exp, rot(circle_poly(cx, cy, d / 2), r)))
        elif code == 4:
            exp, n = a[1], int(a[2])
            pts = [(a[3 + 2 * k], a[4 + 2 * k]) for k in range(n + 1)]
            out.append((exp, rot(pts, a[5 + 2 * n] if len(a) > 5 + 2 * n else 0)))
        elif code == 5:
            exp, n, cx, cy, d = a[1:6]; r = a[6] if len(a) > 6 else 0
            out.append((exp, rot([(cx + d / 2 * math.cos(2 * math.pi * k / n), cy + d / 2 * math.sin(2 * math.pi * k / n)) for k in range(int(n))], r)))
        elif code in (2, 20):
            exp, w, x0, y0, x1, y1, r = a[1:8]
            L = math.hypot(x1 - x0, y1 - y0) or 1e-9
            nx, ny = -(y1 - y0) / L * w / 2, (x1 - x0) / L * w / 2
            out.append((exp, rot([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)], r)))
        elif code == 21:
            exp, w, h, cx, cy, r = a[1:7]
            out.append((exp, rot(_rect(w, h, cx, cy), r)))
        elif code == 22:  # lower-left line (古い)
            exp, w, h, x0, y0, r = a[1:7]
            out.append((exp, rot(_rect(w, h, x0 + w / 2, y0 + h / 2), r)))
        else:
            raise Unsupported(f'aperture macro primitive {code}')
    return out


def aperture_shapes(g, ap):
    """→ (shapes [(exp, poly)], round_radius or None)。mm に換算 (AD/AM の寸法はファイルの単位)"""
    shapes, r = _aperture_shapes(g, ap); k = g.unit or 1.0
    if k == 1.0: return shapes, r
    return [(e, [(x * k, y * k) for x, y in pts]) for e, pts in shapes], (r * k if r is not None else None)


def _aperture_shapes(g, ap):
    if ap not in g.aps: raise Unsupported(f'undefined aperture D{ap}')
    name, p = g.aps[ap]
    hole = []
    if name == 'C':
        if len(p) > 1: hole = [(0, circle_poly(0, 0, p[1] / 2))]
        return [(1, circle_poly(0, 0, p[0] / 2))] + hole, (p[0] / 2 if not hole else None)
    if name in ('R', 'O', 'P'):
        hi = {'R': 2, 'O': 2, 'P': 3}[name]
        if len(p) > hi: hole = [(0, circle_poly(0, 0, p[hi] / 2))]
    if name == 'R':
        return [(1, _rect(p[0], p[1]))] + hole, None
    if name == 'O':
        w, h = p[0], p[1]; r = min(w, h) / 2
        if w >= h:
            s = [(1, _rect(w - 2 * r, h)), (1, circle_poly(-w / 2 + r, 0, r)), (1, circle_poly(w / 2 - r, 0, r))]
        else:
            s = [(1, _rect(w, h - 2 * r)), (1, circle_poly(0, -h / 2 + r, r)), (1, circle_poly(0, h / 2 - r, r))]
        return s + hole, None
    if name == 'P':
        d, n = p[0], int(p[1]); r = p[2] if len(p) > 2 else 0
        return [(1, rot([(d / 2 * math.cos(2 * math.pi * k / n), d / 2 * math.sin(2 * math.pi * k / n)) for k in range(n)], r))] + hole, None
    if name not in g.macros: raise Unsupported(f'undefined aperture macro {name}')
    return macro_shapes(g.macros[name], p), None


def hull(pts):
    pts = sorted(set(pts))
    if len(pts) < 3: return pts

    def cr(o, a, b): return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cr(lo[-2], lo[-1], p) <= 0: lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cr(up[-2], up[-1], p) <= 0: up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def stroke_polys(shapes, r, a, b):
    """aperture を a→b に掃いた形 [(exp, poly)]"""
    (xa, ya), (xb, yb) = a, b
    if r is not None:
        out = []
        L = math.hypot(xb - xa, yb - ya)
        if L > 1e-9:
            nx, ny = -(yb - ya) / L * r, (xb - xa) / L * r
            out.append((1, [(xa + nx, ya + ny), (xb + nx, yb + ny), (xb - nx, yb - ny), (xa - nx, ya - ny)]))
        for cx, cy in ((xa, ya), (xb, yb)):
            out.append((1, [(px + cx, py + cy) for px, py in shapes[0][1]]))
        return out
    return [(1, hull([(p[0] + xa, p[1] + ya) for p in pts] + [(p[0] + xb, p[1] + yb) for p in pts])) for exp, pts in shapes if exp]


# ---------------------------------------------------------------- raster
class Raster:
    """mm 座標 (Gerber, Y 上向き) ↔ 画素 (行 0 = 上)"""

    def __init__(self, x0, y0, x1, y1, px):
        self.x0, self.y0, self.y1, self.px = x0, y0, y1, px
        self.W = int(math.ceil((x1 - x0) / px)); self.H = int(math.ceil((y1 - y0) / px))

    def xy2pix(self, x, y):
        return ((x - self.x0) / self.px - 0.5, (self.y1 - y) / self.px - 0.5)

    def pix2xy(self, c, r):
        return (self.x0 + (c + 0.5) * self.px, self.y1 - (r + 0.5) * self.px)

    def new(self, val=0):
        return Image.new('L', (self.W, self.H), val)


def render(g, R, img=None, net_filter=None):
    """Gerber を 'L' 画像に描く (255 = 銅)。net_filter(net) が False の物は描かない (診断用)"""
    img = img if img is not None else R.new()
    dr = ImageDraw.Draw(img); cache = {}

    def poly(pts, val):
        q = [R.xy2pix(x, y) for x, y in pts]
        if len(q) >= 3: dr.polygon(q, fill=val)

    for pol, kind, data, net in g.objs:
        if net_filter is not None and not net_filter(net): continue
        val = 255 if pol == 'D' else 0
        if kind == 'region':
            for c in data: poly(c, val)
            continue
        ap = data[0]
        if ap not in cache: cache[ap] = aperture_shapes(g, ap)
        shapes, r = cache[ap]
        if kind == 'flash':
            x, y = data[1]
            if all(exp for exp, _ in shapes):
                for exp, pts in shapes: poly([(px + x, py + y) for px, py in pts], val)
                continue
            # 穴付き aperture / exposure off のマクロ: 穴は aperture の中だけを抜く (下の銅は消さない、clear で銅を足さない)
            q = [[R.xy2pix(px + x, py + y) for px, py in pts] for _, pts in shapes]
            c0 = int(math.floor(min(c for s in q for c, _ in s))) - 1; r0 = int(math.floor(min(r for s in q for _, r in s))) - 1
            c1 = int(math.ceil(max(c for s in q for c, _ in s))) + 2; r1 = int(math.ceil(max(r for s in q for _, r in s))) + 2
            mask = Image.new('L', (c1 - c0, r1 - r0), 0); md = ImageDraw.Draw(mask)
            for (exp, _), s in zip(shapes, q):
                if len(s) >= 3: md.polygon([(c - c0, r - r0) for c, r in s], fill=255 if exp else 0)
            img.paste(val, (c0, r0, c1, r1), mask)
        else:
            for exp, pts in stroke_polys(shapes, r, data[1], data[2]):
                poly(pts, val)
    return img


def flash_bbox(g, ap, x, y):
    shapes, _ = aperture_shapes(g, ap)
    xs = [p[0] for e, s in shapes if e for p in s]; ys = [p[1] for e, s in shapes if e for p in s]
    return (x + min(xs), y + min(ys), x + max(xs), y + max(ys))


# ---------------------------------------------------------------- Excellon
class Hole:
    __slots__ = ('x0', 'y0', 'x1', 'y1', 'd', 'plated', 'span', 'src')

    def __init__(self, x0, y0, x1, y1, d, plated, span, src):
        self.x0, self.y0, self.x1, self.y1, self.d, self.plated, self.span, self.src = x0, y0, x1, y1, d, plated, span, src

    @property
    def x(self): return (self.x0 + self.x1) / 2

    @property
    def y(self): return (self.y0 + self.y1) / 2

    @property
    def is_slot(self): return (self.x0, self.y0) != (self.x1, self.y1)

    def samples(self, step):
        L = math.hypot(self.x1 - self.x0, self.y1 - self.y0)
        if L <= 0: return [(self.x0, self.y0)]
        n = max(1, int(L / step) + 1)
        return [(self.x0 + (self.x1 - self.x0) * k / n, self.y0 + (self.y1 - self.y0) * k / n) for k in range(n + 1)]

    def dist(self, X, Y):
        """穴の中心線 (点 or 線分) からの距離 (numpy 配列可)"""
        vx, vy = self.x1 - self.x0, self.y1 - self.y0; L2 = vx * vx + vy * vy
        if L2 == 0: return np.hypot(X - self.x0, Y - self.y0)
        t = np.clip(((X - self.x0) * vx + (Y - self.y0) * vy) / L2, 0, 1)
        return np.hypot(X - (self.x0 + t * vx), Y - (self.y0 + t * vy))


def parse_drill(path, nlayers):
    """KiCad の Excellon (小数点付き座標)。PTH/NPTH は TA.AperFunction / FileFunction から。G85 と M15/M16 の長穴に対応"""
    tools, holes = {}, []
    unit = 1.0; cur = None; file_pl = None; span = (1, nlayers); tool_pl = None
    in_header = True; route = None; pos = None
    name = os.path.basename(path)
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.strip()
        if not ln: continue
        if ln.startswith(';'):
            m = re.search(r'TF\.FileFunction,(\w+),(\d+),(\d+)', ln)
            if m:
                kind = m.group(1)
                file_pl = {'Plated': True, 'NonPlated': False}.get(kind)
                span = (int(m.group(2)), int(m.group(3)))
            if 'TA.AperFunction' in ln:
                tool_pl = 'NonPlated' not in ln and 'NPTH' not in ln
            continue
        if ln in ('METRIC', 'M71') or ln.startswith('METRIC,'): unit = 1.0; continue
        if ln in ('INCH', 'M72') or ln.startswith('INCH,'): unit = 25.4; continue
        if ln == '%' or ln == 'M95': in_header = False; continue
        m = re.match(r'^T(\d+)(?:F\d+S\d+)?C([\d.]+)', ln)
        if m:
            pl = tool_pl if tool_pl is not None else (file_pl if file_pl is not None else True)
            tools[int(m.group(1))] = (float(m.group(2)) * unit, pl); tool_pl = None; continue
        m = re.match(r'^T(\d+)$', ln)
        if m:
            cur = int(m.group(1))
            if cur == 0: cur = None
            continue
        if ln in ('M48', 'G90', 'G05', 'FMAT,2', 'M30', 'M00', 'G01', 'M17'): continue
        if ln == 'M15': route = pos; continue
        if ln == 'M16': route = None; continue
        m = re.match(r'^(G00|G01)?X([+-]?[\d.]+)Y([+-]?[\d.]+)(?:G85X([+-]?[\d.]+)Y([+-]?[\d.]+))?$', ln)
        if m:
            if '.' not in m.group(2) + m.group(3): raise Unsupported(f'{name}: coordinates without decimal point')
            x, y = float(m.group(2)) * unit, float(m.group(3)) * unit
            if cur is None: raise Unsupported(f'{name}: hit without tool')
            d, pl = tools[cur]
            if m.group(1) == 'G00':
                pos = (x, y); continue
            if m.group(1) == 'G01' and route is not None:
                holes.append(Hole(route[0], route[1], x, y, d, pl, span, name)); route = (x, y); continue
            if m.group(4):
                holes.append(Hole(x, y, float(m.group(4)) * unit, float(m.group(5)) * unit, d, pl, span, name))
            else:
                holes.append(Hole(x, y, x, y, d, pl, span, name))
            continue
        if re.match(r'^(G0[0-3]|M\d+|ICI|VER|FMAT|TCST|ATC|R\d)', ln) or in_header:
            if re.match(r'^(G02|G03|R\d)', ln): raise Unsupported(f'{name}: {ln}')
            continue
        raise Unsupported(f'{name}: unknown line {ln!r}')
    return holes


# ---------------------------------------------------------------- IPC-D-356
def parse_d356(path):
    """kicad-cli pcb export ipcd356 → test point の list。
    ネット名は 14 文字まで・大文字 (長い名前は NNAME 別名を戻す)。acc: 0=スルー、n=n 層目 (1=表、最下層=裏)"""
    alias, pts = {}, []
    unit = 0.00254  # UNITS CUST 0 = inch, 0.0001 in
    for ln in open(path, encoding='utf-8', errors='replace'):
        ln = ln.rstrip('\n')
        if ln.startswith('P  UNITS'):
            if 'CUST 1' in ln or 'SI' in ln: unit = 0.001
            continue
        if ln.startswith('P  NNAME'):
            alias[ln[8:14].strip()] = ln[14:].strip(); continue
        if ln[:3] not in ('317', '327', '367'): continue
        net = ln[3:17].strip(); net = alias.get(net, net)
        m = re.search(r'A(\d\d)X([+-]\d+)Y([+-]\d+)X(\d+)Y(\d+)R(\d+)', ln)
        if not m: raise Unsupported(f'IPC-D-356 line: {ln!r}')
        hm = re.search(r'D(\d{4})([PU])', ln)
        pts.append(dict(type=ln[:3], net=net, ref=ln[20:26].strip(), pin=ln[27:31].strip(),
                        mid='M' in ln[31:32], x=int(m.group(2)) * unit, y=int(m.group(3)) * unit,
                        acc=int(m.group(1)), sx=int(m.group(4)) * unit, sy=int(m.group(5)) * unit,
                        rot=int(m.group(6)), hole=int(hm.group(1)) * unit if hm else 0.0,
                        plated=(hm.group(2) == 'P') if hm else None))
    return pts


# ---------------------------------------------------------------- run-length 連結成分
class Runs:
    """2 値画像を行ごとの run [s, e) にして 4 近傍で連結成分を付ける。画像は帯ごとに読み、保持しない"""

    def __init__(self, img, band=512):
        W, H = img.size; self.W, self.H = W, H; self.B = W + 2
        rows, ss, es = [], [], []
        for r0 in range(0, H, band):
            r1 = min(H, r0 + band)
            a = np.asarray(img.crop((0, r0, W, r1))) > 127
            pad = np.zeros((r1 - r0, W + 2), np.int8); pad[:, 1:-1] = a
            d = np.diff(pad, axis=1)
            sr, sc = np.nonzero(d == 1); _, ec = np.nonzero(d == -1)
            rows.append(sr.astype(np.int32) + r0); ss.append(sc.astype(np.int32)); es.append(ec.astype(np.int32))
        self.row = np.concatenate(rows) if rows else np.zeros(0, np.int32)
        self.s = np.concatenate(ss) if ss else np.zeros(0, np.int32)
        self.e = np.concatenate(es) if es else np.zeros(0, np.int32)
        B = self.B
        self.kS = self.row.astype(np.int64) * B + self.s
        kE = self.row.astype(np.int64) * B + self.e
        nxt = (self.row.astype(np.int64) + 1) * B
        lo = np.searchsorted(kE, nxt + self.s, 'right')
        hi = np.searchsorted(self.kS, nxt + self.e, 'left')
        cnt = np.maximum(hi - lo, 0)
        a = np.repeat(np.arange(len(self.row)), cnt)
        b = np.repeat(lo, cnt) + (np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt))
        self.lab = _components(len(self.row), a, b)

    def labels(self):
        return np.unique(self.lab)

    def label_at(self, r, c):
        i = int(np.searchsorted(self.kS, np.int64(r) * self.B + c, 'right')) - 1
        if i >= 0 and self.row[i] == r and self.s[i] <= c < self.e[i]: return int(self.lab[i])
        return -1

    def labels_in_disk(self, R, x, y, rad):
        """中心 (x,y) mm、半径 rad mm の円に掛かる run の label → {label: 画素数}"""
        c0, r0 = R.xy2pix(x, y); rp = rad / R.px; out = {}
        for r in range(int(math.floor(r0 - rp)), int(math.ceil(r0 + rp)) + 1):
            if r < 0 or r >= self.H: continue
            dy = r - r0
            if dy * dy > rp * rp: continue
            hw = math.sqrt(rp * rp - dy * dy); ca, cb = int(math.ceil(c0 - hw)), int(math.floor(c0 + hw))
            if cb < ca: continue
            i0 = max(int(np.searchsorted(self.kS, np.int64(r) * self.B + ca, 'right')) - 1, 0)
            i1 = int(np.searchsorted(self.kS, np.int64(r) * self.B + cb, 'right'))
            for i in range(i0, i1):
                if self.row[i] == r and self.s[i] <= cb and self.e[i] > ca:
                    n = min(int(self.e[i]), cb + 1) - max(int(self.s[i]), ca)
                    out[int(self.lab[i])] = out.get(int(self.lab[i]), 0) + n
        return out

    def window(self, r0, r1, c0, c1, labels=None):
        """[r0,r1)×[c0,c1) の 2 値 (labels を指定するとその label の run だけ)"""
        out = np.zeros((max(r1 - r0, 0), max(c1 - c0, 0)), bool)
        i0 = int(np.searchsorted(self.kS, np.int64(r0) * self.B, 'left'))
        i1 = int(np.searchsorted(self.kS, np.int64(r1) * self.B, 'left'))
        sel = np.arange(i0, i1)
        sel = sel[(self.e[sel] > c0) & (self.s[sel] < c1)]
        if labels is not None:
            sel = sel[np.isin(self.lab[sel], np.fromiter(labels, np.int64))]
        for i in sel:
            out[self.row[i] - r0, max(self.s[i], c0) - c0:min(self.e[i], c1) - c0] = True
        return out

    def bbox_of(self, labels):
        m = np.isin(self.lab, np.fromiter(labels, np.int64))
        if not m.any(): return None
        return int(self.row[m].min()), int(self.row[m].max()) + 1, int(self.s[m].min()), int(self.e[m].max())


def _components(n, a, b):
    """辺 (a,b) の連結成分 (最小 index を代表に)。min-label 伝播 + pointer jumping"""
    lab = np.arange(n)
    if len(a) == 0: return lab
    while True:
        m = np.minimum(lab[a], lab[b])
        new = lab.copy(); np.minimum.at(new, lab[a], m); np.minimum.at(new, lab[b], m)
        while True:
            nn = new[new]
            if np.array_equal(nn, new): break
            new = nn
        if np.array_equal(new, lab): return lab
        lab = new


class DSU:
    def __init__(self): self.p = {}

    def find(self, x):
        p = self.p; p.setdefault(x, x)
        while p[x] != x:
            p[x] = p[p[x]]; x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb


# ---------------------------------------------------------------- 外形
def outline_loops(segs, tol=0.002):
    """Edge_Cuts の線分 → (閉路の list, 開いた鎖の数)"""
    def key(p): return (round(p[0] / tol), round(p[1] / tol))
    adj = {}
    for i, (a, b) in enumerate(segs):
        adj.setdefault(key(a), []).append(i); adj.setdefault(key(b), []).append(i)
    used = [False] * len(segs); loops = []; open_chains = 0
    for i in range(len(segs)):
        if used[i]: continue
        used[i] = True; a, b = segs[i]; chain = [a, b]; start = key(a); cur = key(b)
        while cur != start:
            nxt = [j for j in adj.get(cur, []) if not used[j]]
            if not nxt: break
            j = nxt[0]; used[j] = True; p, q = segs[j]
            if key(p) == cur: chain.append(q); cur = key(q)
            else: chain.append(p); cur = key(p)
        if cur == start and len(chain) >= 4: loops.append(chain[:-1])
        else: open_chains += 1
    return loops, open_chains


def poly_area(p):
    return 0.5 * sum(p[i][0] * p[i - 1][1] - p[i - 1][0] * p[i][1] for i in range(len(p)))


def point_in_poly(x, y, p):
    inside = False; n = len(p)
    for i in range(n):
        x1, y1 = p[i]; x2, y2 = p[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1: inside = not inside
    return inside


def seg_dist(X, Y, segs):
    """点群 (X,Y) から線分群 segs (N×4) までの最短距離"""
    d = np.full(len(X), np.inf)
    for k in range(0, len(segs), 64):
        s = segs[k:k + 64]
        x0, y0, x1, y1 = (s[:, i][None, :] for i in range(4))
        vx, vy = x1 - x0, y1 - y0; L2 = np.maximum(vx * vx + vy * vy, 1e-18)
        t = np.clip(((X[:, None] - x0) * vx + (Y[:, None] - y0) * vy) / L2, 0, 1)
        d = np.minimum(d, np.hypot(X[:, None] - (x0 + t * vx), Y[:, None] - (y0 + t * vy)).min(axis=1))
    return d
