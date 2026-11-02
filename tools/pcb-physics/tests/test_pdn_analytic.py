#!/usr/bin/env python3
"""pdn の手計算検証 6 ケース (合否 ±5%)。ngspice (.ac) と Python 直接解 (MNA) の両方を手計算と比べる。
1 RLC 直列: 自己共振 f = 1/(2π√(LC)) と |Z(f)| = ESR、10 kHz と 100 MHz の |Z|
2 並列反共振: 2 個のコンデンサの反共振 f_ar (ω²(L1+L2) = 1/C1 + 1/C2) と |Z(f_ar)| の解析式
3 ビアインダクタンス: L = μ0h/2π (ln(4h/d)+1) を H. Johnson の実用式 (nH・inch 単位、例 0.063"/0.016" → 1.20 nH) と、格子から抽出した値で
4 平行平板: 幅広の帯 (w/h = 100) の抽出 L = μ0 h ℓ / w
5 マイクロストリップ式: Z0_air(w = h) = 126 Ω (Wheeler/Hammerstad の表の値)、細い帯 (w 0.3 mm) の抽出 L = L'(w,h)·ℓ
6 相反性: 抽出した 3 ポート網の Z12 = Z21 (ngspice と Python)、ngspice と Python の一致
7 core の pad_alias で重なった pad を 1 ポートに、εr を格子 meta.layers[].er_below から (2026-10-14)"""
import os, sys, math
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from tests.test_irdrop_analytic import make, check
from pdn import extract as EX
from pdn.circuit import Netlist

TOL = 0.05
MU0 = 4e-7 * math.pi


def zin(net, f, node='a'):
    """1 A を node に注いだときの電圧 = 入力インピーダンス (Python と ngspice)"""
    zp = net.solve(f, [(node, '0')])[0]
    F, zs = net.spice('t', [(node, '0')], f[0], f[-1], 200)
    return zp, F, zs[0]


def strip_L(w_cells, len_cells, h_mm, dx=0.1, edge_ports=True):
    """L0 に w×len の帯。両端の列をポートにして抽出した L (1/γ) を返す"""
    ny, nx = w_cells + 4, len_cells + 4
    m = np.zeros((2, ny, nx), bool); m[0, 2:2 + w_cells, 2:2 + len_cells] = True
    pads = [('A', '1', 0, [(j, 2) for j in range(2, 2 + w_cells)]), ('B', '1', 0, [(j, 1 + len_cells) for j in range(2, 2 + w_cells)])]
    g = make(m, dx=dx, diel=(h_mm,), pads=pads)
    red = EX.reduce(g, 'N', [('A', 'A', '1'), ('B', 'B', '1')], log=lambda *a: None)
    return 1 / -red['Gam'][0, 1], 1 / -red['G'][0, 1], (len_cells - 1) * dx * 1e-3


def main():
    res = []
    # 1 RLC 直列
    C, L, R = 4.7e-6, 0.4e-9, 0.010
    n = Netlist(); n.add('I', 's', '0', 'a', 1.0); n.add('R', 'r', 'a', 'b', R); n.add('L', 'l', 'b', 'c', L); n.add('C', 'c', 'c', '0', C)
    f0 = 1 / (2 * math.pi * math.sqrt(L * C))
    zp, F, zs = zin(n, np.array([1e4, f0, 1e8]))
    zpf = n.solve(F, [('a', '0')])[0]
    fs = F[np.argmin(abs(zs))]
    res.append(check('1a RLC 直列 自己共振 f (ngspice の |Z| 最小)', fs, f0))
    res.append(check('1b |Z(f0)| = ESR (Python)', abs(zp[1]), R))
    hand = lambda f: abs(R + 1j * (2 * math.pi * f * L - 1 / (2 * math.pi * f * C)))
    res.append(check('1c |Z(10 kHz)| (ngspice)', abs(zs[0]), hand(1e4)))
    res.append(check('1d |Z(100 MHz)| (ngspice)', abs(zs[-1]), hand(1e8)))
    res.append(check('1e ngspice と Python の一致 (全周波数の最大差)', 1 + np.max(abs(zs - zpf) / abs(zpf)), 1.0, -1e-3, 1e-3))
    # 2 並列反共振 (4.7 µF と 100 nF)
    C1, L1, R1, C2, L2, R2 = 4.7e-6, 0.4e-9, 0.010, 100e-9, 0.4e-9, 0.030
    n = Netlist(); n.add('I', 's', '0', 'a', 1.0)
    n.add('R', 'r1', 'a', 'b1', R1); n.add('L', 'l1', 'b1', 'c1', L1); n.add('C', 'c1', 'c1', '0', C1)
    n.add('R', 'r2', 'a', 'b2', R2); n.add('L', 'l2', 'b2', 'c2', L2); n.add('C', 'c2', 'c2', '0', C2)
    far = math.sqrt((1 / C1 + 1 / C2) / (L1 + L2)) / (2 * math.pi)
    zp = n.solve(np.array([far]), [('a', '0')])[0]
    F, zs = n.spice('t', [('a', '0')], 5e6, 2.4e7, 2000); fpk = F[np.argmax(abs(zs[0]))]   # 2 つの自己共振 (3.7 / 25 MHz) の間の極大
    w = 2 * math.pi * far
    z1 = R1 + 1j * (w * L1 - 1 / (w * C1)); z2 = R2 + 1j * (w * L2 - 1 / (w * C2))
    ws = 2 * np.pi * F; Z1 = R1 + 1j * (ws * L1 - 1 / (ws * C1)); Z2 = R2 + 1j * (ws * L2 - 1 / (ws * C2))
    fhand = F[np.argmax(abs(Z1 * Z2 / (Z1 + Z2)))]      # 損失込みの解析式の極大 (損失なしの式 f_ar より ESR の分だけ下がる)
    res.append(check('2a 反共振の極大 f (ngspice) = 損失込み解析式 |Z1Z2/(Z1+Z2)| の極大', fpk, fhand, -0.01, 0.01))
    res.append(check('2a\' 同 f と損失なしの式 f_ar = √((1/C1+1/C2)/(L1+L2))/2π (ESR で下がる分 ±10%)', fpk, far, -0.10, 0.10))
    res.append(check('2b |Z(f_ar)| = |Z1 Z2 / (Z1 + Z2)| (Python)', abs(zp[0]), abs(z1 * z2 / (z1 + z2))))
    # 3 ビア
    h, d = 0.063 * 0.0254, 0.016 * 0.0254
    hand = 5.08e-9 * 0.063 * (math.log(4 * 0.063 / 0.016) + 1)      # Johnson の実用式 L[nH] = 5.08 h[in] (ln(4h/d) + 1) = 1.20 nH
    res.append(check("3a ビア L 式 (Johnson の例 板厚 0.063\"、穴 0.016\" → 5.08 h (ln 4h/d + 1) = 1.20 nH)", EX.via_l(h, d), hand))
    m = np.zeros((2, 5, 5), bool); m[:, 2, 2] = True
    g = make(m, diel=(1.5,), pads=[('A', '1', 0, [(2, 2)]), ('B', '1', 1, [(2, 2)])], vias=[(2, 2, 0.3)])
    g.meta['board_thickness_mm'] = 1.6
    red = EX.reduce(g, 'N', [('A', 'A', '1'), ('B', 'B', '1')], log=lambda *a: None)
    res.append(check('3b 格子のビア 1 本の抽出 L (d 0.3 mm、h 1.6 mm)', 1 / -red['Gam'][0, 1], EX.via_l(1.6e-3, 0.3e-3)))
    # 4 平行平板 (w 20 mm、h 0.2 mm、ℓ 9.9 mm)
    Lx, Rx, ell = strip_L(200, 100, 0.2)
    res.append(check('4 平行平板 L = μ0 h ℓ / w (w/h 100、縁の効果込みで ±5%)', Lx, MU0 * 0.2e-3 * ell / 20e-3))
    # 5 マイクロストリップ
    res.append(check('5a Z0_air(w/h = 1) = 126 Ω', float(EX.z0_air(1.0, 1.0)), 126.0))
    Lx, Rx, ell = strip_L(3, 200, 1.53)
    res.append(check('5b 細い帯 0.3 mm × 19.9 mm、h 1.53 mm の抽出 L = L\'·ℓ', Lx, float(EX.lprime(0.3e-3, 1.53e-3)) * ell))
    res.append(check('5c 同じ帯の抽出 R = ρℓ/(wt)', Rx, 1.72e-8 * ell / (0.3e-3 * 35e-6)))
    # 6 相反性: T 字の帯に 3 ポート
    m = np.zeros((2, 40, 60), bool); m[0, 18:21, 2:58] = True; m[0, 2:21, 29:32] = True
    pads = [('A', '1', 0, [(j, 2) for j in range(18, 21)]), ('B', '1', 0, [(j, 57) for j in range(18, 21)]), ('C', '1', 0, [(2, i) for i in range(29, 32)])]
    g = make(m, diel=(1.53,), pads=pads)
    red = EX.reduce(g, 'N', [(p[0], p[0], '1') for p in pads], log=lambda *a: None)
    def net3(src, cap=True):
        n = Netlist(); names = red['names']
        for i, j, R, L in EX.branches(red):
            n.add('R', f'r{i}{j}', names[i], f'm{i}{j}', R); n.add('L', f'l{i}{j}', f'm{i}{j}', names[j], L)
        n.add('R', 'gnd', 'A', '0', 1e-3)       # A を基準へ (源)
        n.add('C', 'cb', 'B', '0', 1e-7); n.add('C', 'cc', 'C', '0', 1e-8)
        n.add('I', 's', '0', src, 1.0)
        return n
    nB, nC = net3('B'), net3('C')
    F, zBC = nB.spice('t', [('C', '0')], 1e4, 5e8, 20); F, zCB = nC.spice('t', [('B', '0')], 1e4, 5e8, 20)
    res.append(check('6a 相反性 Z_CB = Z_BC (ngspice、全周波数の最大差)', 1 + np.max(abs(zBC[0] - zCB[0]) / abs(zBC[0])), 1.0, -1e-3, 1e-3))
    zp = nB.solve(F, [('C', '0')])[0]
    res.append(check('6b ngspice と Python の一致 (Z_BC、最大差)', 1 + np.max(abs(zBC[0] - zp) / abs(zp)), 1.0, -1e-3, 1e-3))
    # 6c 帯域制限した負荷 (④ VREGIN): 1 Ω ∥ C の一次遅れ + VCCS → |V| = ΔI·R / |1 + j f/f_bw|
    fbw = 1e5; n = Netlist(); n.add('I', 's', '0', 'lp', 0.08); n.add('R', 'r1', 'lp', '0', 1.0); n.add('C', 'c1', 'lp', '0', 1 / (2 * math.pi * fbw))
    n.add('G', 'g', 'a', '0', ('lp', '0', 1.0)); n.add('R', 'rl', 'a', '0', 0.5)
    F, z = n.spice('t', [('a', '0')], 1e4, 1e7, 10); zp = n.solve(F, [('a', '0')])[0]
    hand = 0.08 * 0.5 / np.abs(1 + 1j * F / fbw)
    res.append(check('6c 帯域制限した負荷 |V| = ΔI R / |1 + jf/f_bw| (ngspice、最大差)', 1 + np.max(abs(abs(z[0]) - hand) / hand), 1.0, -1e-3, 1e-3))
    res.append(check('6d 同 Python (最大差)', 1 + np.max(abs(abs(zp) - hand) / hand), 1.0, -1e-3, 1e-3))
    # 7 pad_alias: 帯の両端 A.1・B.1 と、A.1 の隣の列の別 pad C.1。セルは重ならないが pad_alias で同じグループ → C.1 は A.1 の別名 (core の判定を使う確認)
    from pdn import run as PR
    w_, n_ = 4, 30; m = np.zeros((2, w_ + 4, n_ + 4), bool); m[0, 2:2 + w_, 2:2 + n_] = True
    pa = [(j, 2) for j in range(2, 2 + w_)]
    g = make(m, dx=0.1, diel=(1.5,), pads=[('A', '1', 0, pa), ('B', '1', 0, [(j, 1 + n_) for j in range(2, 2 + w_)]), ('C', '1', 0, [(j, 3) for j in range(2, 2 + w_)])])
    g.a['pad_alias'] = np.array([0, -1, 0], np.int32)
    red = EX.reduce(g, 'N', [('A', 'A', '1'), ('B', 'B', '1'), ('C', 'C', '1')], log=lambda *a: None)
    L2 = strip_L(w_, n_ - 1, 1.5)[0]      # A と C が 1 節点 → 端は列 3、長さ 1 セル短い帯と同じ
    res.append(check('7a pad_alias: C.1 は A.1 の別名 (alias 表)', 1.0 if red['alias'] == {'C': 'A'} else 0.0, 1.0, -1e-9, 1e-9))
    res.append(check('7b A=C を 1 節点にした A-B の L = 1 セル短い帯の抽出', 1 / -red['Gam'][0, 1], L2, -1e-6, 1e-6))
    an = {'eps_r': {'value': 4.5}}
    g.meta['layers'] = [dict(name='L0', cu_mm=0.035, er_below=4.1, er_below_src='基板 YAML'), dict(name='L1', cu_mm=0.035)]
    e, src = PR.eps_r(g, an)
    res.append(check('7c εr は格子 meta.layers[].er_below から', e, 4.1, -1e-9, 1e-9))
    g.meta['layers'] = [dict(name='L0', cu_mm=0.035), dict(name='L1', cu_mm=0.035)]
    res.append(check('7d er_below が無ければ pdn.yaml の eps_r', PR.eps_r(g, an)[0], 4.5, -1e-9, 1e-9))
    # 8 判定上限 = min(0.35/tr, 準静的の上限, 設定値) (2026-10-14 作者 決定: 設定値 50 MHz)。手計算: tr 0.833 ns → 0.35/tr = 420 MHz、f_qs 328 MHz、設定 50 MHz → 50 MHz
    fj = 0.35 / (0.1 / 120e6)
    res.append(check('8a 設定値 50 MHz が最小 → 50 MHz (cfg)', PR.judge_limit(fj, 328e6, 50e6)[0], 50e6, -1e-9, 1e-9))
    res.append(check('8b 決めた項は cfg', 1.0 if PR.judge_limit(fj, 328e6, 50e6)[1] == 'cfg' else 0.0, 1.0, -1e-9, 1e-9))
    res.append(check('8c 設定なし → min(420, 328) = 328 MHz (qs)', PR.judge_limit(fj, 328e6, None)[0], 328e6, -1e-9, 1e-9))
    res.append(check('8d 0.35/tr 10.5 MHz (tr 33.3 ns) が最小 → 10.5 MHz (tr)', PR.judge_limit(0.35 / 33.333e-9, 328e6, 50e6)[0], 0.35 / 33.333e-9, -1e-9, 1e-9))
    res.append(check('8e 0.35/tr なし・設定 50 MHz → 50 MHz', PR.judge_limit(None, 328e6, 50e6)[0], 50e6, -1e-9, 1e-9))
    c50, src = PR.judge_cap({'analysis': {'f_judge_max': {'value': 5.0e7, 'basis': 'design', 'from': '2026-10-14 作者 決定'}}})
    res.append(check('8f judge_cap は値と根拠 (決定日) を返す', c50 if '2026-10-14' in src else 0.0, 5.0e7, -1e-9, 1e-9))
    res.append(check('8g judge_cap 設定なし → None', 1.0 if PR.judge_cap({'analysis': {}})[0] is None else 0.0, 1.0, -1e-9, 1e-9))
    print(f'{sum(res)}/{len(res)} 通過')
    return all(res)


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
