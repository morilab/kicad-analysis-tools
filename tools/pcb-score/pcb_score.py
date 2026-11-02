#!/usr/bin/env python3
"""pcb_score — .kicad_pcb の配置・配線の「詰まり具合」を数値にする (配置案の比較用)。

  python3 pcb_score.py board.kicad_pcb [more.kicad_pcb ...] [--json out.json] [--res 0.05]

出力 (1 基板ごと):
  board_area / board_bbox        外形 (Edge.Cuts) の面積・外接矩形
  courtyard_sum / union          footprint の courtyard 面積の和 / 和集合 (表裏別)
  density                        (courtyard_sum + 予約領域) / board_area (表裏合計なので両面実装では 100% を超えうる)
  comp_bbox, margin              全 courtyard の外接矩形と、外形までの余白 (上下左右)、comp_bbox_ratio = 外接矩形 / 外形
  track_len (per layer), tracks, vias
  empty_ratio                    外形のうち courtyard (表裏) にも track/via (全層) にも掛からない面積の割合 (= ベタだけの面積)
  front_empty_ratio              表面だけで見た空き (F.CrtYd + F.Cu track + via)
  hpwl / mst                     ネットごとの pad 座標の半周長 / 最小全域木 (ユークリッド) の合計。配線前の配置比較に使う
                                 signal = 電源・GND (名前で判定) と pad 1 個のネットを除いたもの
  detour                         signal ネットの配線長 / MST (配線済みのとき)
  far_empty_ratio                courtyard/track/via から 2 mm 以上離れた空きの割合 (空きが固まっている = 間延び)
  max_empty_radius_mm            最大の空き円の半径 (Chebyshev 近似)
  area_at_target                 courtyard 和 / 目標密度 (既定 40%) = 縮められる面積の目安
  grades                         下書きのしきい値による pass / warn / fail と 0-100 の合成点 (rules: 本ファイル THRESHOLDS)
  profile                        --profile 指定時: 比較スコア S (上限なし、高いほど良い。score_profiles.py)。
                                 基準は --baseline (.kicad_pcb か pcb_score の JSON)、無ければ先頭の基板。
                                 python3 pcb_score.py a.kicad_pcb b.kicad_pcb --profile cost --baseline cur.kicad_pcb [--review-toml review.toml]
pcbnew (SWIG) は使わず S 式を直接読む (kpcb.py)。"""
import argparse, json, math, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kpcb

POWER_RE = re.compile(r'(^|/)(GND|AGND|DGND|PGND|GNDA|GNDD|VSS|VBUS|VCC|VDD|VIN|VBAT|VSYS|\+?\d+V\d*|\+\d+V|V\d+V\d+|.*_?3V3.*|.*1V2.*|.*1V8.*|.*5V.*|AVDD.*|DVDD.*|VCCIO.*|VDDIO.*)$', re.I)

# 下書きのしきい値 (2026-10-09、4 基板の実測から。rules/pcb-layout-review.md と同期すること)
THRESHOLDS = {
    # name: (good_if, pass, warn)   good_if='high' なら >= pass で pass、>= warn で warn、それ未満 fail
    'density': ('high', 0.40, 0.30),
    'empty_ratio': ('low', 0.45, 0.60),
    'far_empty_ratio': ('low', 0.10, 0.25),
    'max_margin_mm': ('low', 3.0, 6.0),
    'detour': ('low', 1.6, 2.2),
    'vias_per_signal_net': ('low', 1.5, 3.0),
}
WEIGHTS = {'density': 3, 'empty_ratio': 2, 'far_empty_ratio': 2, 'max_margin_mm': 1, 'detour': 1, 'vias_per_signal_net': 1}
FAR_MM = 2.0          # far_empty: courtyard/track/via から FAR_MM 以上離れた空き
TARGET_DENSITY = 0.40  # area_at_target: courtyard 和 / この密度 = 目標外形面積の目安


def mst_len(pts):
    n = len(pts)
    if n < 2:
        return 0.0
    seen = [False] * n; d = [math.inf] * n; d[0] = 0; tot = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not seen[i]), key=lambda i: d[i])
        seen[u] = True; tot += d[u]
        for v in range(n):
            if not seen[v]:
                dd = math.dist(pts[u], pts[v])
                if dd < d[v]:
                    d[v] = dd
    return tot


def far_empty(used, board_m, res, far_mm, cell=0.25):
    """used (bool 配列) から FAR_MM 以上離れた基板内セルの割合と、最大の空き円の半径 (Chebyshev 近似、cell 刻み)。
    基板の外は「空き」扱い (= 縁の余白も空きとして数える)。"""
    import numpy as np
    k = max(1, int(round(cell / res)))
    H, W = (used.shape[0] // k) * k, (used.shape[1] // k) * k
    u = used[:H, :W].reshape(H // k, k, W // k, k).any(axis=(1, 3))
    bm = board_m[:H, :W].reshape(H // k, k, W // k, k).mean(axis=(1, 3)) > 0.5
    e = ~u
    n_board = bm.sum()
    steps = int(round(far_mm / cell)); frac = 0.0; r = 0
    while e[bm].any():
        p = np.pad(e, 1, constant_values=True)
        e = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:] & p[:-2, :-2] & p[:-2, 2:] & p[2:, :-2] & p[2:, 2:]
        r += 1
        if r == steps:
            frac = float((e & bm).sum()) / n_board
        if r > 400:
            break
    return frac, round(r * cell, 2)


def grade(name, v):
    if v is None or name not in THRESHOLDS:
        return None, None
    good, p, w = THRESHOLDS[name]
    if good == 'high':
        g = 'pass' if v >= p else 'warn' if v >= w else 'fail'
        sc = max(0.0, min(1.0, (v - (2 * w - p)) / (p - (2 * w - p))))
    else:
        g = 'pass' if v <= p else 'warn' if v <= w else 'fail'
        sc = max(0.0, min(1.0, ((2 * w - p) - v) / ((2 * w - p) - p)))
    return g, round(sc * 100)


def score(path, res=0.05, reserve=()):
    b = kpcb.Board(path)
    outer, holes, open_n = b.outline()
    R = {'board': b.name, 'file': b.path, 'copper_layers': len(b.copper), 'footprints': len(b.footprints)}
    if outer is None:
        R['error'] = 'Edge.Cuts の閉じた外形が無い'
        return R
    board_area = kpcb.poly_area(outer) - sum(kpcb.poly_area(h) for h in holes)
    bb = kpcb.bbox(outer)
    R.update(board_bbox=[round(v, 3) for v in bb], board_w=round(bb[2] - bb[0], 2), board_h=round(bb[3] - bb[1], 2),
             board_area=round(board_area, 1), outline_open_chains=open_n, cutouts=len(holes))
    # courtyard
    ras = kpcb.Raster(bb, res)
    bim, bd = ras.new(); ras.poly(bd, outer)
    for h in holes:
        ras.poly(bd, h, 0)
    board_m = ras.arr(bim)
    side_m = {}
    cy_sum = {'F': 0.0, 'B': 0.0}
    comp_bb = None; no_cy = []
    for f in b.footprints:
        polys = f.courtyard()
        if not polys:
            cb = f.courtyard_bbox()
            if cb is None:
                no_cy.append(f.ref); continue
            polys = [[(cb[0], cb[1]), (cb[2], cb[1]), (cb[2], cb[3]), (cb[0], cb[3])]]
        if f.side not in side_m:
            side_m[f.side] = ras.new()
        im, d = side_m[f.side]
        for p in polys:
            ras.poly(d, p)
            cy_sum[f.side] += kpcb.poly_area(p)
            comp_bb = kpcb.bbox_union(comp_bb, kpcb.bbox(p))
    cyF = ras.arr(side_m['F'][0]) if 'F' in side_m else board_m & False
    cyB = ras.arr(side_m['B'][0]) if 'B' in side_m else board_m & False
    # tracks / vias
    tim, td = ras.new(); fim, fd = ras.new()
    per_layer = {}
    for t in b.tracks + b.arcs:
        L = kpcb.arc_length(t['pts'])
        per_layer[t['layer']] = per_layer.get(t['layer'], 0.0) + L
        ras.line(td, t['pts'], t['w'])
        if t['layer'] == 'F.Cu':
            ras.line(fd, t['pts'], t['w'])
    for v in b.vias:
        ras.circle(td, v['at'], v['size'] / 2); ras.circle(fd, v['at'], v['size'] / 2)
    tr = ras.arr(tim); trF = ras.arr(fim)
    # 機械的に場所を取るもの (例: 基板に重ねて載る OLED module の外形を F.SilkS / User 層に描いたもの) を占有として数える
    rim, rd = ras.new(); n_res = 0
    for L in reserve:
        items = b.gr.get(L, [])
        loops, _ = kpcb.chain_loops([pa for p, paths, w in items if not p for pa in paths])
        for p in [p for p, paths, w in items if p] + loops:
            ras.poly(rd, p); n_res += 1
    rs = ras.arr(rim) & board_m
    reserved_area = ras.area(rs & ~(cyF | cyB))
    cyF = cyF | rs
    used = (cyF | cyB | tr) & board_m
    usedF = (cyF | trF) & board_m
    union_area = ras.area((cyF | cyB) & board_m)
    R.update(courtyard_sum=round(cy_sum['F'] + cy_sum['B'], 1), courtyard_sum_F=round(cy_sum['F'], 1), courtyard_sum_B=round(cy_sum['B'], 1),
             courtyard_union=round(union_area, 1), footprints_without_courtyard=no_cy,
             reserved_shapes=n_res, reserved_area=round(reserved_area, 1),
             density=round((cy_sum['F'] + cy_sum['B'] + reserved_area) / board_area, 3),
             union_density=round(union_area / board_area, 3))
    if comp_bb:
        R['comp_bbox'] = [round(v, 2) for v in comp_bb]
        m = {'left': comp_bb[0] - bb[0], 'top': comp_bb[1] - bb[1], 'right': bb[2] - comp_bb[2], 'bottom': bb[3] - comp_bb[3]}
        R['margin'] = {k: round(v, 2) for k, v in m.items()}
        R['max_margin_mm'] = round(max(m.values()), 2)
        R['comp_bbox_ratio'] = round((comp_bb[2] - comp_bb[0]) * (comp_bb[3] - comp_bb[1]) / board_area, 3)
    tot_len = sum(per_layer.values())
    R.update(track_len_total=round(tot_len, 1), track_len_per_layer={k: round(v, 1) for k, v in sorted(per_layer.items())},
             tracks=len(b.tracks) + len(b.arcs), vias=len(b.vias),
             track_area_ratio=round(ras.area(tr & board_m) / board_area, 3),
             empty_ratio=round(1 - ras.area(used) / board_area, 3),
             front_empty_ratio=round(1 - ras.area(usedF) / board_area, 3))
    fe, rmax = far_empty(used, board_m, res, FAR_MM)
    R.update(far_empty_ratio=round(fe, 3), max_empty_radius_mm=rmax,
             area_at_target=round((cy_sum['F'] + cy_sum['B'] + reserved_area) / TARGET_DENSITY, 0),
             area_reduction_at_target=round(1 - (cy_sum['F'] + cy_sum['B'] + reserved_area) / TARGET_DENSITY / board_area, 3))
    # nets: HPWL / MST
    nets = b.nets()
    zone_nets = {z['net'] for z in b.zones if z['net'] and not z['keepout']}
    hp_all = mst_all = hp_sig = mst_sig = 0.0
    sig_nets = []
    for name, pads in nets.items():
        if name.startswith('unconnected-') or len(pads) < 2:
            continue
        pts = [(p.x, p.y) for p in pads]
        x0, y0, x1, y1 = kpcb.bbox(pts)
        hp = (x1 - x0) + (y1 - y0); ml = mst_len(pts)
        hp_all += hp; mst_all += ml
        if not POWER_RE.search(name) and name not in zone_nets:
            hp_sig += hp; mst_sig += ml; sig_nets.append(name)
    sig = set(sig_nets)
    sig_len = sum(kpcb.arc_length(t['pts']) for t in b.tracks + b.arcs if t['net'] in sig)
    sig_vias = sum(1 for v in b.vias if v['net'] in sig)
    R.update(nets=len([n for n in nets if not n.startswith('unconnected-')]), signal_nets=len(sig_nets),
             hpwl_all=round(hp_all, 1), mst_all=round(mst_all, 1), hpwl_signal=round(hp_sig, 1), mst_signal=round(mst_sig, 1),
             signal_track_len=round(sig_len, 1), signal_vias=sig_vias,
             detour=round(sig_len / mst_sig, 2) if sig_len > 0 and mst_sig > 0 else None,
             vias_per_signal_net=round(sig_vias / len(sig_nets), 2) if sig_nets and b.tracks else None,
             zone_nets=sorted(zone_nets))
    # grades
    G = {}; scs = []
    for k in THRESHOLDS:
        g, sc = grade(k, R.get(k))
        if g:
            G[k] = {'value': R.get(k), 'grade': g, 'score': sc}; scs.append((sc, WEIGHTS.get(k, 1)))
    R['grades'] = G
    R['score'] = round(sum(a * w for a, w in scs) / sum(w for a, w in scs)) if scs else None
    return R


def text_report(R):
    if 'error' in R:
        return f"{R['board']}: ERROR {R['error']}"
    L = [f"== {R['board']}  ({R['copper_layers']} 層, footprint {R['footprints']})",
         f"  外形 {R['board_w']} x {R['board_h']} mm, 面積 {R['board_area']} mm2" + (f", 切り欠き {R['cutouts']}" if R['cutouts'] else ''),
         f"  courtyard 和 {R['courtyard_sum']} mm2 (F {R['courtyard_sum_F']} / B {R['courtyard_sum_B']}), 和集合 {R['courtyard_union']} mm2"
         + (f", 予約領域 {R['reserved_area']} mm2 ({R['reserved_shapes']} 個)" if R['reserved_shapes'] else ''),
         f"  密度 {R['density']*100:.1f}% (和集合 {R['union_density']*100:.1f}%)   部品外接矩形/外形 {R.get('comp_bbox_ratio', 0)*100:.1f}%   余白 {R.get('margin')}",
         f"  配線 {R['track_len_total']} mm {R['track_len_per_layer']}  track {R['tracks']} 本, via {R['vias']}",
         f"  空き率 (courtyard/track/via 以外) {R['empty_ratio']*100:.1f}%  (表面のみ {R['front_empty_ratio']*100:.1f}%)  "
         f"{FAR_MM}mm 以上離れた空き {R['far_empty_ratio']*100:.1f}%  最大空き円 半径 {R['max_empty_radius_mm']} mm",
         f"  密度 {TARGET_DENSITY*100:.0f}% にしたときの面積 {R['area_at_target']:.0f} mm2 (現状比 {-R['area_reduction_at_target']*100:+.0f}%)",
         f"  HPWL 全 {R['hpwl_all']} / signal {R['hpwl_signal']} mm   MST 全 {R['mst_all']} / signal {R['mst_signal']} mm   "
         f"signal 配線長 {R['signal_track_len']} mm (detour {R['detour']}), signal via {R['signal_vias']} ({R['vias_per_signal_net']}/net)"]
    if R['footprints_without_courtyard']:
        L.append(f"  courtyard 無し: {R['footprints_without_courtyard']}")
    L.append('  判定: ' + ', '.join(f"{k}={v['value']}:{v['grade']}" for k, v in R['grades'].items()) + f"  → 合成点 {R['score']}/100 (下書き)")
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pcb', nargs='+')
    ap.add_argument('--json', help='JSON の出力先 (省略時は標準出力にテキストのみ)')
    ap.add_argument('--res', type=float, default=0.05, help='面積ラスタの解像度 mm (既定 0.05)')
    ap.add_argument('--reserve', action='append', default=[], metavar='LAYER',
                    help='この層の閉じた図形を占有領域として数える (例: F.SilkS に描いた載せ物 module の外形、User.1)。複数可')
    ap.add_argument('--table', action='store_true', help='最後に比較表 (Markdown) を出す')
    ap.add_argument('--profile', help='比較スコア S のプロファイル (default/size/signal/thermal/cost か review.toml [score.profiles.*])')
    ap.add_argument('--baseline', help='S の基準 (.kicad_pcb か pcb_score の JSON)。省略時は先頭の基板')
    ap.add_argument('--review-toml', help='プロジェクトの review.toml ([score] で係数を上書き)')
    a = ap.parse_args()
    res = [score(p, a.res, a.reserve) for p in a.pcb]
    if a.profile:
        import score_profiles as SP
        P = SP.load(); w = SP.weights(a.profile, SP.project_config(a.review_toml), P)
        if a.baseline and a.baseline.endswith('.json'):
            base = json.load(open(a.baseline)); base = base[0] if isinstance(base, list) else base
        else:
            base = score(a.baseline, a.res, a.reserve) if a.baseline else res[0]
        ranked = SP.rank({R['board'] + f'#{i}': R for i, R in enumerate(res) if 'error' not in R}, base, w, P)
        for i, R in enumerate(res):
            if 'error' not in R:
                R['profile'] = dict(name=a.profile, baseline=base.get('board'), **ranked[R['board'] + f'#{i}'])
        print(f'\n比較スコア (profile {a.profile}、基準 {base.get("board")}、上限なし・高いほど良い)')
        print(SP.table(ranked, P))
    for R in res:
        print(text_report(R))
    if a.table or len(res) > 1:
        print('\n| board | 層 | 外形 mm | 面積 mm2 | 密度 | 最大余白 mm | 配線長 mm | via | 空き率 | 2mm空き | HPWL sig | detour | 点 |')
        print('|---|---|---|---|---|---|---|---|---|---|---|---|---|')
        for R in res:
            if 'error' in R:
                continue
            print(f"| {R['board']} | {R['copper_layers']} | {R['board_w']}x{R['board_h']} | {R['board_area']:.0f} | {R['density']*100:.1f}% | "
                  f"{R.get('max_margin_mm')} | {R['track_len_total']:.0f} | {R['vias']} | "
                  f"{R['empty_ratio']*100:.1f}% | {R['far_empty_ratio']*100:.1f}% | {R['hpwl_signal']:.0f} | {R['detour']} | {R['score']} |")
    if a.json:
        json.dump(res if len(res) > 1 else res[0], open(a.json, 'w'), indent=1, ensure_ascii=False)


if __name__ == '__main__':
    main()
