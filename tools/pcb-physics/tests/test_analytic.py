#!/usr/bin/env python3
"""解析解との比較 (selftest)。python tests/test_analytic.py — 失敗があれば終了コード 1"""
import math, os, sys
import numpy as np
from scipy.special import k0
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from thermal.model import Network
from thermal import env as E

FAIL = []


def check(name, got, exp, tol):
    e = abs(got - exp) / abs(exp)
    ok = e <= tol
    print(f"{'ok  ' if ok else 'FAIL'} {name}: 計算 {got:.4f} / 解析解 {exp:.4f} (誤差 {e * 100:.2f}%、許容 {tol * 100:.1f}%)")
    if not ok:
        FAIL.append(name)


def const_h(h_top, h_bot):
    def f(face, Tf):
        return (h_top if face == 'top' else h_bot), np.zeros_like(Tf)
    return f


def t1_uniform_two_layer():
    """一様発熱の板 (2 層 + 厚さ方向): 1D の厚さ方向の解析解"""
    n, dx = 20, 1e-3; act = np.ones((n, n), bool); A = dx * dx
    q = 500.0; Gz = 2000.0; h1, h2 = 10.0, 5.0; Ta = 25.0
    net = Network(act, dx); net.area = np.full((n, n), A)
    net.add_layer(np.full((n, n), 0.0135), np.full((n, n), q * A)); net.add_layer(np.full((n, n), 0.0135))
    net.zlinks = [np.full((n, n), Gz * A)]
    net.build(); T, _ = net.solve(const_h(h1, h2), Ta, Ta)
    # q = h1 θ1 + Gz(θ1-θ2), Gz(θ1-θ2) = h2 θ2
    th2 = q / (h1 * (h2 + Gz) / Gz + h2); th1 = th2 * (h2 + Gz) / Gz
    check('T1 一様発熱 上面', net.field(0)[5, 5] - Ta, th1, 1e-6)
    check('T1 一様発熱 下面', net.field(1)[5, 5] - Ta, th2, 1e-6)


def t2_fin():
    """1D フィン (先端断熱)、根元に P: θ(x) = P cosh(m(L-x)) / (g w m sinh(mL))、m = sqrt(2h/g)"""
    N, dx = 400, 0.25e-3; L = N * dx; g = 385 * 35e-6; h = 10.0; P = 0.01; w = dx
    act = np.ones((1, N), bool); net = Network(act, dx); net.area = np.full((1, N), dx * dx)
    qq = np.zeros((1, N)); qq[0, 0] = P; net.add_layer(np.full((1, N), g), qq)
    net.build(); T, _ = net.solve(const_h(h, h), 0.0, 0.0)
    m = math.sqrt(2 * h / g)
    for x_mm in (5.0, 20.0, 60.0):
        i = int(x_mm * 1e-3 / dx); x = (i + 0.5) * dx
        # 有限体積: 発熱はセル 0 の中、熱流の起点はセル 0 の中心 → x を dx/2 ずらす
        exp = P * math.cosh(m * (L - x + dx / 2)) / (g * w * m * math.sinh(m * L))
        check(f'T2 フィン x={x_mm:.0f}mm', T[i], exp, 0.01)


def t3_radial():
    """広い板の点熱源: θ(r) = P/(2π g) K0(m r)、m = sqrt(2h/g) (両面 h)"""
    N, dx = 301, 0.5e-3; g = 385 * 35e-6; h = 10.0; P = 0.2
    act = np.ones((N, N), bool); net = Network(act, dx); net.area = np.full((N, N), dx * dx)
    qq = np.zeros((N, N)); c = N // 2; qq[c, c] = P; net.add_layer(np.full((N, N), g), qq)
    net.build(); T, _ = net.solve(const_h(h, h), 0.0, 0.0)
    F = net.field(0); m = math.sqrt(2 * h / g)
    # 板の大きさ 150 mm = 5.8/m なので端の影響は小さい (K0(m·75mm) / K0(m·10mm) ≈ 3%)。端を補正しない比較なので遠方ほど甘く
    for r_mm, tol in ((5.0, 0.02), (10.0, 0.02), (20.0, 0.04)):
        i = int(round(r_mm * 1e-3 / dx))
        check(f'T3 点熱源 r={r_mm:.0f}mm', F[c, c + i], P / (2 * math.pi * g) * k0(m * i * dx), tol)


def t4_radiation():
    """放射だけの板 (両面 ε=0.9): q = 2 εσ (T⁴ - Ta⁴)"""
    n, dx = 10, 1e-3; act = np.ones((n, n), bool); net = Network(act, dx); net.area = np.full((n, n), dx * dx)
    q = 200.0; eps = 0.9; Ta = 25.0
    net.add_layer(np.full((n, n), 0.0135), np.full((n, n), q * dx * dx)); net.build()

    def f(face, Tf):
        return 0.0, E.h_rad(eps, np.nan_to_num(Tf, nan=Ta), Ta)
    T, it = net.solve(f, Ta, Ta, iters=50, tol=1e-6)
    Tk = (q / (2 * eps * E.SIGMA) + (Ta + 273.15) ** 4) ** 0.25 - 273.15
    check(f'T4 放射のみ (反復 {it} 回)', T[0] - Ta, Tk - Ta, 1e-4)


def t5_h_values():
    """相関式の値: 52×41 mm、表面 80 ℃・周囲 50 ℃ (Incropera の式で手計算: 上面 ≈ 9、下面 ≈ 4.5 W/m²K)"""
    env = E.merge(E.DEFAULT, {})
    ht = E.h_conv(env, 'top', 80.0, (0.052, 0.041)); hb = E.h_conv(env, 'bottom', 80.0, (0.052, 0.041))
    Lc = 0.052 * 0.041 / (2 * (0.052 + 0.041)); k, nu, al, Pr = E.air(65 + 273.15)
    Ra = 9.81 / (65 + 273.15) * 30 * Lc ** 3 / (nu * al)
    check('T5 h 上面 (0.54 Ra^1/4)', ht, 0.54 * Ra ** 0.25 * k / Lc, 1e-9)
    check('T5 h 下面 (0.27 Ra^1/4)', hb, 0.27 * Ra ** 0.25 * k / Lc, 1e-9)
    print(f'     h 上面 {ht:.2f}、下面 {hb:.2f}、放射 {E.h_rad(0.9, 80, 50):.2f} W/m²K')
    env2 = E.merge(E.DEFAULT, {'airflow': {'velocity_m_s': 1.0}})
    print(f"     風速 1 m/s: 上面 {E.h_conv(env2, 'top', 80.0, (0.052, 0.041)):.2f} W/m²K")


for t in (t1_uniform_two_layer, t2_fin, t3_radial, t4_radiation, t5_h_values):
    t()
print('selftest:', 'FAIL ' + ', '.join(FAIL) if FAIL else 'すべて ok')
sys.exit(1 if FAIL else 0)
