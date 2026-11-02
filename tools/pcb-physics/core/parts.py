"""parts — 部品熱データ (data/parts/<LCSC>.yaml) と案件の消費電力 (thermal.yaml の parts) を読む。

値の書き方 (スキーマ、docs/design.md 4 章): 数値は必ず「値オブジェクト」で書き、区分 basis を付ける。
  basis: datasheet  → src: {page: 必須, table: 表/図番号 (必須、無ければ節の名前), item: 記述}
                      資料名・版・URL は YAML 先頭の datasheet: {title, doc, rev, url} (案件側は part: <LCSC> で部品の資料を指す)
  basis: assumed    → reason: どう仮定したか (必須)、calc: 計算式と代入値 (必須。値をそのまま置いた時はその旨と範囲)
  basis: board      → from: .kicad_pcb / 3D モデルのどこから (ツールが自動で作る行に使う)
  basis: design     → from: 案件の資料 (review.toml、仕様書、規格) の名前と項目
  value: null は「データシートに記載なし」(checked: 確認したページ を書く)
ピンごとの電流 (IR ドロップ・ジュール熱用、部品 YAML または案件 thermal.yaml の parts.<ref>):
  pins:
    "1": {name: VIN, net_role: power_in, current: {typ: <値オブジェクト>, max: <値オブジェクト>}}   # 正 = 部品へ流れ込む [A]
  current は typ と max の両方が必須 (cases.py と同じ規則)。
  v_range (任意): ピンの許容電圧範囲 {min: <値オブジェクト>, max: <値オブジェクト>, kind: recommended|absolute}。各値は basis 必須 (出典ページ or 仮定の根拠と計算式)
"""
import glob, os
import yaml

BASIS = {'datasheet': 'データシート値', 'assumed': '仮定値', 'board': '基板・3Dモデル由来', 'design': '案件条件', 'calc': '計算値'}
THETA_JA_TO_JB = 0.35   # θJB が無く θJA しか無い時の逆算係数 (仮定、下の thermal_path の calc に式を出す)


class DataError(Exception):
    pass


def basis_of(x):
    if not isinstance(x, dict):
        return None
    if x.get('basis'):
        return x['basis']
    if x.get('assumed'):
        return 'assumed'
    if x.get('src'):
        return 'datasheet'
    return None


def check_value(x, where):
    if not isinstance(x, dict) or 'value' not in x:
        return
    b = basis_of(x)
    if x['value'] is None:
        return
    if b not in BASIS:
        raise DataError(f'{where}: basis (datasheet / assumed / board / design) が無い')
    if b == 'datasheet':
        s = x.get('src') or {}
        if not isinstance(s, dict) or s.get('page') is None or not (s.get('table') or s.get('item')):
            raise DataError(f'{where}: datasheet 値には src.page と src.table (表/図番号) が必要')
    if b == 'assumed':
        if not x.get('reason') or not x.get('calc'):
            raise DataError(f'{where}: 仮定値には reason (根拠) と calc (計算式と代入値) が必要')
    if b in ('board', 'design') and not x.get('from'):
        raise DataError(f'{where}: {b} 値には from (どこから) が必要')


def walk(y, where, f):
    if isinstance(y, dict):
        if 'value' in y:
            f(y, where)
        for k, v in y.items():
            walk(v, f'{where}.{k}', f)
    elif isinstance(y, list):
        for i, v in enumerate(y):
            walk(v, f'{where}[{i}]', f)


def doc_text(ds):
    if not ds:
        return ''
    return f"{ds.get('title', '')} {ds.get('doc', '')} {ds.get('rev', '')}、{ds.get('url', '')}".strip()


def note(x, ds=None):
    """備考: データシート値 → 資料名・版・URL・ページ・表、仮定値 → 根拠と計算式"""
    if not isinstance(x, dict):
        return ''
    b = basis_of(x)
    if x.get('value') is None:
        return f"データシートに記載なし (確認: {x.get('checked', '-')})。{doc_text(ds)}"
    if b == 'datasheet':
        s = x['src']
        return f"{doc_text(ds)}、p.{s.get('page')} {s.get('table', '')}: {s.get('item', '')}".replace('  ', ' ')
    if b == 'assumed':
        return f"根拠: {x['reason']} / 計算: {x['calc']}"
    return f"{x.get('from', '')}" + (f" / 計算: {x['calc']}" if x.get('calc') else '')


def label(x):
    if isinstance(x, dict) and x.get('value') is None:
        return '記載なし'
    return BASIS.get(basis_of(x), '?')


def check_pins(y, where):
    for pn, pv in (y.get('pins') or {}).items():
        vr = (pv or {}).get('v_range')
        if vr is not None:
            if not isinstance(vr, dict) or not ({'min', 'max'} & set(vr)):
                raise DataError(f'{where}: pins.{pn}.v_range は min か max (値オブジェクト) が必要')
            for k in ('min', 'max'):
                if k in vr:
                    if not isinstance(vr[k], dict) or 'value' not in vr[k]:
                        raise DataError(f'{where}: pins.{pn}.v_range.{k} は値オブジェクト ({{value, basis, ...}})')
                    check_value(vr[k], f'{where}: pins.{pn}.v_range.{k}')
        c = (pv or {}).get('current')
        if c is None:
            continue
        if not isinstance(c, dict) or 'typ' not in c or 'max' not in c:
            raise DataError(f'{where}: pins.{pn}.current は typ と max の両方が必要')


SIGNAL_KEYS = {'default', 'pins', 'datasheet', 'notes'}


def check_signal(y, where):
    """signal: (信号特性。data/signal/parts と同じ形、commit bd287ae)。
    signal.default.<項目> と signal.pins.<ピン>.<項目> は値オブジェクト {value, basis, ...} (tr_ns・f_max_mhz・r_out_ohm など)、
    pins.<ピン> の kind (usb_hs などファミリ名)・note は文字列。signal.datasheet は熱と資料 (版) が違う時の資料。中身は検査だけで使わない"""
    s = y.get('signal')
    if s is None:
        return
    if not isinstance(s, dict) or not set(s) <= SIGNAL_KEYS:
        raise DataError(f'{where}: signal: のキーは {sorted(SIGNAL_KEYS)} (あったのは {sorted(s) if isinstance(s, dict) else type(s).__name__})')

    def vals(d, w):
        for k, x in (d or {}).items():
            if k in ('kind', 'note', 'notes'):
                if not isinstance(x, (str, list)):
                    raise DataError(f'{w}.{k} は文字列')
                continue
            if not isinstance(x, dict) or 'value' not in x:
                raise DataError(f'{w}.{k} は値オブジェクト ({{value, basis, ...}})')
            check_value(x, f'{w}.{k}')
    vals(s.get('default'), f'{where}: signal.default')
    for pn, pv in (s.get('pins') or {}).items():
        vals(pv, f'{where}: signal.pins.{pn}')


def has_thermal(pd):
    """部品 YAML に熱データ (θ のどれか、または tj_max) があるか。無い部品 (ピン電流・信号だけ) は熱側で info 扱い"""
    th = (pd or {}).get('theta') or {}
    return any(_v(x) is not None for x in th.values()) or _v((pd or {}).get('tj_max')) is not None


def load_library(d):
    lib = {}
    for f in sorted(glob.glob(os.path.join(d, '*.yaml'))):
        y = yaml.safe_load(open(f, encoding='utf-8'))
        walk(y, os.path.basename(f), check_value)
        check_pins(y, os.path.basename(f))
        check_signal(y, os.path.basename(f))
        y['_file'] = os.path.basename(f)
        lib[str(y['lcsc'])] = y
    return lib


def check_project(cfg):
    walk(cfg.get('parts') or {}, 'thermal.yaml:parts', check_value)



def _v(x):
    return x.get('value') if isinstance(x, dict) else x


def _src(x, ds=None):
    return f"{label(x)}: {note(x, ds)}"


def thermal_path(pd):
    """θ の選び方: θJB > Ψ_JB > θJC(底/タブ) > θJA から逆算。戻り値 dict (calc に計算過程)"""
    th = pd.get('theta') or {}; ds = pd.get('datasheet')
    jc_top = _v(th.get('jc_top'))
    for key, kind in (('jb', 'θJB'), ('psi_jb', 'Ψ_JB'), ('jc_bottom', 'θJC(底/タブ)')):
        x = th.get(key)
        if _v(x) is not None:
            return dict(theta=float(_v(x)), kind=kind, key=key, src=_src(x, ds), derived=basis_of(x) == 'assumed',
                        calc=f'{kind} = {_v(x)} ℃/W をそのまま接合 → 熱を流す pad の枝に使う', jc_top=jc_top)
    ja = th.get('ja')
    if _v(ja) is not None:
        t = THETA_JA_TO_JB * float(_v(ja))
        return dict(theta=t, kind=f'θJA×{THETA_JA_TO_JB} (逆算)', key='ja', src=_src(ja, ds), derived=True, jc_top=jc_top,
                    calc=f'θJB ≈ {THETA_JA_TO_JB} × θJA = {THETA_JA_TO_JB} × {_v(ja)} = {t:.1f} ℃/W (係数は JESD51-7 の QFP/SOIC で θJB/θJA ≈ 0.3〜0.4 の仮定)')
    return None


def power_of(spec, lib):
    """thermal.yaml の parts.<ref>.power。戻り値 (P [W], 式の文字列, 項目のリスト [(名前, 値オブジェクト, 単位, 資料)])"""
    p = spec.get('power')
    def ds_of(x):
        return (lib.get(str(x.get('part'))) or {}).get('datasheet') if isinstance(x, dict) else None
    items = []
    if p.get('model') == 'vi':
        v = p['v']; cur = p['currents']
        I = sum(float(_v(c)) for c in cur); P = float(_v(v)) * I
        items.append(('電圧 V', v, 'V', ds_of(v)))
        for c in cur:
            items.append((f"電流 {c['name']}", c, 'A', ds_of(c)))
        f = f"P = V × ΣI = {_v(v)} V × ({' + '.join(f'{float(_v(c)) * 1000:g}' for c in cur)}) mA = {_v(v)} × {I * 1000:g} mA = {P:.4f} W"
        return P, f, items
    if p.get('model') == 'ldo':
        vin, vout, iq = p['vin'], p['vout'], p.get('iq')
        loads = p['loads']; I = sum(float(_v(c)) for c in loads)
        IQ = float(_v(iq)) if iq else 0.0
        P = (float(_v(vin)) - float(_v(vout))) * I + float(_v(vin)) * IQ
        items += [('入力電圧 Vin', vin, 'V', ds_of(vin)), ('出力電圧 Vout', vout, 'V', ds_of(vout))]
        for c in loads:
            items.append((f"負荷電流 {c['name']}", c, 'A', ds_of(c)))
        if iq:
            items.append(('静止電流 Iq', iq, 'A', ds_of(iq)))
        f = (f"P = (Vin - Vout) × ΣIout + Vin × Iq = ({_v(vin)} - {_v(vout)}) V × ({' + '.join(f'{float(_v(c)) * 1000:g}' for c in loads)}) mA"
             f" + {_v(vin)} V × {IQ * 1e6:g} µA = {_v(vin) - _v(vout):.2f} × {I * 1000:g} mA + {_v(vin) * IQ * 1000:.3f} mW = {P:.4f} W")
        return P, f, items
    if p.get('model') == 'i2r':   # 受動部品 (フェライトビーズ・抵抗・インダクタ) の導通損: P = I² R
        r, cur = p['r'], p['current']
        I = float(_v(cur)); R = float(_v(r)); P = I * I * R
        items += [('抵抗 R (DCR)', r, 'Ω', ds_of(r)), ('電流 I', cur, 'A', ds_of(cur))]
        f = f"P = I² × R = ({I * 1000:g} mA)² × {R:g} Ω = {P * 1000:.4f} mW"
        return P, f, items
    raise DataError(f"power model {p.get('model')} は未対応 (vi / ldo / i2r)")


def series_resistance(part):
    """直列素子 (ヒューズ・ビーズ・抵抗) の直流抵抗を IR / PDN 用に {typ, max, min?, tc_per_c?, t_ref_c?} と読んだキー名で返す。
    部品 YAML の書き方は 2 通りあり、どちらも読む:
      - dc_resistance: {typ, max, min, tc_per_c, t_ref_c}         (IR 側の書き方、例 C1015・C207020)
      - resistance: {typ, max, tc_per_c?, t_ref_c?} + resistance_min (熱側の書き方、例 C207022・C468889)
    両方ある時は dc_resistance を使う。どちらも無ければ (None, None)"""
    if not part:
        return None, None
    dc = part.get('dc_resistance')
    if dc:
        return dc, 'dc_resistance'
    r = part.get('resistance')
    if isinstance(r, dict) and ('typ' in r or 'max' in r):
        d = dict(r)
        if 'min' not in d and part.get('resistance_min') is not None:
            d['min'] = part['resistance_min']
        return d, 'resistance'
    return None, None
