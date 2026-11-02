"""IR 側が未実装の間のスタブ: ジュール熱 0 を返す (連成ドライバの動作確認用)"""
import numpy as np
from .interface import IrSolver, IrResult


class StubIrSolver(IrSolver):
    name = 'stub (ジュール熱 0)'

    def solve(self, grid, temp_c, sources, cond):
        return IrResult(heat_w=np.zeros(grid.cu_frac.shape), info=dict(note='スタブ: 発熱 0'))
