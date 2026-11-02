"""pcbread — .kicad_pcb (KiCad 10) を読む (読み取りのみ)。
外形 (Edge.Cuts)、銅層、板厚、stackup (あれば)、footprint (位置・回転・pad・courtyard・LCSC)、track、via、zone の filled_polygon。"""
import math, re

_tok = re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+)')


def parse_sexp(s):
    stack, cur = [], []
    for m in _tok.finditer(s):
        t = m.group(1)
        if t == '(':
            stack.append(cur); cur = []
        elif t == ')':
            x = cur; cur = stack.pop(); cur.append(x)
        else:
            cur.append(t[1:-1].replace('\\"', '"') if t[0] == '"' else t)
    return cur[0]


def kids(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def kid(node, name):
    k = kids(node, name)
    return k[0] if k else None


def val(node, name, i=1, d=None):
    k = kid(node, name)
    return k[i] if k is not None and len(k) > i else d


def fnum(x):
    return float(x)


def rot(x, y, deg):
    # KiCad: 回転は画面上で反時計回り (y 下向き座標で -deg)
    a = math.radians(-deg)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def _arc_pts(s, m, e, n=16):
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [s, e]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, m, e))
    def norm(a): return a % (2 * math.pi)
    sweep = norm(a3 - a1); mid = norm(a2 - a1)
    if mid > sweep:
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]


def net_of(node):
    k = kid(node, 'net')
    return k[-1] if k is not None and len(k) > 1 else ''


def _xy(node):
    return (fnum(node[1]), fnum(node[2]))


def _outline(root):
    segs = []
    for typ in ('gr_line', 'gr_arc', 'gr_rect', 'gr_circle', 'gr_poly'):
        for g in kids(root, typ):
            if val(g, 'layer') != 'Edge.Cuts':
                continue
            if typ == 'gr_line':
                segs.append([_xy(kid(g, 'start')), _xy(kid(g, 'end'))])
            elif typ == 'gr_arc':
                segs.append(_arc_pts(_xy(kid(g, 'start')), _xy(kid(g, 'mid')), _xy(kid(g, 'end'))))
            elif typ == 'gr_rect':
                (x1, y1), (x2, y2) = _xy(kid(g, 'start')), _xy(kid(g, 'end'))
                return [[(x1, y1), (x2, y1), (x2, y2), (x1, y2)]]
            elif typ == 'gr_circle':
                c, e = _xy(kid(g, 'center')), _xy(kid(g, 'end')); r = math.dist(c, e)
                return [[(c[0] + r * math.cos(t * math.pi / 32), c[1] + r * math.sin(t * math.pi / 32)) for t in range(64)]]
            else:
                return [[_xy(p) for p in kids(kid(g, 'pts'), 'xy')]]
    # 線分をつないで閉じた多角形にする
    polys = []
    segs = [list(s) for s in segs]
    while segs:
        poly = segs.pop(0)
        changed = True
        while changed and segs:
            changed = False
            for i, s in enumerate(segs):
                if math.dist(poly[-1], s[0]) < 1e-3:
                    poly += s[1:]; segs.pop(i); changed = True; break
                if math.dist(poly[-1], s[-1]) < 1e-3:
                    poly += s[::-1][1:]; segs.pop(i); changed = True; break
        polys.append(poly)
    return polys


def _pad(p, fx, fy, frot, flip):
    num = p[1]; ptype = p[2]; shape = p[3]
    at = kid(p, 'at'); px, py = fnum(at[1]), fnum(at[2]); prot = fnum(at[3]) if len(at) > 3 else 0.0
    sz = kid(p, 'size'); w, h = fnum(sz[1]), fnum(sz[2])
    layers = kid(p, 'layers')[1:]
    dx, dy = rot(px, py, frot)
    drill = kid(p, 'drill')
    d = 0.0
    if drill is not None:
        nums = [x for x in drill[1:] if not isinstance(x, list) and x != 'oval']
        d = fnum(nums[0]) if nums else 0.0
    return dict(net=net_of(p), num=num, type=ptype, shape=shape, x=fx + dx, y=fy + dy, rot=prot, w=w, h=h,
                layers=layers, drill=d, rr=fnum(val(p, 'roundrect_rratio', d=0) or 0))


def _fp_poly_box(fp, layer_suffix):
    pts = []
    for typ in ('fp_line', 'fp_rect', 'fp_poly', 'fp_circle'):
        for g in kids(fp, typ):
            ly = val(g, 'layer', d='')
            if not ly.endswith(layer_suffix):
                continue
            if typ == 'fp_poly':
                pts += [_xy(q) for q in kids(kid(g, 'pts'), 'xy')]
            elif typ == 'fp_circle':
                c, e = _xy(kid(g, 'center')), _xy(kid(g, 'end')); r = math.dist(c, e)
                pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
            else:
                pts += [_xy(kid(g, 'start')), _xy(kid(g, 'end'))]
    return pts


def read_pcb(path):
    root = parse_sexp(open(path, encoding='utf-8').read())
    gen = kid(root, 'general')
    thick = fnum(val(gen, 'thickness', d='1.6'))
    cu_layers = [l[1] for l in kid(root, 'layers')[1:] if isinstance(l, list) and l[1].endswith('.Cu')]
    # 物理順: F.Cu, In1.Cu.., B.Cu
    def order(n):
        return -1 if n == 'F.Cu' else 999 if n == 'B.Cu' else int(re.sub(r'\D', '', n))
    cu_layers.sort(key=order)
    stack = None
    setup = kid(root, 'setup')
    if setup is not None and kid(setup, 'stackup') is not None:
        stack = []
        for l in kids(kid(setup, 'stackup'), 'layer'):
            stack.append(dict(name=l[1], type=val(l, 'type'), thickness=float(val(l, 'thickness', d='0') or 0),
                              material=val(l, 'material'), epsilon_r=float(val(l, 'epsilon_r')) if val(l, 'epsilon_r') else None))
    fps = []
    for fp in kids(root, 'footprint'):
        at = kid(fp, 'at'); fx, fy = fnum(at[1]), fnum(at[2]); frot = fnum(at[3]) if len(at) > 3 else 0.0
        side = val(fp, 'layer')
        props = {pr[1]: pr[2] for pr in kids(fp, 'property')}
        attr = kid(fp, 'attr')
        pads = [_pad(p, fx, fy, frot, side == 'B.Cu') for p in kids(fp, 'pad')]
        cy = _fp_poly_box(fp, '.Fab') or _fp_poly_box(fp, '.CrtYd')
        if cy:
            xs, ys = [q[0] for q in cy], [q[1] for q in cy]
            corners = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]
        else:
            corners = []
        outline = [(fx + rot(x, y, frot)[0], fy + rot(x, y, frot)[1]) for x, y in corners]
        models = []
        for m in kids(fp, 'model'):
            def v3(n):
                k = kid(m, n); x = kid(k, 'xyz') if k is not None else None
                return tuple(float(t) for t in x[1:4]) if x is not None else ((1, 1, 1) if n == 'scale' else (0, 0, 0))
            models.append(dict(path=m[1], offset=v3('offset'), scale=v3('scale'), rotate=v3('rotate'),
                               hide=any(isinstance(c, list) and c[:2] == ['hide', 'yes'] for c in m)))
        fps.append(dict(models=models, ref=props.get('Reference', '?'), value=props.get('Value', ''), lcsc=props.get('LCSC', ''),
                        mpn=props.get('MPN', ''), footprint=fp[1], side=side, x=fx, y=fy, rot=frot,
                        dnp=bool(attr and 'dnp' in attr) or any(isinstance(c, list) and c[:2] == ['dnp', 'yes'] for c in fp),
                        pads=pads, outline=outline))
    tracks = []
    for s in kids(root, 'segment'):
        tracks.append(dict(a=_xy(kid(s, 'start')), b=_xy(kid(s, 'end')), w=fnum(val(s, 'width')), layer=val(s, 'layer'), net=net_of(s)))
    for s in kids(root, 'arc'):
        pts = _arc_pts(_xy(kid(s, 'start')), _xy(kid(s, 'mid')), _xy(kid(s, 'end')), 8)
        for a, b in zip(pts, pts[1:]):
            tracks.append(dict(a=a, b=b, w=fnum(val(s, 'width')), layer=val(s, 'layer'), net=net_of(s)))
    vias = []
    for v in kids(root, 'via'):
        ly = kid(v, 'layers')
        vias.append(dict(x=_xy(kid(v, 'at'))[0], y=_xy(kid(v, 'at'))[1], size=fnum(val(v, 'size')),
                         drill=fnum(val(v, 'drill')), layers=ly[1:] if ly else ['F.Cu', 'B.Cu'], net=net_of(v)))
    zones = []; zones_unfilled = []
    for z in kids(root, 'zone'):
        if not kids(z, 'filled_polygon') and kid(z, 'keepout') is None:
            zones_unfilled.append(dict(net=net_of(z), layer=val(z, 'layer') or ' '.join((kid(z, 'layers') or ['', ''])[1:])))
        for fpg in kids(z, 'filled_polygon'):
            zones.append(dict(layer=val(fpg, 'layer'), net=val(z, 'net_name') or net_of(z),
                              pts=[_xy(q) for q in kids(kid(fpg, 'pts'), 'xy')]))
    return dict(path=path, thickness=thick, cu_layers=cu_layers, stackup=stack, outline=_outline(root),
                footprints=fps, tracks=tracks, vias=vias, zones=zones, zones_unfilled=zones_unfilled)
