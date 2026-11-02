"""env — 環境条件 (YAML) と表面の熱伝達率 h (自然対流・強制対流・放射)。
相関式: Incropera, Fundamentals of Heat and Mass Transfer 7th ed. 式 9.30/9.31/9.32 (水平板、L = 面積/周長)、
式 9.27 (垂直板、Churchill-Chu)、式 7.30 (平板層流 平均 Nu = 0.664 Re^1/2 Pr^1/3)、混合対流は n = 3 の合成 (Churchill)。"""
import math

SIGMA = 5.670374e-8
DEFAULT = dict(ambient_c=50.0, orientation='horizontal_top_up', airflow=dict(velocity_m_s=0.0, direction='x'),
               emissivity=dict(top=0.9, bottom=0.9),
               case=dict(enabled=False, internal_air_rise_c=0.0, h_factor=1.0, wall_rise_c=0.0, wall_emissivity=0.9),
               h_override=dict(top=None, bottom=None))


def merge(d, u):
    out = dict(d)
    for k, v in (u or {}).items():
        out[k] = merge(d[k], v) if isinstance(v, dict) and isinstance(d.get(k), dict) else v
    return out


def air(Tf):
    """空気の物性 (1 atm)、Tf [K]。Incropera 表 A.4 の 300〜400 K を直線近似"""
    k = 0.0263 + (0.0338 - 0.0263) * (Tf - 300) / 100
    nu = 15.89e-6 + (23.38e-6 - 15.89e-6) * (Tf - 300) / 100
    alpha = 22.5e-6 + (33.8e-6 - 22.5e-6) * (Tf - 300) / 100
    return k, nu, alpha, nu / alpha


def h_natural(face, orientation, Ts, Ta, Lc, Lv):
    """face: 'top'/'bottom'。Lc = A/P (水平)、Lv = 垂直方向の長さ。W/m²K"""
    dT = max(Ts - Ta, 0.01); Tf = (Ts + Ta) / 2 + 273.15
    k, nu, alpha, Pr = air(Tf); g = 9.81; beta = 1 / Tf
    if orientation == 'vertical':
        Ra = g * beta * dT * Lv**3 / (nu * alpha)
        Nu = 0.68 + 0.670 * Ra**0.25 / (1 + (0.492 / Pr)**(9 / 16))**(4 / 9)
        return Nu * k / Lv
    hot_up = (face == 'top') == (orientation == 'horizontal_top_up')   # 上向きの熱い面
    Ra = g * beta * dT * Lc**3 / (nu * alpha)
    if hot_up:
        Nu = 0.54 * Ra**0.25 if Ra < 1e7 else 0.15 * Ra**(1 / 3)
    else:
        Nu = 0.27 * Ra**0.25     # 下向きの熱い面 (式 9.32、Ra 1e5..1e10。小さい Ra でも安全側)
    return Nu * k / Lc


def h_forced(Ts, Ta, v, L):
    if v <= 0:
        return 0.0
    k, nu, alpha, Pr = air((Ts + Ta) / 2 + 273.15)
    Re = v * L / nu
    Nu = 0.664 * Re**0.5 * Pr**(1 / 3) if Re < 5e5 else (0.037 * Re**0.8 - 871) * Pr**(1 / 3)
    return Nu * k / L


def h_conv(env, face, Ts, size_xy_m):
    ov = env['h_override'].get(face)
    if ov is not None:
        return float(ov)
    Ta = env['ambient_c'] + (env['case']['internal_air_rise_c'] if env['case']['enabled'] else 0)
    Lx, Ly = size_xy_m
    Lc = Lx * Ly / (2 * (Lx + Ly)); Lv = Ly if env.get('vertical_axis', 'y') == 'y' else Lx
    hn = h_natural(face, env['orientation'], Ts, Ta, Lc, Lv)
    af = env['airflow']; L = Lx if af.get('direction', 'x') == 'x' else Ly
    hf = h_forced(Ts, Ta, float(af.get('velocity_m_s', 0) or 0), L)
    h = (hn**3 + hf**3) ** (1 / 3)
    if env['case']['enabled']:
        h *= float(env['case']['h_factor'])
    return h


def h_rad(eps, Ts_c, Tw_c):
    Ts, Tw = Ts_c + 273.15, Tw_c + 273.15
    return eps * SIGMA * (Ts**2 + Tw**2) * (Ts + Tw)


def sinks(env):
    """(対流の空気温度, 放射の相手の温度、放射の実効放射率の係数)"""
    c = env['case']
    if c['enabled']:
        return env['ambient_c'] + c['internal_air_rise_c'], env['ambient_c'] + c['wall_rise_c'], c['wall_emissivity']
    return env['ambient_c'], env['ambient_c'], 1.0
