"""ビアめっき厚の決定 (IR ドロップのビア抵抗用)。
優先順: ① 案件 YAML (irdrop.yaml の plating) > ② 基板 YAML (data/board の process.via_plating) > ③ core の既定 (core/stackup.T_PLATING = 18 µm)。
JLCPCB の注文画面にはめっき厚の項目が無い (capabilities の公称値のみ) ので、基板 YAML では order: ではなく process: に書く。
値は値オブジェクト {value: µm, basis, ...}。決めた値と出典を grid.meta の plating_mm / plating_src に記録する。
2026-10-16: main d495b2f (33c8a0b) で core/stackup.resolve_plating に同じ優先順が入ったので、判定はそちらに寄せた
(core.grid.build(project=...) でも同じ値になる)。ここは irdrop.yaml を案件 YAML として渡す薄い窓口と、--grid で読んだ格子への上書き。"""
from core import stackup as S


def resolve(icfg=None, board_spec=None):
    """→ (mm, 出典の文字列)。core/stackup.resolve_plating と同じ (案件 YAML = irdrop.yaml)"""
    return S.resolve_plating(icfg or {}, board_spec)


def apply(grid, icfg=None, board_spec=None, log=None):
    mm, src = resolve(icfg, board_spec)
    grid.meta['plating_mm'] = mm; grid.meta['plating_src'] = src
    if log:
        log(f'ビアめっき {mm * 1e3:g} µm ← {src}')
    return mm, src
