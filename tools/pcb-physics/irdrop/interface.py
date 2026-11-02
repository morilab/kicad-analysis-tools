"""irdrop のインターフェイス (連成ドライバと熱解析が呼ぶ形)。実装は IR ドロップ担当。

入力
  grid     : core.grid.PcbGrid  (cu_frac・net_id (0 なし・-1 衝突・k=nets[k-1])・edge_*・pad→セル CSR・via_*・meta.layers[].cu_mm。交換形式は core/grid.py の先頭)
  temp_c   : (L,Ny,Nx) ℃ の温度マップ、または None (= 一様 ref_temp_c)。基板外は NaN
  sources  : 電流の出入り。部品 YAML / thermal.yaml の parts.<ref>.pins.<pin>.current (typ/max) を cases.resolve で選んだもの
             [{ref, pin, net, x_mm, y_mm, layer, current_a (正 = 部品へ流れ込む)}]
  cond     : {case: 'typ'|'worst', ref_temp_c: 20.0, rho_cu_ohm_m: 1.72e-8, alpha_per_c: ALPHA_CU}
出力 IrResult
  heat_w   : (L,Ny,Nx) W  セルごとのジュール熱 (core.grid.save_map(kind='heat') で渡す。合計 = 全損失)
  volt_v   : (L,Ny,Nx) V  セルの電位 (ネットごとの基準からの値、銅なしは NaN)
  drops    : [{net, from, to, drop_v}]  要約
  info     : dict (反復、ソルバー、警告)
銅の抵抗の温度補正: ρ(T) = ρ20 × (1 + α (T − 20 ℃))、α = 0.00393 /℃ (約 0.39 %/℃、焼鈍銅)。
"""
from dataclasses import dataclass, field
import numpy as np

ALPHA_CU = 0.00393      # 1/℃ (20 ℃ 基準、IEC 60028 焼鈍銅)
RHO_CU_20 = 1.72e-8     # Ω·m (20 ℃、IEC 60028: 1/58 Ω·mm²/m)


def rho_cu(temp_c, rho20=RHO_CU_20, alpha=ALPHA_CU):
    return rho20 * (1 + alpha * (np.asarray(temp_c) - 20.0))


@dataclass
class IrResult:
    heat_w: np.ndarray
    volt_v: np.ndarray = None
    drops: list = field(default_factory=list)
    info: dict = field(default_factory=dict)


class IrSolver:
    """IR ドロップ担当はこのクラスを継承して solve を実装する"""
    name = 'abstract'

    def solve(self, grid, temp_c, sources, cond) -> IrResult:
        raise NotImplementedError('irdrop は未実装 (IR ドロップ担当)')
