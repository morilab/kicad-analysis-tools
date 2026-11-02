#!/usr/bin/env python3
"""kisch: KiCad 回路図 (.kicad_sch, KiCad 6〜10) を標準ライブラリだけで読むための小さなモデル。

- S式パーサ (parse_sexpr)
- lib_symbols からピン・本体図形を取り出し、配置 (at/rotation/mirror) で回路図座標へ変換
  * シンボル定義の中は Y 軸が上向き、回路図は Y 軸が下向き
  * 回転は画面上で反時計回り、ミラーは回転の後に画面座標で適用 (KiCad の TRANSFORM と同じ)
- ワイヤ・ラベル・ピン・接続点などの幾何接続 (Connectivity)
- 階層シートの展開 (load_hierarchy)

座標はすべて mm。内部では 1e-4 mm 単位の整数に丸めて比較する。
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

Q = 10000  # 1 mm = 10000 単位 (0.1 µm)


def qi(v: float) -> int:
    return int(round(v * Q))


def qp(p):
    return (qi(p[0]), qi(p[1]))


# ---------------------------------------------------------------- S式
class QStr(str):
    """引用符付き文字列 (シンボル名と区別する)"""


_tok = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))', re.S)
_esc = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}


def _unescape(s: str) -> str:
    if "\\" not in s:
        return s
    return re.sub(r"\\(.)", lambda m: _esc.get(m.group(1), m.group(1)), s)


_tok2 = re.compile(r'(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+)', re.S)


def parse_sexpr(text: str):
    stack = []
    cur = []
    for m in _tok2.finditer(text):
        lp, rp, qs, at = m.groups()
        if lp:
            stack.append(cur)
            cur = []
        elif rp:
            if not stack:
                raise ValueError("括弧の対応が取れていません")
            parent = stack.pop()
            parent.append(cur)
            cur = parent
        elif qs is not None:
            cur.append(QStr(_unescape(qs)))
        else:
            cur.append(at)
    if stack:
        raise ValueError("括弧の対応が取れていません")
    return cur[0] if cur else None


def head(n):
    return n[0] if isinstance(n, list) and n else None


def find(n, name):
    for c in n[1:] if isinstance(n, list) else []:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def findall(n, name):
    return [c for c in (n[1:] if isinstance(n, list) else []) if isinstance(c, list) and c and c[0] == name]


def val(n, name, default=None, idx=1):
    c = find(n, name)
    if c is None or len(c) <= idx:
        return default
    return c[idx]


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def is_yes(n, name):
    """(hide yes) / (hide) / 旧形式の裸の 'hide' atom を判定"""
    c = find(n, name)
    if c is not None:
        return len(c) == 1 or c[1] in ("yes", "true")
    return name in [x for x in n[1:] if not isinstance(x, list)]


def at_of(n):
    a = find(n, "at")
    if a is None:
        return (0.0, 0.0, 0.0)
    return (fnum(a[1]), fnum(a[2]), fnum(a[3]) if len(a) > 3 else 0.0)


# ---------------------------------------------------------------- 用紙
PAPER = {  # 横置き (landscape) の幅×高さ mm
    "A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841),
    "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
    "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4), "GERBER": (812.8, 812.8),
}


def paper_of(root):
    p = find(root, "paper")
    if p is None:
        return "A4", 297.0, 210.0
    name = str(p[1])
    if name == "User":
        return name, fnum(p[2]), fnum(p[3])
    w, h = PAPER.get(name, (297, 210))
    if "portrait" in p[2:]:
        w, h = h, w
        name += " (縦)"
    return name, float(w), float(h)


# ---------------------------------------------------------------- 文字の大きさ
def text_effects(n):
    eff = find(n, "effects")
    size = 1.27
    face = None
    hidden = False
    justify = []
    bold = False
    if eff is not None:
        f = find(eff, "font")
        if f is not None:
            s = find(f, "size")
            if s is not None:
                size = fnum(s[1], 1.27)
            face = val(f, "face")
            bold = is_yes(f, "bold")
        hidden = is_yes(eff, "hide")
        j = find(eff, "justify")
        if j is not None:
            justify = [str(x) for x in j[1:]]
    if is_yes(n, "hide"):
        hidden = True
    return dict(size=size, face=face, hidden=hidden, justify=justify, bold=bold)


def text_bbox(txt, x, y, angle, size, justify, kind="text"):
    """文字列の外接矩形の近似。1 文字の送り幅は KiCad 10 のストロークフォントを PDF に出して実測した値
    (size 比: 英大文字・数字 ≈0.95、英小文字 ≈0.78、かな ≈1.05、漢字・全角 ≈1.48、半角カナ ≈0.53)"""
    txt = re.sub(r"[~_^]\{([^}]*)\}", r"\1", str(txt))  # 上線・下付き・上付きの書式は幅に数えない
    lines = txt.split("\n") or [""]

    def cw(ch):
        o = ord(ch)
        if o < 0x80:
            if ch.isupper() or ch.isdigit():
                return 0.95
            if ch.islower():
                return 0.78
            return 0.75
        if 0xFF61 <= o <= 0xFF9F:
            return 0.53
        if 0x3041 <= o <= 0x30FF:
            return 1.05
        if o >= 0x2E80:
            return 1.48
        return 0.95

    def w_of(line):
        return sum(size * cw(ch) for ch in line)

    w = max(w_of(l) for l in lines)
    h = size * (1.6 * (len(lines) - 1) + 1.0)
    if kind in ("label", "global_label", "hierarchical_label"):
        # ラベルはアンカーから文字が伸びる (角度方向)。global は枠付きで少し大きい
        pad = size * (0.5 if kind == "label" else 1.2)
        w += pad
        a = int(round(angle)) % 360
        if a == 0:
            return (x, y - h, x + w, y + (h * 0.2 if kind == "label" else h * 0.6))
        if a == 180:
            return (x - w, y - h, x, y + (h * 0.2 if kind == "label" else h * 0.6))
        if a == 90:
            return (x - h, y - w, x + (h * 0.2 if kind == "label" else h * 0.6), y)
        return (x - h, y, x + (h * 0.2 if kind == "label" else h * 0.6), y + w)
    # 一般テキスト (text / field)。KiCad の既定は中央揃え
    hj = "left" if "left" in justify else ("right" if "right" in justify else "center")
    vj = "top" if "top" in justify else ("bottom" if "bottom" in justify else "center")
    x0 = {"left": 0, "center": -w / 2, "right": -w}[hj]
    y0 = {"top": 0, "center": -h / 2, "bottom": -h}[vj]
    pts = [(x0, y0), (x0 + w, y0), (x0, y0 + h), (x0 + w, y0 + h)]
    a = math.radians(angle)
    ca, sa = math.cos(a), math.sin(a)
    tp = [(x + px * ca + py * sa, y - px * sa + py * ca) for px, py in pts]
    xs = [p[0] for p in tp]
    ys = [p[1] for p in tp]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------- ライブラリシンボル
@dataclass
class LibPin:
    number: str
    name: str
    etype: str
    x: float
    y: float
    angle: float
    length: float
    hidden: bool
    unit: int
    style: int


@dataclass
class LibSym:
    name: str
    power: str | None  # None / 'global' / 'local'
    pins: list
    shapes: list  # (unit, style, [(x,y),...]) 本体図形の点列 (ライブラリ座標)
    extends: str | None = None
    pin_names_hidden: bool = False


def _shape_points(g):
    h = g[0]
    pts = []
    if h in ("rectangle",):
        s = find(g, "start")
        e = find(g, "end")
        if s and e:
            pts = [(fnum(s[1]), fnum(s[2])), (fnum(e[1]), fnum(e[2]))]
    elif h in ("polyline", "bezier"):
        p = find(g, "pts")
        if p:
            pts = [(fnum(xy[1]), fnum(xy[2])) for xy in findall(p, "xy")]
    elif h == "circle":
        c = find(g, "center")
        r = fnum(val(g, "radius"), 0)
        if c:
            cx, cy = fnum(c[1]), fnum(c[2])
            pts = [(cx - r, cy - r), (cx + r, cy + r)]
    elif h == "arc":
        for k in ("start", "mid", "end"):
            c = find(g, k)
            if c:
                pts.append((fnum(c[1]), fnum(c[2])))
        if len(pts) == 3:  # 円弧の外接: 中心と半径から 4 方向の極値も含める
            (x1, y1), (x2, y2), (x3, y3) = pts
            d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
            if abs(d) > 1e-9:
                ux = ((x1 ** 2 + y1 ** 2) * (y2 - y3) + (x2 ** 2 + y2 ** 2) * (y3 - y1) + (x3 ** 2 + y3 ** 2) * (y1 - y2)) / d
                uy = ((x1 ** 2 + y1 ** 2) * (x3 - x2) + (x2 ** 2 + y2 ** 2) * (x1 - x3) + (x3 ** 2 + y3 ** 2) * (x2 - x1)) / d
                r = math.hypot(x1 - ux, y1 - uy)
                a1 = math.atan2(y1 - uy, x1 - ux)
                a2 = math.atan2(y2 - uy, x2 - ux)
                a3 = math.atan2(y3 - uy, x3 - ux)

                def norm(a):
                    return a % (2 * math.pi)

                # start→mid→end の向きで掃引範囲を判定
                ccw = norm(a2 - a1) < norm(a3 - a1)
                for k in range(4):
                    ak = k * math.pi / 2
                    inside = norm(ak - a1) <= norm(a3 - a1) if ccw else norm(a1 - ak) <= norm(a1 - a3)
                    if inside:
                        pts.append((ux + r * math.cos(ak), uy + r * math.sin(ak)))
    return pts


def parse_lib_symbol(node):
    name = str(node[1])
    pw = find(node, "power")
    power = None
    if pw is not None:
        power = str(pw[1]) if len(pw) > 1 else "global"
    pins, shapes = [], []
    base = name.split(":")[-1]
    pn = find(node, "pin_names")
    pin_names_hidden = bool(pn is not None and is_yes(pn, "hide"))
    ext = val(node, "extends")

    def take(sub, unit, style):
        for g in sub[1:]:
            if not isinstance(g, list):
                continue
            if g[0] == "pin":
                x, y, a = at_of(g)
                nm = find(g, "name")
                nb = find(g, "number")
                pins.append(LibPin(str(nb[1]) if nb else "", str(nm[1]) if nm else "", str(g[1]) if len(g) > 1 else "",
                                   x, y, a, fnum(val(g, "length"), 0.0), is_yes(g, "hide"), unit, style))
            elif g[0] in ("rectangle", "polyline", "circle", "arc", "bezier"):
                pts = _shape_points(g)
                if pts:
                    shapes.append((unit, style, pts))

    take(node, 0, 0)
    for sub in findall(node, "symbol"):
        sn = str(sub[1])
        m = re.match(r"^(.*)_(\d+)_(\d+)$", sn)
        unit, style = (int(m.group(2)), int(m.group(3))) if m else (0, 0)
        take(sub, unit, style)
    return LibSym(name, power, pins, shapes, ext, pin_names_hidden)


# ---------------------------------------------------------------- 変換
def make_transform(rot, mirror):
    """ライブラリ座標 (Y上) → 回路図の相対座標 (Y下) の 2x2 行列 (a,b,c,d): X=a*x+b*y, Y=c*x+d*y"""
    a, b, c, d = 1, 0, 0, -1  # Y 反転
    r = int(round(rot)) % 360
    # 画面上で反時計回りに回転 (Y下座標): (X,Y) -> (X cos + Y sin, -X sin + Y cos)
    cs = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}.get(r)
    if cs is None:
        cs = (math.cos(math.radians(r)), math.sin(math.radians(r)))
    co, si = cs
    a, b, c, d = (co * a + si * c, co * b + si * d, -si * a + co * c, -si * b + co * d)
    if mirror == "x":  # X 軸に対して反転 (上下反転)
        c, d = -c, -d
    elif mirror == "y":  # Y 軸に対して反転 (左右反転)
        a, b = -a, -b
    return (a, b, c, d)


def apply_t(t, x, y, ox, oy):
    a, b, c, d = t
    return (ox + a * x + b * y, oy + c * x + d * y)


ANG_VEC = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}


@dataclass
class Pin:
    sym: "SymInst"
    number: str
    name: str
    etype: str
    pt: tuple  # 接続点 (回路図座標)
    end: tuple  # 本体側の端
    hidden: bool
    dir: tuple  # 接続点から本体へ向かう単位ベクトル (回路図座標)


@dataclass
class SymInst:
    lib_id: str
    lib: LibSym | None
    x: float
    y: float
    rot: float
    mirror: str | None
    unit: int
    style: int
    props: dict  # name -> dict(value, x, y, angle, size, hidden, justify, face)
    uuid: str
    instances: list  # (path, reference, unit)
    in_bom: bool
    on_board: bool
    dnp: bool
    pins: list = field(default_factory=list)
    body: tuple | None = None  # 本体の外接矩形 (回路図座標)
    pinbox: tuple | None = None

    @property
    def ref(self):
        return self.props.get("Reference", {}).get("value", "?")

    @property
    def value(self):
        return self.props.get("Value", {}).get("value", "")

    @property
    def is_power(self):
        return bool(self.lib and self.lib.power) or self.ref.startswith("#PWR") or self.ref.startswith("#FLG")

    @property
    def is_flag(self):
        return self.ref.startswith("#FLG") or "PWR_FLAG" in self.lib_id

    def ref_for(self, path):
        for p, r, u in self.instances:
            if p == path:
                return r
        return self.ref


def build_symbol(node, libs):
    lib_id = str(val(node, "lib_id", ""))
    lib_name = val(node, "lib_name")
    lib = libs.get(str(lib_name)) if lib_name else None
    if lib is None:
        lib = libs.get(lib_id)
    x, y, rot = at_of(node)
    mirror = val(node, "mirror")
    unit = int(fnum(val(node, "unit", 1), 1))
    style = int(fnum(val(node, "body_style", val(node, "convert", 1)), 1))
    props = {}
    for p in findall(node, "property"):
        px, py, pa = at_of(p)
        te = text_effects(p)
        props[str(p[1])] = dict(value=str(p[2]) if len(p) > 2 else "", x=px, y=py, angle=pa, **te)
    insts = []
    ins = find(node, "instances")
    if ins is not None:
        for proj in findall(ins, "project"):
            for path in findall(proj, "path"):
                insts.append((str(path[1]), str(val(path, "reference", "")), int(fnum(val(path, "unit", unit), unit))))
    s = SymInst(lib_id, lib, x, y, rot, str(mirror) if mirror else None, unit, style, props,
                str(val(node, "uuid", "")), insts, val(node, "in_bom", "yes") == "yes",
                val(node, "on_board", "yes") == "yes", val(node, "dnp", "no") == "yes")
    if lib is not None:
        t = make_transform(rot, s.mirror)
        for lp in lib.pins:
            if lp.unit not in (0, unit) or lp.style not in (0, style):
                continue
            pt = apply_t(t, lp.x, lp.y, x, y)
            vx, vy = ANG_VEC.get(int(round(lp.angle)) % 360, (1, 0))
            end = apply_t(t, lp.x + vx * lp.length, lp.y + vy * lp.length, x, y)
            dx, dy = end[0] - pt[0], end[1] - pt[1]
            L = math.hypot(dx, dy)
            if L > 1e-9:
                dv = (dx / L, dy / L)
            else:
                dv = (t[0] * vx + t[1] * vy, t[2] * vx + t[3] * vy)
            s.pins.append(Pin(s, lp.number, lp.name, lp.etype, pt, end, lp.hidden, dv))
        xs, ys = [], []
        for u, st, pts in lib.shapes:
            if u not in (0, unit) or st not in (0, style):
                continue
            for px, py in pts:
                X, Y = apply_t(t, px, py, x, y)
                xs.append(X)
                ys.append(Y)
        if xs:
            s.body = (min(xs), min(ys), max(xs), max(ys))
        allx = xs + [p.pt[0] for p in s.pins] + [p.end[0] for p in s.pins]
        ally = ys + [p.pt[1] for p in s.pins] + [p.end[1] for p in s.pins]
        if allx:
            s.pinbox = (min(allx), min(ally), max(allx), max(ally))
    return s


# ---------------------------------------------------------------- シート
_KI_ESC = {"{slash}": "/", "{backslash}": "\\", "{lt}": "<", "{gt}": ">", "{colon}": ":", "{dblquote}": '"',
           "{quote}": "'", "{brace}": "{", "{space}": " ", "{tab}": "\t", "{return}": "\n", "{bar}": "|"}


def net_name(t):
    """KiCad のエスケープ ({slash} 等) を戻したネット名"""
    if "{" not in t:
        return t
    for k, v in _KI_ESC.items():
        t = t.replace(k, v)
    return t


@dataclass
class Label:
    kind: str  # label / global_label / hierarchical_label / netclass_flag / directive_label
    text: str
    x: float
    y: float
    angle: float
    shape: str | None
    size: float
    face: str | None
    uuid: str

    @property
    def pt(self):
        return (self.x, self.y)


@dataclass
class SheetPin:
    name: str
    shape: str
    x: float
    y: float
    angle: float

    @property
    def pt(self):
        return (self.x, self.y)


@dataclass
class SheetSym:
    name: str
    file: str
    x: float
    y: float
    w: float
    h: float
    uuid: str
    pins: list
    props: dict


@dataclass
class Sheet:
    path: str  # ファイルパス
    root: list
    version: int
    paper: tuple
    title_block: dict
    libs: dict
    symbols: list
    wires: list  # ((x1,y1),(x2,y2))
    buses: list
    bus_entries: list  # ((x1,y1),(x2,y2))
    junctions: list
    no_connects: list
    labels: list
    texts: list  # dict(kind, text, x, y, angle, size, face, justify, bbox)
    rects: list  # (x1,y1,x2,y2)
    polylines: list
    sheets: list
    uuid: str
    bus_aliases: dict = field(default_factory=dict)


def load_sheet(path):
    with open(path, encoding="utf-8") as f:
        root = parse_sexpr(f.read())
    if head(root) != "kicad_sch":
        raise ValueError(f"{path}: kicad_sch ではありません")
    libs = {}
    ls = find(root, "lib_symbols")
    if ls is not None:
        for n in findall(ls, "symbol"):
            L = parse_lib_symbol(n)
            libs[L.name] = L
        for L in list(libs.values()):  # extends の解決
            if L.extends and not L.pins:
                base = libs.get(L.extends) or next((v for k, v in libs.items() if k.split(":")[-1] == L.extends), None)
                if base:
                    L.pins, L.shapes = base.pins, base.shapes
    tb = {}
    t = find(root, "title_block")
    if t is not None:
        for c in t[1:]:
            if isinstance(c, list):
                if c[0] == "comment":
                    tb[f"comment{c[1]}"] = str(c[2]) if len(c) > 2 else ""
                else:
                    tb[str(c[0])] = str(c[1]) if len(c) > 1 else ""
    sh = Sheet(path, root, int(fnum(val(root, "version", 0))), paper_of(root), tb, libs, [], [], [], [], [], [], [], [], [], [], [],
               str(val(root, "uuid", "")))
    for n in root[1:]:
        if not isinstance(n, list):
            continue
        h = n[0]
        if h == "symbol":
            sh.symbols.append(build_symbol(n, libs))
        elif h in ("wire", "bus"):
            pts = [(fnum(p[1]), fnum(p[2])) for p in findall(find(n, "pts") or [], "xy")]
            for a, b in zip(pts, pts[1:]):
                (sh.wires if h == "wire" else sh.buses).append((a, b))
        elif h == "bus_entry":
            x, y, _ = at_of(n)
            s = find(n, "size")
            sh.bus_entries.append(((x, y), (x + fnum(s[1]), y + fnum(s[2]))))
        elif h == "junction":
            x, y, _ = at_of(n)
            sh.junctions.append((x, y))
        elif h == "no_connect":
            x, y, _ = at_of(n)
            sh.no_connects.append((x, y))
        elif h in ("label", "global_label", "hierarchical_label", "netclass_flag", "directive_label"):
            x, y, a = at_of(n)
            te = text_effects(n)
            sh.labels.append(Label(h, str(n[1]) if len(n) > 1 and not isinstance(n[1], list) else "", x, y, a,
                                   val(n, "shape"), te["size"], te["face"], str(val(n, "uuid", ""))))
        elif h in ("text", "text_box"):
            x, y, a = at_of(n)
            te = text_effects(n)
            d = dict(kind=h, text=str(n[1]), x=x, y=y, angle=a, **te)
            if h == "text_box":
                s = find(n, "size")
                if s is None:  # KiCad 8+: start/end の場合もある
                    st, en = find(n, "start"), find(n, "end")
                    d["bbox"] = (fnum(st[1]), fnum(st[2]), fnum(en[1]), fnum(en[2])) if st and en else (x, y, x, y)
                else:
                    d["bbox"] = (x, y, x + fnum(s[1]), y + fnum(s[2]))
            else:
                d["bbox"] = text_bbox(d["text"], x, y, a, te["size"], te["justify"])
            sh.texts.append(d)
        elif h == "rectangle":
            s, e = find(n, "start"), find(n, "end")
            if s and e:
                sh.rects.append((min(fnum(s[1]), fnum(e[1])), min(fnum(s[2]), fnum(e[2])),
                                 max(fnum(s[1]), fnum(e[1])), max(fnum(s[2]), fnum(e[2]))))
        elif h == "polyline":
            pts = [(fnum(p[1]), fnum(p[2])) for p in findall(find(n, "pts") or [], "xy")]
            sh.polylines.append(pts)
        elif h == "bus_alias":
            m = find(n, "members")
            sh.bus_aliases[str(n[1])] = [str(x) for x in (m[1:] if m else [])]
        elif h == "sheet":
            x, y, _ = at_of(n)
            s = find(n, "size")
            props = {str(p[1]): str(p[2]) for p in findall(n, "property") if len(p) > 2}
            name = props.get("Sheetname", props.get("Sheet name", ""))
            fn = props.get("Sheetfile", props.get("Sheet file", ""))
            pins = []
            for p in findall(n, "pin"):
                px, py, pa = at_of(p)
                pins.append(SheetPin(str(p[1]), str(p[2]) if len(p) > 2 and not isinstance(p[2], list) else "", px, py, pa))
            sh.sheets.append(SheetSym(name, fn, x, y, fnum(s[1]) if s else 0, fnum(s[2]) if s else 0,
                                      str(val(n, "uuid", "")), pins, props))
    return sh


def load_hierarchy(top):
    """トップから階層をたどり、[(sheet_obj, instance_path, sheet_name_path)] を返す。ファイルは 1 回だけ読む"""
    cache = {}
    out = []

    def get(p):
        p = os.path.abspath(p)
        if p not in cache:
            cache[p] = load_sheet(p)
        return cache[p]

    def walk(p, ipath, npath, depth):
        sh = get(p)
        out.append((sh, ipath, npath))
        if depth > 20:
            return
        for s in sh.sheets:
            child = os.path.join(os.path.dirname(p), s.file)
            if os.path.exists(child):
                walk(child, ipath + "/" + s.uuid, npath + s.name + "/", depth + 1)

    t = get(top)
    walk(top, "/" + t.uuid, "/", 0)
    return out, cache


# ---------------------------------------------------------------- 幾何
def on_segment_interior(p, a, b):
    """整数座標 p が線分 a-b の内部 (端点を除く) にあるか"""
    (px, py), (ax, ay), (bx, by) = p, a, b
    if (px, py) == (ax, ay) or (px, py) == (bx, by):
        return False
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > max(abs(bx - ax), abs(by - ay)) * 2:  # 許容 ~2e-4mm
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


def seg_rect_intersect_len(a, b, r):
    """線分 a-b が矩形 r の内部を通る長さ (Liang-Barsky)"""
    x1, y1 = a
    x2, y2 = b
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - r[0]), (dx, r[2] - x1), (-dy, y1 - r[1]), (dy, r[3] - y1)):
        if abs(p) < 1e-12:
            if q <= 0:
                return 0.0
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return 0.0
                t0 = max(t0, t)
            else:
                if t < t0:
                    return 0.0
                t1 = min(t1, t)
    if t1 <= t0:
        return 0.0
    return (t1 - t0) * math.hypot(dx, dy)


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        p = self.p
        p.setdefault(x, x)
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


class Connectivity:
    """1 枚のシート内の幾何接続。

    KiCad の規則 (KiCad 10.0.7 で実測して確認した。tests/README 参照):
      * ワイヤ端点どうし、ワイヤ端点とピン・ラベル・シートピン・バスエントリが同じ点 → 接続
      * ワイヤ端点が別ワイヤの途中に乗る (T 字) → 接続点 (junction) が無いと接続しない
      * ラベルはワイヤの途中に置いても接続
      * ピンがワイヤの途中に乗るだけ → 接続しない (接続点を置けば接続)
      * 交差するだけのワイヤ → 接続しない (接続点を置けば接続)
    """

    def __init__(self, sh: Sheet, pin_on_wire_interior_connects=False):
        self.sh = sh
        uf = UF()
        self.uf = uf
        W = [(qp(a), qp(b)) for a, b in sh.wires]
        self.W = W
        # 点 → 項目
        ends = {}  # 点 -> [wire idx]
        for i, (a, b) in enumerate(W):
            uf.find(("w", i))
            ends.setdefault(a, []).append(i)
            ends.setdefault(b, []).append(i)
        self.ends = ends
        # 空間索引 (水平/垂直ワイヤ用の簡易グリッド)
        self._cell = qi(25.4)
        grid = {}
        for i, (a, b) in enumerate(W):
            cx0, cx1 = sorted((a[0] // self._cell, b[0] // self._cell))
            cy0, cy1 = sorted((a[1] // self._cell, b[1] // self._cell))
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    grid.setdefault((cx, cy), []).append(i)
        self._grid = grid
        for p, ws in ends.items():
            for w in ws[1:]:
                uf.union(("w", ws[0]), ("w", w))
        # T 字: 端点が別ワイヤの途中に乗るだけでは接続しない (KiCad 10.0.7 実測)。接続点があれば接続
        self.t_points = {}  # 点 -> [途中を通るワイヤ idx]
        for p in ends:
            mids = self.wires_through(p)
            if mids:
                self.t_points[p] = mids
        # 接続点
        self.junction_pts = [qp(j) for j in sh.junctions]
        for j in self.junction_pts:
            uf.find(("j", j))
            for w in ends.get(j, []) + self.wires_through(j):
                uf.union(("j", j), ("w", w))
        jset = set(self.junction_pts)
        # ピン
        self.pin_pts = {}  # 点 -> [(sym idx, pin)]
        for si, s in enumerate(sh.symbols):
            for pin in s.pins:
                p = qp(pin.pt)
                self.pin_pts.setdefault(p, []).append((si, pin))
        self.pin_mid_wire = []  # (pin, wire idx) 途中に乗るだけで未接続
        for p, lst in self.pin_pts.items():
            for si, pin in lst:
                key = ("p", si, pin.number, id(pin))
                uf.find(key)
                for w in ends.get(p, []):
                    uf.union(key, ("w", w))
                mids = self.wires_through(p)
                if mids:
                    if p in jset or pin_on_wire_interior_connects:
                        for w in mids:
                            uf.union(key, ("w", w))
                    elif not ends.get(p):
                        self.pin_mid_wire.append((pin, mids[0]))
                if p in jset:
                    uf.union(key, ("j", p))
            # ピンどうし
            keys = [("p", si, pin.number, id(pin)) for si, pin in lst]
            for k in keys[1:]:
                uf.union(keys[0], k)
        # ラベル
        self.label_keys = []
        for li, L in enumerate(sh.labels):
            p = qp(L.pt)
            key = ("l", li)
            uf.find(key)
            for w in ends.get(p, []) + self.wires_through(p):
                uf.union(key, ("w", w))
            for si, pin in self.pin_pts.get(p, []):
                uf.union(key, ("p", si, pin.number, id(pin)))
            if p in jset:
                uf.union(key, ("j", p))
        # シートピン
        for sj, S in enumerate(sh.sheets):
            for pi, sp in enumerate(S.pins):
                p = qp(sp.pt)
                key = ("s", sj, pi)
                uf.find(key)
                for w in ends.get(p, []) + self.wires_through(p):
                    uf.union(key, ("w", w))
                for si, pin in self.pin_pts.get(p, []):
                    uf.union(key, ("p", si, pin.number, id(pin)))
        # バスエントリ: ワイヤ側の端だけを見る (どちらがワイヤ側かは両方試す)
        self.bus_entry_pts = set()
        for a, b in sh.bus_entries:
            self.bus_entry_pts.add(qp(a))
            self.bus_entry_pts.add(qp(b))
        self.nc_pts = set(qp(p) for p in sh.no_connects)
        # バス: 端点どうし・接続点で連結し、乗っているバス名 (ラベル/シートピン) を集める
        B = [(qp(a), qp(b)) for a, b in sh.buses]
        self.B = B
        bu = UF()
        bends = {}
        for i, (a, b) in enumerate(B):
            bu.find(i)
            bends.setdefault(a, []).append(i)
            bends.setdefault(b, []).append(i)
        for p, bs in bends.items():
            for j in bs[1:]:
                bu.union(bs[0], j)

        def bus_at(p, interior=True):
            out = list(bends.get(p, []))
            if interior:
                out += [i for i, (a, b) in enumerate(B) if on_segment_interior(p, a, b)]
            return out

        for j in jset:
            bs = bus_at(j)
            for k in bs[1:]:
                bu.union(bs[0], k)
        self.bus_names = {}  # バス成分 root -> [(kind, name)]
        if B:
            for li, L in enumerate(sh.labels):
                bs = bus_at(qp(L.pt))
                if bs:
                    self.bus_names.setdefault(bu.find(bs[0]), []).append((L.kind, net_name(L.text), li))
            for sj, S in enumerate(sh.sheets):
                for pi, sp in enumerate(S.pins):
                    bs = bus_at(qp(sp.pt), interior=False)
                    if bs:
                        self.bus_names.setdefault(bu.find(bs[0]), []).append(("sheet_pin", net_name(sp.name), (sj, pi)))

    def wires_through(self, p):
        """点 p を途中 (端点以外) に含むワイヤ"""
        c = (p[0] // self._cell, p[1] // self._cell)
        out = []
        for i in self._grid.get(c, ()):
            a, b = self.W[i]
            if on_segment_interior(p, a, b):
                out.append(i)
        return out

    def pin_key(self, si, pin):
        return ("p", si, pin.number, id(pin))


# ---------------------------------------------------------------- 階層全体の接続 (ネットリスト照合用)
_BUS_VEC = re.compile(r"^(.*?)\[(\d+)\.\.(\d+)\]$")


def _split_group(name):
    """'PRE{A, B C}' -> ('PRE', ['A','B','C'])。~{..} _{..} ^{..} は書式なのでグループではない"""
    if not name.endswith("}"):
        return None
    depth = 0
    for i in range(len(name) - 1, -1, -1):
        ch = name[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                if i > 0 and name[i - 1] in "~_^":
                    return None
                inner = name[i + 1:-1]
                parts, cur, d = [], "", 0
                for c in inner:
                    if c == "{":
                        d += 1
                    elif c == "}":
                        d -= 1
                    if d == 0 and (c in ", \t"):
                        if cur:
                            parts.append(cur)
                        cur = ""
                    else:
                        cur += c
                if cur:
                    parts.append(cur)
                return name[:i], parts
    return None


def bus_members(name, aliases=None, _depth=0):
    """バス名を (メンバーの対応キー, ネット名) の列に展開する。
    A[0..3] -> (0,A0)...  /  P{X Y[0..1]} -> (X,P.X) (Y0,P.Y0)...  /  エイリアス名はメンバーに展開"""
    aliases = aliases or {}
    if _depth > 8:
        return []
    m = _BUS_VEC.match(name)
    if m:
        a, b = int(m.group(2)), int(m.group(3))
        step = 1 if b >= a else -1
        # ベクタバスどうしは並び順で対応する (IO[8..15] と IO[0..7] が同じバスなら IO8↔IO0。vme-wren デモで確認)。
        # グループ内のメンバーは名前で対応
        return [(("#", k) if _depth == 0 else f"{m.group(1)}{i}", f"{m.group(1)}{i}") for k, i in enumerate(range(a, b + step, step))]

    def expand(part):
        if part in aliases or _BUS_VEC.match(part) or _split_group(part):
            return bus_members(part, aliases, _depth + 1)
        return [(part, part)]

    g = _split_group(name)
    if g:
        pre, parts = g
        out = []
        for part in parts:
            for short, full in expand(part):
                out.append((short, f"{pre}.{full}" if pre else full))
        return out
    if name in aliases:
        out = []
        for part in aliases[name]:
            out += expand(part)
        return out
    return []


def is_ground_name(n):
    return bool(re.search(r"(?i)(^|[^A-Z])(GND|VSS|EARTH|CHASSIS|0V)", n)) or n.upper() in ("PE", "GNDREF")


def global_partition(top, conn_cache=None):
    """階層全体のピン (ref, pin番号) を接続グループに分ける。
    戻り値: (pad_root: dict[(ref,pin)] -> root, notes: list[str])"""
    hier, files = load_hierarchy(top)
    conn_cache = conn_cache if conn_cache is not None else {}
    uf = UF()
    notes = []
    aliases = {}
    for sh in files.values():
        aliases.update(sh.bus_aliases)
    for sh, ip, npath in hier:
        if sh.path not in conn_cache:
            conn_cache[sh.path] = Connectivity(sh)
        c = conn_cache[sh.path]
        cu = c.uf
        # シート内の全項目を (ip, root) に集約
        def node(k):
            return (ip, cu.find(k))
        for i in range(len(sh.wires)):
            uf.find(node(("w", i)))
        on_bus_labels = set(x[2] for v in c.bus_names.values() for x in v if x[0] != "sheet_pin")
        on_bus_pins = set(x[2] for v in c.bus_names.values() for x in v if x[0] == "sheet_pin")
        for li, L in enumerate(sh.labels):
            n = node(("l", li))
            t = net_name(L.text)
            if L.kind == "label":
                uf.union(n, (ip, "L", t))
            elif L.kind == "global_label":
                uf.union(n, ("G", t))
            elif L.kind == "hierarchical_label":
                uf.union(n, (ip, "H", t))
                uf.union(n, (ip, "L", t))  # 同じシートの同名ローカルラベルとも接続する (KiCad 10.0.7 実測)
                if li not in on_bus_labels:
                    for short, full in bus_members(t, aliases):
                        uf.union((ip, "L", full), (ip, "HB", t, short))
        for si, s in enumerate(sh.symbols):
            ref = s.ref_for(ip)
            for pin in s.pins:
                n = node(c.pin_key(si, pin))
                if s.is_power and not (pin.hidden and pin.etype == "power_in" and not (s.lib and s.lib.power)):
                    if s.is_flag:
                        continue  # PWR_FLAG はネット名を持たない
                    if s.lib and s.lib.power == "local":
                        uf.union(n, (ip, "L", s.value))
                    elif s.lib and s.lib.power:
                        uf.union(n, ("G", s.value))
                    continue
                if pin.hidden and pin.etype == "power_in":
                    uf.union(n, ("G", pin.name))  # 非表示の電源ピンは名前で暗黙接続 (旧ライブラリ)
                if not ref.startswith("#"):
                    uf.union(n, ("PAD", ref, pin.number))
        for sj, S in enumerate(sh.sheets):
            cip = ip + "/" + S.uuid
            for pi, sp in enumerate(S.pins):
                n = node(("s", sj, pi))
                t = net_name(sp.name)
                uf.union(n, (cip, "H", t))
                if (sj, pi) not in on_bus_pins:
                    for short, full in bus_members(t, aliases):
                        uf.union((ip, "L", full), (cip, "HB", t, short))
        # 同じシートにある同名のグローバルラベル/電源記号とはローカルラベルも接続する (KiCad 10.0.7 実測。別シートとは接続しない)
        gnames = set(net_name(L.text) for L in sh.labels if L.kind == "global_label")
        gnames |= set(s.value for s in sh.symbols if s.is_power and not s.is_flag and s.lib and s.lib.power == "global")
        gnames |= set(p.name for s in sh.symbols for p in s.pins if p.hidden and p.etype == "power_in" and not (s.lib and s.lib.power))
        for L in sh.labels:
            if L.kind == "label" and net_name(L.text) in gnames:
                uf.union((ip, "L", net_name(L.text)), ("G", net_name(L.text)))
        # バス: 成分ごとにドライバ名 (グローバル > ローカル > 階層ラベル > シートピン) を選び、
        # 他の名前のメンバーを対応キー (ベクタは並び順、グループは名前) でドライバのメンバーにつなぐ
        prio = {"global_label": 0, "label": 1, "hierarchical_label": 2, "sheet_pin": 3}
        for root, names in c.bus_names.items():
            cand = [x for x in names if x[0] in prio and bus_members(x[1], aliases)]
            if not cand:
                continue
            dk, dt, _ = min(cand, key=lambda x: (prio[x[0]], x[1]))
            dmem = dict(bus_members(dt, aliases))

            def key_for(full):
                return ("G", full) if dk == "global_label" else (ip, "L", full)
            for kind, t, extra in cand:
                for short, full in bus_members(t, aliases):
                    tgt = dmem.get(short)
                    if tgt is None:
                        continue
                    T = key_for(tgt)
                    if kind == "label":
                        uf.union((ip, "L", full), T)
                    elif kind == "global_label":
                        uf.union(("G", full), T)
                    elif kind == "hierarchical_label":
                        uf.union((ip, "HB", t, short), T)
                    elif kind == "sheet_pin":
                        sj, pi = extra
                        uf.union((ip + "/" + sh.sheets[sj].uuid, "HB", t, short), T)
    pads = {}
    for k in list(uf.p):
        if isinstance(k, tuple) and k and k[0] == "PAD":
            pads[(k[1], k[2])] = uf.find(k)
    return pads, notes


def parse_netlist(path):
    """kicad-cli sch export netlist (kicadsexpr) を読む → (nets: {name: set((ref,pin))}, comps: {ref: dict})"""
    r = parse_sexpr(open(path, encoding="utf-8").read())
    nets = {}
    for n in findall(find(r, "nets") or [], "net"):
        name = str(val(n, "name", ""))
        nets[name] = set((str(val(x, "ref")), str(val(x, "pin"))) for x in findall(n, "node"))
    comps = {}
    for c in findall(find(r, "components") or [], "comp"):
        ref = str(val(c, "ref"))
        ls = find(c, "libsource")
        comps[ref] = dict(value=str(val(c, "value", "")), footprint=str(val(c, "footprint", "")),
                          lib=f"{val(ls, 'lib', '')}:{val(ls, 'part', '')}" if ls else "")
    return nets, comps


def compare_partition(pads, nets):
    """schlint の接続モデルとネットリストを照合。戻り値: (一致ネット数, 不一致リスト)"""
    bad = []
    ok = 0
    root_of_net = {}
    for name, nodes in nets.items():
        roots = set(pads.get(p) for p in nodes)
        if None in roots:
            bad.append((name, "ピンがモデルに無い", sorted(p for p in nodes if p not in pads)[:5]))
            continue
        if len(roots) != 1:
            bad.append((name, "モデルでは分断", sorted(nodes)[:8]))
            continue
        root_of_net.setdefault(next(iter(roots)), []).append(name)
        ok += 1
    for r, names in root_of_net.items():
        if len(names) > 1:
            bad.append((",".join(names[:4]), "モデルでは結合", []))
    return ok, bad
