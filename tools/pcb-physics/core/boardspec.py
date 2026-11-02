"""boardspec — 基板の製造条件 (data/board/<基板>.yaml)。項目名と選択肢は JLCPCB の注文画面 (Order now → PCB Specifications / High-spec Options) に合わせる。
出典: https://jlcpcb.com/capabilities/pcb-capabilities (2026-10-14 確認)、https://jlcpcb.com/help/article/jlcpcb-copper-weight (2026-09-09 更新)、
      注文画面の項目名は公開されている注文記録 (Build Guide 等) と照合。選択肢は変わることがあるので、注文前に注文画面で確認する。
各項目は部品 YAML と同じ値オブジェクト {value, basis, ...}。basis: design (決定済み、from 必須) / board (.kicad_pcb 由来、from 必須) / assumed (未定、reason と calc 必須)。
層構成の優先順位: thermal.yaml の stackup (明示上書き) > 基板 YAML > .kicad_pcb の stackup > JLC 標準。食い違いは warnings に出す。"""
import os
import yaml
from .parts import DataError, check_value, basis_of, BASIS

OZ_MM = 0.035   # 1 oz = 35 µm (JLC 銅厚ガイド)
INNER_MM = {'0.5 oz': 0.0152, '1 oz': 0.0305, '2 oz': 0.0610}   # JLC の stackup 表の仕上がり内層厚 (0.5 oz = 15.2 µm、1 oz/2 oz は同表の比例値 = 仮定)

# 注文画面の項目名 → 選択肢 (None = 自由入力・数値)。熱/IR に効くもの + 主な注文項目
OPTIONS = {
    'Base Material': ['FR-4', 'Flex', 'Aluminum', 'Copper Core', 'Rogers', 'PTFE Teflon'],
    'Layers': [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 32],
    'PCB Thickness': [0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0, 2.5, 3.0, 3.2, 3.5, 4.0, 4.5],
    'Material Type': ['FR4-Standard TG 135-140', 'FR-4 TG155', 'FR4-TG170'],
    'Surface Finish': ['HASL(with lead)', 'LeadFree HASL', 'ENIG', 'OSP'],
    'Outer Copper Weight': ['1 oz', '2 oz', '2.5 oz', '3.5 oz', '4.5 oz'],
    'Inner Copper Weight': ['0.5 oz', '1 oz', '2 oz'],
    'Specify Layer Sequence': ['No', 'Yes'],
    'Specify Stackup': ['No', 'Yes'],
    'Layer Stackup': None,          # 例 JLC04161H-7628 (Specify Stackup = Yes の時)
    'Impedance Control': ['No', 'Yes'],
    'Via Covering': ['Tented', 'Untented', 'Plugged', 'Epoxy Filled & Capped', 'Copper paste Filled & Capped'],
    'Min via hole size/diameter': None,
    'PCB Color': ['Green', 'Purple', 'Red', 'Yellow', 'Blue', 'White', 'Black'],
    'Silkscreen': ['White', 'Black'],
    'Board Outline Tolerance': ['±0.2mm(Regular)', '±0.1mm(Precision)'],
}
REQUIRED = ['Base Material', 'Layers', 'PCB Thickness', 'Outer Copper Weight', 'Specify Stackup']
# JLC の公開 stackup (Specify Stackup = Yes で選ぶ名前) → 銅厚・誘電体厚 mm
JLC_STACKUPS = {'JLC04161H-7628': dict(cu=[0.035, 0.0152, 0.0152, 0.035], diel=[0.2104, 1.065, 0.2104])}


def _oz(s):
    return float(str(s).split()[0]) * OZ_MM


def load(path):
    y = yaml.safe_load(open(path, encoding='utf-8'))
    o = y.get('order') or {}
    for k, x in o.items():
        if k not in OPTIONS:
            raise DataError(f'{os.path.basename(path)}: order."{k}" は JLC 注文画面の項目名にない (core/boardspec.OPTIONS)')
        if not isinstance(x, dict) or 'value' not in x:
            raise DataError(f'{os.path.basename(path)}: order."{k}" は {{value, basis, ...}} で書く')
        check_value(x, f'{os.path.basename(path)}: order."{k}"')
        if x['value'] is not None and OPTIONS[k] is not None and x['value'] not in OPTIONS[k]:
            raise DataError(f'{os.path.basename(path)}: order."{k}" = {x["value"]!r} は選択肢 {OPTIONS[k]} にない')
    for k in REQUIRED:
        if k not in o or o[k]['value'] is None:
            raise DataError(f'{os.path.basename(path)}: order."{k}" が必要')
    y['_file'] = os.path.basename(path)
    return y


def find(project, board_dir):
    """data/board/<project>*.yaml を探す (thermal.yaml の board: が優先)"""
    if not os.path.isdir(board_dir):
        return None
    c = sorted(f for f in os.listdir(board_dir) if f.endswith('.yaml') and f.startswith(project))
    return os.path.join(board_dir, c[-1]) if c else None


def stackup(spec, board, kicad_st=None):
    """基板 YAML → dict(cu, diel, src, assumed, warnings, thickness)。kicad_st = .kicad_pcb の stackup 解決結果 (比較用)"""
    from .stackup import T_MASK
    o = spec['order']; v = lambda k: (o.get(k) or {}).get('value'); W = []
    n = int(v('Layers')); H = float(v('PCB Thickness'))
    if n != len(board['cu_layers']):   # 格子は .kicad_pcb の銅層で作るので、層数が違う時は基板 YAML を使えない
        r = dict(kicad_st); r['warnings'] = [f"層数の食い違い: 基板 YAML {n} 層 / .kicad_pcb {len(board['cu_layers'])} 層 (基板 YAML は使わず {kicad_st['src']})"]
        return r
    if abs(H - float(board['thickness'])) > 1e-6:
        W.append(f"板厚の食い違い: 基板 YAML {H} mm / .kicad_pcb {board['thickness']} mm (基板 YAML を使う)")
    if v('Base Material') != 'FR-4':
        W.append(f"材質 {v('Base Material')} は FR-4 の熱伝導率で計算している (未対応)")
    name = v('Layer Stackup') if v('Specify Stackup') == 'Yes' else None
    if name and name in JLC_STACKUPS:
        cu = list(JLC_STACKUPS[name]['cu']); diel = list(JLC_STACKUPS[name]['diel']); how = f'JLC stackup {name}'
    else:
        if name:
            W.append(f"Layer Stackup {name} の寸法が未登録 (core/boardspec.JLC_STACKUPS)。銅厚から誘電体を等分")
        out = _oz(v('Outer Copper Weight')); inn = INNER_MM.get(v('Inner Copper Weight') or '0.5 oz', 0.0152)
        cu = [out] + [inn] * (n - 2) + [out] if n >= 2 else [out]
        diel = [H - sum(cu) - 2 * T_MASK] if n == 2 else [(H - sum(cu) - 2 * T_MASK) / (n - 1)] * (n - 1)
        how = f"外層 {v('Outer Copper Weight')}" + (f"・内層 {v('Inner Copper Weight') or '0.5 oz'}" if n > 2 else '') + f"・板厚 {H} mm から誘電体を算出"
    if kicad_st and not kicad_st['assumed']:
        if len(kicad_st['cu']) == len(cu) and any(abs(a - b) > 0.002 for a, b in zip(kicad_st['cu'], cu)):
            W.append(f"銅厚の食い違い: 基板 YAML {[round(c * 1000, 1) for c in cu]} µm / .kicad_pcb stackup {[round(c * 1000, 1) for c in kicad_st['cu']]} µm (基板 YAML を使う)")
    used = ['Layers', 'PCB Thickness', 'Outer Copper Weight'] + (['Inner Copper Weight'] if n > 2 else []) + ['Specify Stackup'] + (['Layer Stackup'] if name else [])
    asm = [k for k in used if basis_of(o.get(k)) == 'assumed']
    src = f"基板 YAML {spec['_file']} (JLC 注文条件: {how}" + (f"、仮定: {', '.join(asm)}" if asm else '') + ')'
    from .stackup import jlc_er
    if kicad_st and not kicad_st['assumed'] and kicad_st.get('er') and len(kicad_st['er']) == len(diel) and '.kicad_pcb の stackup (epsilon_r' in kicad_st['er_src'][0]:
        er, es = kicad_st['er'], kicad_st['er_src']   # 基板 YAML の注文項目に εr は無いので、.kicad_pcb に書いてあればそれ
    else:
        er, es = jlc_er(n, name)
    return dict(cu=cu, diel=diel, src=src, assumed=bool(asm), warnings=W, thickness=H, er=er, er_src=es)


def table_rows(spec):
    """部品表に載せる行: [部品, 品番, 項目, 値, 単位, 区分, 備考]"""
    T = []
    for k, x in spec['order'].items():
        b = basis_of(x)
        note = x.get('from') or (f"{x.get('reason')}。{x.get('calc')}" if b == 'assumed' else '')
        T.append(['基板', spec['_file'], k, '—' if x['value'] is None else str(x['value']), 'mm' if k == 'PCB Thickness' else '',
                  BASIS.get(b, '記載なし') if x['value'] is not None else '未定 (不要)', note])
    return T
