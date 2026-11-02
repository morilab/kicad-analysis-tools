#!/usr/bin/env python3
"""return_path — リターンパスの点数 (P20 GND ベタの切れ目またぎ、P21 層を変える via の GND via)。2026-10-14 作者 指示で点数制。

  python3 return_path.py <project_dir> [--out DIR]

1. 信号特性表 (signal_table.py、data/signal/) からネットごとの実効立ち上がり時間 tr_eff (直列抵抗で鈍る分を含む) を取る。
2. 立ち上がりの長さ Lr = tr_eff × v (v = 150 mm/ns、FR-4 マイクロストリップ)。
3. 区間ごとの「不連続の長さ」D と点数:
   - P20 (切れ目またぎ): 信号配線を 0.1 mm 刻みで歩き、参照層 (2 層 = 反対面、4 層の外層 = 隣の内層、内層 = 上下どちらか) の GND の
     filled_polygon が真下に無い区間 (0.3 mm 以上。pad の上と自分の pad / via から 0.8 mm 以内は除く) を 1 区間とする。
     スリット (両端が GND) なら、区間の手前と先の GND を結ぶ帰路の最短距離 (GND の上と GND via / GND のスルーホールで層を渡ってよい、
     0.25 mm 格子の 8 近傍の距離) = 迂回の長さ。上限 40 mm (橋なし)。GND の無い領域 (片側が GND の外) は迂回 = またぐ長さ。
     D = max(またぐ長さ, 迂回の長さ)。
     参照層 (2026-10-14 改訂): 隣の層に GND ベタがあればそれ。無ければ隣の電源ベタ (+3V3 など) を参照とみなす (仮定: 電源ベタは
     デカップリング C で GND と AC 的に結ばれ、帰路になる。表の「参照」列に電源名を出す)。隣にベタが無い配線は「評価不能」として
     本数と長さを数える (点数は付けない)。
   - P21 (層を変える信号 via): via につながる配線の層 (前後の層) と、その層の参照ベタ (via の周り) を見る。
     前後の参照が同じ層・同じネット (例: F.Cu ↔ In2.Cu がどちらも In1.Cu の GND 参照) → 帰路は同じ面を流れるので GND via 不要 (0 点)。
     参照の層が違い、ネットが同じ (GND ↔ GND) → 最寄りのそのネットの via / スルーホール (縫い via) までの距離 d。
     参照のネットが違う (GND ↔ +3V3 など) → 両方のネットにつながるコンデンサ (ステッチング C) までの距離 d。
     前後の層が分からない via (片側だけの配線) は従来どおり最寄りの GND via。D = 2d (行きと帰り)。
   - 点数 = 100 × D / (Lr / 10) = 1000 × D / Lr。100 点 = 不連続が立ち上がりの長さの 1/10。
4. 判定 (2026-10-14 作者確定（運用で見直しあり）、SKILL.md §2.6 に根拠): 区間の最大点 ≥100 fail、≥30 warn、それ未満 pass。
出力: <out>/return_path_<層>.png (ヒートマップ: 区間の点数を色の濃さ、上位に番号)、return_path.md / .json (表)。
"""
import argparse, json, math, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import is_power, is_gnd, KB
sys.path.insert(0, os.path.join(KB, 'tools', 'pcb-score'))
import kpcb
import signal_table as st

RES = 0.05          # 検出の格子 (mm)
CRES = 0.25         # 迂回の最短距離の格子 (mm)
STEP = 0.1
MIN_RUN = 0.3
FANOUT = 0.8
DETOUR_CAP = 40.0   # 迂回の探索の上限 (mm)。超えたら「橋なし」
TH_FAIL = 100       # 2026-10-14 作者確定（運用で見直しあり）: D ≥ Lr/10
TH_WARN = 30        # 2026-10-14 作者確定（運用で見直しあり）: D ≥ Lr/33
TOP_N = 25          # 番号を付ける上位の数 (warn 以上は全部、足りなければ上位から)


def filled_gnd(b):
    out = {}
    for z in kpcb.kids(b.root, 'zone'):
        net = str(kpcb.val(z, 'net_name', default='') or kpcb.val(z, 'net', default='') or '')
        if not is_gnd(net):
            continue
        for fp_ in kpcb.kids(z, 'filled_polygon'):
            L = str(kpcb.val(fp_, 'layer', default=''))
            pts = kpcb.kid(fp_, 'pts')
            poly = [(float(c[1]), float(c[2])) for c in (pts or ['pts'])[1:] if isinstance(c, list) and c[0] == 'xy']
            if len(poly) >= 3:
                out.setdefault(L, []).append(poly)
    return out


def filled_planes(b):
    """GND と電源のベタの filled_polygon -> {層: {ネット: [多角形]}}"""
    out = {}
    for z in kpcb.kids(b.root, 'zone'):
        net = str(kpcb.val(z, 'net_name', default='') or kpcb.val(z, 'net', default='') or '')
        if not (is_gnd(net) or is_power(net)):
            continue
        for fp_ in kpcb.kids(z, 'filled_polygon'):
            L = str(kpcb.val(fp_, 'layer', default=''))
            pts = kpcb.kid(fp_, 'pts')
            poly = [(float(c[1]), float(c[2])) for c in (pts or ['pts'])[1:] if isinstance(c, list) and c[0] == 'xy']
            if len(poly) >= 3:
                out.setdefault(L, {}).setdefault(net, []).append(poly)
    return out


def ref_layers(L, copper):
    if L not in copper:
        return []
    i = copper.index(L)
    if len(copper) == 2:
        return [copper[1 - i]]
    return [copper[j] for j in (i - 1, i + 1) if 0 <= j < len(copper)]


def score_of(D, tr_eff, v):
    return 1000.0 * D / (tr_eff * v)


def grade_of(s):
    return 'fail' if s >= TH_FAIL else 'warn' if s >= TH_WARN else 'pass'


class Geo:
    """GND の帰路の最短距離 (層をまたぐのは GND via / GND スルーホールだけ)"""
    def __init__(s, ras, layers, x0, y0, links):
        import numpy as np
        k = int(round(CRES / RES))
        s.layers = [L for L in layers if L in ras]
        arrs = []
        for L in s.layers:
            a = ras[L]; H, W = a.shape[0] // k * k, a.shape[1] // k * k
            arrs.append(a[:H, :W].reshape(H // k, k, W // k, k).mean(axis=(1, 3)) >= 0.5)
        s.g = np.stack(arrs) if arrs else None
        s.x0, s.y0 = x0, y0
        s.link = np.zeros(s.g.shape[1:], bool) if arrs else None
        for (x, y) in links:
            i, j = s.cell(x, y)
            if 0 <= j < s.link.shape[0] and 0 <= i < s.link.shape[1]:
                s.link[j, i] = True

    def cell(s, x, y):
        return int((x - s.x0) / CRES), int((y - s.y0) / CRES)

    def snap(s, x, y, li):
        import numpy as np
        i, j = s.cell(x, y)
        H, W = s.g.shape[1:]
        for r in range(0, 5):
            j0, j1, i0, i1 = max(0, j - r), min(H, j + r + 1), max(0, i - r), min(W, i + r + 1)
            sub = s.g[li, j0:j1, i0:i1]
            if sub.any():
                jj, ii = np.argwhere(sub)[0]
                return j0 + jj, i0 + ii
        return None

    def dist(s, a, b, refs):
        """a, b: (x, y)。refs: 参照層の名前 (始点・終点をその層の GND に置く)。-> mm (上限超えは None)"""
        import numpy as np
        lis = [s.layers.index(r) for r in refs if r in s.layers]
        if not lis or s.g is None:
            return None
        A = None; B = None
        for li in lis:
            A = A or ((li,) + s.snap(*a, li) if s.snap(*a, li) else None)
            B = B or ((li,) + s.snap(*b, li) if s.snap(*b, li) else None)
        if not A or not B:
            return None
        n = int(DETOUR_CAP / CRES) + 1
        H, W = s.g.shape[1:]
        cy, cx = (A[1] + B[1]) // 2, (A[2] + B[2]) // 2
        j0, j1, i0, i1 = max(0, cy - n), min(H, cy + n), max(0, cx - n), min(W, cx + n)
        g = s.g[:, j0:j1, i0:i1]; link = s.link[j0:j1, i0:i1]
        vis = np.zeros_like(g); fr = np.zeros_like(g)
        fr[A[0], A[1] - j0, A[2] - i0] = True; vis |= fr
        tb = (B[0], B[1] - j0, B[2] - i0)
        d = 0.0
        for it in range(int(DETOUR_CAP / CRES * 1.5)):
            if vis[tb]:
                return d
            nb = fr.copy()
            nb[:, 1:, :] |= fr[:, :-1, :]; nb[:, :-1, :] |= fr[:, 1:, :]
            nb[:, :, 1:] |= nb[:, :, :-1]; nb[:, :, :-1] |= nb[:, :, 1:]  # 8 近傍 (対角を含む)
            if it % 2:      # 4 近傍と 8 近傍を交互 = 八角形の波面 (距離の誤差 ≤8%)
                nb = fr.copy()
                nb[:, 1:, :] |= fr[:, :-1, :]; nb[:, :-1, :] |= fr[:, 1:, :]; nb[:, :, 1:] |= fr[:, :, :-1]; nb[:, :, :-1] |= fr[:, :, 1:]
            on_link = (nb | vis).any(axis=0) & link
            nb |= on_link[None, :, :]
            nb &= g & ~vis
            if not nb.any():
                return None
            vis |= nb; fr = nb
            d += CRES * (1.207 if it % 2 == 0 else 1.0) / 1.1   # 交互の平均で直線距離に合わせる
            if d > DETOUR_CAP:
                return None
        return None


def analyze(b, table, meta, cfg=None):
    from PIL import Image, ImageDraw
    import numpy as np
    cfg = cfg or {}
    v = float(st.V(meta['v_mm_per_ns']))
    outline, holes, _ = b.outline()
    pts_all = outline or [t['a'] for t in b.tracks]
    xs = [p[0] for p in pts_all]; ys = [p[1] for p in pts_all]
    x0, y0, x1, y1 = min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1
    W, H = int((x1 - x0) / RES) + 1, int((y1 - y0) / RES) + 1
    px = lambda x, y: ((x - x0) / RES, (y - y0) / RES)
    gnd = filled_gnd(b)
    planes = filled_planes(b)
    ras = {}
    rpw = {}        # 電源ベタ: {層: {ネット: raster}}
    for L in b.copper:
        im = Image.new('1', (W, H), 0); d = ImageDraw.Draw(im)
        for poly in gnd.get(L, []):
            d.polygon([px(*p) for p in poly], fill=1)
        ras[L] = np.array(im, dtype=bool)
        for net, polys in planes.get(L, {}).items():
            if is_gnd(net):
                continue
            im = Image.new('1', (W, H), 0); d = ImageDraw.Draw(im)
            for poly in polys:
                d.polygon([px(*p) for p in poly], fill=1)
            rpw.setdefault(L, {})[net] = np.array(im, dtype=bool)

    def on_r(r, x, y):
        i, j = int((x - x0) / RES), int((y - y0) / RES)
        return 0 <= j < H and 0 <= i < W and r[j, i]

    def on(L, x, y):
        return on_r(ras[L], x, y)

    def track_refs(L):
        """配線の層 L の参照: [(参照層, ネット, raster)]、種別 ('GND' / '電源' / None = 評価不能)"""
        adj = ref_layers(L, b.copper)
        g = [(x, 'GND', ras[x]) for x in adj if gnd.get(x)]
        if g:
            return g, 'GND'
        pw_ = [(x, n, r) for x in adj for n, r in sorted(rpw.get(x, {}).items())]
        return (pw_, '電源') if pw_ else ([], None)

    def plane_at(RL, x, y, rad):
        """参照層 RL で (x, y) の周り (via の穴抜きの外、半径 rad) にあるベタのネット (GND 優先、無ければ最多の電源)"""
        hits = {}
        for k in range(12):
            a = 2 * math.pi * k / 12
            q = (x + rad * math.cos(a), y + rad * math.sin(a))
            if on(RL, *q):
                hits['GND'] = hits.get('GND', 0) + 1
            for n, r in rpw.get(RL, {}).items():
                if on_r(r, *q):
                    hits[n] = hits.get(n, 0) + 1
        if not hits:
            return None
        if hits.get('GND', 0) >= 3:
            return 'GND'
        return max(hits, key=hits.get)

    gvias = [v_['at'] for v_ in b.vias if is_gnd(v_['net'])]
    gtht = [(p.x, p.y) for f in b.footprints for p in f.pads if is_gnd(p.net) and p.type == 'thru_hole']
    geo = Geo(ras, [L for L in b.copper if gnd.get(L)], x0, y0, gvias + gtht)
    pgeo = {}

    def geo_for(net):
        """電源ベタの迂回: そのネットのベタだけを歩き、そのネットの via / スルーホールで層を渡る"""
        if net == 'GND':
            return geo
        if net not in pgeo:
            r = {L: rpw[L][net] for L in rpw if net in rpw[L]}
            lk = [v_['at'] for v_ in b.vias if v_['net'] == net] + [(p.x, p.y) for f in b.footprints for p in f.pads if p.net == net and p.type == 'thru_hole']
            pgeo[net] = Geo(r, [L for L in b.copper if L in r], x0, y0, lk)
        return pgeo[net]
    caps = []       # ステッチング C (2 端子、別々のベタのネットにつながる): (ネットの組, 中心)
    for f in b.footprints:
        if re.match(r'^C\d', f.ref) and 'dnp' not in f.attr:
            ns = {p.net for p in f.pads if p.net}
            if len(ns) == 2 and all(is_gnd(x) or is_power(x) for x in ns):
                caps.append((frozenset('GND' if is_gnd(x) else x for x in ns), (f.x, f.y), f.ref))
    stats = {L: dict(tracks=0, len=0.0, gnd=0, power=0, none=0, none_len=0.0, refs=set()) for L in b.copper}
    sig = lambda n: n and not is_gnd(n) and not is_power(n)
    pad_polys = [(p.poly(), p) for f in b.footprints for p in f.pads]
    own = {}
    for f in b.footprints:
        for p in f.pads:
            own.setdefault(p.net, []).append((p.x, p.y))
    for v_ in b.vias:
        own.setdefault(v_['net'], []).append(tuple(v_['at']))
    ext = None

    def info(net):
        e = table.get(net)
        if e:
            return e
        return dict(driver='(表になし)', tr_ns={'value': 2.0, 'basis': 'assumed'}, tr_eff_ns=2.0, f_max_mhz=None, series_r=None,
                    source='表になし → 仮定 tr 2 ns (3.3 V CMOS)', certainty='仮')

    items = []; cache = {}
    for t in b.tracks + b.arcs:
        if not sig(t['net']) or t['layer'] not in b.copper:
            continue
        if re.search(st.NOT_SIGNAL, t['net'].split('/')[-1], re.I):
            continue
        refs, rkind = track_refs(t['layer'])
        S_ = stats[t['layer']]; tl = kpcb.arc_length(t['pts'])
        S_['tracks'] += 1; S_['len'] += tl
        if not refs:
            S_['none'] += 1; S_['none_len'] += tl
            continue
        S_['gnd' if rkind == 'GND' else 'power'] += 1
        Rs = sorted({x for x, _, _ in refs}, key=b.copper.index)
        rlabel = '/'.join(f'{x} {n}' for x, n in sorted({(x, n) for x, n, _ in refs}))
        S_['refs'].add(rlabel)
        e = info(t['net'])
        tre = float(e['tr_eff_ns'])
        pts = t['pts']
        for a, c in zip(pts, pts[1:]):
            L = math.hypot(c[0] - a[0], c[1] - a[1])
            n = max(1, int(L / STEP))
            run = []; before = [None]; prev = None; prev_on = True

            def flush(run, after):
                if not run:
                    return
                ln = (len(run) - 1) * L / n if n else 0
                if ln < MIN_RUN:
                    return
                slit = before[0] is not None and after is not None
                if slit:
                    key = (t['net'], round(before[0][0], 1), round(before[0][1], 1), round(after[0], 1), round(after[1], 1))
                    dt = cache.get(key, 'x')
                    rn = hit_net(*before[0]) or 'GND'; an = hit_net(*after) or rn
                    if dt == 'x':
                        if rn == an:
                            dt = geo_for(rn).dist(before[0], after, [x for x, n, _ in refs if n == rn])
                        else:   # 電源ベタの分割 (+3V3 → +1V2 など) をまたぐ: 帰路は 2 つのネットをつなぐ C を通る
                            cs = [math.hypot(c[0] - before[0][0], c[1] - before[0][1]) + math.hypot(c[0] - after[0], c[1] - after[1])
                                  for ns_, c, r_ in caps if ns_ == frozenset((rn, an))]
                            dt = min(cs) if cs and min(cs) <= DETOUR_CAP else None
                        cache[key] = dt
                    detour = dt if dt is not None else DETOUR_CAP
                    nobridge = dt is None
                    split = (rn, an) if rn != an else None
                else:
                    split = None
                if not slit:
                    detour = ln; nobridge = False
                D = max(ln, detour)
                s = score_of(D, tre, v)
                mid = run[len(run) // 2]
                items.append(dict(kind='cross', net=t['net'], layer=t['layer'], ref=rlabel, ref_kind=rkind, x=mid[0], y=mid[1], len=ln, detour=detour,
                                  nobridge=nobridge, slit=slit, split=split, D=D, score=s, pts=[run[0], run[-1]], w=t['w'], tr_eff=tre, e=e))
            hit_net = lambda x, y: next((n_ for x_, n_, r_ in refs if on_r(r_, x, y)), None)
            for k in range(n + 1):
                x, y = a[0] + (c[0] - a[0]) * k / n, a[1] + (c[1] - a[1]) * k / n
                inpad = any(kpcb.point_in_poly(x, y, pp) for pp, p in pad_polys if abs(p.x - x) < 3 and abs(p.y - y) < 3 and set(Rs) & set(p.copper_layers(b.copper)))
                near_own = any(abs(q[0] - x) < FANOUT and abs(q[1] - y) < FANOUT and math.hypot(q[0] - x, q[1] - y) < FANOUT for q in own.get(t['net'], ()))
                g = hit_net(x, y) is not None
                ok = g or inpad or near_own
                if not ok:
                    if not run:
                        before[0] = prev if (prev_on and k > 0 and prev_g) else None
                    run.append((x, y))
                else:
                    flush(run, (x, y) if g else None); run = []
                prev = (x, y); prev_on = ok; prev_g = g
            flush(run, None)
    cands = gvias + gtht
    seg_ends = {}
    for t in b.tracks + b.arcs:
        seg_ends.setdefault(t['net'], []).append((t['layer'], t['pts'][0], t['pts'][-1]))
    via_stats = dict(same_ref=0, stitch_via=0, stitch_cap=0, legacy=0, no_ref=0)

    def via_layers(v_):
        r = v_['size'] / 2 + 0.05; x, y = v_['at']
        Ls = {L for L, a, c in seg_ends.get(v_['net'], ()) if math.hypot(a[0] - x, a[1] - y) <= r or math.hypot(c[0] - x, c[1] - y) <= r}
        for f in b.footprints:
            for p in f.pads:
                if p.net == v_['net'] and abs(p.x - x) < 3 and abs(p.y - y) < 3 and kpcb.point_in_poly(x, y, p.poly()):
                    Ls |= set(p.copper_layers(b.copper))
        return sorted(Ls, key=b.copper.index)

    for v_ in b.vias:
        if not sig(v_['net']) or re.search(st.NOT_SIGNAL, v_['net'].split('/')[-1], re.I):
            continue
        e = info(v_['net']); tre = float(e['tr_eff_ns'])
        x, y = v_['at']
        Ls = via_layers(v_)
        rad = v_['size'] / 2 + 0.7
        refs_v = {}
        for L in Ls:
            rr = [(RL, plane_at(RL, x, y, rad)) for RL in ref_layers(L, b.copper)]
            rr = [(RL, n) for RL, n in rr if n]
            rr.sort(key=lambda q: q[1] != 'GND')    # GND 優先
            refs_v[L] = rr
        mode = 'legacy'
        if len(Ls) >= 2 and all(refs_v[L] for L in Ls):
            worst = None
            for i_, A in enumerate(Ls):
                for B in Ls[i_ + 1:]:
                    sa, sb = set(refs_v[A]), set(refs_v[B])
                    if sa & sb:
                        cand = (0.0, 'same', sorted(sa & sb)[0], None)
                    else:
                        na, nb = refs_v[A][0][1], refs_v[B][0][1]
                        if na == nb:
                            pts_ = cands if na == 'GND' else [w['at'] for w in b.vias if w['net'] == na] + \
                                [(p.x, p.y) for f in b.footprints for p in f.pads if p.net == na and p.type == 'thru_hole']
                            d_ = min((math.hypot(x - g[0], y - g[1]) for g in pts_), default=DETOUR_CAP)
                            cand = (d_, 'via', (refs_v[A][0], refs_v[B][0]), na)
                        else:
                            want = frozenset((na, nb))
                            cs = [(math.hypot(x - c[0], y - c[1]), r_) for ns_, c, r_ in caps if ns_ == want]
                            d_, cr = min(cs, default=(DETOUR_CAP, None))
                            cand = (d_, 'cap', (refs_v[A][0], refs_v[B][0]), cr)
                    if worst is None or cand[0] > worst[0] or (worst[1] == 'same' and cand[1] != 'same'):
                        worst = cand
            dmin, mode, info_, extra = worst
        elif len(Ls) >= 2:
            mode = 'no_ref'
        if mode in ('legacy', 'no_ref'):
            dmin = min((math.hypot(x - g[0], y - g[1]) for g in cands), default=DETOUR_CAP)
            info_ = extra = None
        via_stats[{'same': 'same_ref', 'via': 'stitch_via', 'cap': 'stitch_cap'}.get(mode, mode)] += 1
        D = 2 * dmin
        rl = ' ↔ '.join(f"{L}({'/'.join(f'{RL} {n}' for RL, n in refs_v.get(L, [])) or '参照なし'})" for L in Ls) or '層不明'
        items.append(dict(kind='via', net=v_['net'], layer='via', x=x, y=y, len=0.0, detour=dmin, D=D, ref=rl, mode=mode, via_info=info_, via_extra=extra,
                          score=score_of(D, tre, v), pts=[], tr_eff=tre, e=e, size=v_['size'], nobridge=False, slit=False))
    for it in items:
        it['grade'] = grade_of(it['score'])
        if it['kind'] == 'cross':
            what = 'スリット' if it['slit'] else 'ベタの無い領域'
            if it.get('split'):
                what = f"分割 ({it['split'][0]} → {it['split'][1]}、帰路は両ネットをつなぐ C 経由)"
            it['what'] = f"{it['ref']} のベタの{what}を {it['len']:.1f} mm またぐ" + (f"、迂回 {it['detour']:.1f} mm" + (' (橋なし、上限)' if it['nobridge'] else '') if it['slit'] else '')
            if it.get('split'):
                it['fix'] = f"分割の境界の真上に {it['split'][0]}–{it['split'][1]} のコンデンサ (ステッチング C) を置く / 分割をまたがない経路へ / 隣を GND ベタにする"
            elif it['slit']:
                it['fix'] = ('切れ目の両側を GND via でつなぐ (信号の真横に stitching via、迂回が 0 に近づく) / 切れ目を作っている反対面の配線を短く・別ルートへ'
                             + (' / 直列抵抗を入れて tr を鈍らせる' if not it['e'].get('series_r') else ''))
            else:
                it['fix'] = 'GND ベタを配線の下まで広げる (zone の範囲・keepout を見直す) / 信号を GND の上へ移す'
        else:
            m = it.get('mode')
            if m == 'same':
                RL, n = it['via_info']
                it['what'] = f"{it['ref']}: 前後の参照が同じ {RL} の {n} → 帰路は同じ面を流れ、GND via 不要"
                it['fix'] = '不要'
            elif m == 'via':
                (RA, na), (RB, nb) = it['via_info']
                it['what'] = f"{it['ref']}: 参照が {RA} → {RB} (どちらも {na}) に変わる。最寄りの {na} の縫い via まで {it['detour']:.1f} mm (帰路の寄り道 {it['D']:.1f} mm)"
                it['fix'] = f'この via の横 (≤1 mm) に {na} の via を 1 本足す / via を減らして同じ層で配線'
            elif m == 'cap':
                (RA, na), (RB, nb) = it['via_info']
                cr = it['via_extra']
                it['what'] = (f"{it['ref']}: 参照が {RA} {na} → {RB} {nb} (別ネット)。" +
                              (f"最寄りのステッチング C {cr} ({na}–{nb}) まで {it['detour']:.1f} mm (帰路の寄り道 {it['D']:.1f} mm)" if cr else
                               f"{na}–{nb} をつなぐ C が無い (上限 {DETOUR_CAP:.0f} mm)"))
                it['fix'] = f'この via の近く (≤2 mm) に {na}–{nb} のコンデンサ (ステッチング C) を置く / 参照が同じ層どうしで乗り換える / via を減らす'
            else:
                it['what'] = (('前後の層の参照ベタが無い (評価は従来の GND via 距離)。' if m == 'no_ref' else '') +
                              f"層を変える via から最寄りの GND via まで {it['detour']:.1f} mm (帰路の寄り道 {it['D']:.1f} mm)")
                it['fix'] = 'この via の横 (≤1 mm) に GND via を 1 本足す / via を減らして同じ層で配線'
    items.sort(key=lambda i: -i['score'])
    nwarn = sum(1 for i in items if i['score'] >= TH_WARN)
    for k, it in enumerate(items, 1):
        it['no'] = k if k <= max(TOP_N, nwarn) else None
    for L in stats:
        stats[L]['refs'] = sorted(stats[L]['refs'])
    return items, ras, dict(x0=x0, y0=y0, W=W, H=H, stats=stats, via_stats=via_stats, rpw=rpw)


def cmap(s):
    """点数 → 色 (0 = 薄い黄、30 = 橙、100 以上 = 濃い赤)"""
    t = max(0.0, min(1.0, s / 150.0))
    stops = [(0.0, (255, 236, 150)), (0.2, (255, 170, 40)), (0.667, (220, 20, 20)), (1.0, (110, 0, 40))]
    for (a, ca), (b_, cb) in zip(stops, stops[1:]):
        if t <= b_:
            u = (t - a) / (b_ - a)
            return tuple(int(ca[i] + (cb[i] - ca[i]) * u) for i in range(3))
    return stops[-1][1]


def draw(b, items, ras, geo, out):
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    x0, y0 = geo['x0'], geo['y0']
    sc = 20
    try:
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 13)
        sfont = ImageFont.truetype('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', 16)
    except Exception:
        font = sfont = ImageFont.load_default()
    paths = []
    for L in b.copper:
        W, H = int(geo['W'] * RES * sc), int(geo['H'] * RES * sc)
        RLs = [x for x in ref_layers(L, b.copper) if ras[x].any()]
        pw_ = []
        if not RLs:
            pw_ = [(x, n, r) for x in ref_layers(L, b.copper) for n, r in geo.get('rpw', {}).get(x, {}).items()]
        lab = '/'.join(f'{x} GND' for x in RLs) or '/'.join(f'{x} {n} (電源ベタを参照とみなす)' for x, n, _ in pw_) or '参照ベタなし = 評価不能'
        arrs = [ras[x] for x in RLs] + [r for _, _, r in pw_] or [ras[L] & False]
        g = Image.fromarray((np.logical_or.reduce(arrs) * 255).astype('uint8')).resize((W, H))
        base = Image.new('RGB', (W, H + 70), (255, 255, 255))
        base.paste((215, 215, 215), (0, 0, W, H), mask=g)
        d = ImageDraw.Draw(base)
        P = lambda x, y: ((x - x0) * sc, (y - y0) * sc)
        ol, holes, _ = b.outline()
        if ol:
            d.line([P(*p) for p in ol + [ol[0]]], fill=(0, 0, 0), width=2)
        for f in b.footprints:
            for p in f.pads:
                if L in p.copper_layers(b.copper):
                    d.polygon([P(*q) for q in p.poly()], fill=(160, 160, 160) if is_gnd(p.net) else (175, 200, 175))
        for t in b.tracks + b.arcs:
            if t['layer'] == L:
                d.line([P(*p) for p in t['pts']], fill=(150, 150, 150) if is_gnd(t['net']) or is_power(t['net']) else (120, 140, 200), width=max(1, int(t['w'] * sc)))
        for v in b.vias:
            r = v['size'] / 2 * sc; c = P(*v['at'])
            d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=(70, 70, 70) if is_gnd(v['net']) else (200, 200, 200), outline=(90, 90, 90))
        mine = sorted([i for i in items if (i['kind'] == 'cross' and i['layer'] == L) or i['kind'] == 'via'], key=lambda i: i['score'])
        for it in mine:      # 点数の低い順に塗る (濃いものが上)
            col = cmap(it['score'])
            if it['kind'] == 'cross':
                d.line([P(*p) for p in it['pts']], fill=col, width=max(5, int(it['w'] * sc) + 4))
            else:
                c = P(it['x'], it['y']); r = max(6, it['size'] / 2 * sc + 3)
                d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], outline=col, width=4)
        for it in mine:
            if not it['no']:
                continue
            c = P(it['x'], it['y']); r = 10; col = cmap(it['score'])
            ox, oy = c[0] + 15, c[1] - 15
            d.line([c, (ox, oy)], fill=(0, 0, 0), width=1)
            box = [ox - r, oy - r, ox + r, oy + r]
            (d.ellipse if it['kind'] == 'cross' else d.rectangle)(box, fill=col, outline=(0, 0, 0))
            s = str(it['no']); tw = d.textlength(s, font=font)
            d.text((ox - tw / 2, oy - 8), s, fill=(255, 255, 255) if it['score'] >= 40 else (0, 0, 0), font=font)
        # 凡例 (カラーバー)
        y = H + 8
        d.text((6, y), f'{b.name} {L} の配線 (灰 = 参照 {lab})。色 = 区間の点数 (1000×D/(tr×v))、● = またぎ、■ = 信号 via (番号 = 表)', fill=(0, 0, 0), font=sfont)
        for k in range(300):
            d.line([(10 + k, y + 30), (10 + k, y + 48)], fill=cmap(k * 150 / 300))
        for sv in (0, 30, 100, 150):
            xx = 10 + sv * 2
            d.line([(xx, y + 26), (xx, y + 52)], fill=(0, 0, 0))
            d.text((xx + 2, y + 50), f'{sv}{"+" if sv == 150 else ""}', fill=(0, 0, 0), font=font)
        d.text((330, y + 30), f'warn ≥{TH_WARN} / fail ≥{TH_FAIL} (確定 2026-10-14)', fill=(0, 0, 0), font=sfont)
        fn = os.path.join(out, f'return_path_{L.replace(".", "_")}.png')
        base.save(fn); paths.append(fn)
    return paths


def to_md(name, items, pngs, meta):
    nf = sum(1 for i in items if i['grade'] == 'fail'); nw = sum(1 for i in items if i['grade'] == 'warn')
    L = [f'## リターンパスの点数 ({name}、tools/review/return_path.py)', '',
         f"- 点数 = 1000 × D / (tr_eff × v)、v = {st.V(meta['v_mm_per_ns'])} mm/ns、直列抵抗の後ろの負荷 {st.V(meta['c_load_pf'])} pF。D = max(またぐ長さ, 迂回の長さ) (via は 2 × 最寄りの縫い via / ステッチング C 距離、前後の参照が同じなら 0)",
         f'- 判定 (2026-10-14 作者確定（運用で見直しあり）): ≥{TH_FAIL} fail / ≥{TH_WARN} warn。fail {nf} 区間、warn {nw} 区間、全 {len(items)} 区間 (またぎ {sum(1 for i in items if i["kind"] == "cross")}、信号 via {sum(1 for i in items if i["kind"] == "via")})', '']
    sts = meta.get('stats') or {}
    if sts:
        L += ['| 配線の層 | 信号配線 (本) | GND 参照 | 電源ベタ参照 (仮定) | 評価不能 (参照ベタなし) | 参照 |', '|---|---|---|---|---|---|']
        L += [f"| {k} | {v['tracks']} | {v['gnd']} | {v['power']} | {v['none']} ({v['none_len']:.0f} mm) | {'; '.join(v['refs']) or '-'} |" for k, v in sts.items()]
        L += ['', '- 電源ベタ参照: 隣に GND ベタが無い層は隣の電源ベタを帰路とみなす (デカップリング C で GND と AC 結合している仮定)。評価不能の配線は点数なし', '']
    vs = meta.get('via_stats')
    if vs:
        L += [f"- 信号 via: 前後の参照が同じ (GND via 不要) {vs['same_ref']} / 同ネット別層 (縫い via で評価) {vs['stitch_via']} / 別ネット (ステッチング C で評価) {vs['stitch_cap']} / 前後の層不明 (従来の GND via 距離) {vs['legacy']} / 参照なし {vs['no_ref']}", '']
    L += [f'![{os.path.basename(p)}]({os.path.basename(p)})' for p in pngs] + ['']
    L += ['| 番号 | 判定 | 点数 | ネット | 区分 | 層 | 参照 | 座標 (mm) | tr → tr_eff (ns) | tr の出典 (確度) | ドライバ / 直列抵抗 | またぐ長さ (mm) | 迂回の長さ (mm) | 内容 | 直し方の候補 |',
          '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for it in items:
        if not it['no']:
            continue
        e = it['e']; rs = e.get('series_r')
        L.append(f"| {it['no']} | {it['grade']} | {it['score']:.0f} | {it['net']} | {'またぎ' if it['kind'] == 'cross' else 'via'} | {it['layer']} | {it.get('ref', '')} | ({it['x']:.2f}, {it['y']:.2f}) | "
                 f"{st.V(e['tr_ns'])} → {e['tr_eff_ns']} | {e['source'][:160].replace('|', '/')} ({e['certainty']}) | {e['driver']}" + (f" / {rs['path']} ({rs['ohm']:g} Ω)" if rs else ' / なし') +
                 f" | {it['len']:.2f} | {it['detour']:.2f}{' (橋なし)' if it.get('nobridge') else ''} | {it['what']} | {it['fix']} |")
    rest = [i for i in items if not i['no']]
    if rest:
        L += ['', f'番号なし (下位) {len(rest)} 区間: 最大 {max(i["score"] for i in rest):.0f} 点。全区間は return_path.json']
    return '\n'.join(L) + '\n'


def run(b, nl, name, out, cfg=None):
    table, meta, _ = st.table_for(nl, name, b)
    items, ras, geo = analyze(b, table, meta, cfg)
    pngs = draw(b, items, ras, geo, out)
    stats = dict(layers=geo['stats'], vias=geo['via_stats'])
    open(os.path.join(out, 'return_path.md'), 'w').write(to_md(b.name, items, pngs, dict(meta, stats=geo['stats'], via_stats=geo['via_stats'])))
    json.dump(stats, open(os.path.join(out, 'return_path_stats.json'), 'w'), indent=1, ensure_ascii=False)
    json.dump([dict({k: v for k, v in i.items() if k not in ('pts', 'e')}, tr_src=i['e']['source'], certainty=i['e']['certainty'], driver=i['e']['driver'])
               for i in items], open(os.path.join(out, 'return_path.json'), 'w'), indent=1, ensure_ascii=False, default=str)
    import yaml as _y  # noqa (signal_table が読み込み済み)
    _y.safe_dump(table, open(os.path.join(out, 'signal_table.yaml'), 'w'), allow_unicode=True, sort_keys=False, width=200)
    return items, pngs, stats


def main():
    import glob
    from common import Netlist, export_netlist
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('project_dir'); ap.add_argument('--out', default='/tmp/return_path')
    a = ap.parse_args()
    pdir = os.path.abspath(a.project_dir)
    pro = glob.glob(os.path.join(pdir, '*.kicad_pro'))[0]
    name = os.path.splitext(os.path.basename(pro))[0]
    os.makedirs(a.out, exist_ok=True)
    x = os.path.join(a.out, 'netlist.xml')
    export_netlist(os.path.join(pdir, name + '.kicad_sch'), x)
    b = kpcb.Board(os.path.join(pdir, name + '.kicad_pcb'))
    items, pngs, stats = run(b, Netlist(x), name, a.out)
    for L, v in stats['layers'].items():
        print(f"{L}: 信号配線 {v['tracks']} 本 (GND 参照 {v['gnd']} / 電源ベタ参照 {v['power']} / 評価不能 {v['none']})")
    print('via:', stats['vias'])
    for i in items[:30]:
        print(i['no'], i['grade'], f"{i['score']:.0f}", i['net'], i['layer'], f"({i['x']:.2f},{i['y']:.2f})", i['what'], 'tr_eff', i['tr_eff'])
    print('\n'.join(pngs))


if __name__ == '__main__':
    sys.exit(main())
