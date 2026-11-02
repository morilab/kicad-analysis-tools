"""pdn の実行: 格子からポート間 R・L を抽出し、コンデンサ・レギュレータ・直列素子・負荷 ΔI を加えた回路を
ngspice (.ac) と Python (MNA) で解いて、ピンごとの |Z|、目標超過帯域、効いていないコンデンサ、kreview 形式 JSON を出す。
  python -m pdn.run <project_dir> --cond pdn/examples/<案件>/pdn.yaml --grid grid.npz --out DIR
判定 (2026-10-14 作者 決定 ①〜⑥): ピン k の Z_eff(f) = |Σ_j Z_kj ΔI_j| / ΣΔI ≤ Ztarget_k = 予算_k / ΣΔI (f ≤ min(f_judge_k, f_qs))。
予算_k = (源の最低電圧 − 許容最小 − IR 最悪) × ratio。全ピンの ΔI を同位相で同時に流す (最悪の重ね合わせ)。"""
import argparse, datetime, glob, json, math, os, sys, time
import numpy as np
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pcbread, grid as CG, parts as CP
from pdn import extract as EX
from pdn.circuit import Netlist
from irdrop.run import stage_grade

VERSION = '0.1 (2026-10-14)'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS = os.path.join(HERE, 'data', 'parts')
def v(x):
    x = None if x is None else (x.get('value') if isinstance(x, dict) else x)
    return float(x) if isinstance(x, str) and x.replace('.', '').replace('e', '').replace('-', '').replace('+', '').isdigit() else x


def log(*a):
    print(*a, flush=True)


def pad_xy(grid, ref, pin):
    from irdrop.solver import find_pads
    m = grid.meta; pts = []
    for q in find_pads(grid, ref, pin):
        js, is_, _ = grid.pad_cells(q); pts += list(zip(js, is_))
    if not pts:
        return None
    j, i = np.mean(pts, 0)
    return (m['x0_mm'] + (i + .5) * m['dx_mm'], m['y0_mm'] + (j + .5) * m['dx_mm'])


def net_pads(grid, net):
    A = grid.a; k = grid.net_index(net)
    s = sorted({(str(r), str(p)) for r, p, n in zip(A['pad_ref'], A['pad_pin'], A['pad_net']) if n == k})
    return s


def nearest(grid, cands, ref, pin):
    xy = pad_xy(grid, ref, pin)
    same = [c for c in cands if c[0] == ref] or cands
    return min(same, key=lambda c: math.dist(xy, pad_xy(grid, *c) or (1e9, 1e9)))


def dcbias(cap, V):
    t = np.array(v(cap['dc_bias']), float)
    return float(np.interp(V, t[:, 0], t[:, 1]))


def extract_all(grid, cfg, outdir):
    prog = os.path.join(outdir, 'progress'); os.makedirs(prog, exist_ok=True)
    RED = {}
    for net in cfg['nets'] + [cfg['gnd']]:
        f = os.path.join(prog, f"red_{net.replace('/', '_').replace('+', 'p')}.json")
        if os.path.exists(f):
            d = json.load(open(f)); d['G'] = np.array(d['G']); d['Gam'] = np.array(d['Gam']); RED[net] = d
            log(f'  {net}: 途中経過から読み込み ({len(d["names"])} ポート)'); continue
        pads = net_pads(grid, net); t = time.time()
        red = EX.reduce(grid, net, [(f'{r}.{p}', r, p) for r, p in pads], with_l=(net != cfg['gnd']))
        log(f"  {net}: ポート {len(pads)}、節点 {red['info']['cells']}、ビア {red['info']['vias']}、{time.time() - t:.1f} s、"
            f"未接続ポート {red['info']['missing_ports']}")
        json.dump(dict(names=red['names'], G=red['G'].tolist(), Gam=red['Gam'].tolist(), info=red['info']), open(f, 'w'))
        RED[net] = red
    return RED


def build(grid, board, cfg, lib, RED, IR):
    gnd = cfg['gnd']; n = Netlist(); notes = []
    def node(net, name):
        name = RED[net]['info'].get('alias', {}).get(name, name)      # 重なった pad は代表のポートへ
        return f'{net}:{name}' if net != gnd else f'G:{name}'
    g0 = f"{cfg['ground_node']['ref']}.{cfg['ground_node']['pin']}"
    for net, red in RED.items():
        for i, j, R, L in EX.branches(red, 1e-7):
            a, b = node(net, red['names'][i]), node(net, red['names'][j])
            a, b = ('0' if a == f'G:{g0}' else a), ('0' if b == f'G:{g0}' else b)
            n.add('RL', f'w:{net}:{i}:{j}', a, b, (R, L if net != gnd else 0.0))
    N = lambda net, name: '0' if node(net, name) == f'G:{g0}' else node(net, name)
    gpads = [tuple(x.split('.', 1)) for x in RED[gnd]['names']]
    # 源
    for key, s in cfg['sources'].items():
        if s['kind'] == 'ideal':
            for p in s['power']:
                n.add('R', f'src:{key}:p{p}', f'{key}_i', N('VBUS', f"{s['ref']}.{p}"), 1e-6)
            n.add('V', f'src:{key}', f'{key}_i', N(gnd, f"{s['ref']}.{s['gnd'][0]}"), 0)
            notes.append(f"{key}: {s['ref']} を理想源 (⑤ ホスト・ケーブルは含めない)")
        else:
            part = lib[s['part']]; z = part['zout'] if 'zout' in part else part['pins'][s['pin']]['zout']
            R = float(v(z['r']))
            L = float(v(z['l'])) if 'l' in z else R / (2 * math.pi * float(v(z['f_bw'])))
            gp = s['gnd'] if s['gnd'] != 'nearest' else nearest(grid, gpads, s['ref'], s['out'])[1]
            net = 'VBUS' if False else next(nt for nt in cfg['nets'] if f"{s['ref']}.{s['out']}" in RED[nt]['names'])
            n.add('V', f'src:{key}', f'{key}_i', N(gnd, f"{s['ref']}.{gp}"), 0)
            n.add('RL', f'src:{key}:z', f'{key}_i', N(net, f"{s['ref']}.{s['out']}"), (R, L))
            notes.append(f"{key}: {s['ref']}.{s['out']} 出力インピーダンス R {R * 1e3:.0f} mΩ + L {L * 1e9:.0f} nH ({CP.BASIS.get(CP.basis_of(z['r']), '?')}、{s['part']}、GND {s['ref']}.{gp})")
    # 直列素子
    def net_of(name):
        return next(nt for nt in cfg['nets'] if name in RED[nt]['names'])
    for key, s in cfg['series'].items():
        a, b = f"{s['ref']}.{s['a']}", f"{s['ref']}.{s['b']}"; na, nb = net_of(a), net_of(b); part = lib[s['part']]
        dcr, _ = CP.series_resistance(part)
        if not dcr:
            raise CP.DataError(f"{key}: 部品 {s['part']} に直流抵抗 (dc_resistance / resistance) が無い")
        R = float(v(dcr.get('max', dcr)) if isinstance(dcr, dict) and 'max' in dcr else v(dcr))
        if s['kind'] == 'bead':
            bm = part['bead_model']; m = f'{key}_m'
            n.add('R', f'ser:{key}:dcr', N(na, a), m, R)
            n.add('L', f'ser:{key}:l', m, N(nb, b), v(bm['l'])); n.add('R', f'ser:{key}:r', m, N(nb, b), v(bm['r'])); n.add('C', f'ser:{key}:c', m, N(nb, b), v(bm['c']))
            notes.append(f"{key}: DCR {R} Ω + (L {v(bm['l']) * 1e6:.1f} µH ∥ R {v(bm['r'])} Ω ∥ C {v(bm['c']) * 1e12:.0f} pF) (仮定)")
        else:
            n.add('R', f'ser:{key}', N(na, a), N(nb, b), R); notes.append(f"{key}: R {R} Ω (worst)")
    # コンデンサ
    caps = []
    for fp in board['footprints']:
        lc = str(fp.get('lcsc') or '')
        if lc not in lib or 'capacitor' not in lib[lc]:
            continue
        pn = {str(p['num']): p['net'] for p in fp['pads']}
        pw = [(k, x) for k, x in pn.items() if x in cfg['nets']]; gg = [(k, x) for k, x in pn.items() if x == gnd]
        if not pw or not gg:
            continue
        (pp, net), (gpn, _) = pw[0], gg[0]; cap = lib[lc]['capacitor']
        V = cfg['rail_v'][net]; k = dcbias(cap, V); C = v(cap['c_nom']) * k
        nm = f"cap:{fp['ref']}:"; mid1, mid2 = f"{fp['ref']}_m1", f"{fp['ref']}_m2"
        pa, pb = N(net, f"{fp['ref']}.{pp}"), N(gnd, f"{fp['ref']}.{gpn}")
        if 'model' in cap and os.path.exists(os.path.join(PARTS, v(cap['model']))):
            # メーカー (代用品) の SPICE サブ回路を展開し、C 素子すべてに DC バイアスの比を掛ける
            import re as _re
            for ln in open(os.path.join(PARTS, v(cap['model']))):
                t_ = ln.split()
                if not t_ or not _re.match(r'^[RLC]\d', t_[0]):
                    continue
                mp = lambda x: pa if x == 'Port1' else pb if x == 'Port2' else f"{fp['ref']}_{x}"
                val = float(t_[3]) * (k if t_[0][0] == 'C' else 1.0)
                n.add(t_[0][0], nm + t_[0], mp(t_[1]), mp(t_[2]), val)
        else:
            n.add('R', nm + 'r', pa, mid1, v(cap['esr'])); n.add('L', nm + 'l', mid1, mid2, v(cap['esl']))
            n.add('C', nm + 'c', mid2, pb, C)
        caps.append(dict(ref=fp['ref'], net=net, lcsc=lc, mpn=lib[lc].get('mpn'), c_nom=v(cap['c_nom']), v_dc=V, ratio=k, c_eff=C,
                         esr=v(cap['esr']), esl=v(cap['esl']), basis=CP.basis_of(cap['dc_bias']),
                         substitute=(cap.get('substitute') or {}).get('mpn'), model=v(cap.get('model'))))
    # 負荷 (ΔI)
    loads = []
    for r in IR['worst']:
        key = (r['ref'], r['pin']); t = IR['typ'].get(key)
        it = (t['current_ma'] if t and t['current_ma'] is not None else 0.0) * 1e-3
        im = None if r['current_ma'] is None else r['current_ma'] * 1e-3
        if r['ref'] in ('F1', 'FB1') or r['ref'] == cfg['sources']['U3']['ref'] and r['pin'] == cfg['sources']['U3']['out']:
            continue
        if im is None:
            dI = it; rule = 'max の記載なし → typ の 2 倍を最悪と仮定 (ΔI = typ)'
        else:
            dI = im - it; rule = 'max − typ'     # max = typ (R3 の LED など一定電流) は ΔI 0 で除外
        if dI <= 0:
            continue
        name = f"{r['ref']}.{r['pin']}"; net = r['net']
        gp = nearest(grid, gpads, r['ref'], r['pin'])
        if r['ref'] == 'R3':
            gp = ('D1', '1') if ('D1', '1') in gpads else gp
        lim = (cfg.get('band_limited_loads') or {}).get(name)
        f_bw = None
        if lim:
            # ④ LDO 入力の ΔI はレギュレータの帯域で制限: I(f) = ΔI / (1 + j f / f_bw)。補助節点の 1 Ω ∥ C で一次遅れを作り VCCS で注入
            zz = lib[lim['part']]['pins'][lim['pin']]['zout'] if 'pins' in lib[lim['part']] and lim['pin'] in lib[lim['part']]['pins'] else lib[lim['part']]['zout']
            f_bw = float(v(lim.get('f_bw') or zz['f_bw'])); lp = f'lp_{name}'
            n.add('I', f'ld:{name}:src', '0', lp, dI); n.add('R', f'ld:{name}:r', lp, '0', 1.0); n.add('C', f'ld:{name}:c', lp, '0', 1 / (2 * math.pi * f_bw))
            n.add('G', f'ld:{name}', N(net, name), N(gnd, '.'.join(gp)), (lp, '0', 1.0))
            rule += f'、{lim["part"]} の帯域 {f_bw / 1e3:g} kHz で一次遅れ (④)'
        else:
            n.add('I', f'ld:{name}', N(net, name), N(gnd, '.'.join(gp)), dI)
        bud = None
        if r.get('margin_mv') is not None:
            bud = (r['margin_mv'] - r['drop_mv']) * 1e-3 * float(v(cfg['ripple']['ratio']))
        loads.append(dict(name=name, ref=r['ref'], pin=r['pin'], net=net, value=r.get('value'), di=dI, i_typ=it, i_max=im, rule=rule, f_bw=f_bw,
                          gnd='.'.join(gp), margin_mv=r.get('margin_mv'), ir_mv=r['drop_mv'], budget=bud, a=N(net, name), b=N(gnd, '.'.join(gp))))
    return n, caps, loads, notes


def judge_cap(cfg):
    """判定上限の設定値 analysis.f_judge_max (値オブジェクト)。無ければ None。戻り値 (Hz, 根拠の文)"""
    x = (cfg.get('analysis') or {}).get('f_judge_max')
    if not x or v(x) is None:
        return None, None
    return float(v(x)), f"{float(v(x)) / 1e6:g} MHz ({CP.BASIS.get(CP.basis_of(x), '?')}: {x.get('from') or x.get('reason')})"


def judge_limit(fj, f_qs, cap):
    """判定上限 = min(0.35/tr, 準静的の上限, 設定値)。fj / cap は None なら除く。戻り値 (Hz, 決めた項 'tr' / 'qs' / 'cfg')"""
    c = [(f_qs, 'qs')] + ([(fj, 'tr')] if fj else []) + ([(cap, 'cfg')] if cap else [])
    return min(c, key=lambda t: t[0])


def f_judge(cfg, ref):
    if ref in (cfg.get('f_judge_direct') or {}):
        x = cfg['f_judge_direct'][ref]; return float(v(x)), f"{v(x) / 1e6:g} MHz ({CP.BASIS.get(CP.basis_of(x), '?')}: {x.get('reason') or x.get('from')})"
    x = (cfg.get('clocks') or {}).get(ref)
    if x is None:
        return None, 'クロック不明'
    fc = float(v(x)); tr = 0.1 / fc
    return 0.35 / tr, f"f_clk {fc / 1e6:g} MHz → tr = 10% 周期 {tr * 1e9:.3g} ns → 0.35/tr = {0.35 / tr / 1e6:.0f} MHz ({CP.BASIS.get(CP.basis_of(x), '?')})"


def bands(F, ok_mask):
    """ok_mask が False の連続区間 → [(f0, f1)]"""
    out = []; s = None
    for k, ok in enumerate(ok_mask):
        if not ok and s is None:
            s = k
        if (ok or k == len(F) - 1) and s is not None:
            e = k - 1 if ok else k; out.append((F[s], F[e])); s = None
    return out


def eps_r(grid, an):
    """準静的上限の εr: 格子 meta.layers[].er_below (core が基板 YAML / .kicad_pcb の stackup から記録) の最大 (f_qs が低い = 安全側)。
    無ければ pdn.yaml の analysis.eps_r (2026-10-14 置き換え)"""
    L = [(x['er_below'], x.get('er_below_src', '')) for x in grid.meta.get('layers', []) if x.get('er_below')]
    if L:
        e, src = max(L, key=lambda t: t[0])
        return float(e), f"格子 meta.layers[].er_below の最大 ({src})"
    return float(v(an['eps_r'])), 'pdn.yaml analysis.eps_r (格子に er_below なし)'


def fmt_f(f):
    return f'{f / 1e6:.3g} MHz' if f >= 1e6 else f'{f / 1e3:.3g} kHz'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project'); ap.add_argument('--cond', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--grid', required=True); ap.add_argument('--parts', default=os.path.join(HERE, 'data', 'parts'))
    a = ap.parse_args()
    pcb = [p for p in glob.glob(os.path.join(a.project, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
    board = pcbread.read_pcb(pcb); cfg = yaml.safe_load(open(a.cond, encoding='utf-8')); lib = CP.load_library(a.parts)
    os.makedirs(a.out, exist_ok=True)
    grid = CG.PcbGrid.load(a.grid)
    if grid.meta['pcb_sha256'] != __import__('hashlib').sha256(open(pcb, 'rb').read()).hexdigest():
        sys.exit('格子が .kicad_pcb と違う (pcb_sha256)')
    log(f"格子 {grid.shape} dx {grid.meta['dx_mm']} mm")
    irdir = v(cfg['ripple']['ir_rows'])
    IR = {c: {(r['ref'], r['pin']): r for r in json.load(open(os.path.join(irdir, f'rows_{c}.json')))['rows']} for c in ('typ', 'worst')}
    IR['worst'] = list(IR['worst'].values())
    log('抽出 (Kron 縮約)'); RED = extract_all(grid, cfg, a.out)
    net, caps, loads, notes = build(grid, board, cfg, lib, RED, IR)
    an = cfg['analysis']; f0, f1, ppd = float(v(an['f_min'])), float(v(an['f_max'])), int(v(an['points_per_decade']))
    F = np.logspace(math.log10(f0), math.log10(f1), int(round(math.log10(f1 / f0) * ppd)) + 1)
    er, er_src = eps_r(grid, an); Lmax = max(grid.meta['nx'], grid.meta['ny']) * grid.meta['dx_mm'] * 1e-3
    f_qs = EX.C0 / (10 * Lmax * math.sqrt((er + 1) / 2))
    log(f"回路: 素子 {len(net.el)}、節点 {len(net.nodes())}、コンデンサ {len(caps)}、負荷 {len(loads)}、f_qs {f_qs / 1e6:.0f} MHz (εr {er:g}: {er_src})")
    # ngspice (全 ΔI 同時) → V_k
    t = time.time()
    Fs, Vs = net.spice('usb-jtag-ft2232h rev0.2 PDN', [(l['a'], l['b']) for l in loads], f0, f1, ppd, keep=a.out)
    log(f'ngspice {time.time() - t:.1f} s ({len(Fs)} 点)')
    t = time.time(); ports = [(l['a'], l['b']) for l in loads]
    Z = net.zmatrix(Fs, ports)
    dI = np.array([[l['di'] / (1 + 1j * f / l['f_bw']) if l.get('f_bw') else l['di'] for f in Fs] for l in loads])   # (負荷, 周波数)
    Vp = np.einsum('kjf,jf->kf', Z, dI)
    dev = float(np.max(abs(abs(Vp) - abs(Vs)) / abs(Vs)))      # 振幅で比べる (SPICE の電流源の向きの約束で全体の符号が反転するため)
    dev_c = float(min(np.max(abs(Vp - Vs) / abs(Vs)), np.max(abs(Vp + Vs) / abs(Vs))))   # 複素で、全体の符号を除いて
    log(f'複素での差 (全体の符号を除く) {dev_c * 100:.4f}%')
    dev = max(dev, dev_c)
    log(f'Python Z 行列 {time.time() - t:.1f} s、ngspice との最大差 {dev * 100:.4f}%')
    rows = []; M = []; pngs = []
    def Mt(id, title, value, grade, threshold='', detail=None):
        M.append(dict(stage='pdn', id=id, title=title, value=value, grade=grade, threshold=threshold, detail=list(detail or []), source='pcb-physics pdn'))
    # 系統ごとの ΔI 合計 (Ztarget の分母)
    rail_of = {'+3.3VA': '+3V3', '/+3V3_EXT': '+3V3'}
    fcap, fcap_src = judge_cap(cfg)
    if fcap:
        log(f'判定上限の設定値 {fcap_src}')
    sumdi = {}
    for l in loads:
        sumdi[rail_of.get(l['net'], l['net'])] = sumdi.get(rail_of.get(l['net'], l['net']), 0) + l['di']
    for k, l in enumerate(loads):
        fj, fjn = f_judge(cfg, l['ref']); S = sumdi[rail_of.get(l['net'], l['net'])]
        zeff = abs(Vs[k]) / S; zself = abs(Z[k, k])
        r = dict(l, f_judge=fj, f_judge_note=fjn, sum_di=S, zeff=zeff, zself=zself)
        if l['budget'] is None:
            r.update(ztarget=None, verdict='対象外 (許容電圧なし)', over=[], over_info=[])
        elif l['budget'] <= 0:
            r.update(ztarget=0.0, verdict='予算 0 以下 (IR ドロップで余裕を使い切り)', over=[(Fs[0], Fs[-1])], over_info=[])
        else:
            zt = l['budget'] / S; lim, lim_by = judge_limit(fj, f_qs, fcap)
            jm = Fs <= lim
            over = bands(Fs[jm], zeff[jm] <= zt); over_i = bands(Fs[~jm], zeff[~jm] <= zt) if (~jm).any() else []
            r.update(ztarget=zt, over=over, over_info=over_i, verdict='不合格' if over else '合格', f_lim=lim, f_lim_by=lim_by)
        rows.append(r)
    # 効いていないコンデンサ
    rel = float(v(cfg['ineffective_rel']))
    for c in caps:
        n2 = net.without(f"cap:{c['ref']}:"); Z2 = n2.zmatrix(Fs, ports); V2 = np.einsum('kjf,jf->kf', Z2, dI)
        worst = 0.0; where = None
        for k, r in enumerate(rows):
            if r['net'] not in (c['net'],) and rail_of.get(r['net'], r['net']) != rail_of.get(c['net'], c['net']):
                continue
            lim = r.get('f_lim') or judge_limit(r['f_judge'], f_qs, fcap)[0]; m = Fs <= lim
            # 判定範囲内の Z_eff の最大値 (合否を決める値) が何 % 変わるか。周波数ごとの差だと共振の移動で何千 % にもなるので最大値で比べる
            p0 = float(np.max(np.abs(Vs[k][m]))); p1 = float(np.max(np.abs(V2[k][m])))
            d = abs(p1 - p0) / p0
            if d > worst:
                worst = d; where = (r['name'], float(Fs[m][int(np.argmax(np.abs(V2[k][m])))]))
        c['sens'] = worst; c['sens_at'] = where; c['effective'] = worst >= rel
        log(f"  {c['ref']} ({c['net']}): 外したときの最大変化 {worst * 100:.1f}% @ {where}")
    # 図
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for fpth in glob.glob('/usr/share/fonts/**/NotoSerifCJK*.tt*', recursive=True)[:1]:
        font_manager.fontManager.addfont(fpth); plt.rcParams['font.family'] = font_manager.FontProperties(fname=fpth).get_name()
    def _xt(x):
        t = [1e4, 1e5, 1e6, 1e7, 1e8, 5e8]
        x.set_xticks(t); x.set_xticklabels(['10k', '100k', '1M', '10M', '100M', '500M']); x.tick_params(axis='x', labelbottom=True)
    for rail in cfg['judged']:
        rr = [r for r in rows if r['net'] == rail]
        if not rr:
            continue
        fig, ax = plt.subplots(1, 2, figsize=(15, 6), sharey=True)
        for r in rr:
            ln, = ax[0].loglog(Fs, r['zeff'], lw=1.2, label=f"{r['name']} ({r['value'] or ''})"[:40])
            ax[1].loglog(Fs, r['zself'], lw=1.2, color=ln.get_color(), label=r['name'])
            if r['ztarget']:
                ax[0].axhline(r['ztarget'], color=ln.get_color(), ls='--', lw=0.8)
            if r['f_judge']:
                ax[0].axvline(r.get('f_lim') or min(r['f_judge'], f_qs), color=ln.get_color(), ls=':', lw=0.8)
        for x in ax:
            tr = x.get_xaxis_transform()
            x.axvline(f_qs, color='k', ls='-.', lw=0.8); x.text(f_qs, 0.02, ' 準静的の上限', fontsize=8, rotation=90, va='bottom', transform=tr)
            for ref in cfg.get('reference_only') or []:
                x.axvline(ref['f'], color='gray', ls=':', lw=0.8); x.text(ref['f'], 0.02, ' ' + ref['label'], fontsize=7, rotation=90, va='bottom', color='gray', transform=tr)
            x.set_xlim(f0, f1); x.grid(True, which='both', alpha=0.3); x.set_xlabel('周波数 [Hz]'); _xt(x)
        ax[0].set_ylabel('|Z| [Ω]'); ax[0].set_title(f'{rail}: Z_eff = |V_k| / ΣΔI (全 ΔI 同時、破線 = Ztarget、点線 = 判定上限)', fontsize=10)
        ax[1].set_title(f'{rail}: ピンの自己 |Z_kk| (そのピンだけに 1 A)', fontsize=10)
        ax[0].legend(fontsize=7, loc='upper left'); fig.tight_layout()
        p = os.path.join(a.out, f"z_{rail.replace('/', '_').replace('+', 'p')}.png"); fig.savefig(p, dpi=110); plt.close(fig); pngs.append(p)
        for r in rr:     # ピンごと
            fig, x = plt.subplots(figsize=(7, 4.5))
            x.loglog(Fs, r['zeff'], label='Z_eff (全 ΔI 同時)'); x.loglog(Fs, r['zself'], label='自己 |Z_kk|', alpha=0.7)
            if r['ztarget']:
                x.axhline(r['ztarget'], color='r', ls='--', label=f"Ztarget {r['ztarget'] * 1e3:.1f} mΩ")
            if r.get('f_lim'):
                x.axvline(r['f_lim'], color='r', ls=':', label=f"判定上限 {fmt_f(r['f_lim'])}")
            for b0, b1 in r['over']:
                x.axvspan(b0, b1, color='r', alpha=0.15)
            x.set_xlim(f0, f1); _xt(x); x.grid(True, which='both', alpha=0.3); x.legend(fontsize=7); x.set_xlabel('周波数 [Hz]'); x.set_ylabel('|Z| [Ω]')
            x.set_title(f"{r['net']} {r['name']} — {r['verdict']}", fontsize=10); fig.tight_layout()
            p = os.path.join(a.out, f"pin_{r['name'].replace('.', '_')}.png"); fig.savefig(p, dpi=100); plt.close(fig); pngs.append(p)
    # 指標
    for k, rail in enumerate(cfg['judged']):
        rr = [r for r in rows if r['net'] == rail and r['ztarget'] is not None]
        if not rr:
            Mt(f'PDN{k + 1}', f'{rail} の目標インピーダンス', None, 'skip', '', ['判定できるピンが無い']); continue
        bad = [r for r in rr if r['verdict'] != '合格']
        det = []
        for r in rr:
            det.append(f"{r['name']}: Ztarget {r['ztarget'] * 1e3:.2f} mΩ (予算 {r['budget'] * 1e3 if r['budget'] is not None else 0:.1f} mV / ΣΔI {r['sum_di'] * 1e3:.1f} mA)、"
                       f"0.35/tr {fmt_f(r['f_judge']) if r.get('f_judge') else '-'} → 判定範囲 {fmt_f(r['f_lim']) if r.get('f_lim') else '-'} まで ({dict(tr='0.35/tr', qs='準静的の上限', cfg='設定値').get(r.get('f_lim_by'), '-')} で決定)、{r['verdict']}"
                       + (f"、超過 {', '.join(f'{fmt_f(b0)}〜{fmt_f(b1)}' for b0, b1 in r['over'])}" if r['over'] else ''))
        worst = max(rr, key=lambda r: max(r['zeff'][Fs <= r.get('f_lim', f_qs)] / r['ztarget']) if r['ztarget'] else 1e9)
        val_ = round(float(max(worst['zeff'][Fs <= worst.get('f_lim', f_qs)]) / worst['ztarget']), 3) if worst['ztarget'] else None
        Mt(f'PDN{k + 1}', f'{rail} の Z_eff / Ztarget 最大 (判定範囲内)', val_, 'fail' if bad else 'pass', '≤ 1 (f ≤ min(0.35/tr, 準静的上限))', det)
    over_i = [f"{r['name']}: {', '.join(f'{fmt_f(b0)}〜{fmt_f(b1)}' for b0, b1 in r['over_info'])}" for r in rows if r.get('over_info')]
    Mt('PDN5', '判定上限より上での超過 (参考)', len(over_i), 'info', f'準静的上限 {fmt_f(f_qs)}、USB HS 480 MHz は参考', over_i)
    lowc = [c for c in caps if c['ratio'] < float(v(cfg['dc_bias_warn']))]
    Mt('PDN6', 'DC バイアスによる容量低下 (⑥)', len(lowc), 'warn' if lowc else 'pass', f"実効容量 ≥ 公称の {float(v(cfg['dc_bias_warn'])):.0%}",
       [f"{c['ref']} {c['mpn']} ({c['net']} {c['v_dc']} V): {c['c_nom'] * 1e6:.3g} µF → {c['c_eff'] * 1e6:.3g} µF ({c['ratio']:.0%}、DC バイアス特性は{CP.BASIS.get(c['basis'], c['basis'])})" for c in lowc])
    ine = [c for c in caps if not c['effective']]
    Mt('PDN7', '効いていないコンデンサ (1 個外して判定範囲内の Z_eff 最大値の変化 < 5%)', len(ine), 'info', '', [f"{c['ref']} ({c['net']}, {c['c_nom'] * 1e9:.0f} nF): 最大 {c['sens'] * 100:.2f}%" for c in ine])
    Mt('PDN8', 'ngspice と Python 直接解の差', round(dev * 100, 5), 'pass' if dev < 1e-3 else 'warn', '< 0.1%', [])
    sg = stage_grade(M)
    J = dict(tool='pdn', version=VERSION, project=os.path.splitext(os.path.basename(pcb))[0], dir=os.path.abspath(a.project), out=os.path.abspath(a.out),
             date=datetime.datetime.now().isoformat(timespec='seconds'),
             overall=dict(grade=sg['grade'], score=sg['score'], fail=sg['counts']['fail'], warn=sg['counts']['warn']),
             stages=dict(pdn=dict(title='PDN (電源インピーダンス)', **sg)), metrics=M,
             grid=dict(dx_mm=grid.meta['dx_mm'], shape=list(grid.shape), grid_id=grid.meta['grid_id'], pcb_sha256=grid.meta['pcb_sha256']),
             f_qs_hz=f_qs, f_judge_max=dict(hz=fcap, src=fcap_src, rule='判定上限 = min(0.35/tr, 準静的の上限, 設定値 analysis.f_judge_max)'), eps_r=er, eps_r_src=er_src, notes=notes, extract={k: r['info'] for k, r in RED.items()},
             pins=[{k: (val if not isinstance(val, np.ndarray) else None) for k, val in r.items() if k not in ('zeff', 'zself')} for r in rows],
             caps=caps, pngs=pngs)
    json.dump(J, open(os.path.join(a.out, 'pdn.json'), 'w'), indent=1, ensure_ascii=False, default=lambda o: float(o) if isinstance(o, np.floating) else str(o))
    np.savez(os.path.join(a.out, 'z_curves.npz'), f=Fs, names=np.array([r['name'] for r in rows]), zeff=np.array([r['zeff'] for r in rows]), zself=np.array([r['zself'] for r in rows]))
    md = [f"# PDN — {J['project']} ({VERSION})", '', f"準静的上限 {fmt_f(f_qs)} (εr {er:g}、{er_src})、ngspice と Python の差 {dev * 100:.4f}%", '',
          f"判定上限 = min(0.35/tr, 準静的の上限, 設定値)。設定値: {fcap_src or 'なし'}", '',
          '| 指標 | 値 | 判定 | 基準 |', '|---|---|---|---|'] + [f"| {m['id']} {m['title']} | {m['value']} | {m['grade']} | {m['threshold']} |" for m in M]
    for m in M:
        if m['detail']:
            md += ['', f"**{m['id']}**"] + [f'- {d}' for d in m['detail']]
    md += ['', '## ピン', '', '| ネット | ピン | ΔI mA | ΔI の根拠 | 余裕 mV | IR mV | 予算 mV | ΣΔI mA | Ztarget mΩ | 判定上限 | 判定 | 超過帯域 |', '|' + '---|' * 12]
    for r in rows:
        md.append(f"| {r['net']} | {r['name']} | {r['di'] * 1e3:.2f} | {r['rule']} | {r['margin_mv']} | {r['ir_mv']:.2f} | "
                  f"{'-' if r['budget'] is None else f'{r['budget'] * 1e3:.1f}'} | {r['sum_di'] * 1e3:.1f} | {'-' if r['ztarget'] is None else f'{r['ztarget'] * 1e3:.2f}'} | "
                  f"{r['f_judge_note']} | {r['verdict']} | {', '.join(f'{fmt_f(b0)}〜{fmt_f(b1)}' for b0, b1 in r['over']) or '-'} |")
    md += ['', '## コンデンサ', '', '| 部品 | ネット | 品番 | 公称 | DC | 実効 | ESR mΩ | ESL nH | 外したときの最大変化 | 効いているか |', '|' + '---|' * 10]
    for c in caps:
        md.append(f"| {c['ref']} | {c['net']} | {c['mpn']} | {c['c_nom'] * 1e6:.3g} µF | {c['v_dc']} V | {c['c_eff'] * 1e6:.3g} µF ({c['ratio']:.0%}) | {c['esr'] * 1e3:.0f} | {c['esl'] * 1e9:.2f} | "
                  f"{c['sens'] * 100:.2f}% @ {c['sens_at'][0] if c['sens_at'] else '-'} {fmt_f(c['sens_at'][1]) if c['sens_at'] else ''} | {'○' if c['effective'] else '× 効いていない'} |")
    md += ['', '## モデル', ''] + [f'- {x}' for x in notes] + ['', '## 図', ''] + [f'- `{p}`' for p in pngs]
    open(os.path.join(a.out, 'pdn.md'), 'w').write('\n'.join(md) + '\n')
    for m in M:
        log(f"{m['id']:5s} {m['grade']:5s} {m['value']}  {m['title']}")
        for d in m['detail'][:8]:
            log('       ', d)
    log(f"総合 {sg['grade']} 点数 {sg['score']} → {a.out}/pdn.json, pdn.md")


if __name__ == '__main__':
    main()
