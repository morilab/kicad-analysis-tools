"""circuit — 集中定数回路 (R/L/C/電流源/0 V 電圧源) の組み立て、ngspice .ac での求解、Python (MNA) での直接解。
節点名 '0' が基準。電流源 ('I', a, b, 値) は SPICE と同じ向き (a → 源 → b、つまり a から電流を引き b に注ぐ)。"""
import os, re, subprocess, tempfile
import numpy as np

NGSPICE = os.environ.get('NGSPICE', '/usr/bin/ngspice')


def _nm(s):
    return '0' if s == '0' else 'n' + re.sub(r'[^A-Za-z0-9_]', '_', s)


class Netlist:
    def __init__(self):
        self.el = []          # (kind, name, a, b, value)

    def add(self, kind, name, a, b, value):
        if kind == 'G':        # 電圧制御電流源: value = (cp, cn, gm)、a → 源 → b に gm·(V(cp) − V(cn))
            self.el.append((kind, name, a, b, (value[0], value[1], float(value[2])))); return
        self.el.append((kind, name, a, b, tuple(map(float, value)) if kind == 'RL' else float(value)))

    @staticmethod
    def _y(kind, v, w):
        if kind == 'R':
            return 1 / v
        if kind == 'L':
            return 1 / (1j * w * v)
        if kind == 'C':
            return 1j * w * v
        return 1 / (v[0] + 1j * w * v[1])      # RL 直列

    def zmatrix(self, freqs, ports):
        """ports: [(a, b)]。各ポートに 1 A を注いだときの全ポートの電圧 → Z (P, P, nf)。電流源・電圧源は 0 として扱う (電圧源は短絡)"""
        N = self.nodes(); ix = {n: k for k, n in enumerate(N)}; n = len(N)
        vs = [e for e in self.el if e[0] == 'V']; m = len(vs); P = len(ports)
        g = lambda x: ix.get(x, -1)
        B = np.zeros((n + m, P))
        for p, (a, b) in enumerate(ports):
            if g(a) >= 0: B[g(a), p] += 1
            if g(b) >= 0: B[g(b), p] -= 1
        Z = np.zeros((P, P, len(freqs)), complex)
        rl = [(g(a), g(b), kind, v) for kind, name, a, b, v in self.el if kind in ('R', 'L', 'C', 'RL')]
        for fi, f in enumerate(freqs):
            w = 2 * np.pi * f
            Y = np.zeros((n + m, n + m), complex)
            for ia, ib, kind, v in rl:
                y = self._y(kind, v, w)
                if ia >= 0: Y[ia, ia] += y
                if ib >= 0: Y[ib, ib] += y
                if ia >= 0 and ib >= 0: Y[ia, ib] -= y; Y[ib, ia] -= y
            for q, (kind, name, a, b, v) in enumerate(vs):
                ia, ib = g(a), g(b); r = n + q
                if ia >= 0: Y[ia, r] += 1; Y[r, ia] += 1
                if ib >= 0: Y[ib, r] -= 1; Y[r, ib] -= 1
            Y[np.arange(n), np.arange(n)] += 1e-12      # gmin (浮いた節点対策、ngspice の既定と同じ大きさ)
            X = np.linalg.solve(Y, B.astype(complex))
            Z[:, :, fi] = B.T @ X
        return Z

    def without(self, prefix):
        n = Netlist(); n.el = [e for e in self.el if not e[1].startswith(prefix)]; return n

    def nodes(self):
        s = []
        seen = {'0'}
        for kind, _, a, b, v in self.el:
            for x in (a, b) + ((v[0], v[1]) if kind == 'G' else ()):
                if x not in seen:
                    seen.add(x); s.append(x)
        return s

    # ---- Python 直接解 (MNA) ----
    def solve(self, freqs, probes, scale_sources=None):
        """probes: [(a, b)] → |V(a) − V(b)| の (len(probes), len(freqs)) 複素配列。scale_sources: {電流源名: 倍率}"""
        N = self.nodes(); ix = {n: k for k, n in enumerate(N)}; n = len(N)
        vs = [e for e in self.el if e[0] == 'V']; m = len(vs)
        out = np.zeros((len(probes), len(freqs)), complex)
        g = lambda x: ix.get(x, -1)
        for fi, f in enumerate(freqs):
            w = 2 * np.pi * f
            Y = np.zeros((n + m, n + m), complex); J = np.zeros(n + m, complex)
            for kind, name, a, b, v in self.el:
                ia, ib = g(a), g(b)
                if kind in ('R', 'L', 'C', 'RL'):
                    y = self._y(kind, v, w)
                    if ia >= 0: Y[ia, ia] += y
                    if ib >= 0: Y[ib, ib] += y
                    if ia >= 0 and ib >= 0: Y[ia, ib] -= y; Y[ib, ia] -= y
                elif kind == 'I':
                    s = v * (scale_sources or {}).get(name, 1.0)
                    if ia >= 0: J[ia] -= s
                    if ib >= 0: J[ib] += s
                elif kind == 'G':
                    cp, cn, gm = g(v[0]), g(v[1]), v[2] * (scale_sources or {}).get(name, 1.0)
                    for r_, sg in ((ia, 1), (ib, -1)):
                        if r_ < 0: continue
                        if cp >= 0: Y[r_, cp] += sg * gm
                        if cn >= 0: Y[r_, cn] -= sg * gm
            for q, (kind, name, a, b, v) in enumerate(vs):
                ia, ib = g(a), g(b); r = n + q
                if ia >= 0: Y[ia, r] += 1; Y[r, ia] += 1
                if ib >= 0: Y[ib, r] -= 1; Y[r, ib] -= 1
                J[r] = 0.0
            x = np.linalg.solve(Y, J)
            V = lambda s: 0.0 if s == '0' else x[ix[s]]
            for p, (a, b) in enumerate(probes):
                out[p, fi] = V(a) - V(b)
        return out

    # ---- ngspice ----
    def to_spice(self, title, probes, f0, f1, ppd, outfile):
        L = [f'* {title}']
        for kind, name, a, b, v in self.el:
            nm = re.sub(r'[^A-Za-z0-9_]', '_', name)
            if kind == 'I':
                L.append(f'I{nm} {_nm(a)} {_nm(b)} DC 0 AC {v:.9g}')
            elif kind == 'G':
                L.append(f'G{nm} {_nm(a)} {_nm(b)} {_nm(v[0])} {_nm(v[1])} {v[2]:.9g}')
            elif kind == 'V':
                L.append(f'V{nm} {_nm(a)} {_nm(b)} DC 0 AC 0')
            elif kind == 'RL':
                mid = _nm(a) + '_' + nm
                if v[1] <= 0:
                    L.append(f'R{nm} {_nm(a)} {_nm(b)} {v[0]:.9g}'); continue
                L.append(f'R{nm} {_nm(a)} {mid} {v[0]:.9g}'); L.append(f'L{nm} {mid} {_nm(b)} {v[1]:.9g}')
            else:
                L.append(f'{kind}{nm} {_nm(a)} {_nm(b)} {v:.9g}')
        vec = ' '.join(f'vr({_nm(a)},{_nm(b)}) vi({_nm(a)},{_nm(b)})' if b != '0' else f'vr({_nm(a)}) vi({_nm(a)})' for a, b in probes)
        L += ['.control', 'set wr_singlescale', 'set wr_vecnames', 'option noinit', f'ac dec {ppd} {f0:g} {f1:g}',
              f'wrdata {outfile} {vec}', 'quit', '.endc', '.end']
        return '\n'.join(L) + '\n'

    def spice(self, title, probes, f0=1e4, f1=5e8, ppd=40, keep=None, log=print):
        """ngspice で解く → (freqs, (len(probes), nf) 複素配列)。probes は 1 回に多すぎると行が長くなるので分割"""
        d = tempfile.mkdtemp(prefix='pdn_sp_')
        F = None; res = []
        for c0 in range(0, len(probes), 40):
            pr = probes[c0:c0 + 40]
            out = os.path.join(d, f'out{c0}.txt'); cir = os.path.join(d, f'c{c0}.cir')
            open(cir, 'w').write(self.to_spice(title, pr, f0, f1, ppd, out))
            p = subprocess.run([NGSPICE, '-b', cir], capture_output=True, text=True, timeout=1800)
            if not os.path.exists(out):
                raise RuntimeError('ngspice 失敗: ' + p.stdout[-2000:] + p.stderr[-2000:])
            a = np.atleast_2d(np.loadtxt(out, skiprows=1))
            F = a[:, 0]; z = a[:, 1::2] + 1j * a[:, 2::2]
            res.append(z.T)
            if keep and c0 == 0:
                os.makedirs(keep, exist_ok=True)
                open(os.path.join(keep, 'circuit.cir'), 'w').write(open(cir).read())
        return F, np.vstack(res)
