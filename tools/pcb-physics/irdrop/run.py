"""irdrop の実行: 案件の irdrop.yaml + 部品 YAML のピン電流で typ / worst を解き、表・PNG・kreview 形式 JSON を出す。
  python -m irdrop.run <project_dir> --cond irdrop/examples/<案件>/irdrop.yaml --out DIR [--dx 0.1] [--grid grid.npz] [--tmap-typ/--tmap-worst npz]
合否 (worst): 負荷ピンの降下 (行き + GND 帰り) ≤ 余裕 × criterion.ratio (余裕 = 源の電圧 − ピンの許容最小)。typ は info。"""
import argparse, copy, datetime, glob, json, os, sys, time
import numpy as np
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pcbread, grid as CG, parts as CP, boardspec
from irdrop import plating as PL
from core.cases import _pick, PICK, NAME
from irdrop.solver import solve_net, hot_spots
from irdrop.interface import RHO_CU_20, ALPHA_CU

VERSION = '0.1 (2026-10-14)'
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def val(x):
    return None if x is None else (x.get('value') if isinstance(x, dict) else x)


def src_note(x):
    if not isinstance(x, dict):
        return ''
    return f"{CP.BASIS.get(CP.basis_of(x), '?')}: " + str(x.get('from') or x.get('reason') or (x.get('src') or {}).get('item', ''))


def setup_case(board, cfg, lib, case):
    """負荷ピン (行)・ネットごとの sink / fixed の元データ"""
    key = PICK[case]
    c = _pick(copy.deepcopy({k: v for k, v in cfg.items()}), key)
    fps = {f['ref']: f for f in board['footprints']}
    rails = c['rails']; railnets = set(rails)
    loads = []          # dict(ref, pin, net, I, vr_min(valobj), gnd_part, gnd_pins, kind)
    for ref, fp in fps.items():
        pd = lib.get(str(fp['lcsc'])) if fp['lcsc'] else None
        pins = copy.deepcopy((pd or {}).get('pins') or {})
        pins = {str(k): _pick(v, key) if isinstance(v, dict) else v for k, v in pins.items()}
        over = (c.get('parts') or {}).get(ref) or {}
        for pn, pv in (over.get('pins') or {}).items():
            pins.setdefault(str(pn), {}).update(pv)
        padnet = {str(p['num']): p.get('net', '') for p in fp['pads']}
        gnd = [str(x) for x in over.get('gnd') or []] or [k for k, v in pins.items() if isinstance(v, dict) and v.get('net_role') == 'ground']
        for pn, pv in pins.items():
            net = padnet.get(pn, '')
            if net not in railnets or not isinstance(pv, dict):
                continue
            I = val(pv.get('current')) or 0.0
            role = pv.get('net_role', 'power_in')
            loads.append(dict(ref=ref, pin=pn, net=net, I=float(I), I_src=src_note(pv.get('current')), role=role,
                              vmin=(pv.get('v_range') or {}).get('min'), gnd_part=over.get('gnd_ref_part', ref),
                              gnd_pins=gnd, value=fp['value']))
    return c, loads


def source_min(board, lib, r, V):
    """源の最低電圧 (2026-10-14 作者 決定: 合否の余裕からレギュレータの出力精度と負荷変動を引く)。
    rails.<net>.v_min (案件) > 源ピンの部品 YAML: v_out_min (データシートの範囲) > v_out_accuracy + load_regulation (比率)。無ければ公称"""
    if r.get('v_min') is not None:
        return float(val(r['v_min'])), src_note(r['v_min'])
    ref = r['source']['ref']; pin = str(r['source']['pins'][0])
    fp = next((f for f in board['footprints'] if f['ref'] == ref), None)
    pv = (((lib.get(str(fp['lcsc'])) if fp and fp['lcsc'] else None) or {}).get('pins') or {}).get(pin) or {}
    if pv.get('v_out_min') is not None:
        return float(val(pv['v_out_min'])), f"{ref}.{pin} v_out_min {val(pv['v_out_min'])} V ({src_note(pv['v_out_min'])})"
    acc = val(pv.get('v_out_accuracy')); lr = val(pv.get('load_regulation'))
    if acc is not None or lr is not None:
        a = float(acc or 0); b = float(lr or 0)
        return V * (1 - a - b), f"{ref}.{pin} 公称 {V} V × (1 − 出力精度 {a:.1%} − 負荷変動 {b:.1%}) = {V * (1 - a - b):.4f} V (部品 YAML)"
    return V, '公称 (精度の差し引きなし)'


def series_r(board, lib, r, s, case, temp_c, grid, T0):
    """直列素子の抵抗: rails.<net>.r (案件) > 部品 YAML の dc_resistance (無ければ resistance、core/parts.series_resistance)。tc_per_c があれば pad 温度 (温度マップ、無ければ一様温度) で補正"""
    key = PICK[case]
    if r.get('r') is not None:
        x = r['r']; return float(val(x)), src_note(x)
    fp = next(f for f in board['footprints'] if f['ref'] == s['ref'])
    dc, dk = CP.series_resistance(lib.get(str(fp['lcsc'])))
    if not dc:
        raise CP.DataError(f"{s['ref']}: 直列抵抗が irdrop.yaml にも部品 YAML (dc_resistance / resistance) にも無い")
    x = dc[key]; R0 = float(val(x)); note = f"{fp['lcsc']} {dk}.{key} {R0} Ω ({src_note(x)})"
    tc = val(dc.get('tc_per_c'))
    if tc:
        tref = float(val(dc.get('t_ref_c')) or 20.0)
        if temp_c is not None:
            from irdrop.solver import find_pads
            vs = []
            for pn in (s['from_pin'], s['to_pin']):
                for k in find_pads(grid, s['ref'], pn):
                    js, is_, ws = grid.pad_cells(k); vs.append(float(np.nanmean(temp_c[int(grid.a['pad_layer'][k]), js, is_])))
            T = float(np.mean(vs))
        else:
            T = T0
        R = R0 * (1 + float(tc) * (T - tref)); note += f"、温度補正 ×(1 + {tc} × ({T:.1f} − {tref:g})) = {R:.4f} Ω"
        return R, note
    return R0, note + '、温度補正なし'


def run_case(board, grid, cfg, lib, case, temp_c=None, log=print):
    c, loads = setup_case(board, cfg, lib, case)
    rails = c['rails']; order = list(rails)
    T0 = float(val(c['temperature'])) if c.get('temperature') else (25.0 if case == 'typ' else 50.0)
    cond = dict(case=case, ref_temp_c=T0, rho_cu_ohm_m=RHO_CU_20, alpha_per_c=ALPHA_CU)
    sinks = {n: [] for n in order}; fixed = {n: [] for n in order}; feeds = []
    for L_ in loads:
        if L_['role'] in ('regulator_out',):
            continue
        if L_['I']:
            sinks[L_['net']].append((L_['ref'], L_['pin'], L_['I']))
    # 下流から電流の合計 → 上流の入力 (直列素子・レギュレータ入力)
    tot = {}
    for n in reversed(order):
        r = rails[n]; tot[n] = sum(s[2] for s in sinks[n])
        if 'series' in r:
            s = r['series']; sinks[s['upstream']].append((s['ref'], s['from_pin'], tot[n])); feeds.append((s['upstream'], s['ref'], s['from_pin']))
        if 'input' in r:
            i = r['input']; Iin = tot[n] + float(val(i.get('iq')) or 0.0)
            sinks[i['net']].append((i['ref'], i['pin'], Iin)); feeds.append((i['net'], i['ref'], i['pin']))
    # GND の帰り: 終端負荷 (feed でない) の電流をその部品の GND ピンへ等分。レギュレータの Iq はその GND ピンへ
    gsink = []
    feedset = {(r_, p_) for _, r_, p_ in feeds}
    for L_ in loads:
        if L_['role'] == 'regulator_out' or not L_['I'] or (L_['ref'], L_['pin']) in feedset:
            continue
        gp = L_['gnd_pins']
        if not gp:
            log(f"  注意: {L_['ref']} の GND ピン未指定 → 帰りを数えない"); continue
        for p in gp:
            gsink.append((L_['gnd_part'], p, -L_['I'] / len(gp)))
    for n in order:
        i = rails[n].get('input')
        if i and val(i.get('iq')):
            for p in i.get('gnd_pins', []):
                gsink.append((i['ref'], p, -float(val(i['iq'])) / len(i['gnd_pins'])))
    gcfg = c['gnd']; t0 = time.time()
    gres = solve_net(grid, gcfg['net'], temp_c, gsink, [(gcfg['ref']['ref'], p, 0.0) for p in gcfg['ref']['pins']], cond)
    log(f"  [{case}] {gcfg['net']}: 節点 {gres['info']['nodes']}、帰り {-gres['info']['sink_total_a'] * 1e3:.1f} mA、{time.time() - t0:.1f} s")
    def vg(ref, pins):
        vs = [gres['pin_v'].get((ref, str(p))) for p in pins]
        if any(v is None for v in vs):
            r = solve_net  # noqa  (pin が sink/fixed に無い時は pad の値を取り直す)
            from irdrop.solver import find_pads
            out = []
            for p in pins:
                ks = find_pads(grid, ref, p)
                v = []
                for k in ks:
                    js, is_, ws = grid.pad_cells(k); l = int(grid.a['pad_layer'][k]); vv = gres['volt'][l, js, is_]
                    ok = ~np.isnan(vv)
                    if ok.any():
                        v.append(float(np.dot(vv[ok], ws[ok]) / ws[ok].sum()))
                out.append(np.mean(v) if v else np.nan)
            vs = out
        return float(np.nanmean(vs))
    res = {gcfg['net']: gres}; vsrc = {}; root = {}; ser_info = {}
    for n in order:
        r = rails[n]
        if 'series' in r:
            s = r['series']; up = res[s['upstream']]
            Rs, rnote = series_r(board, lib, r, s, case, temp_c, grid, T0)
            ser_info[n] = dict(ref=s['ref'], r_ohm=round(Rs, 5), note=rnote, i_a=tot[n], v_drop_mv=round(tot[n] * Rs * 1e3, 3))
            V = up['pin_v'][(s['ref'], s['from_pin'])] - tot[n] * Rs
            root[n] = root[s['upstream']]; src = [(s['ref'], s['to_pin'], V)]
        else:
            V = float(val(r['v'])); vmin_src, tol_note = source_min(board, lib, r, V)
            root[n] = dict(net=n, v=V, v_min=vmin_src, tol_note=tol_note, gnd_ref=r['gnd_ref'], vg_ref=vg(r['gnd_ref']['ref'], r['gnd_ref']['pins']))
            src = [(r['source']['ref'], p, V) for p in r['source']['pins']]
        vsrc[n] = V; t0 = time.time()
        res[n] = solve_net(grid, n, temp_c, sinks[n], src, cond)
        log(f"  [{case}] {n}: 源 {V:.4f} V、負荷 {res[n]['info']['sink_total_a'] * 1e3:.1f} mA、節点 {res[n]['info']['nodes']}、"
            f"未接続 {res[n]['info']['unconnected_nodes']}、{time.time() - t0:.1f} s")
    rows = []
    for L_ in loads + [dict(ref=i_r, pin=i_p, net=i_n) for i_n, i_r, i_p in feeds if not any(x['ref'] == i_r and x['pin'] == i_p for x in loads)]:
        n = L_['net']
        if L_.get('role') == 'regulator_out' or (L_['ref'], L_['pin']) not in res[n]['pin_v']:
            continue
        rt = root[n]; vp = res[n]['pin_v'][(L_['ref'], L_['pin'])]
        gp = L_.get('gnd_pins') or []
        gpart = L_.get('gnd_part', L_['ref'])
        if not gp:   # feed (直列素子・レギュレータ入力) は 源の GND で見る
            vgl = rt['vg_ref']
            if L_['ref'] == 'U3' and 'input' in rails.get('+3V3', {}):
                vgl = vg('U3', rails['+3V3']['input'].get('gnd_pins', ['2']))
            if L_['ref'] == 'U1':
                vgl = vg('U1', cfg['parts']['U1']['gnd'])
        else:
            vgl = vg(gpart, gp)
        d_pow = rt['v'] - vp; d_gnd = vgl - rt['vg_ref']; drop = d_pow + d_gnd
        vmin = val(L_.get('vmin')); margin = (rt['v_min'] - vmin) if vmin is not None else None   # 余裕 = 源の最低 (出力精度・負荷変動を引いた値) − 許容最小
        I = next((s[2] for s in sinks[n] if s[0] == L_['ref'] and s[1] == L_['pin']), 0.0)
        rows.append(dict(net=n, ref=L_['ref'], pin=L_['pin'], value=L_.get('value', ''), current_ma=round(I * 1e3, 3), v_source=round(rt['v'], 4),
                         source=f"{rt['net']}", v_source_min=round(rt['v_min'], 4), source_tol=rt['tol_note'], drop_power_mv=round(d_pow * 1e3, 3), drop_gnd_mv=round(d_gnd * 1e3, 3), drop_mv=round(drop * 1e3, 3),
                         v_pin=round(rt['v'] - drop, 4), v_min=vmin, vmin_note=src_note(L_.get('vmin')) if L_.get('vmin') else '記載なし (許容値なし)',
                         margin_mv=None if margin is None else round(margin * 1e3, 1),
                         ratio=None if not margin else round(drop / margin, 4)))
    return dict(case=case, temp_c=T0, res=res, rows=rows, vsrc=vsrc, root=root, totals=tot, series=ser_info)


def plot(grid, board, R, outdir, nets, log=print):
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    for f in ('Noto Sans CJK JP', 'Noto Serif CJK JP', 'IPAexGothic', 'IPAGothic', 'Noto Sans JP'):
        if any(f == x.name for x in fm.fontManager.ttflist):
            plt.rcParams['font.family'] = f; break
    m = grid.meta; dx = m['dx_mm']; ext = [m['x0_mm'], m['x0_mm'] + m['nx'] * dx, m['y0_mm'] + m['ny'] * dx, m['y0_mm']]
    paths = []
    for n in nets:
        r = R['res'][n]; isg = n not in R['vsrc']
        data = r['volt'] * 1e3 if isg else (R['vsrc'][n] - r['volt']) * 1e3
        vmax = np.nanmax(data) if np.isfinite(np.nanmax(data)) else 1.0
        for l, ly in enumerate(m['layer_names']):
            fig, ax = plt.subplots(figsize=(8, 6.6))
            ax.imshow(np.where(grid.a['board_frac'] > 0.5, 1.0, np.nan), extent=ext, cmap='Greys', vmin=0, vmax=4, interpolation='nearest')
            other = (grid.a['net_major'][l] > 0) & np.isnan(r['volt'][l])
            ax.imshow(np.where(other, 1.0, np.nan), extent=ext, cmap='Greys', vmin=0, vmax=2.5, interpolation='nearest', alpha=0.6)
            im = ax.imshow(data[l], extent=ext, cmap='viridis' if isg else 'plasma', vmin=0, vmax=max(vmax, 1e-3), interpolation='nearest')
            for fp in board['footprints']:
                if fp['outline']:
                    xs = [p[0] for p in fp['outline']] + [fp['outline'][0][0]]; ys = [p[1] for p in fp['outline']] + [fp['outline'][0][1]]
                    ax.plot(xs, ys, color='white', lw=0.4, alpha=0.6)
                    if fp['ref'][0] in 'UJF' or fp['ref'] == 'FB1':
                        ax.text(fp['x'], fp['y'], fp['ref'], color='white', fontsize=6, ha='center', va='center')
            cb = fig.colorbar(im, ax=ax, shrink=0.8)
            cb.set_label('GND 電位 [mV] (J1 GND 基準)' if isg else f'源からの降下 [mV] (源 {R["vsrc"][n]:.3f} V)')
            ax.set_title(f"{n} {ly} {NAME[R['case']]} 銅 {R['temp_c']:.0f} ℃  最大 {np.nanmax(data[l]) if np.isfinite(np.nanmax(data[l])) else 0:.2f} mV", fontsize=10)
            ax.set_xlabel('x [mm]'); ax.set_ylabel('y [mm]')
            fn = os.path.join(outdir, f"ir_{R['case']}_{n.strip('/').replace('+', 'p').replace('.', '_')}_{'F' if ly == 'F.Cu' else 'B'}.png")
            fig.tight_layout(); fig.savefig(fn, dpi=130); plt.close(fig); paths.append(fn)
    return paths


def metrics(Rw, Rt, grid, ratio_lim, primary):
    M = []
    def Mt(id, title, value, grade, threshold='', detail=None):
        M.append(dict(stage='irdrop', id=id, title=title, value=value, grade=grade, threshold=threshold, detail=list(detail or []), source='pcb-physics irdrop'))
    for k, n in enumerate(primary):
        rows = [r for r in Rw['rows'] if r['net'] == n]
        ok = [r for r in rows if r['ratio'] is not None]
        if not ok:
            Mt(f'IR{k + 1}', f'{n} の電圧降下 (worst)', None, 'skip', f'≤ 余裕の {ratio_lim:.0%}', ['許容電圧の分かるピンが無い']); continue
        w = max(ok, key=lambda r: r['ratio'])
        tw = next((r for r in Rt['rows'] if r['ref'] == w['ref'] and r['pin'] == w['pin']), None)
        assumed = [r for r in ok if r['vmin_note'].startswith('仮定')]
        Mt(f'IR{k + 1}', f'{n} の電圧降下 (worst、行き + GND 帰り)', round(w['ratio'] * 100, 2), 'pass' if w['ratio'] <= ratio_lim else 'fail',
           f'≤ 許容電圧余裕の {ratio_lim:.0%}',
           [f"最悪 {w['ref']}.{w['pin']} ({w['net']}): 降下 {w['drop_mv']:.2f} mV (行き {w['drop_power_mv']:.2f} + GND {w['drop_gnd_mv']:.2f}) / 余裕 {w['margin_mv']:.0f} mV (源の最低 {w['v_source_min']} V − 許容最小 {w['v_min']} V) = {w['ratio'] * 100:.2f}%",
            f"源の最低: {w['source_tol']}",
            f"typ: {tw['drop_mv']:.2f} mV ({tw['ratio'] * 100:.2f}%)" if tw and tw['ratio'] is not None else 'typ: -',
            f"対象ピン {len(ok)} 本、うち許容値が仮定 {len(assumed)} 本"])
    hs = []
    for n, r in Rw['res'].items():
        h = hot_spots(grid, r, 3)
        if h:
            hs.append(f"{n}: " + ', '.join(f"{x['j_a_mm2']:.1f} A/mm² ({x['layer']} {x['x_mm']:.1f},{x['y_mm']:.1f})" for x in h))
    jmax = max((float(np.max(J[-1])) for r in Rw['res'].values() for J in r['jedges'] if len(J[-1])), default=0)
    Mt('IR8', '最大電流密度 (worst)', round(jmax, 2), 'info', '参考 (外層 1 oz で数十 A/mm² 以下なら発熱は小さい)', hs)
    jw = sum(r['info']['joule_w'] for r in Rw['res'].values())
    Mt('IR9', 'ジュール熱 合計 (worst)', round(jw * 1e3, 3), 'info', 'mW', [f"{n}: {r['info']['joule_w'] * 1e3:.3f} mW" for n, r in Rw['res'].items()])
    w = grid.meta.get('warnings', []) + sorted({x for r in Rw['res'].values() for x in r['info']['warnings']})
    Mt('IR10', '格子・接続の警告', len(w), 'warn' if any('衝突' in x or '除外' in x for x in w) and False else 'info', '', w)
    return M


def stage_grade(ms):
    c = {g: sum(1 for m in ms if m['grade'] == g) for g in ('pass', 'warn', 'fail', 'skip', 'info', 'waived')}
    n = c['pass'] + c['warn'] + c['fail']
    score = round((c['pass'] + 0.5 * c['warn']) / n * 100) if n else None
    grade = '-' if n == 0 else 'A' if c['fail'] == 0 and c['warn'] <= 1 else 'B' if c['fail'] == 0 else 'C' if c['fail'] <= 2 else 'D'
    return dict(counts=c, score=score, grade=grade)


def to_md(R2, M, grid, cfg, pngs, st):
    out = [f"# IR ドロップ — {grid.meta['pcb']} ({VERSION})", '',
           f"格子 dx {grid.meta['dx_mm']} mm ({grid.shape})、銅厚 {[l['cu_mm'] for l in grid.meta['layers']]} mm ({grid.meta['stackup_src']})、"
           f"めっき {grid.meta['plating_mm'] * 1e3:g} µm ({grid.meta.get('plating_src', '格子の既定')})、ρ(T) = 1.72e-8 (1 + 0.00393 (T − 20))。合否は worst: {cfg['criterion']['from']}", '',
           '| 指標 | 値 | 判定 | 基準 |', '|---|---|---|---|']
    out += [f"| {m['id']} {m['title']} | {m['value']} | {m['grade']} | {m['threshold']} |" for m in M]
    for m in M:
        if m['detail']:
            out += ['', f"**{m['id']}**"] + [f"- {d}" for d in m['detail']]
    for R in R2:
        out += ['', f"## ピンごとの降下 — {NAME[R['case']]} (銅 {R['temp_c']:.0f} ℃)", '',
                '| ネット | ピン | 部品 | 電流 mA | 行き mV | GND mV | 計 mV | ピン電圧 V | 許容最小 V | 余裕 mV | 余裕比 | 許容値の根拠 |', '|' + '---|' * 12]
        for r in sorted(R['rows'], key=lambda r: (r['net'], -(r['ratio'] or 0))):
            out.append(f"| {r['net']} | {r['ref']}.{r['pin']} | {r['value']} | {r['current_ma']} | {r['drop_power_mv']:.2f} | {r['drop_gnd_mv']:.2f} | {r['drop_mv']:.2f} | "
                       f"{r['v_pin']:.4f} | {r['v_min'] if r['v_min'] is not None else '-'} | {r['margin_mv'] if r['margin_mv'] is not None else '-'} | "
                       f"{'-' if r['ratio'] is None else f'{r['ratio'] * 100:.2f}%'} | {r['vmin_note'][:60]} |")
        out += ['', f"### 電流密度の高い箇所 — {NAME[R['case']]}", '', '| ネット | A/mm² | 層 | x mm | y mm |', '|---|---|---|---|---|']
        for n, r in R['res'].items():
            for h in hot_spots(grid, r, 5):
                out.append(f"| {n} | {h['j_a_mm2']:.2f} | {h['layer']} | {h['x_mm']} | {h['y_mm']} |")
    out += ['', '## 図', ''] + [f"- `{p}`" for p in pngs]
    out += ['', '## 仮定・未計上', '', '- 源のピン (pad 全面) を等電位とする。負荷電流は pad のセルに面積で配る。',
            '- 斜め配線は 4 近傍の階段近似で抵抗を大きめ (dx 0.1 mm の 0.2 mm 線で最大 +12%、安全側) に出す (tests/test_irdrop_analytic.py 6d)。',
            '- 未計上: プルアップ (計 約 1.5 mA)、U2、Q1。USB シールド SH は GND の帰りに数えない。銅温度は一様 (自己発熱なし)。']
    return '\n'.join(out) + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project'); ap.add_argument('--cond', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--dx', type=float, default=0.1); ap.add_argument('--grid'); ap.add_argument('--parts', default=os.path.join(HERE, 'data', 'parts'))
    ap.add_argument('--tmap-typ'); ap.add_argument('--tmap-worst')
    ap.add_argument('--board', help='基板 YAML (省略時 irdrop.yaml の board:、無ければ data/board/<基板名>*.yaml)')
    a = ap.parse_args()
    pcb = [p for p in glob.glob(os.path.join(a.project, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
    board = pcbread.read_pcb(pcb); cfg = yaml.safe_load(open(a.cond, encoding='utf-8'))
    CP.walk(cfg, os.path.basename(a.cond), CP.check_value); CP.check_pins((cfg.get('parts') or {}).get('U4', {}), 'irdrop.yaml')
    lib = CP.load_library(a.parts)
    os.makedirs(a.out, exist_ok=True)
    bp = a.board or (os.path.join(os.path.dirname(os.path.abspath(a.cond)), cfg['board']) if cfg.get('board') else None) \
        or boardspec.find(os.path.splitext(os.path.basename(pcb))[0], os.path.join(HERE, 'data', 'board'))
    bs = boardspec.load(bp) if bp else None
    print(f'基板 YAML: {bp}')
    if a.grid:
        grid = CG.PcbGrid.load(a.grid)
        if grid.meta['pcb_sha256'] != __import__('hashlib').sha256(open(pcb, 'rb').read()).hexdigest():
            sys.exit('格子が .kicad_pcb と違う (pcb_sha256)')
        st = None
    else:
        grid, st = CG.build(board, 'artwork', a.dx, 4, board_spec=bs)
    PL.apply(grid, cfg, bs, log=print)      # 案件 YAML > 基板 YAML > core 既定 (--grid で読んだ格子にも上書き)
    print(f"格子 {grid.shape} dx {grid.meta['dx_mm']} mm、警告 {len(grid.meta.get('warnings', []))}")
    R2 = []
    for case, tm in (('typ', a.tmap_typ), ('worst', a.tmap_worst)):
        T = CG.load_map(tm, grid.meta, 'temperature')[0] if tm else None
        R2.append(run_case(board, grid, cfg, lib, case, T))
    primary = list(cfg['rails'])
    pngs = []
    for R in R2:
        pngs += plot(grid, board, R, a.out, [n for n in primary if 'series' not in cfg['rails'][n]] + [cfg['gnd']['net']])
        CG.save_map(os.path.join(a.out, f"heat_{R['case']}_artwork.npz"), grid.meta, 'heat', sum(r['heat'] for r in R['res'].values()), 'W',
                    extra=dict(source='irdrop ジュール熱', case=R['case']))
    M = metrics(R2[1], R2[0], grid, cfg['criterion']['ratio'], primary)
    sg = stage_grade(M)
    J = dict(tool='irdrop', version=VERSION, project=os.path.splitext(os.path.basename(pcb))[0], dir=os.path.abspath(a.project), out=os.path.abspath(a.out),
             date=datetime.datetime.now().isoformat(timespec='seconds'),
             overall=dict(grade=sg['grade'], score=sg['score'], fail=sg['counts']['fail'], warn=sg['counts']['warn']),
             stages=dict(irdrop=dict(title='IR ドロップ', **sg)), metrics=M,
             grid=dict(plating_mm=grid.meta['plating_mm'], plating_src=grid.meta.get('plating_src'), board_yaml=bp, dx_mm=grid.meta['dx_mm'], shape=list(grid.shape), grid_id=grid.meta['grid_id'], pcb_sha256=grid.meta['pcb_sha256'],
                       stackup_src=grid.meta['stackup_src'], checks={k: v for k, v in grid.meta.get('checks', {}).items() if k != 'islands'}),
             rows={R['case']: R['rows'] for R in R2}, pngs=pngs)
    json.dump(J, open(os.path.join(a.out, 'irdrop.json'), 'w'), indent=1, ensure_ascii=False, default=str)
    open(os.path.join(a.out, 'irdrop.md'), 'w').write(to_md(R2, M, grid, cfg, pngs, st))
    for m in M:
        print(f"{m['id']:5s} {m['grade']:5s} {m['value']}  {m['title']}")
        for d in m['detail'][:4]:
            print('       ', d)
    print(f"総合 {sg['grade']} 点数 {sg['score']} → {a.out}/irdrop.json, irdrop.md")


if __name__ == '__main__':
    main()
