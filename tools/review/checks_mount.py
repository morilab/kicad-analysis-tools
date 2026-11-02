"""段階 3 (PCB) の実装性チェック: P18b・P26〜P33 (2026-10-16 作者 承認、しきい値はすべて仮)。

項目と根拠は notes/2026-10-16-mount-check-items.md (MC 番号との対応は各 Metric の source)。checks_pcb.run の最後で呼ぶ。
- P18b_via_near_pad      穴縁・ランド縁 → マスク開口 < 0.10 mm (テント切れの恐れ)。穴が開口にかかる via は P33 で数える
- P26_zone_connect_audit ゾーン接続の解決 (パッド > フットプリント > .kicad_dru > ゾーン) の監査 (info、判定は P28)
- P27_thermal_balance    手はんだ: ベタ側 / 信号側の 232 ℃ 到達時間比と、低い方のこて先での定常温度 (コネクタ + hand_solder)。
                         工程 hand (process / hand_solder) だけ fail まで、reflow (既定) はリワーク性として warn 止まり
- P28_pad_pour_balance   レビュー担当 P24_pad_pour_balance 案 (ベタ直結 vs 配線のみ、マスク定義辺) + チップ立ちの点 (R_th 比・Δt、サイズ別)
- P29_reflow_timing      多ピン部品 (P27 の対象) のパッド間の 217 ℃ 到達差 (RC 集中定数)
- P30_rework_clearance   P27 の対象の courtyard 間隙 (重なりは P06)
- P31_connector_land     メーカー推奨ランドとの差・位置決めピンの遊び (pcb-score/connectors.json の land)
- P32_paste_vs_coplanarity  体積換算のペースト厚 (ステンシル × gerber のペースト開口 / 銅) ≥ リードのコプラナリティ (connectors.json の coplanarity_mm)
ペースト量 (P32/P33) は gerber 実データ (mount_gerber.py)、マスク開口は .kicad_pcb のベクトル (開口用パッドを含む)。
- P33_solder_wicking     穴がマスク開口にかかる via の吸い込み比 (P18 はここで判定し二重に減点しない)
"""
import glob, json, math, os, subprocess, sys
from common import Metric, KB, HERE
sys.path.insert(0, os.path.join(KB, 'tools', 'pcb-score'))
import kpcb
import mount_geom as mg

S = 'pcb'
ORDER = ['pass', 'warn', 'fail']
def worse(a, b): return max(a, b, key=ORDER.index)

URL = dict(
    jlc_cap='https://jlcpcb.com/capabilities/pcb-capabilities',
    jlc_smd='https://jlcpcb.com/help/article/how-to-order-boards-with-solder-mask-defined-pads',
    jlc_tr='https://jlcpcb.com/blog/thermal-relief-pad-design',
    ipc4761='https://www.electronics.org/TOC/IPC-4761.pdf',
    ti='https://www.ti.com/lit/an/sloa122/sloa122.pdf',
    mchp='https://ww1.microchip.com/downloads/en/AppNotes/00001843A.pdf',
    efd='https://smtnet.com/library/files/upload/EFD-Tombstone-Troubleshooting.pdf',
    plexus='https://smtnet.com/library/files/upload/tombstoning0201.pdf',
    altium='https://resources.altium.com/p/how-to-design-circuit-boards-with-thermal-relief-pads',
    ipc7711='https://webstore.ansi.org/preview-pages/IPC/preview_IPC+7711+7721C-2017.pdf',
    preheat='https://www.hisco.com/UserFiles/whitepapers/pdf/The-Role-Of-Preheating-In-Electronics-Rework.pdf',
    ti_rework='https://www.ti.com/lit/an/slva439a/slva439a.pdf',
)
# ---- 既定のしきい値 (すべて仮。review.toml [config.mount] で変更可)
DEF = dict(
    stencil_mm=mg.STENCIL_MM, paste_transfer=mg.TRANSFER, tent_min_mm=mg.TENT_MIN,
    wick_warn=0.10, wick_fail=0.25, wick_ep_fail=0.50, via_filled=False,
    tomb_rth_ratio_fail={'0201': 1.5, '0402': 2.0, '0603': 3.0, '0805': 4.0},
    tomb_dt_fail_0402_s=1.0, tomb_pass=75, tomb_warn=50, tomb_max_size='0805', balance_max_size='0603',
    hand_solder=[], hand_ratio=[2.0, 4.0], hand_tss=[250.0, 232.0], tips_c=[300, 350], tip_g=0.05,
    reflow_dt=[2.0, 5.0], rework_gap=[1.0, 0.25], land_tol=[0.05, 0.10],
    md_edge_frac=0.5, h_reflow=50.0, thermal=True, thermal_python='python3',
    rf_refs=[], high_current_nets=[], exclude={},
    gerber=True,                 # ペースト・マスク開口を gerber 実データ (kicad-cli 出力) で見る。false なら .kicad_pcb の margin から計算
    chip_res_coarse=0.1, refine_margin=12,   # 熱計算の 2 段 (高速化): チップを粗い格子で解き、点 < tomb_pass + refine_margin だけ 0.05 mm で解き直す。0 で常に 0.05 mm。margin 12 = 粗い格子の点の過大側の誤差 最大 7 点 (8 基板 430 部品、2026-10-16) + 余裕 5
    process={}, process_default='reflow',   # 部品の工程 {"J2" = "hand"}。hand_solder の部品は hand。hand 以外の P27 は warn 止まり (リワーク性)
)
DIM = {'0201': (0.6, 0.3, 0.3), '0402': (1.0, 0.5, 0.5), '0603': (1.6, 0.8, 0.8), '0805': (2.0, 1.25, 0.8), '1206': (3.2, 1.6, 0.8)}   # L×W×T (mm、代表値)
RHOC_TERM = 3.0e6   # 仮: 端子 (L/4 の帯) の体積熱容量 J/m³K


def conf(cfg):
    c = dict(DEF); c.update(cfg.get('mount', {}) or {})
    rr = dict(DEF['tomb_rth_ratio_fail']); rr.update((cfg.get('mount', {}) or {}).get('tomb_rth_ratio_fail', {}) or {}); c['tomb_rth_ratio_fail'] = rr
    return c


def read_keep_solid(pdir):
    """review.toml の [pad_thermal.keep_solid] (レビュー担当の除外リスト、"REF.PAD" = "理由")"""
    p = os.path.join(pdir, 'review.toml')
    if not os.path.exists(p):
        return {}
    import tomllib
    return (tomllib.load(open(p, 'rb')).get('pad_thermal', {}) or {}).get('keep_solid', {}) or {}


def thermal_results(ctx, C, chips, hands, pour_nets, extra=None):
    """mount_thermal を呼ぶ (scipy があれば同じプロセス、無ければ thermal_python で nice 付き別プロセス)。/tmp/kreview-cache にキャッシュ"""
    if not C.get('thermal', True):
        return None, 'review.toml [config.mount] thermal = false'
    import mount_thermal as mt
    params = dict(tips_c=C['tips_c'], tip_g=C['tip_g'], **(extra or {}))
    key = mt.sha1(ctx['pcb'], json.dumps([sorted(chips), sorted(hands), params, sorted(pour_nets), getattr(mt, 'MODEL', '')]))   # 基板 + 設定 + 計算法の版
    cdir = os.environ.get('KREVIEW_CACHE', '/tmp/kreview-cache'); os.makedirs(cdir, exist_ok=True)
    cp = os.path.join(cdir, f'mount_thermal-{key}.json')
    if os.path.exists(cp):
        return json.load(open(cp)), f'キャッシュ {cp}'
    try:
        import scipy  # noqa
        R = mt.run(ctx['pcb'], chips, hands, params, pour_nets); how = '同じプロセス'
    except ImportError:
        py = os.environ.get('KREVIEW_THERMAL_PY') or C.get('thermal_python')
        if not py or not os.path.exists(py):
            return None, f'scipy が無く thermal_python ({py}) も無い'
        r = subprocess.run(['nice', '-n', '10', py, os.path.join(HERE, 'mount_thermal.py'), ctx['pcb'], '--chips', ','.join(chips), '--hand', ','.join(hands),
                            '--pour-nets', ','.join(pour_nets), '--params', json.dumps(params), '--out', cp], capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not os.path.exists(cp):
            return None, f'mount_thermal 失敗: {(r.stderr or r.stdout)[-300:]}'
        R = json.load(open(cp)); how = f'{py} (nice)'
    json.dump(R, open(cp, 'w'), ensure_ascii=False)
    return R, f"{how}、{R.get('elapsed_s')} s"


def loc(x, y, r, ref, label, grade):
    return dict(x=round(x, 3), y=round(y, 3), r=round(r, 2), ref=ref, label=label, grade=grade)


def run(ctx):
    b = ctx['board']; cfg = ctx['cfg']; C = conf(cfg); M = []
    st = mg.setup_info(b); zs = mg.zone_settings(b); fills = mg.zone_fills(b); CI = mg.CopperIndex(b, fills)
    t_pcb = mg.board_thickness(b)
    dru = ''.join(open(p).read() for p in glob.glob(os.path.join(ctx['dir'], '*.kicad_dru')))
    keep = read_keep_solid(ctx['dir'])
    ex_ = C.get('exclude') or {}
    excl_cfg = {k: '指定' for k in ex_} if isinstance(ex_, list) else dict(ex_)   # {"R1" = "理由"} か ["R1", "J2.3"]
    filled = bool(st['filled'] or C.get('via_filled'))   # via_in_pad_ok (P18 の許容) は穴埋めの発注ではないので数えない
    pour_nets = {(L, n) for (L, n) in fills}
    O, osrc = None, '.kicad_pcb の margin と開口用パッドから計算 (gerber = false)'
    if C.get('gerber', True):
        try:
            import mount_gerber
            gd, files = mount_gerber.export(ctx['pcb'])
            if gd:
                O = mount_gerber.Openings(b, files); osrc = f'ペースト = gerber 実データ ({gd}、kicad-cli を nice で出力、0.01 mm 画素で面積 +3% 程度)、マスク = .kicad_pcb のベクトル (開口用パッドを含む)'
            else:
                osrc = f'gerber を出せず .kicad_pcb から計算: {files}'
        except Exception as e:
            osrc = f'gerber を読めず .kicad_pcb から計算: {e}'
    ctx['mount_openings'] = osrc
    fps = [f for f in b.footprints if 'dnp' not in f.attr]

    def excluded(f, p):
        k = f'{f.ref}.{p.num}'
        if k in excl_cfg: return f'review.toml mount.exclude: {excl_cfg[k]}'
        if f.ref in excl_cfg: return f'review.toml mount.exclude: {excl_cfg[f.ref]}'
        if k in keep: return f'keep_solid: {keep[k]}'
        if mg.is_ep_or_tab(f, p): return 'EP・タブ (対象外)'
        if f.ref in C['rf_refs'] or mg.RF_RE.search(f.lib + ' ' + (f.value or '')): return 'RF (対象外)'
        if p.net in C['high_current_nets']: return '3 A 超の電源 (対象外)'
        return None

    # ---- パッドごとの基本量
    info = {}
    for f in fps:
        for p in mg.smd_pads(f):
            mp = mg.mask_paste(f, p, st)
            po = None
            ap = mg.aperture_pads(f, p, f.side)
            # マスク開口: .kicad_pcb のベクトル (パッド ∪ 開口用パッド、+ margin) で厳密に。gerber の画素 (0.01 mm) は 0.1 mm の判定には粗い
            vr = mg.via_relations(b, p, mp['mask_margin'], extras=ap)
            if O is not None:
                po = O.pad_open(f, p, 'Paste')       # ペースト量: gerber 実データ (開口用パッドの延長も入る)
                sol = (po['area_mm2'] if po else 0.0) * C['stencil_mm'] * C['paste_transfer'] * mg.METAL_VOL
            else:
                ext = [q for q in ap if any(L.endswith('.Paste') for L in q.layers)]
                area = mp['paste_w'] * mp['paste_h'] + sum(q.w * q.h for q in ext)
                po = dict(area_mm2=round(area, 4), ext_mm=round(max([mg.rect_dist(x, y, p) for q in ext for x, y in q.poly()] or [0.0]), 3))
                sol = area * C['stencil_mm'] * C['paste_transfer'] * mg.METAL_VOL
            info[(f.ref, p.num, p.x, p.y)] = dict(f=f, p=p, mp=mp, vr=vr, solder=sol, paste=po)

    # ---- P33 はんだ吸い込み (P18 の判定もここ)
    rows = []; locs = []; g33 = 'pass'; worst = 0.0
    for (ref, num, _, _), d in info.items():
        f, p = d['f'], d['p']
        w = mg.wicking(d['vr'], d['solder'], t_pcb, filled)
        if not w['exposed']:
            continue
        ep = mg.is_ep_or_tab(f, p)
        lim = C['wick_ep_fail'] if ep else C['wick_fail']
        g = 'pass' if w['ratio'] < C['wick_warn'] else 'warn' if w['ratio'] <= lim and (ep or w['ratio'] < lim) else 'fail'
        g33 = worse(g33, g); worst = max(worst, w['ratio'])
        v = min(w['exposed'], key=lambda v: v['drill_edge'])
        rows.append((w['ratio'], f"{g.upper()} {ref}.{num} ({p.net}{'、EP・タブ' if ep else ''}) ({p.x:.2f}, {p.y:.2f}): 穴が開口にかかる via {len(w['exposed'])} 個 (φ{v['drill']}、穴の {v['f_open']:.0%} が開口内)、"
                     f"穴容積 {w['hole_mm3']} mm³ / はんだ {d['solder']:.4f} mm³ = {w['ratio']:.0%} (按分の下限 {w['lower']:.0%})"
                     + (f"（{excluded(f, p)}。直結の理由であり吸い込みの免除ではない）" if (f'{ref}.{num}' in keep) else '')))
        if g != 'pass':
            locs.append(loc(p.x, p.y, max(p.w, p.h) * 0.8, ref, f'P33 {ref}.{num} {w["ratio"]:.0%}', g))
    rows.sort(key=lambda x: -x[0])
    det = [r for _, r in rows][:15]
    det.append(f"開口: {osrc}")
    det.append(f"はんだ = ペースト開口 × ステンシル {C['stencil_mm']} mm × 転写 {C['paste_transfer']} × 金属 {mg.METAL_VOL}、穴容積 = π(d/2)² × 板厚 {t_pcb} mm (穴全体へ流れるとした上限で判定)")
    det.append(('穴埋め指定あり (setup plugging/filling/capping か mount.via_filled) → 0 扱い' if filled else '穴埋めの指定なし (IPC-4761 Type V〜VII を注文するなら review.toml [config.mount] via_filled = true)'))
    det.append(f"根拠: IPC-4761 {URL['ipc4761']}、Microchip AN18.15 {URL['mchp']}、TI SLOA122 (ボイド <50%) {URL['ti']}。25% はレビュー担当案、10%/50% と合わせて仮")
    M.append(dict(Metric(S, 'P33_solder_wicking', 'はんだ吸い込み: 穴がマスク開口にかかる via の容積 / パッドのはんだ (P18 の判定はここ)', f"{len(rows)} パッド / 最大 {worst:.0%}", g33 if rows else 'pass',
                         f"<{C['wick_warn']:.0%} pass / <{C['wick_fail']:.0%} warn / ≥{C['wick_fail']:.0%} fail、EP・タブは ≤{C['wick_ep_fail']:.0%} まで warn (仮、2026-10-16 承認)",
                         det, 'mount_check MC16 (checks_mount)'), locs=locs))
    ctx['mount_p33_pads'] = {(r, n) for (r, n, _, _), d in info.items() if mg.wicking(d['vr'], d['solder'], t_pcb, filled)['exposed']}

    # ---- P18b テント余裕 (穴が開口にかかるものは P33)
    rows = []; locs = []
    for (ref, num, _, _), d in info.items():
        p = d['p']
        near = [v for v in d['vr'] if v['f_open'] == 0 and (v['drill_edge'] < C['tent_min_mm'] or v['land_edge'] < C['tent_min_mm'])]
        if not near:
            continue
        v = min(near, key=lambda v: v['drill_edge'])
        worst_r = math.pi * ((v['drill'] + mg.DRILL_TOL_PLUS) / 2) ** 2 * t_pcb / d['solder'] if d['solder'] else None
        rows.append((v['drill_edge'], f"{ref}.{num} ({p.net}) via ({v['at'][0]:.2f}, {v['at'][1]:.2f}) φ{v['drill']}/{v['size']}: 穴縁→開口 {v['drill_edge']:.3f} mm、ランド縁→開口 {v['land_edge']:+.3f} mm"
                     + (f"、テントが切れたら吸い込み比 {worst_r:.0%} (穴 +{mg.DRILL_TOL_PLUS})" if worst_r else '')))
        lab = f'穴 {v["drill_edge"]:.3f}' if v['drill_edge'] < C['tent_min_mm'] else f'ランド {v["land_edge"]:+.3f}'
        locs.append((v['drill_edge'], loc(v['at'][0], v['at'][1], max(0.45, v['size']), ref, f'P18b {ref}.{num} {lab}', 'warn')))
    rows.sort(); locs = [l for _, l in sorted(locs, key=lambda x: x[0])]
    n_drill = sum(1 for d_, _ in rows if d_ < C['tent_min_mm'])
    g = 'pass' if not rows or filled else 'warn'
    M.append(dict(Metric(S, 'P18b_via_near_pad', 'パッド近傍 via のテント余裕 (穴縁・ランド縁 → マスク開口、中心がパッド外のものも)', len(rows), g,
                         f"≥{C['tent_min_mm']} mm (JLC soldermask bridge 0.10 mm を準用、仮)。穴が開口にかかる via は P33",
                         [f"穴縁 < {C['tent_min_mm']} mm: {n_drill} 件、ランド縁だけ < {C['tent_min_mm']} mm: {len(rows) - n_drill} 件 (レビュー担当仕様「ビア穴・ランド」)"] + [r for _, r in rows][:15] + [f"根拠: JLC capabilities {URL['jlc_cap']} (soldermask bridge 0.10、plugged via は開口から ≥0.35 mm)、JLC SMD パッド {URL['jlc_smd']}"]
                         + (['穴埋め指定あり → テント余裕は問題にしない'] if filled else []), 'mount_check MC09 (checks_mount)'), locs=locs))

    # ---- 対象部品とパッドの縁
    chips = [f for f in fps if mg.is_chip(f)]
    conns = [f for f in fps if mg.is_connector(f)]
    def size_le(s, lim): return s in mg.SIZE_ORDER and mg.SIZE_ORDER.index(s) <= mg.SIZE_ORDER.index(lim)
    ring = {}
    zcd = {}
    for f in chips + conns:
        for p in mg.smd_pads(f):
            z = mg.resolve_zone_connection(b, f, p, zs, dru); zcd[(f.ref, p.num, p.x, p.y)] = z
            ring[(f.ref, p.num, p.x, p.y)] = mg.pad_ring(b, f, p, CI, z)
    def K(f, p): return (f.ref, p.num, p.x, p.y)

    # ---- P26 ゾーン接続の監査 (info)
    cnt = {}
    for f in fps:
        for p in mg.smd_pads(f):
            k = K(f, p)
            z = zcd.get(k) or mg.resolve_zone_connection(b, f, p, zs, dru)
            if not z['zone']:
                continue
            key = f"{z['mode']} ({z['src']})"; cnt[key] = cnt.get(key, 0) + 1
    det = ['SMD パッドのゾーン接続 (解決後): ' + ', '.join(f'{k} {v}' for k, v in sorted(cnt.items()))]
    det += [f"ゾーン {z['net']} {'/'.join(z['layers'])}: connect_pads={z['connect']} gap {z['gap']} spoke {z['spoke']}" for z in zs]
    det.append(f".kicad_dru の zone_connection 規則: {'あり' if 'zone_connection' in dru else 'なし'}、keep_solid {len(keep)} パッド、mount.exclude {len(excl_cfg)}")
    det.append('判定は P28_pad_pour_balance (同じパッドを二重に減点しない)')
    M.append(dict(Metric(S, 'P26_zone_connect_audit', 'ゾーン接続の監査 (パッド > フットプリント > .kicad_dru > ゾーン)', sum(cnt.values()), 'info', '情報のみ', det, 'mount_check MC15 (checks_mount)'), locs=[]))

    # ---- 熱回路網 (P27/P28/P29)
    pnets = sorted({n for (_, n) in pour_nets})
    # P28 で対象外 (mount.exclude・keep_solid・EP・RF・大電流) の部品は R_th を使わないので解かない (2026-10-16 高速化)
    tchips = [f.ref for f in chips if size_le(mg.chip_size(f), C['tomb_max_size']) and not any(excluded(f, p) for p in mg.smd_pads(f))]
    def on_pour(f):
        return any((ring[K(f, p)]['layer'], p.net) in pour_nets for p in mg.smd_pads(f))
    hands = sorted({f.ref for f in conns if on_pour(f)} | {r for r in C['hand_solder'] if b.fp(r)})
    proc = {r: 'hand' for r in C['hand_solder']}; proc.update(C.get('process') or {})
    def process(ref): return proc.get(ref, C['process_default'])
    tau_b = (2.0e6 * t_pcb * 1e-3 + 2 * 3.45e6 * 35e-6) / (2 * C['h_reflow'])
    dt_fail = {s: round(C['tomb_dt_fail_0402_s'] * (DIM[s][0] / 1.0) ** 2, 2) for s in DIM}
    def tomb(f, R):
        """チップ立ちの点 -> (点, R_th 比, Δt, [R_th]) 。R_th が 2 つ揃わなければ None"""
        sm = mg.smd_pads(f)
        if not R or len(R) != 2 or any(p.num not in R for p in sm):
            return None
        s = mg.chip_size(f); rv = [R[p.num] for p in sm]; rr = max(rv) / min(rv)
        L_, W_, T_ = DIM.get(s, DIM['0402']); c_term = W_ * T_ * L_ / 4 * 1e-9 * RHOC_TERM
        lam = []
        for p, Rk in zip(sm, rv):
            A = p.w * p.h * 1e-6; mp = info[K(f, p)]['mp']
            Cp = 3.45e6 * 35e-6 * A + 1.7e6 * mg.solder_mm3(mp, C['stencil_mm'], C['paste_transfer']) * 1e-9 + c_term
            Gb = 1 / Rk; lam.append((Cp + Gb * tau_b) / (C['h_reflow'] * A + Gb))
        dt = abs(lam[0] - lam[1]); rf = C['tomb_rth_ratio_fail'].get(s, 2.0); dtf = dt_fail.get(s, 1.0)
        x1 = min(1.0, max(0.0, math.log(rr) / math.log(rf))); x2 = min(1.0, dt / dtf)
        return round(100 * (1 - 0.5 * x1 - 0.5 * x2)), rr, dt, rv
    def classify(f, p):
        r = ring[K(f, p)]; z = zcd[K(f, p)]
        direct = z['mode'] == 'solid' and (r['md_edges'] >= 1 or r['pour_frac'] >= 0.3)
        track_only = r['pour_frac'] == 0
        return direct, track_only
    def struct_warn(f):
        """P28 の構造の warn (≤balance_max_size の 2 端子で、片側ベタ直結・反対側が配線のみ)"""
        sm = mg.smd_pads(f)
        if len(sm) != 2 or not size_le(mg.chip_size(f), C['balance_max_size']):
            return False
        cl = [classify(f, p) for p in sm]
        return bool((cl[0][0] and cl[1][1]) or (cl[1][0] and cl[0][1]))
    # 2 段の熱計算 (2026-10-16 高速化): チップは まず粗い格子 (chip_res_coarse) で全部解き、点が tomb_pass + refine_margin 未満
    # (warn・fail と境目に近いもの) だけ細かい格子 (0.05 mm) で解き直す。明らかに pass のチップは粗い値のまま (表には出ない)
    coarse = C.get('chip_res_coarse') or 0; sc0 = {}
    if coarse:
        # 表に R_th が出る部品 (構造の warn) は粗い格子を飛ばして最初から細かい格子で解く
        sw = [r for r in tchips if struct_warn(b.fp(r))]
        TR, how = thermal_results(ctx, C, [r for r in tchips if r not in sw], hands, pnets, dict(chip_res=coarse))
        sc0 = {r: (tomb(b.fp(r), ((TR or {}).get('chips') or {}).get(r)) or (0,))[0] for r in tchips if r not in sw}
        ref_ = sw + [r for r in sc0 if sc0[r] < C['tomb_pass'] + C['refine_margin']]
        if TR is not None and ref_:
            TF, how2 = thermal_results(ctx, C, ref_, [], pnets)
            if TF is not None:
                TR['chips'].update(TF['chips'])
            how += f"; 粗い格子 {coarse} mm で {len(sc0)} 部品、点 < {C['tomb_pass'] + C['refine_margin']} の {len(ref_) - len(sw)} 部品と構造の warn {len(sw)} 部品を 0.05 mm で ({how2})"
        elif TR is not None:
            how += f"; 粗い格子 {coarse} mm で {len(sc0)} 部品 (解き直し 0)"
    else:
        TR, how = thermal_results(ctx, C, tchips, hands, pnets)

    # ---- P27 手はんだ
    rows = []; locs = []; g27 = 'pass'
    for ref in hands:
        f = b.fp(ref); h = (TR or {}).get('hand', {}).get(ref)
        if not h or not h['pour'] or not h['other']:
            continue
        tr = h['transient']; lo, hi = str(min(C['tips_c'])), str(max(C['tips_c']))
        gp = min(h['pour'], key=lambda n: h['Rth'][n]); sp_ = max(h['other'], key=lambda n: h['Rth'][n])
        tg = tr[gp][hi]['t_reach_s'] or float('inf'); ts = tr[sp_][hi]['t_reach_s'] or float('inf')
        ratio = tg / ts if ts else float('inf')
        tss = tr[gp][lo]['T_ss']
        g1 = 'pass' if ratio <= C['hand_ratio'][0] else 'warn' if ratio <= C['hand_ratio'][1] else 'fail'
        g2 = 'pass' if tss >= C['hand_tss'][0] else 'warn' if tss >= C['hand_tss'][1] else 'fail'
        g = worse(g1, g2); pr = process(ref); capped = ''
        if pr != 'hand' and g == 'fail':
            g = 'warn'; capped = f'、工程 {pr} なので warn 止まり (リワーク性。IPC-7711/21C は SMT の取り外し・付け直しに hot gas と予熱を使う)'
        g27 = worse(g27, g)
        p = [q for q in mg.smd_pads(f) if q.num == gp][0]
        rows.append(f"{g.upper()} {ref} [工程 {pr}]{capped}: こて {hi} ℃ で 232 ℃ まで ベタ側 {ref}.{gp} {tg:.2f} s / 信号側 {ref}.{sp_} {ts:.2f} s = {ratio:.1f} 倍 ({g1})、こて {lo} ℃ の定常 {tss} ℃ ({g2})、"
                    f"R_th ベタ側 {min(h['Rth'][n] for n in h['pour'])}〜{max(h['Rth'][n] for n in h['pour'])} / 信号側 {min(h['Rth'][n] for n in h['other'])}〜{max(h['Rth'][n] for n in h['other'])} K/W"
                    + (f"、{lo} ℃ では {tr[gp][lo]['t_reach_s']} s" if tr[gp][lo]['t_reach_s'] else f'、{lo} ℃ では 15 s で届かない'))
        if g != 'pass':
            locs.append(loc(p.x, p.y, max(p.w, p.h) * 0.9, ref, f'P27 {ref} {ratio:.1f}x', g))
    det = rows + [f"熱回路網: {how}" if TR else f'熱回路網なし: {how}',
                  f"こて先 {C['tips_c']} ℃、接触 {C['tip_g']} W/K、到達 232 ℃、h 10 W/m²K (仮)。根拠: JLC {URL['jlc_tr']}、Altium {URL['altium']}",
                  f"工程: hand (手はんだ工程に指定、[config.mount] process / hand_solder) は fail まで、それ以外 (既定 {C['process_default']}) はリワーク性として warn 止まり。"
                  f"比べるのは SMD パッドだけ (シールド・ペグの PTH は比に入れない、熱の逃げ道としては銅に入る)。"
                  f"根拠: IPC-7711/21C 目次 (SMT 取り外しは hot gas / hot air 法) {URL['ipc7711']}、予熱 95〜105 ℃ でベタ・コネクタ・シールドの熱の逃げを抑える (PACE/Hisco) {URL['preheat']}、TI SLVA439A (hot air + 下面ヒータ) {URL['ti_rework']}"]
    M.append(dict(Metric(S, 'P27_thermal_balance', '手はんだの熱バランス (ベタ側/信号側の到達時間比、低温こてで溶けるか)', len(rows) if TR else None,
                         (g27 if rows else 'pass') if TR else 'skip', f"比 ≤{C['hand_ratio'][0]} pass / ≤{C['hand_ratio'][1]} warn、定常 ≥{C['hand_tss'][0]:g} pass / ≥{C['hand_tss'][1]:g} ℃ warn (仮)",
                         det, 'mount_check MC01/MC02 (checks_mount + mount_thermal)'), locs=locs))

    # ---- P28 ベタ直結の不均衡 + チップ立ち
    def jlc_note(f, cls):
        rec = mg.JLC_SPOKE[cls]; out = []
        for p in mg.smd_pads(f):
            z = zcd[K(f, p)]; r = ring[K(f, p)]
            if z['mode'] == 'thermal':
                ok = rec['spoke'][0] <= (z['spoke'] or 0) <= rec['spoke'][1] and rec['gap'][0] <= (z['gap'] or 0) <= rec['gap'][1]
                out.append(f"{p.num}: スポーク {z['spoke']}/gap {z['gap']} ({r['spokes']} 本 + 配線 {r['tracks']} 本){'' if ok else ' JLC 推奨外 (表示のみ)'}")
        return out
    rows = []; locs = []; g28 = 'pass'; nobj = 0; nexc = []; tsc = {}
    for f in chips:
        s = mg.chip_size(f)
        if not size_le(s, C['tomb_max_size']):
            continue
        sm = mg.smd_pads(f); ex = [excluded(f, p) for p in sm]
        if any(ex):
            nexc.append(f"{f.ref} ({'; '.join(x for x in ex if x)[:60]})"); continue
        nobj += 1; g = 'pass'; why = []
        if size_le(s, C['balance_max_size']):
            cl = [classify(f, p) for p in sm]
            if (cl[0][0] and cl[1][1]) or (cl[1][0] and cl[0][1]):
                g = 'warn'; why.append('ベタ直結 vs 配線のみ')
        R = (TR or {}).get('chips', {}).get(f.ref)
        sc = None; ts = tomb(f, R)
        if ts:
            sc, rr, dt, rv = ts; tsc[f.ref] = sc
            gs = 'pass' if sc >= C['tomb_pass'] else 'warn' if sc >= C['tomb_warn'] else 'fail'
            if gs != 'pass': why.append(f'チップ立ち {sc} 点')
            g = worse(g, gs)
        g28 = worse(g28, g)
        if g != 'pass':
            rg = [ring[K(f, p)] for p in sm]
            rows.append(((ORDER.index(g), -(sc if sc is not None else 100)), f"{g.upper()} {f.ref} {s} {f.value}: {'、'.join(why)}。"
                         + ' / '.join(f"{p.num} {p.net} {zcd[K(f, p)]['mode']}・ベタ接触 {r['pour_frac']:.0%}・出口 {r['exit_w']} mm・マスク定義辺 {r['md_edges']}" for p, r in zip(sm, rg))
                         + (f"、R_th {R[sm[0].num]}/{R[sm[1].num]} K/W (比 {max(R.values()) / min(R.values()):.2f}、fail {C['tomb_rth_ratio_fail'].get(s)})、Δt {dt:.3f} s (fail {dt_fail.get(s)})" if sc is not None else '')
                         + ('、' + '; '.join(jlc_note(f, 'passive' if size_le(s, '0603') else 'smd')) if jlc_note(f, 'passive' if size_le(s, '0603') else 'smd') else '')))
            locs.append(loc((sm[0].x + sm[1].x) / 2, (sm[0].y + sm[1].y) / 2, 0.9, f.ref, f'P28 {f.ref}' + (f' {sc}' if sc is not None else ''), g))
    for f in conns:
        sm = mg.smd_pads(f); nobj += 1; gi = 'pass'; bad = []
        pitch = min((math.hypot(p.x - q.x, p.y - q.y) for p in sm for q in sm if p is not q), default=0)
        for p in sm:
            if excluded(f, p):
                continue
            d_, _ = classify(f, p)
            if not d_:
                continue
            nb = [q for q in sm if q is not p and math.hypot(p.x - q.x, p.y - q.y) <= pitch * 1.6 and q.net != p.net and classify(f, q)[1] and not q.net.startswith('unconnected')]
            if nb:
                bad.append((p, nb))
        if bad:
            gi = 'warn'; g28 = worse(g28, gi)
            r0 = ring[K(f, bad[0][0])]
            rows.append(((1, 0), f"WARN {f.ref} (コネクタ): ベタ直結パッド {', '.join(p.num for p, _ in bad)} の隣が配線のみ ({', '.join(sorted({q.num for _, nb in bad for q in nb}, key=lambda x: (len(x), x)))})、"
                         f"例 {f.ref}.{bad[0][0].num}: ベタ接触 {r0['pour_frac']:.0%}・出口 {r0['exit_w']} mm・マスク定義辺 {r0['md_edges']}"))
            for p, _ in bad:
                locs.append(loc(p.x, p.y, max(p.w, p.h) * 0.75, f.ref, f'P28 {f.ref}.{p.num}', 'warn'))
        jn = jlc_note(f, 'smd')
        if jn:
            rows.append(((0, 0), f"info {f.ref}: " + '; '.join(jn[:6])))
    rows.sort(key=lambda x: (-x[0][0], x[0][1]))
    det = [r for _, r in rows][:20]
    det.append(f"対象: ≤{C['balance_max_size']} の 2 端子 (構造) と ≤{C['tomb_max_size']} (点)、コネクタの SMD パッド。対象外: EP・タブ・3 A 超電源・RF・review.toml (mount.exclude、pad_thermal.keep_solid) {len(nexc)} 部品"
               + (': ' + ', '.join(nexc[:12]) if nexc else ''))
    det.append(f"チップ立ちの点 = 100 × (1 − 0.5·min(1, ln(R_th 比)/ln(比 fail)) − 0.5·min(1, Δt/Δt fail))、≥{C['tomb_pass']} pass / ≥{C['tomb_warn']} warn。R_th 比 fail {C['tomb_rth_ratio_fail']}、Δt fail {dt_fail} s (0402 基準を L² で換算)。すべて仮")
    det.append(f"JLC 推奨 (表示のみ) {URL['jlc_tr']}: " + '; '.join(f"{v['label']} スポーク {v['spoke'][0]}〜{v['spoke'][1]} / gap {v['gap'][0]}〜{v['gap'][1]} mm" for v in mg.JLC_SPOKE.values()) + '、3 A 超は直結')
    det.append(f"根拠: EFD {URL['efd']}、Plexus {URL['plexus']}。熱回路網: {how}" if TR else f"熱回路網なし ({how}) → 構造の判定だけ")
    M.append(dict(Metric(S, 'P28_pad_pour_balance', 'ベタ直結パッドの不均衡とチップ立ち (レビュー担当 P24_pad_pour_balance 案 + R_th 比・溶融時刻差)', f"{sum(1 for r in rows if r[0][0] == 2)}/{sum(1 for r in rows if r[0][0] == 1)}/{nobj}",
                         g28, '構造: ベタ直結の反対側/隣が配線のみ → warn。点: ≥75 pass / ≥50 warn / <50 fail (仮)。値 = fail/warn/対象', det, 'mount_check MC04/MC05/MC17 + レビュー担当案 (checks_mount)'), locs=locs,
                  tomb_scores=tsc, tomb_scores_coarse=sc0))   # 部品ごとのチップ立ちの点 (最終 / 粗い格子)。回帰・精度確認用

    # ---- P29 多ピン部品のパッド間の溶融時刻差 (RC)
    rows = []; g29 = 'pass'; locs = []
    for ref in hands:
        h = (TR or {}).get('hand', {}).get(ref)
        if not h:
            continue
        f = b.fp(ref); lam = {}
        for p in mg.smd_pads(f):
            if p.num not in h['Rth']:
                continue
            A = p.w * p.h * 1e-6; mp = info[K(f, p)]['mp']
            Cp = 3.45e6 * 35e-6 * A + 1.7e6 * mg.solder_mm3(mp, C['stencil_mm'], C['paste_transfer']) * 1e-9
            Gb = 1 / h['Rth'][p.num]; lam[p.num] = (Cp + Gb * tau_b) / (C['h_reflow'] * A + Gb)
        if len(lam) < 2:
            continue
        dt = max(lam.values()) - min(lam.values())
        g = 'pass' if dt <= C['reflow_dt'][0] else 'warn' if dt <= C['reflow_dt'][1] else 'fail'; g29 = worse(g29, g)
        rows.append(f"{g.upper()} {ref}: パッド間の 217 ℃ 到達差 {dt:.3f} s (遅い {max(lam, key=lam.get)} / 早い {min(lam, key=lam.get)})")
    M.append(dict(Metric(S, 'P29_reflow_timing', '多ピン部品のパッド間のリフロー溶融時刻差 (RC 集中定数、一様加熱)', len(rows) if TR else None, (g29 if rows else 'pass') if TR else 'skip',
                         f"≤{C['reflow_dt'][0]} s pass / ≤{C['reflow_dt'][1]} s warn (仮)", rows + [f"λ = (C + G_b τ_b)/(G_a + G_b)、τ_b {tau_b:.1f} s、h {C['h_reflow']} W/m²K (仮)。炉内の温度むらは入らない"],
                         'mount_check MC03 (checks_mount)'), locs=locs))

    # ---- P30 リワークの間隙 (P27 の対象)
    gaps = mg.courtyard_gaps(b, [b.fp(r) for r in hands])
    rows = []; g30 = 'pass'; locs = []
    for ref, gl in gaps.items():
        pos = [x for x in gl if x[0] > 0]
        if not pos:
            continue
        gmin = pos[0][0]; g = 'pass' if gmin >= C['rework_gap'][0] else 'warn' if gmin >= C['rework_gap'][1] else 'fail'
        g30 = worse(g30, g)
        rows.append(f"{g.upper()} {ref}: courtyard の最小間隙 {gmin} mm ({', '.join(f'{r} {d}' for d, r in pos[:6] if d < C['rework_gap'][0])})"
                    + (f"、重なり (P06) {', '.join(r for d, r in gl if d <= 0)}" if any(d <= 0 for d, _ in gl) else ''))
        if g != 'pass':
            f = b.fp(ref); c = f.courtyard_bbox()
            locs.append(loc((c[0] + c[2]) / 2, (c[1] + c[3]) / 2, max(c[2] - c[0], c[3] - c[1]) / 2 + 0.5, ref, f'P30 {ref} {gmin}', g))
    M.append(dict(Metric(S, 'P30_rework_clearance', '手はんだ・リワーク対象 (コネクタ + hand_solder) の courtyard 間隙', len(rows), g30 if rows else 'pass',
                         f"≥{C['rework_gap'][0]} mm pass / ≥{C['rework_gap'][1]} mm warn (仮、重なり 0 は P06)", rows, 'mount_check MC10 (checks_mount)'), locs=locs))

    # ---- P31/P32 コネクタの台帳 (pcb-score/connectors.json の land / coplanarity_mm)
    led = json.load(open(os.path.join(KB, 'tools', 'pcb-score', 'connectors.json')))['connectors']
    import re as _re
    r31 = []; g31 = 'pass'; r32 = []; g32 = 'pass'; l31 = []; l32 = []
    for f in conns:
        e = next((x for x in led if ('land' in x or 'coplanarity_mm' in x) and _re.search(x['match'], f.lib)), None)
        if not e:
            continue
        sm = mg.smd_pads(f)
        if 'land' in e:
            L = e['land']; diffs = []
            sig = [p for p in sm if not p.net.startswith('unconnected')] or sm
            pw = min(min(p.w, p.h) for p in sig); ph = max(max(p.w, p.h) for p in sig)
            diffs += [('パッド幅', pw, L['pad_w']), ('パッド長', ph, L['pad_l'])]
            xs = sorted({round(p.x, 3) for p in sm}); ys = sorted({round(p.y, 3) for p in sm})
            along = xs if len(xs) > len(ys) else ys; across = ys if along is xs else xs
            if len(along) > 1: diffs.append(('ピッチ', round(min(b_ - a for a, b_ in zip(along, along[1:])), 3), L['pitch']))
            if len(across) == 2 and 'row_gap' in L: diffs.append(('列間 (パッド内側)', round(across[1] - across[0] - ph, 3), L['row_gap']))
            pegs = [p for p in f.pads if p.type == 'np_thru_hole']
            if pegs and 'peg_hole' in L:
                diffs.append(('位置決め穴', pegs[0].drill, L['peg_hole']))
                if len(pegs) == 2 and 'peg_pitch' in L: diffs.append(('穴間隔', round(math.hypot(pegs[0].x - pegs[1].x, pegs[0].y - pegs[1].y), 3), L['peg_pitch']))
            dm = max(abs(a - c) for _, a, c in diffs)
            g = 'pass' if dm <= C['land_tol'][0] else 'warn' if dm <= C['land_tol'][1] else 'fail'
            txt = ', '.join(f'{n} {a:g}/{c:g}' for n, a, c in diffs)
            if 'peg_d' in L and pegs:
                play = ((L['peg_hole'] + L.get('peg_hole_tol', 0)) - (L['peg_d'] - L.get('peg_d_tol', 0))) / 2
                margin = (pw - L.get('tail_w', pw)) / 2
                g = worse(g, 'pass' if play < margin else 'warn'); txt += f"、位置決めピンの最大遊び {play:.2f} mm vs ピン先のパッド内余裕 {margin:.2f} mm"
            g31 = worse(g31, g)
            r31.append(f"{g.upper()} {f.ref} ({e['match']}): 最大差 {dm:.3f} mm ({txt})、出典 {L.get('source', e.get('note', ''))[:90]}")
            if g != 'pass':
                c = f.courtyard_bbox(); l31.append(loc((c[0] + c[2]) / 2, (c[1] + c[3]) / 2, 2.0, f.ref, f'P31 {f.ref}', g))
        if 'coplanarity_mm' in e:
            cp = e['coplanarity_mm']; T = C['stencil_mm']
            # パッドごとの体積換算の厚さ t_eq = ステンシル厚 × ペースト開口面積 / パッド銅面積 (外側へのオーバープリントはリフローでパッドへ引き戻される、仮)
            tq = []
            for p in sm:
                d = info.get((f.ref, p.num, p.x, p.y))
                if not d:
                    continue
                pa = d['paste']['area_mm2'] if d.get('paste') else d['mp']['paste_w'] * d['mp']['paste_h']
                ext = d['paste']['ext_mm'] if d.get('paste') else 0.0
                tq.append((T * pa / (p.w * p.h), p, pa, ext))
            if tq:
                tmin, pm, pa, ext = min(tq, key=lambda x: x[0])
                g = 'pass' if tmin >= cp else 'warn'; g32 = worse(g32, g)
                need = (cp / T - 1) * pm.w * pm.h / min(pm.w, pm.h)
                exts = sorted({round(x[3], 2) for x in tq})
                r32.append(f"{g.upper()} {f.ref}: コプラナリティ {cp} mm vs 体積換算のペースト厚 最小 {tmin:.3f} mm ({f.ref}.{pm.num}: ステンシル {T} × 開口 {pa:.3f} / 銅 {pm.w * pm.h:.3f} mm²、"
                           f"開口のパッド外への延長 {ext:.2f} mm)。全パッドの延長 {exts} mm"
                           + ('' if g == 'pass' else f"。足りない分: ステンシル {cp} mm、または長辺方向へ合計 +{need:.2f} mm の延長 (段付きステンシルも可)。JLC は開口を既定で直すので注文備考が要る"))
            else:
                g = 'pass' if C['stencil_mm'] >= cp else 'warn'; g32 = worse(g32, g)
                r32.append(f"{g.upper()} {f.ref}: コプラナリティ {cp} mm / ステンシル {C['stencil_mm']} mm")
            if g != 'pass':
                c = f.courtyard_bbox(); l32.append(loc((c[0] + c[2]) / 2, (c[1] + c[3]) / 2, 2.4, f.ref, f'P32 {f.ref}', g))
    M.append(dict(Metric(S, 'P31_connector_land', 'コネクタのランドとメーカー推奨の差・位置決めピンの遊び (connectors.json の land)', len(r31), g31 if r31 else 'skip',
                         f"差 ≤{C['land_tol'][0]} pass / ≤{C['land_tol'][1]} mm warn、遊び < パッド内余裕 (仮)", r31 or ['connectors.json に land のあるコネクタなし'], 'mount_check MC13/MC14 (checks_mount)'), locs=l31))
    M.append(dict(Metric(S, 'P32_paste_vs_coplanarity', 'ステンシル厚とリードのコプラナリティ (connectors.json の coplanarity_mm)', len(r32), g32 if r32 else 'skip',
                         '体積換算のペースト厚 (ステンシル × 開口面積 / 銅面積) ≥ コプラナリティ で pass、満たさなければ warn (仮)', (r32 + [f'開口: {osrc}']) if r32 else ['connectors.json に coplanarity_mm のあるコネクタなし'], 'mount_check MC08 (checks_mount)'), locs=l32))
    ctx['mount'] = dict(thermal=TR, how=how)
    return M
