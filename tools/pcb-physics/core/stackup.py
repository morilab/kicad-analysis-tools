"""stackup — 層構成。優先順位: thermal.yaml の stackup (明示上書き) > 基板 YAML (data/board、core/boardspec) > .kicad_pcb の stackup > JLCPCB 標準 (2026-10 時点の公開値)。"""

K_CU = 385.0      # W/m·K 銅 (圧延・電解の代表値)
K_FR4_XY = 0.81   # W/m·K FR4 面内 (代表値、文献 0.8〜0.9)
K_FR4_Z = 0.29    # W/m·K FR4 厚さ方向 (代表値、文献 0.25〜0.35)
K_MASK = 0.2      # W/m·K ソルダーレジスト
T_MASK = 0.010    # mm ソルダーレジスト (JLC 約 10〜15 µm)
T_PLATING = 0.018 # mm ビアのめっき厚の既定 (JLC 公称 18 µm 以上、IPC Class 2 平均 20 µm)。実際の値は resolve_plating() で決める

# JLCPCB 標準 (https://jlcpcb.com/impedance の stackup 表、2 層 = 外層 1 oz、4 層 JLC04161H-7628 = 外層 1 oz / 内層 0.5 oz)
# 比誘電率: JLCPCB https://jlcpcb.com/impedance (2026-10-14 確認): プリプレグ 7628 = 4.4、3313 = 4.1、1080 = 3.91、2116 = 4.16、コア = 4.6
ER_JLC = {'7628': 4.4, '3313': 4.1, '1080': 3.91, '2116': 4.16, 'core': 4.6}
ER_SRC = 'JLCPCB https://jlcpcb.com/impedance (プリプレグ 7628 4.4 / コア 4.6、2026-10-14 確認)'


def er_series(parts):
    """[(厚さ, εr)] を直列 (平行平板) で 1 つに: h / Σ(h_i/εr_i)"""
    h = sum(t for t, _ in parts)
    return h / sum(t / e for t, e in parts) if h > 0 else None


JLC_DEFAULT = {
    2: dict(src='JLCPCB 標準 2 層: 外層 1 oz (35 µm)、コア = 板厚 - 銅', cu=[0.035, 0.035], diel=None),
    4: dict(src='JLCPCB JLC04161H-7628 (1.6 mm): 外層 35 µm、内層 15.2 µm、プリプレグ 0.2104 mm、コア 1.065 mm',
            cu=[0.035, 0.0152, 0.0152, 0.035], diel=[0.2104, 1.065, 0.2104]),
}


def resolve(board, override=None, spec=None):
    """戻り値: dict(cu=[銅厚 mm], diel=[誘電体厚 mm], src=出典, assumed=bool, warnings=[食い違い])"""
    if override:
        n = len(override['diel'])
        er = override.get('er') or [ER_JLC['core']] * n
        return dict(cu=override['cu'], diel=override['diel'], src=override.get('src', 'thermal.yaml'), assumed=False, warnings=[],
                    er=list(er), er_src=[override.get('er_src', 'thermal.yaml stackup.er') if override.get('er') else ER_SRC + ' のコア値 (仮定)'] * n)
    if spec:
        from . import boardspec
        return boardspec.stackup(spec, board, _resolve_kicad(board))
    r = _resolve_kicad(board); r['warnings'] = []
    return r


def _resolve_kicad(board):
    n = len(board['cu_layers']); H = board['thickness']
    st = board.get('stackup')
    if st:
        cu = [l['thickness'] for l in st if l['type'] == 'copper']
        diel = []; acc = None; ers = []; part = []
        for l in st:
            if l['type'] == 'copper':
                if acc is not None:
                    diel.append(acc); ers.append(part)
                acc = 0.0; part = []
            elif acc is not None and l['type'] in ('core', 'prepreg'):
                acc += l['thickness']; part.append((l['thickness'], l.get('epsilon_r'), l['type']))
        if len(cu) == n and len(diel) == n - 1 and all(d > 0 for d in diel):
            er = []; es = []
            for p in ers:
                if all(e for _, e, _ in p):
                    er.append(er_series([(t, e) for t, e, _ in p])); es.append('.kicad_pcb の stackup (epsilon_r' + (' を直列で合成)' if len(p) > 1 else ')'))
                else:
                    er.append(er_series([(t, e or (ER_JLC['7628'] if k == 'prepreg' else ER_JLC['core'])) for t, e, k in p]))
                    es.append('.kicad_pcb に epsilon_r なし → ' + ER_SRC + ' (仮定)')
            return dict(cu=cu, diel=diel, src='.kicad_pcb の stackup', assumed=False, er=er, er_src=es)
    d = JLC_DEFAULT.get(n)
    if d is None:
        cu = [0.035] * n; diel = [(H - sum(cu)) / (n - 1)] * (n - 1)
        return dict(cu=cu, diel=diel, src=f'{n} 層: 銅 35 µm・誘電体等分 (仮定)', assumed=True, er=[ER_JLC['core']] * (n - 1), er_src=[ER_SRC + ' のコア値 (仮定)'] * (n - 1))
    diel = d['diel'] or [H - sum(d['cu']) - 2 * T_MASK]
    er, es = jlc_er(n)
    return dict(cu=d['cu'], diel=diel, src=d['src'] + ' (stackup 未定のため)', assumed=True, er=er, er_src=es)


def jlc_er(n, name=None):
    """JLC 標準の比誘電率: 4 層 JLC04161H-7628 = [7628 4.4, コア 4.6, 7628 4.4]、2 層 = コア 4.6 (1.6 mm 両面板もコア材として JLC のコア値を使う仮定)"""
    if n == 4 and name in (None, 'JLC04161H-7628'):
        return [ER_JLC['7628'], ER_JLC['core'], ER_JLC['7628']], [ER_SRC + ' JLC04161H-7628 プリプレグ 7628', ER_SRC + ' コア', ER_SRC + ' JLC04161H-7628 プリプレグ 7628']
    return [ER_JLC['core']] * (n - 1), [ER_SRC + ' のコア値 (2 層・未登録 stackup は仮定)'] * (n - 1)


def resolve_plating(project=None, board_spec=None):
    """ビアめっき厚 → (mm, 出典)。優先順 (irdrop/plating.py と同じ): ① 案件 YAML の plating {value: µm, basis, ...}
    > ② 基板 YAML (data/board) の process.via_plating > ③ 既定 T_PLATING (18 µm)。grid.meta の plating_mm / plating_src に記録する。"""
    from . import parts as CP

    def um(x, where):
        CP.check_value(x, where)
        v = float(x['value'] if isinstance(x, dict) else x)
        if not 0 < v < 200:
            raise ValueError(f'{where}: めっき厚 {v} µm は範囲外 (µm で書く)')
        return v

    def why(x):
        return f"{CP.BASIS.get(CP.basis_of(x), '?')}: {x.get('from') or x.get('reason') or ''}" if isinstance(x, dict) else '?'
    x = (project or {}).get('plating') if isinstance(project, dict) else None
    if x is not None:
        v = um(x, '案件 YAML: plating'); return v * 1e-3, f"案件 YAML plating {v:g} µm ({why(x)})"
    y = ((board_spec or {}).get('process') or {}).get('via_plating')
    if y is not None:
        f = (board_spec or {}).get('_file', '基板 YAML')
        v = um(y, f'{f}: process.via_plating'); return v * 1e-3, f"基板 YAML {f} process.via_plating {v:g} µm ({why(y)})"
    return T_PLATING, f"core 既定 {T_PLATING * 1e3:g} µm (core/stackup.T_PLATING、JLC 公称 18 µm 以上)"
