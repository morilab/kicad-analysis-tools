"""report — 判定、PNG (表・裏)、thermal.md、thermal.json (kreview の Metric 形式)"""
import json, datetime
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager as _fm
for _f in ('Noto Sans CJK JP', 'Noto Serif CJK JP', 'IPAexGothic'):
    if any(_f in x.name for x in _fm.fontManager.ttflist):
        plt.rcParams['font.family'] = _f; break
from matplotlib.patches import Polygon

ERR_BOARD = 0.15    # 基板の温度上昇の誤差 (h の相関式など)、上側で判定
ERR_DERIVED = 0.30  # θ が逆算・仮定の部品
ERR_FLOOR = 5.0     # ℃ 誤差の下限


def judge(rows, t_air):
    for r in rows:
        T = r['tj'] if r['tj'] is not None else r['t_pad']
        derived = bool(r.get('path') and r['path']['derived']) or r['tj'] is None
        e = ERR_DERIVED if derived else ERR_BOARD
        r['err_frac'] = e
        r['t_upper'] = T + max(e * (T - t_air), ERR_FLOOR)
        lim = r.get('tj_max')
        if r.get('no_thermal'):
            r['grade'] = 'info'; r['margin'] = None; r['why'] = '部品 YAML に熱データなし (ピン電流・信号だけ)。pad 温度のみ (情報のみ)'; continue
        if lim is None:
            r['grade'] = 'info'; r['margin'] = None; r['why'] = '上限温度なし (情報のみ)'; continue
        r['margin'] = lim - r['t_upper']
        g = 'pass' if r['t_upper'] <= lim - 25 else 'warn' if r['t_upper'] <= lim else 'fail'
        why = f'上側 {r["t_upper"]:.1f} ℃ (推定 {T:.1f} + 誤差 {e * 100:.0f}% / 最小 {ERR_FLOOR:.0f} ℃)、基準 ≤{lim - 25:.0f} pass / ≤{lim:.0f} warn'
        if g == 'pass' and derived:
            g = 'warn'; why += '。θ が逆算/仮定のため warn 止まり'
        r['grade'] = g; r['why'] = why
    return rows


def _git_rev():
    import subprocess, os
    d = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        h = subprocess.run(['git', '-C', d, 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', d, 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True).stdout.strip()
        return (h or '?') + ('+変更あり' if dirty else '')
    except Exception:
        return '?'


def conditions_text(res, board, cfg, mode):
    """PNG 下部の詳細条件欄 (固定値)"""
    import os
    from . import __version__
    from .pcbmodel import H_BODY_TOP
    info = res['info']; e = info['env']; st = info['stackup']; c = e['case']
    cu = ', '.join(f"{n} {t * 1000:.1f}µm" for n, t in zip(board['cu_layers'], st['cu']))
    L = [f"基板: 板厚 {board['thickness']} mm / 銅厚 {cu} / 誘電体 {', '.join(f'{d:.3f}' for d in st['diel'])} mm / 層構成の出典: {st['src']}" + (f" / 食い違い: {'; '.join(st['warnings'])}" if st.get('warnings') else ''),
         f"銅の面積率: " + ', '.join(f'{k} {v * 100:.0f}%' for k, v in info['cu_fraction'].items()) + f" / ビア・めっき穴 {info['n_holes']} 個 (めっき 18 µm) / 格子 {info['grid_mm']} mm ({info['nx']}×{info['ny']}、未知数 {info['unknowns']}) / 面内伝導 {'セル中心間の経路の銅 (edge)' if info.get('inplane') == 'edge' else 'セルの面積率 (cell)'}",
         f"熱伝達率: 上面 対流 {info['h']['top']['h_conv']:.1f} + 放射 {info['h']['top']['h_rad_mean']:.1f}、下面 対流 {info['h']['bottom']['h_conv']:.1f} + 放射 {info['h']['bottom']['h_rad_mean']:.1f}、部品上面 {H_BODY_TOP:.0f} (仮定) W/m²K / 放射率 上 {e['emissivity']['top']}・下 {e['emissivity']['bottom']}",
         ('ケース詳細: ' + f"内部空気 +{c['internal_air_rise_c']} ℃、h×{c['h_factor']}、壁 +{c['wall_rise_c']} ℃、壁 ε {c['wall_emissivity']} / " if c['enabled'] else '') +
         (f"気流の向き {e['airflow'].get('direction', 'x')} 方向 / " if float(e['airflow']['velocity_m_s'] or 0) > 0 else '') +
         f"材料: 銅 385、FR4 面内 0.81・厚さ方向 0.29 W/m·K / 熱収支誤差 {info['balance_err'] * 100:.3f}%、反復 {info['iterations']} 回",
         f"環境: {'自然空冷' if float(e['airflow']['velocity_m_s'] or 0) == 0 else '風速 ' + str(e['airflow']['velocity_m_s']) + ' m/s'} / 向き {e['orientation']} / ケース{'あり' if c['enabled'] else 'なし'} (周囲温度・格子はタイトル)"]
    L.append('部品 (消費電力 / 熱経路 / 出典区分):')
    parts = []
    for r in sorted(res['rows'], key=lambda r: -r['P']):
        from core import parts as _PT
        kinds = {_PT.basis_of(x) for _, x, _, _ in r.get('p_items', [])}
        pa = '仮定あり' if 'assumed' in kinds else ('データシート' if kinds == {'datasheet'} else 'データシート/案件条件')
        if r.get('path'):
            th = f"{r['path']['kind']} {r['path']['theta']:.0f}℃/W [{'仮定/逆算' if r['path']['derived'] else 'データシート'}]"
        else:
            th = '熱データなし (pad 温度)'
        parts.append(f"{r['ref']} {r['P'] * 1000:.0f} mW [{pa}] {th}")
    for i in range(0, len(parts), 2):
        L.append('  ' + '   |   '.join(parts[i:i + 2]))
    L.append('部品ごとの値・区分 (データシート/仮定/基板・3D)・出典ページ・仮定の計算過程: parts_table.csv、thermal.md「解析条件の部品表」')
    L.append(f"pcb-physics thermal {__version__} (commit {_git_rev()}) / 入力 {os.path.basename(board['path'])}、{cfg.get('_cond_file', 'thermal.yaml')} / 実行 {datetime.datetime.now():%Y-%m-%d %H:%M} JST")
    return L


PLOT_DEFAULT = dict(tmin_c=25.0, tmax_c=95.0, cmap='inferno', over_color='#9b30ff')   # 紫 (inferno の高温端の淡黄と区別)


def plot(res, board, path_png, face, vmin, vmax, title, cond_lines, plot_cfg=None):
    g = res['grid']; F = res['fields'][face]
    W = 9.0; hmap = W * g.ny / g.nx; hcond = 0.15 * len(cond_lines) + 0.1
    fig = plt.figure(figsize=(W, hmap + hcond + 0.9), dpi=130)
    ax = fig.add_axes([0.08, (hcond + 0.5) / (hmap + hcond + 0.9), 0.78, hmap / (hmap + hcond + 0.9)])
    cax = fig.add_axes([0.88, (hcond + 0.5) / (hmap + hcond + 0.9), 0.025, hmap / (hmap + hcond + 0.9)])
    ext = [g.x0, g.x0 + g.nx * g.dx, g.y0 + g.ny * g.dx, g.y0]
    pc = PLOT_DEFAULT | (plot_cfg or {})
    cmap = plt.get_cmap(pc['cmap']).copy(); cmap.set_over(pc['over_color']); cmap.set_under(cmap(0.0))
    vmin, vmax = float(pc['tmin_c']), float(pc['tmax_c'])
    im = ax.imshow(F, extent=ext, cmap=cmap, vmin=vmin, vmax=vmax, interpolation='nearest')
    if np.nanmax(F) > vmax:   # 紫は inferno の中間 (約 45 ℃) の紫と紛れるので、上限超えの領域に白い斜線を重ねる
        plt.rcParams['hatch.color'] = 'white'
        ax.contourf(g.xc, g.yc, np.nan_to_num(F, nan=vmin), levels=[vmax, 1e9], colors='none', hatches=['////'])
        ax.contour(g.xc, g.yc, np.nan_to_num(F, nan=vmin), levels=[vmax], colors='white', linewidths=0.6)
    for poly in board['outline']:
        ax.add_patch(Polygon(poly, closed=True, fill=False, ec='cyan', lw=1))
    side = 'F.Cu' if face == 'F' else 'B.Cu'
    for fp in board['footprints']:
        if len(fp['outline']) < 3:
            continue
        mine = fp['side'] == side
        ax.add_patch(Polygon(fp['outline'], closed=True, fill=False, ec='white' if mine else 'gray', lw=0.7 if mine else 0.4,
                             ls='-' if mine else ':'))
        if mine:
            ax.text(fp['x'], fp['y'], fp['ref'], color='white', fontsize=5, ha='center', va='center')
    j, i = np.unravel_index(np.nanargmax(F), F.shape)
    ax.plot(g.xc[i], g.yc[j], 'c+', ms=10); ax.text(g.xc[i] + 0.8, g.yc[j] - 0.8, f'{F[j, i]:.1f}°C', color='cyan', fontsize=7)
    if face == 'B':
        ax.invert_xaxis()
    ax.set_title(title, fontsize=10); ax.set_xlabel('x [mm]' + (' (裏から見た向き)' if face == 'B' else ''), fontsize=8); ax.set_ylabel('y [mm]', fontsize=8)
    ax.tick_params(labelsize=7)
    cb = fig.colorbar(im, cax=cax, extend='max', label=f"°C ({pc['tmax_c']:g} ℃ 以上は紫 + 白斜線、{pc['tmin_c']:g} ℃ 未満は最下端色)")
    fig.text(0.02, (hcond + 0.12) / (hmap + hcond + 0.9), '\n'.join(cond_lines), fontsize=6.3, va='top', ha='left', family=plt.rcParams['font.family'],
             bbox=dict(boxstyle='square,pad=0.4', fc='#f4f4f4', ec='#999999', lw=0.5))
    fig.savefig(path_png); plt.close(fig)


def title_of(cfg, mode, face, env, grid_mm=None):
    """タイトルは振った条件だけ (ケース・周囲温度・配置/アートワーク・面・格子)。固定の条件 (空冷・向き・ケース) は下の詳細条件欄"""
    t1 = f"{cfg.get('project', '')} rev{cfg.get('rev', '')} [{cfg.get('case_name', '最悪 (worst)')}] — {'配置案 (pad のみ)' if mode == 'placement' else 'アートワーク後'} / {'表面 F.Cu' if face == 'F' else '裏面 B.Cu'}"
    t2 = f"周囲 {env['ambient_c']}℃" + (f" / 格子 {grid_mm} mm" if grid_mm else '')
    return t1 + '\n' + t2


def metrics(res, mode, case='worst'):
    M = []; tag = ('TA' if mode == 'artwork' else 'TP') + ('' if case == 'worst' else 'T')
    for n, r in enumerate(sorted(res['rows'], key=lambda r: -(r['tj'] or r['t_pad']))):
        T = r['tj'] if r['tj'] is not None else r['t_pad']
        d = [f"P {r['P']:.3f} W ({r['p_note']})、出典 {r['p_src']}"]
        if r.get('path'):
            d.append(f"熱経路 {r['path']['kind']} = {r['path']['theta']:.1f} ℃/W ({r['path']['src']})")
        else:
            d.append('熱データなし: pad 直下に発熱を置き、pad の最高温度を表示')
        d += [f"パッケージ上面 {r['t_case_top']:.1f} ℃ ({r['t_case_how']})", r['why']] + [f'仮定: {a}' for a in (r.get('p_assumed') or []) + (r.get('data_assumed') or [])]
        M.append(dict(stage='thermal', id=f'{tag}{n + 1:02d}', title=f"{r['ref']} {r['mpn'] or r['value']} {'Tj' if r['tj'] is not None else 'pad 温度'} ({mode}, {case})",
                      value=round(T, 1), grade=r['grade'] if case == 'worst' else 'info',
                      threshold=f"上側 ≤ Tj上限-25 pass / ≤ Tj上限 warn (Tj上限 {r.get('tj_max')} ℃: {r.get('tj_max_src', '')})" if r.get('tj_max') else '',
                      detail=d, source=r.get('data_file', '')))
    return M


def write(res, board, cfg, out, mode, vrange, case='worst'):
    import os
    os.makedirs(out, exist_ok=True)
    info = res['info']; rows = res['rows']
    pngs = {}
    cond = conditions_text(res, board, cfg, mode)
    for face in 'FB':
        p = os.path.join(out, f'temp_{case}_{mode}_{face}.png')
        plot(res, board, p, face, *vrange, title_of(cfg, mode, face, info['env'], info.get('grid_mm')), cond, cfg.get('plot'))
        pngs[face] = p
    np.savez_compressed(os.path.join(out, f'temp_{case}_{mode}.npz'), F=res['fields']['F'], B=res['fields']['B'],
                        x0=res['grid'].x0, y0=res['grid'].y0, dx=res['grid'].dx)
    M = metrics(res, mode, case)
    js = dict(tool="pcb-physics/thermal", version=__import__("thermal").__version__, date=datetime.datetime.now().isoformat(timespec='seconds'),
              project=cfg.get('project'), rev=cfg.get('rev'), mode=mode, case=case, pcb=board['path'],
              conditions=dict(ambient_c=info['env']['ambient_c'], orientation=info['env']['orientation'],
                              airflow=info['env']['airflow'], case=info['env']['case'], emissivity=info['env']['emissivity'],
                              h={k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in info['h'].items()}),
              stackup=info['stackup'], grid=dict(mm=info['grid_mm'], nx=info['nx'], ny=info['ny'], unknowns=info['unknowns']),
              solver=dict(iterations=info['iterations'], P_total_w=round(info['P_total'], 4), P_out_w=round(info['P_out'], 4),
                          balance_err=info['balance_err']),
              max_board_temp={f: round(float(np.nanmax(res['fields'][f])), 1) for f in 'FB'},
              copper_fraction=info['cu_fraction'],
              metrics=M, png=pngs,
              overall=dict(fail=sum(m['grade'] == 'fail' for m in M), warn=sum(m['grade'] == 'warn' for m in M)))
    return js


def md(R, cfg, out):
    """R: {(case, mode): (js, res)}"""
    import os
    from . import partstable
    L = [f"# 熱解析: {cfg.get('project')} rev{cfg.get('rev')} (pcb-physics thermal 0.2.0)", '',
         f"- 日時: {datetime.datetime.now():%Y-%m-%d %H:%M} (JST)、手法: 2.5D 有限差分 (銅層 + 誘電体中央の節点層、自然対流/強制対流 + 放射、反復)",
         '- 条件: 毎回 **typ (標準)** と **worst (最悪)** の 2 つを計算。**合否は worst で判定** (typ は参考)。',
         '- 判定: 推定値に誤差を足した「上側」で判定 (基板 +15%、θ 逆算/仮定の部品 +30%、最小 +5 ℃)。上側 ≤ Tj上限-25 ℃ pass、≤ Tj上限 warn、超えると fail。θ が逆算/仮定の部品は warn 止まり。',
         '- 上面温度: パッケージ上面の推定 (触れるかの目安。60 ℃ 前後で「熱い」、70 ℃ 超は長く触れない)。',
         '- 段階: ' + '・'.join({'placement': '配置案 (pad のみ)', 'artwork': 'アートワーク後'}[m] for m in ('placement', 'artwork') if ('worst', m) in R) +
         (' (--mode で 1 つだけ解いた)' if sum(('worst', m) in R for m in ('placement', 'artwork')) == 1 else ''), '']
    for mode in ('placement', 'artwork'):
        if ('worst', mode) not in R:
            continue
        jt, rt = R[('typ', mode)]; jw, rw = R[('worst', mode)]
        L += [f"## {'配置案 (pad のみ)' if mode == 'placement' else 'アートワーク後 (配線・ベタ・ビア)'}", '']
        for case, (js, res) in (('typ', (jt, rt)), ('worst', (jw, rw))):
            info = res['info']
            L += [f"- **{case}**: 周囲 {info['env']['ambient_c']} ℃、{info['env']['orientation']}、風速 {info['env']['airflow']['velocity_m_s']} m/s、ケース {'あり' if info['env']['case']['enabled'] else 'なし'}、P 合計 {info['P_total']:.3f} W、"
                  f"h 上 {info['h']['top']['h_conv']:.1f}+{info['h']['top']['h_rad_mean']:.1f} / 下 {info['h']['bottom']['h_conv']:.1f}+{info['h']['bottom']['h_rad_mean']:.1f} W/m²K、"
                  f"基板最高 表 {js['max_board_temp']['F']} / 裏 {js['max_board_temp']['B']} ℃、反復 {info['iterations']}、熱収支誤差 {info['balance_err'] * 100:.3f}%、PNG `{os.path.basename(js['png']['F'])}` `{os.path.basename(js['png']['B'])}`"]
        info = rw['info']
        L += [f"- 共通: 層構成 銅 {info['stackup']['cu']} mm・誘電体 {info['stackup']['diel']} mm ({info['stackup']['src']})、銅の面積率 " + ', '.join(f'{k} {v * 100:.0f}%' for k, v in info['cu_fraction'].items()) +
              f"、ビア/めっき穴 {info['n_holes']} 個、格子 {info['grid_mm']} mm ({info['nx']}×{info['ny']}、未知数 {info['unknowns']})", '',
              '| 部品 | 品番 | 熱経路 | P typ (W) | P worst (W) | Tj typ (℃) | Tj worst (℃) | 上側 worst (℃) | Tj上限 | 余裕 worst (℃) | 判定 (worst) | 上面 typ / worst (℃) |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|']
        tmap = {r['ref']: r for r in rt['rows']}
        for r in sorted(rw['rows'], key=lambda r: -(r['tj'] or r['t_pad'])):
            t = tmap[r['ref']]
            pth = f"{r['path']['kind']} {r['path']['theta']:.1f}" if r.get('path') else '(pad 温度)'
            T = lambda x: x['tj'] if x['tj'] is not None else x['t_pad']
            mg = '-' if r['margin'] is None else f"{r['margin']:.1f}"
            L.append(f"| {r['ref']} | {r['mpn'] or r['value']} | {pth} | {t['P']:.3f} | {r['P']:.3f} | {T(t):.1f} | {T(r):.1f} | {r['t_upper']:.1f} | {r.get('tj_max') or '-'} | {mg} | {r['grade']} | {t['t_case_top']:.1f} / {r['t_case_top']:.1f} |")
        L.append('')
    pm = 'artwork' if ('worst', 'artwork') in R else 'placement'   # 部品表は artwork を優先 (--mode placement だけの時は placement)
    T = partstable.build_both(R[('typ', pm)][1]['rows'], R[('worst', pm)][1]['rows'])
    from core import boardspec, parts as PT
    st = R[('worst', pm)][1]['info']['stackup']
    T = [['基板', '(層構成)', '層構成の出典 (優先: thermal.yaml stackup > 基板 YAML > .kicad_pcb stackup > JLC 標準)', '同左', '同左', '', PT.BASIS['assumed'] if st['assumed'] else PT.BASIS['design'],
          st['src'] + (' / 食い違い: ' + '; '.join(st['warnings']) if st.get('warnings') else '')]] + \
        ([[r[0], r[1], r[2], r[3], r[3], r[4], r[5], r[6]] for r in boardspec.table_rows(cfg['_board_spec'])] if cfg.get('_board_spec') else []) + T
    partstable.write_csv(T, os.path.join(out, 'parts_table.csv'))
    L += ['## 解析条件の部品表', '', '部品ごと・項目ごとの値 (typ / worst) と区分。備考はデータシート値なら資料・版・URL・ページ・表、仮定値なら根拠と計算式 (typ と worst で出典が違う時は両方)。同じ内容を `parts_table.csv` (UTF-8 BOM 付き) に出す。', '']
    L += partstable.md(T) + ['']
    open(os.path.join(out, 'thermal.md'), 'w').write('\n'.join(L) + '\n')
