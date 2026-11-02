"""partstable — 解析条件の部品表 (部品ごと・項目ごとに 値 / 区分 / 備考)。CSV と thermal.md に出す。
区分: データシート値 / 仮定値 / 基板・3Dモデル由来 / 案件条件 / 計算値 / 記載なし。備考に出典 (資料・版・URL・ページ・表) か 根拠と計算式。"""
import csv
from core import parts as PT
from .pcbmodel import H_BODY_TOP

COLS = ['部品', '品番', '項目', '値 typ', '値 worst', '単位', '区分', '備考']
THETAS = [('ja', 'θJA (接合-周囲)'), ('jb', 'θJB (接合-基板)'), ('jc_bottom', 'θJC (接合-底/タブ)'), ('jc_top', 'θJC (接合-上面)'), ('psi_jb', 'Ψ_JB')]


def _fmt(v, unit):
    if v is None:
        return '—'
    if isinstance(v, bool):
        return 'あり' if v else 'なし'
    if unit == 'A':
        return f'{float(v) * 1000:g} mA'
    return f'{v:g}' if isinstance(v, (int, float)) else str(v)


def build(rows):
    T = []
    for r in rows:
        fp, pd, ds = r['fp'], r.get('pd'), (r.get('pd') or {}).get('datasheet')
        mpn = r['mpn'] or r['value']

        def add(item, value, unit, kind, note):
            T.append([r['ref'], mpn, item, value, unit, kind, note])
        add('配置 (面・位置・回転)', f"{fp['side']} ({fp['x']:.2f}, {fp['y']:.2f}) {fp['rot']:g}°", 'mm', PT.BASIS['board'],
            f".kicad_pcb footprint {fp['footprint']}、LCSC {fp['lcsc'] or '-'}")
        add('部品熱データ', pd['_file'] if pd else '未登録', '', PT.BASIS['board'] if not pd else '—',
            (f"data/parts/{pd['_file']} (資料: {PT.doc_text(ds)})" + ('。熱データなし (ピン電流・信号だけ): pad 直下に発熱を置き pad 温度だけを出す、判定は info' if not PT.has_thermal(pd) else '')) if pd else '熱抵抗なし: pad 直下に発熱を置き pad 温度だけを出す (Tj は出さない)')
        # 電力
        add('消費電力 P', f"{r['P']:.4f}", 'W', PT.BASIS['calc'], r['p_note'])
        for name, x, unit, dsx in r['p_items']:
            add(name, _fmt(PT._v(x), unit), '' if unit == 'A' else unit, PT.label(x), PT.note(x, dsx))
        for n in r.get('notes') or []:
            add('電力の扱い', '', '', PT.BASIS['assumed'], n)
        # 熱抵抗
        if pd:
            th = pd.get('theta') or {}
            for k, nm in THETAS:
                if k in th:
                    add(nm, _fmt(PT._v(th[k]), ''), '℃/W', PT.label(th[k]), PT.note(th[k], ds))
            if r.get('path'):
                add('使用した熱経路 (接合 → 基板)', f"{r['path']['theta']:.1f}", '℃/W', PT.BASIS['calc'],
                    f"{r['path']['kind']} を採用 (優先順位 θJB > Ψ_JB > θJC(底/タブ) > θJA×{PT.THETA_JA_TO_JB})。{r['path']['calc']}")
                add('上面の経路 (接合 → 上面 → 空気)', f"{1 / r['g_air']:.0f}" if r.get('g_air') else '—', '℃/W', PT.BASIS['calc'],
                    r.get('g_air_calc', '') + (f"。h = {H_BODY_TOP} W/m²K は仮定 (自然対流 約 8 + 放射 約 7)" if r.get('g_air') else ''))
            if pd.get('tj_max'):
                add('最大 Tj (部品)', _fmt(PT._v(pd['tj_max']), ''), '℃', PT.label(pd['tj_max']), PT.note(pd['tj_max'], ds))
            for k, nm in (('exposed_pad', '露出パッド'), ('tab', 'タブ')):
                if k in pd:
                    add(nm, _fmt(PT._v(pd[k]), ''), '', PT.label(pd[k]), PT.note(pd[k], ds))
            w = (pd.get('heat_path') or {}).get('why')
            if w:
                add('熱を流す pad の選び方', PT._v(w), '', PT.label(w), PT.note(w))
        lim = r.get('tj_lim_obj')
        if lim is not None and r.get('spec', {}).get('tj_limit'):
            add('判定に使う Tj 上限', _fmt(PT._v(lim), ''), '℃', PT.label(lim), PT.note(lim))
        # pad と熱源配置
        tot = sum(p[4] for p in r['padinfo'])
        det = '、'.join(f"{n}: {sh} {w:.2f}×{h:.2f} = {a:.2f}" for n, sh, w, h, a in r['padinfo'])
        add('熱を流す pad (面積)', f'{len(r["padinfo"])} 個 / {tot:.2f}', 'mm²', PT.BASIS['board'],
            f".kicad_pcb の pad 形状を 0.0625 mm 刻みで数えた面積 (pad 番号: 形 幅×高 = mm²): {det}")
        if r.get('junction') is not None:
            add('熱源配置', f"接合節点 → {r['layer_name']} {r['n_cells']} セル", '', PT.BASIS['calc'],
                f"接合節点 1 個に P を入れ、熱を流す pad が掛かるセルへ枝 G_i = (1/{r['path']['theta']:.1f} ℃/W) × A_i / {tot:.2f} mm² で接続 (A_i = セル内の pad 面積)")
        else:
            add('熱源配置', f"{r['layer_name']} の pad 直下 {r['n_cells']} セル", '', PT.BASIS['calc'],
                f"P を pad の面積比で各セルに一様に配る: q_i = {r['P']:.4f} W × A_i / {tot:.2f} mm²")
        m3 = r.get('m3')
        if m3:
            add('外形 (3D モデル)', f"{m3['x']:.2f} × {m3['y']:.2f} × 高さ {m3['height']:.2f}", 'mm', PT.BASIS['board'],
                f"{m3['kicad_path']} → {m3['file']} の CARTESIAN_POINT {m3['n_points']} 点の範囲 (リード込み)")
            add('上面の面積 (3D モデル)', f"{m3['top_x']:.2f} × {m3['top_y']:.2f} = {m3['top_area']:.2f}", 'mm²', PT.BASIS['board'],
                f"z 最大 ({m3['height']:.2f} mm) から 0.05 mm 以内の点の xy 範囲 = 本体上面。上面経路の A上 に使う")
        else:
            add('外形 (3D モデル)', '—', '', '—', '3D モデル (STEP) が見つからない' if fp.get('models') else 'footprint に 3D モデルなし')
    return T


def write_csv(T, path):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:   # Excel で文字化けしないよう BOM 付き
        w = csv.writer(f); w.writerow(COLS); w.writerows(T)


def md(T):
    esc = lambda s: str(s).replace('|', '\\|').replace('\n', ' ')
    L = ['| ' + ' | '.join(COLS) + ' |', '|' + '---|' * len(COLS)]
    for t in T:
        L.append('| ' + ' | '.join(esc(x) for x in t) + ' |')
    return L


def build_both(rows_typ, rows_worst):
    """typ と worst の表を 項目ごとに並べる。区分・備考が違えば 'typ: … / worst: …'"""
    Tt = build(rows_typ); Tw = build(rows_worst)
    idx = {}
    for t in Tt:
        idx.setdefault((t[0], t[2]), []).append(t)
    out = []
    for w in Tw:
        lst = idx.get((w[0], w[2])) or []
        t = lst.pop(0) if lst else None
        if t is None:
            out.append([w[0], w[1], w[2], '—', w[3], w[4], w[5], w[6]]); continue
        kind = w[5] if t[5] == w[5] else f'typ: {t[5]} / worst: {w[5]}'
        note = w[6] if t[6] == w[6] else f'typ: {t[6]} / worst: {w[6]}'
        out.append([w[0], w[1], w[2], t[3], w[3], w[4], kind, note])
    return out
