#!/usr/bin/env python3
"""USB 2.0 HS (480 Mbps) の差動線を ngspice の結合伝送線路 (CPL、損失なし) で流し、アイパターンと TDR (反射) を比べる。
線の L/C 行列は zdiff2d.py の断面計算から作る (形状を直接指定)、または目標 Zdiff に合わせて合成する。

回路 (1 脚ずつ、D+ と D- は逆相):
  ホスト: 0.8 V (17.78 mA x 45 Ω) のテブナン源 + 45 Ω (HS ドライバの終端) → ケーブル (90 Ω 差動、長さ --cable、損失なし)
  → コネクタ → 基板の線 1 (seg1、コネクタ → ESD) → ESD の容量 (--cesd) → 基板の線 2 (seg2、ESD → IC) → IC の容量 (--cin) と 45 Ω 終端。
  長さの差 (--skew) は D+ 側の seg1 の後ろに 1 本だけの線 (単線 Z0 = sqrt(Zodd·Zeven)) として足す。
信号: PRBS7、立ち上がり 10-90% = --tr (既定 500 ps、HS の最速)。
出力: <out>/eye_<名前>.png、tdr.png、results.json (アイの高さ・幅、TDR の最小/最大インピーダンス)。

使い方 (例: usb-jtag-ft2232h rev0.2 B 案):
  python3 usb_hs_spice.py --out /tmp/si/spice --seg1 5.44 --seg2 4.98 --skew 1.27 \
     --case "現状 MS w0.3 s0.2:geom:1.53,0.3,0.2," --case "調整 CPWG:geom:1.53,0.6,0.15,0.15" --case "145Ω (仮定):z:145"
  --case は「名前:geom:h,w,s,g」(g 空 = マイクロストリップ) か「名前:z:Zdiff」(εeff 3.0、結合は弱いとして合成)。
依存: ngspice、numpy、scipy、matplotlib。
"""
import argparse, json, math, os, subprocess
import numpy as np
import zdiff2d as z

C0 = 299792458.0


def lc_from_case(spec):
    kind, val = spec.split(':', 1)
    if kind == 'geom':
        h, w, s, g = val.split(',')
        r = z.solve(float(h), float(w), float(s), float(g) if g else None)
        return r['L'], r['C'], r['zdiff'], math.sqrt(r['zodd'] * r['zeven'])
    zd = float(val); zo = zd / 2; ze = zo * 1.6; ee = 3.0   # 合成: Zeven/Zodd = 1.6 (中程度の結合)、εeff 3.0
    v = C0 / math.sqrt(ee)
    # 1 線あたり: Lo = Zo/v、Co = 1/(Zo v)。L11 = (Le+Lo)/2、L12 = (Le-Lo)/2、C11 = (Ce+Co)/2、C12 = -(Co-Ce)/2
    Lo, Le, Co, Ce = zo / v, ze / v, 1 / (zo * v), 1 / (ze * v)
    L = np.array([[(Le + Lo) / 2, (Le - Lo) / 2], [(Le - Lo) / 2, (Le + Lo) / 2]])
    C = np.array([[(Ce + Co) / 2, -(Co - Ce) / 2], [-(Co - Ce) / 2, (Ce + Co) / 2]])
    return L, C, zd, math.sqrt(zo * ze)


def prbs7(n):
    s = 0x7F; out = []
    for _ in range(n):
        b = ((s >> 6) ^ (s >> 5)) & 1; s = ((s << 1) | b) & 0x7F; out.append(b)
    return out


def pwl(bits, ui, tr, vhi, invert=False):
    t0 = 2e-9; ramp = tr / 0.8   # 10-90% → 0-100%
    pts = [(0, 0 if not invert else vhi)]
    prev = bits[0] ^ invert
    pts = [(0, vhi * prev), (t0, vhi * prev)]
    for k, b in enumerate(bits[1:], 1):
        b ^= invert
        if b != prev:
            t = t0 + k * ui
            pts += [(t - ramp / 2, vhi * prev), (t + ramp / 2, vhi * b)]
            prev = b
    return ' '.join(f'{t:.4e} {v:.4f}' for t, v in pts)


def netlist(L, C, z0s, a, mode, stim):
    m = lambda M: f'{M[0,0]:.4e} {M[0,1]:.4e} {M[1,1]:.4e}'
    zc = 45.0; vcab = C0 / math.sqrt(2.5)
    # 差動 (奇モード) だけで駆動し、負荷も左右対称なので、結合線路は 奇モードの Zodd・速度の 2 本の伝送線路と等価 (偶モードは励振されない)。
    # ngspice の CPL は短い線で非常に遅いので、既定はこの等価な T 線路を使う (--cpl で CPL)。
    Lo, Co = L[0, 0] - L[0, 1], C[0, 0] - C[0, 1]
    zodd, vod = math.sqrt(Lo / Co), 1 / math.sqrt(Lo * Co)
    if a.cpl:
        lines = ['* usb hs', f'.model PL CPL length={a.seg1*1e-3:.5e} R=0 0 0 L={m(L)} G=0 0 0 C={m(C)}',
                 f'.model PL2 CPL length={a.seg2*1e-3:.5e} R=0 0 0 L={m(L)} G=0 0 0 C={m(C)}']
        seg1 = ['P1 cp cn 0 e1p e1n 0 PL']; seg2 = ['P2 ep en 0 rp rn 0 PL2']
    else:
        lines = ['* usb hs (odd mode equivalent)']
        seg1 = [f'T1p cp 0 e1p 0 Z0={zodd:.2f} TD={a.seg1*1e-3/vod:.4e}', f'T1n cn 0 e1n 0 Z0={zodd:.2f} TD={a.seg1*1e-3/vod:.4e}']
        seg2 = [f'T2p ep 0 rp 0 Z0={zodd:.2f} TD={a.seg2*1e-3/vod:.4e}', f'T2n en 0 rn 0 Z0={zodd:.2f} TD={a.seg2*1e-3/vod:.4e}']
    lines += stim
    lines += ['Rsp sp hp 45', 'Rsn sn hn 45',
              f'Tcp hp 0 cp 0 Z0={zc} TD={a.cable*1e-3/vcab:.4e}', f'Tcn hn 0 cn 0 Z0={zc} TD={a.cable*1e-3/vcab:.4e}',
              ] + seg1
    if a.skew > 0:
        v1 = C0 / math.sqrt(2.9)
        lines += [f'Tsk e1p 0 ep 0 Z0={z0s:.2f} TD={a.skew*1e-3/v1:.4e}', 'Rsk e1n en 1e-3']
    else:
        lines += ['Rsk1 e1p ep 1e-3', 'Rsk2 e1n en 1e-3']
    lines += [f'Cep ep 0 {a.cesd}p', f'Cen en 0 {a.cesd}p'] + seg2 + [
              f'Crp rp 0 {a.cin}p', f'Crn rn 0 {a.cin}p', 'Rrp rp 0 45', 'Rrn rn 0 45']
    return lines


def run(lines, tstop, out, nodes):
    cir = out + '.cir'
    open(cir, 'w').write('\n'.join(lines + [f'.tran 5p {tstop:.4e}', '.control', 'run',
                                           f'wrdata {out}.txt ' + ' '.join(f'v({n})' for n in nodes), '.endc', '.end']) + '\n')
    subprocess.run(['ngspice', '-b', cir], capture_output=True, check=True)
    d = np.loadtxt(out + '.txt')
    return d[:, 0], [d[:, 2 * i + 1] for i in range(len(nodes))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--seg1', type=float, default=5.0, help='コネクタ → ESD の長さ mm')
    ap.add_argument('--seg2', type=float, default=5.0, help='ESD → IC の長さ mm')
    ap.add_argument('--skew', type=float, default=0.0, help='D+ と D- の長さの差 mm')
    ap.add_argument('--cable', type=float, default=500.0, help='ケーブル長 mm (90 Ω、損失なし)')
    ap.add_argument('--cesd', type=float, default=1.2, help='ESD の 1 脚あたり容量 pF (USBLC6-2 の I/O-GND 最大の目安)')
    ap.add_argument('--cin', type=float, default=2.0, help='IC の入力容量 pF (仮定)')
    ap.add_argument('--tr', type=float, default=500e-12)
    ap.add_argument('--bits', type=int, default=254)
    ap.add_argument('--cpl', action='store_true', help='ngspice の CPL (結合伝送線路) を使う (遅い)')
    ap.add_argument('--case', action='append', required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    import matplotlib; matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.family'] = ['Noto Sans CJK JP', 'DejaVu Sans']
    ui = 1 / 480e6
    bits = prbs7(a.bits)
    res = {}
    fig_t, ax_t = plt.subplots(figsize=(8, 4.5))
    for spec in a.case:
        name, kind = spec.split(':', 1)
        L, C, zd, z0s = lc_from_case(kind)
        tag = f'case{len(res)}'
        # アイ
        stim = [f'Vp sp 0 PWL({pwl(bits, ui, a.tr, 0.8)})', f'Vn sn 0 PWL({pwl(bits, ui, a.tr, 0.8, True)})']
        t, (vp, vn) = run(netlist(L, C, z0s, a, 'eye', stim), 2e-9 + a.bits * ui + 5e-9, os.path.join(a.out, tag + '_eye'), ['rp', 'rn'])
        vd = vp - vn
        tdel = a.cable * 1e-3 / (C0 / math.sqrt(2.5)) + (a.seg1 + a.seg2) * 1e-3 / (C0 / math.sqrt(3.0))
        tt = t - 2e-9 - tdel
        sel = (tt > 10 * ui) & (tt < (a.bits - 2) * ui)
        ph = np.mod(tt[sel], ui); v = vd[sel]
        # 目の中心 (位相を 64 区切りで最も開く所) で高さ、0 V を横切る位相の幅
        nb = 64; best = (-1, 0); hk = {}
        for k in range(nb):
            m = (ph >= k * ui / nb) & (ph < (k + 1) * ui / nb)
            if m.sum() < 4:
                continue
            hi, lo = v[m][v[m] > 0], v[m][v[m] < 0]
            if len(hi) and len(lo):
                hgt = hi.min() - lo.max(); hk[k] = hgt
                if hgt > best[0]:
                    best = (hgt, k)
        sign = np.sign(vd); cr = np.where(np.diff(sign[sel]) != 0)[0]
        ang = np.mod(tt[sel][cr], ui) / ui * 2 * np.pi
        c = (np.angle(np.mean(np.exp(1j * ang))) / (2 * np.pi) * ui) % ui        # 0 V を横切る位相の平均 (円周平均)
        dev = (np.mod(tt[sel][cr], ui) - c + ui / 2) % ui - ui / 2
        jitter = float(dev.max() - dev.min()) if len(dev) else ui
        cen = (c + ui / 2) % ui                                                 # 目の中心
        dph = (ph - cen + ui / 2) % ui - ui / 2
        m = np.abs(dph) <= 0.2 * ui                                             # 中心 ±20% UI
        h50 = float(v[m][v[m] > 0].min() - v[m][v[m] < 0].max())
        fig, ax = plt.subplots(figsize=(6, 4))
        tsel = tt[sel]
        for k in range(10, a.bits - 3):
            m = (tsel >= (k - 0.5) * ui) & (tsel < (k + 1.5) * ui)
            ax.plot((tsel[m] - k * ui) * 1e9, v[m] * 1e3, color='tab:blue', lw=0.4, alpha=0.35)
        ax.axhline(400, ls=':', c='gray'); ax.axhline(-400, ls=':', c='gray')
        ax.set_title(f'{name}: Zdiff {zd:.0f} Ω\n目の高さ (中心 ±20% UI の最小) {h50*1e3:.0f} mV、ジッタ {jitter*1e12:.0f} ps', fontsize=10)
        ax.set_xlabel('時間 ns (1 UI = 2.083 ns)'); ax.set_ylabel('受信端の差動電圧 mV'); ax.grid(alpha=0.3)
        fig.tight_layout(); fig.savefig(os.path.join(a.out, f'eye_{tag}.png'), dpi=110); plt.close(fig)
        # TDR: ステップ (立ち上がり --tr) をホストから入れ、コネクタの差動電圧から差動インピーダンスを読む
        stim = [f'Vp sp 0 PWL(0 0 1n 0 {1e-9 + a.tr/0.8:.4e} 0.8)', f'Vn sn 0 PWL(0 0.8 1n 0.8 {1e-9 + a.tr/0.8:.4e} 0)']
        t2, (cpv, cnv) = run(netlist(L, C, z0s, a, 'tdr', stim), 1e-9 + 2 * a.cable * 1e-3 / (C0 / math.sqrt(2.5)) + 6e-9,
                             os.path.join(a.out, tag + '_tdr'), ['hp', 'hn'])
        vdh = cpv - cnv   # ホスト側 (源の 45 Ω の後) の差動電圧。入射 = 0.8 V (差動 1.6 V の半分)
        tcab = 2 * a.cable * 1e-3 / (C0 / math.sqrt(2.5))
        tr0 = t2 - 1e-9 - tcab
        win = (tr0 > -0.5e-9) & (tr0 < 3e-9)
        vinc = 0.8
        x = (vdh[win] - vdh[0]) / vinc              # 入射からの変化 = 1+ρ (ステップ前の直流分を引く)
        zt = 90 * x / (2 - x)                        # Z = 90 (1+ρ)/(1-ρ)
        ax_t.plot(tr0[win] * 1e9, zt, label=f'{name} (Zdiff {zd:.0f} Ω)')
        res[name] = dict(zdiff=round(zd, 1), eye_height_mV=round(float(best[0]) * 1e3, 1), eye_height_mid50_mV=round(float(h50) * 1e3, 1), jitter_ps=round(float(jitter) * 1e12, 1),
                         vmax_mV=round(float(v.max()) * 1e3, 1), tdr_zmin=round(float(zt.min()), 1), tdr_zmax=round(float(zt.max()), 1),
                         eye_png=f'eye_{tag}.png')
        print(name, res[name], flush=True)
    ax_t.axhline(90, ls=':', c='gray'); ax_t.set_xlabel('コネクタ通過後の時間 ns (往復)'); ax_t.set_ylabel('TDR で見た差動インピーダンス Ω')
    ax_t.set_title(f'TDR (立ち上がり {a.tr*1e12:.0f} ps、ESD {a.cesd} pF、IC {a.cin} pF)'); ax_t.grid(alpha=0.3); ax_t.legend(fontsize=8)
    fig_t.tight_layout(); fig_t.savefig(os.path.join(a.out, 'tdr.png'), dpi=110)
    json.dump(dict(params=vars(a), results=res), open(os.path.join(a.out, 'results.json'), 'w'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
