"""raster — 銅箔の面積率 φ (セルごと 0..1) と外形マスクを作る。セルを sub×sub に細分して数える。"""
import math
import numpy as np
from matplotlib.path import Path


class Grid:
    def __init__(self, outline_polys, dx, sub=4, margin=0.0):
        pts = np.array([p for poly in outline_polys for p in poly])
        self.x0, self.y0 = pts.min(0) - margin
        x1, y1 = pts.max(0) + margin
        self.dx = dx; self.sub = sub
        self.nx = int(math.ceil((x1 - self.x0) / dx)); self.ny = int(math.ceil((y1 - self.y0) / dx))
        self.fs = dx / sub
        self.fx = self.x0 + (np.arange(self.nx * sub) + 0.5) * self.fs
        self.fy = self.y0 + (np.arange(self.ny * sub) + 0.5) * self.fs
        fine = np.zeros((self.ny * sub, self.nx * sub), bool)
        X, Y = np.meshgrid(self.fx, self.fy)
        P = np.c_[X.ravel(), Y.ravel()]
        for poly in outline_polys:   # 偶奇規則 (穴あき外形にも対応)
            fine ^= Path(poly).contains_points(P).reshape(fine.shape)
        self.fine_board = fine
        self.board = self.coarsen(fine)          # 基板のある割合
        self.xc = self.x0 + (np.arange(self.nx) + 0.5) * dx
        self.yc = self.y0 + (np.arange(self.ny) + 0.5) * dx

    def coarsen(self, fine):
        s = self.sub
        return fine.reshape(self.ny, s, self.nx, s).mean(axis=(1, 3))

    def window(self, xmin, ymin, xmax, ymax):
        i0 = max(0, int((xmin - self.x0) / self.fs)); i1 = min(len(self.fx), int((xmax - self.x0) / self.fs) + 2)
        j0 = max(0, int((ymin - self.y0) / self.fs)); j1 = min(len(self.fy), int((ymax - self.y0) / self.fs) + 2)
        X, Y = np.meshgrid(self.fx[i0:i1], self.fy[j0:j1])
        return (slice(j0, j1), slice(i0, i1)), X, Y

    def cell_of(self, x, y):
        return int((y - self.y0) / self.dx), int((x - self.x0) / self.dx)


def _capsule(X, Y, a, b, r):
    ax, ay = a; bx, by = b
    vx, vy = bx - ax, by - ay; L2 = vx * vx + vy * vy
    t = np.clip(((X - ax) * vx + (Y - ay) * vy) / L2, 0, 1) if L2 > 0 else 0
    return (X - ax - t * vx) ** 2 + (Y - ay - t * vy) ** 2 <= r * r


def paint_track(g, fine, a, b, w):
    r = w / 2
    sl, X, Y = g.window(min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r)
    fine[sl] |= _capsule(X, Y, a, b, r)


def pad_mask(g, pad):
    """pad の形 (rect/roundrect/oval/circle、他は rect) の細分マスク (sl, mask)"""
    R = math.hypot(pad['w'], pad['h']) / 2
    sl, X, Y = g.window(pad['x'] - R, pad['y'] - R, pad['x'] + R, pad['y'] + R)
    a = math.radians(pad['rot'])   # pad の (at) の角度は絶対角
    u = (X - pad['x']) * math.cos(a) - (Y - pad['y']) * math.sin(a)
    v = (X - pad['x']) * math.sin(a) + (Y - pad['y']) * math.cos(a)
    w, h = pad['w'] / 2, pad['h'] / 2
    if pad['shape'] == 'circle':
        m = u * u + v * v <= w * w
    elif pad['shape'] == 'oval':
        if w >= h:
            m = _capsule(u, v, (-(w - h), 0), (w - h, 0), h)
        else:
            m = _capsule(u, v, (0, -(h - w)), (0, h - w), w)
    else:
        m = (abs(u) <= w) & (abs(v) <= h)
    return sl, m


def fill_polygon(g, pts):
    """多角形 (偶奇規則) の細分マスク (sl, mask)。matplotlib Path.contains_points と同じ交差判定
    ((y0 > py) != (y1 > py) かつ px < 交点 x) を、行ごとのトグルの累積 XOR でまとめて計算する (大きなゾーンで 数十倍速い)。"""
    pts = np.asarray(pts, float)
    xmin, ymin = pts.min(0); xmax, ymax = pts.max(0)
    i0 = max(0, int((xmin - g.x0) / g.fs)); i1 = min(len(g.fx), int((xmax - g.x0) / g.fs) + 2)
    j0 = max(0, int((ymin - g.y0) / g.fs)); j1 = min(len(g.fy), int((ymax - g.y0) / g.fs) + 2)
    fx = g.fx[i0:i1]; fy = g.fy[j0:j1]
    a = pts; b = np.roll(pts, -1, axis=0)
    keep = a[:, 1] != b[:, 1]; a = a[keep]; b = b[keep]
    T = np.zeros((len(fy), len(fx) + 1), np.int32)
    ylo = np.minimum(a[:, 1], b[:, 1]); yhi = np.maximum(a[:, 1], b[:, 1])
    r0 = np.searchsorted(fy, ylo, 'right'); r1 = np.searchsorted(fy, yhi, 'right')   # 行 r: ylo < py <= yhi (agg の yflag = y >= py が両端で違う)
    n = r1 - r0; tot = int(n.sum())
    if tot:
        e = np.repeat(np.arange(len(a)), n); r = np.arange(tot) - np.repeat(np.cumsum(n) - n, n) + np.repeat(r0, n)
        py = fy[r]; x0, y0, x1, y1 = a[e, 0], a[e, 1], b[e, 0], b[e, 1]
        f1 = y1 >= py
        tog = lambda px: (((y1 - py) * (x0 - x1)) >= ((x1 - px) * (y0 - y1))) == f1   # agg point_in_path と同じ式。px の小さい側 (前置) がトグル
        xi = x0 + (py - y0) * (x1 - x0) / (y1 - y0)
        c = np.searchsorted(fx, xi, 'left'); N = len(fx)
        for _ in range(2):   # 交点ちょうどの丸めを厳密式で直す
            dn = (c > 0) & ~tog(fx[np.clip(c - 1, 0, N - 1)]); c = np.where(dn, c - 1, c)
            up = (c < N) & tog(fx[np.clip(c, 0, N - 1)]); c = np.where(up, c + 1, c)
        np.add.at(T, (r, np.zeros(tot, int)), 1); np.add.at(T, (r, c), -1)
    m = (np.cumsum(T[:, :-1], axis=1) & 1).astype(bool)
    return (slice(j0, j1), slice(i0, i1)), m


def pad_on(pad, layer):
    L = pad['layers']
    return layer in L or '*.Cu' in L or (layer.endswith('.Cu') and 'F&B.Cu' in L and layer in ('F.Cu', 'B.Cu'))


def copper_layer(g, board, layer, mode, netidx, return_fine=False, return_trk=False):
    """銅の面積率 φ (ny,nx) と 細分格子のネットラベル (ny·sub, nx·sub)。
    ラベル: 0 = 銅なし、1.. = netidx[net] + 1 (ネットの無い銅は 0 にせず -2 = 不明ネット)。
    mode: 'placement' = pad のみ、'artwork' = pad + track + zone + via。φ は ラベルに関係なく銅の有無だけで数える (熱と同じ)。"""
    fine = np.zeros_like(g.fine_board)
    lab = np.zeros(g.fine_board.shape, np.int32)
    trk = np.full(g.fine_board.shape, -1, np.int32) if return_trk else None   # 配線だけが塗った細分 = board['tracks'] の番号、他 -1

    def put(sl, m, net, ti=None):
        if trk is not None:
            if ti is None:
                trk[sl][m] = -1
            else:
                trk[sl][m & ~fine[sl]] = ti
        fine[sl] |= m
        lab[sl][m] = netidx[net] + 1 if net in netidx else -2
    for fp in board['footprints']:
        for p in fp['pads']:
            if p['type'] in ('np_thru_hole',) or not pad_on(p, layer):
                continue
            sl, m = pad_mask(g, p); put(sl, m, p.get('net', ''))
    if mode == 'artwork':
        for ti, t in enumerate(board['tracks']):
            if t['layer'] == layer:
                a, b, r = t['a'], t['b'], t['w'] / 2
                sl, X, Y = g.window(min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r)
                put(sl, _capsule(X, Y, a, b, r), t.get('net', ''), ti)
        for z in board['zones']:
            if z['layer'] != layer or len(z['pts']) < 3:
                continue
            sl, m = fill_polygon(g, z['pts'])
            put(sl, m, z.get('net', ''))
        for v in board['vias']:
            if layer in v['layers'] or (layer.endswith('.Cu') and v['layers'][:1] == ['F.Cu'] and v['layers'][-1:] == ['B.Cu']):
                r = v['size'] / 2; a = (v['x'], v['y'])
                sl, X, Y = g.window(a[0] - r, a[1] - r, a[0] + r, a[1] + r)
                put(sl, _capsule(X, Y, a, a, r), v.get('net', ''))
    fine &= g.fine_board
    lab[~fine] = 0
    if trk is not None:
        trk[~fine] = -1
        return g.coarsen(fine), lab, trk
    return g.coarsen(fine), lab


def copper_fraction(g, board, layer, mode):
    return copper_layer(g, board, layer, mode, {})[0]


def cell_nets(g, lab, n_nets):
    """セルごと: 多数決のネット (0 = 銅なし、同数は番号の小さい方)、セル内のネット数"""
    s = g.sub; L4 = lab.reshape(g.ny, s, g.nx, s).transpose(0, 2, 1, 3).reshape(g.ny * g.nx, s * s)
    cell, sub_ = np.nonzero(L4); k = L4[cell, sub_].astype(np.int64)
    best = np.zeros(g.ny * g.nx, np.int32); nn = np.zeros(g.ny * g.nx, np.int32)
    if len(k):
        off = k.min(); K = int(k.max() - off + 1)
        key, cnt = np.unique(cell * K + (k - off), return_counts=True)
        c = key // K; kk = key % K + off
        nn += np.bincount(c, minlength=len(nn)).astype(np.int32)
        o = np.lexsort((kk, -cnt, c)); first = np.r_[True, c[o][1:] != c[o][:-1]]
        best[c[o][first]] = kk[o][first]
    return best.reshape(g.ny, g.nx), nn.reshape(g.ny, g.nx)


def edges(g, lab):
    """同層の隣接セル間のエッジ: 境界を横切る細分の行 (列) のうち、両側が同じネットの割合 w (0..1) とそのネット。
    x: (ny, nx-1) = セル (j,i)-(j,i+1)、y: (ny-1, nx) = セル (j,i)-(j+1,i)。境界を 2 ネット以上が横切るとネット -1 (衝突)。"""
    s = g.sub; L4 = lab.reshape(g.ny, s, g.nx, s)
    out = {}
    for ax, (A, B) in (('x', (L4[:, :, :-1, s - 1], L4[:, :, 1:, 0])), ('y', (L4[:-1, s - 1, :, :], L4[1:, 0, :, :]))):
        same = (A == B) & (A != 0)
        red = 1 if ax == 'x' else 2
        w = same.sum(axis=red) / s
        hi = np.where(same, A, np.iinfo(np.int32).min).max(axis=red); lo = np.where(same, A, np.iinfo(np.int32).max).min(axis=red)
        net = np.where(w > 0, hi, 0).astype(np.int32); net[(w > 0) & (hi != lo)] = -1
        out[ax] = (w.astype(np.float32), net)
    return out


def _path_sub(g, a, ax):
    """セル中心 → 隣のセル中心 の経路の細分 (edge_conduct と同じ区間)。x: (ny, nx-1, s, m)、y: (ny-1, nx, s, m)。
    s = 流れに直交する細分の行、m = 流れ方向の列 (セル A の後ろ半分 + セル B の前半分)"""
    s = g.sub; a0, b1 = s // 2, (s + 1) // 2
    A4 = a.reshape(g.ny, s, g.nx, s)
    if ax == 'x':
        return np.concatenate([A4[:, :, :-1, a0:], A4[:, :, 1:, :b1]], axis=3).transpose(0, 2, 1, 3)
    return np.concatenate([A4[:-1, a0:, :, :], A4[1:, :b1, :, :]], axis=1).transpose(0, 2, 3, 1)


def _outer_sub(g, a, ax):
    """経路に入らない側の半分: セル A の前半分とセル B の後ろ半分 (形は _path_sub と同じ並び、列 = 各 s//2 列)"""
    s = g.sub; a0, b1 = s // 2, (s + 1) // 2
    A4 = a.reshape(g.ny, s, g.nx, s)
    if ax == 'x':
        return A4[:, :, :-1, :a0].transpose(0, 2, 1, 3), A4[:, :, 1:, b1:].transpose(0, 2, 1, 3)
    return A4[:-1, :a0, :, :].transpose(0, 2, 3, 1), A4[1:, b1:, :, :].transpose(0, 2, 3, 1)


def path_conduct(g, lab, k=None, trim=True):
    """経路コンダクタンスの共通部 (熱の edge_conduct と IR/PDN のネット別版で共有、2026-10-16)。
    k: None = 銅ならネットを問わない (熱。両側のセルに銅がある辺だけ)、{'x': (ny, nx-1) int, 'y': (ny-1, nx) int} = エッジごとに数えるネット (0 = 辺なし、IR/PDN)。
    経路 (細分 s 行 × m 列、セル A の後ろ半分 + セル B の前半分) の列 (流れに直交する帯) ごとに対象の銅の割合を数え、列方向に直列:
    f = m' / Σ(1/列の割合)。trim=True (既定) では両端の空の列を除く (m' = 銅のある列数): 銅がセルの中心まで届かない縁のセル
    (配線の縁が少しかかったセル、斜め配線の階段) が隣から切り離されないように。ただし空の端の向こう (そのセルの経路に入らない半分) に
    対象の銅があれば、空の列はセル内の隙間なので 0。銅の列の間に空の列がある (内部の隙間) 時も 0。
    trim=False は d495b2f の熱の式 (どこかの列に銅が無ければ 0)。戻り値 {'x': (ny, nx-1) float32, 'y': (ny-1, nx) float32}"""
    out = {}
    for ax in ('x', 'y'):
        if k is None:
            cu = lab != 0
            hasP, (OA, OB) = _path_sub(g, cu, ax), _outer_sub(g, cu, ax)
            c = cu.reshape(g.ny, g.sub, g.nx, g.sub).any(axis=(1, 3))      # 両側のセルに銅がある時だけ (IR の k > 0 と同じ。空のセルを端の除外で素通りさせない)
            ok = (c[:, :-1] & c[:, 1:]) if ax == 'x' else (c[:-1, :] & c[1:, :])
        else:
            kk = k[ax][:, :, None, None]; ok = k[ax] > 0
            hasP = _path_sub(g, lab, ax) == kk; OA, OB = (o == kk for o in _outer_sub(g, lab, ax))
        colf = hasP.mean(axis=2); m = colf.shape[-1]; has = colf > 0; nz = has.sum(axis=-1)
        if trim:
            first = np.argmax(has, axis=-1); last = m - 1 - np.argmax(has[..., ::-1], axis=-1)
            cont = (nz > 0) & (nz == last - first + 1)                         # 銅の列が途切れない
            cont &= ~((first > 0) & OA.any(axis=(2, 3)))                        # A 側の空の端の向こうに銅 → セル内の隙間
            cont &= ~((last < m - 1) & OB.any(axis=(2, 3)))                     # B 側も同じ
        else:
            cont = nz == m
        with np.errstate(divide='ignore', invalid='ignore'):
            f = np.where(ok & cont, nz / np.sum(np.where(has, 1.0 / np.maximum(colf, 1e-12), 0.0), axis=-1), 0.0)
        out[ax] = f.astype(np.float32)
    return out


def edge_conduct(g, lab, trim=True):
    """面内の熱の伝わりやすさ (銅の割合 f、0..1) を、セル中心 → 隣のセル中心 の経路で数える (2026-10-16、サーマルリリーフ対策)。
    path_conduct (k=None: 銅ならネットを問わない)。列 (流れに直交する細分の帯) ごとに銅の割合を数え、列方向に直列 (調和平均)。
    パッドとベタの隙間が経路を横切ると、その列の割合 = スポーク幅 / セル幅 で決まり、隙間がセルより細くてもベタ直結扱いにならない
    (旧方式はセルの面積率の調和平均で、隙間をまたぐセルの 0.5〜0.8 がそのまま効いた)。
    行ごとに直列 → 並列 (下限寄り) や両者の平均も試したが、807dc8e のサーマルパッドで 0.05 mm 格子の基準に最も近いのがこの形 (列ごと並列 → 直列)。
    trim (2026-10-16 夕、IR ドロップ担当の指摘): 両端の空の列を除く。d495b2f (trim なし) では銅がセル中心に届かない縁のセルが銅で
    つながらず (807dc8e 0.25 mm で 1 層あたり 700〜940 セル)、斜めの細い配線は 45° 0.1〜0.2 mm 幅で抵抗が 1.5〜9 倍に出ていた。
    x: (ny, nx-1) = セル (j,i)-(j,i+1)、y: (ny-1, nx) = セル (j,i)-(j+1,i)。"""
    return path_conduct(g, lab, None, trim)


def edge_tracks(g, lab, trk):
    """エッジごと: 横切る同ネットの細分がすべて 1 本の配線だけのものならその配線番号、他 -1"""
    s = g.sub; L4 = lab.reshape(g.ny, s, g.nx, s); T4 = trk.reshape(g.ny, s, g.nx, s); out = {}
    for ax, (A, B, TA, TB) in (('x', (L4[:, :, :-1, s - 1], L4[:, :, 1:, 0], T4[:, :, :-1, s - 1], T4[:, :, 1:, 0])),
                               ('y', (L4[:-1, s - 1, :, :], L4[1:, 0, :, :], T4[:-1, s - 1, :, :], T4[1:, 0, :, :]))):
        same = (A == B) & (A != 0); red = 1 if ax == 'x' else 2
        okp = same & (TA == TB) & (TA >= 0)
        hi = np.where(same, TA, np.iinfo(np.int32).min).max(axis=red); lo = np.where(same, np.where(okp, TA, -1), np.iinfo(np.int32).max).min(axis=red)
        out[ax] = np.where((hi == lo) & (lo >= 0), hi, -1).astype(np.int32)
    return out

