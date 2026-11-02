"""高速テスト用: KiCad を使わずに Gerber / Excellon / IPC-D-356 を手書きで作る小さな合成基板。
正解 (D356) と製造データ (Gerber/drill) を別々に組み立てるので、片方だけ壊して故障を注入できる。"""
import os, copy


def gerber(path, func, body, aps='', unit='MM', fs='LAX46Y46', extra=''):
    k = 1e6 if unit == 'MM' else 1e6 / 25.4
    with open(path, 'w') as f:
        f.write(f'%TF.GenerationSoftware,synth*%\n%TF.FileFunction,{func}*%\n{extra}%FS{fs}*%\n%MO{unit}*%\n%LPD*%\nG01*\n{aps}{body}M02*\n')


def C(v, unit='MM'):
    return round(v * (1e6 if unit == 'MM' else 1e6 / 25.4))


class Board:
    """layers: 層数。pads: dict(name, net, layer(1..n), x, y, w, h)、tht/via: dict(name, net, x, y, d, ring, span)、
    tracks: (layer, net, (x0,y0), (x1,y1), width)、zones: (layer, net, [(x,y)...])"""

    def __init__(self, n=2, w=20.0, h=12.0):
        self.n, self.w, self.h = n, w, h
        self.pads, self.holes, self.tracks, self.zones, self.npth, self.raw = [], [], [], [], [], {}
        self.drill_unit = 'METRIC'; self.unit = 'MM'

    def clone(self): return copy.deepcopy(self)

    def lname(self, li):
        return 'F_Cu' if li == 1 else ('B_Cu' if li == self.n else f'In{li - 1}_Cu')

    def side(self, li):
        return 'Top' if li == 1 else ('Bot' if li == self.n else 'Inr')

    # -------- 製造データ
    def write(self, d, skip_layers=(), rename=None):
        os.makedirs(d, exist_ok=True); u = self.unit
        for li in range(1, self.n + 1):
            if li in skip_layers: continue
            aps, body, k = [], [], [10]
            def ap(spec):
                if spec not in aps: aps.append(spec)
                return 10 + aps.index(spec)
            for t in self.tracks:
                if t[0] != li: continue
                a = ap(f'C,{t[4]:.4f}'); body.append(f'%TO.N,{t[1]}*%\nD{a}*\nX{C(t[2][0], u)}Y{C(t[2][1], u)}D02*\nX{C(t[3][0], u)}Y{C(t[3][1], u)}D01*\n%TD*%\n')
            for p in self.pads:
                if p['layer'] != li: continue
                a = ap(f"R,{p['w']:.4f}X{p['h']:.4f}"); body.append(f"%TO.N,{p['net']}*%\nD{a}*\nX{C(p['x'], u)}Y{C(p['y'], u)}D03*\n%TD*%\n")
            for hl in self.holes:
                if not (hl['span'][0] <= li <= hl['span'][1]) or li in hl.get('no_ring', ()): continue
                a = ap(f"C,{hl['ring']:.4f}"); body.append(f"%TO.N,{hl['net']}*%\nD{a}*\nX{C(hl['x'], u)}Y{C(hl['y'], u)}D03*\n%TD*%\n")
            for z in self.zones:
                if z[0] != li: continue
                pts = z[2] + [z[2][0]]
                body.append(f'%TO.N,{z[1]}*%\nG36*\n' + f'X{C(pts[0][0], u)}Y{C(pts[0][1], u)}D02*\n' + ''.join(f'X{C(x, u)}Y{C(y, u)}D01*\n' for x, y in pts[1:]) + 'G37*\n%TD*%\n')
            body += self.raw.get(li, [])
            def conv(spec):  # aperture の寸法もファイルの単位で書く
                if u == 'MM': return spec
                kind, nums = spec.split(',')
                return kind + ',' + 'X'.join(f'{float(v) / 25.4:.6f}' for v in nums.split('X'))
            apd = ''.join(f'%ADD{10 + i}{conv(s)}*%\n' for i, s in enumerate(aps))
            nm = (rename or {}).get(li, self.lname(li))
            gerber(os.path.join(d, f'sb-{nm}.gbr'), f'Copper,L{li},{self.side(li)}', ''.join(body), apd, unit=u)
        e = [(0, 0), (self.w, 0), (self.w, self.h), (0, self.h), (0, 0)]
        gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', '%ADD10C,0.05*%\nD10*\n' + f'X{C(0, u)}Y{C(0, u)}D02*\n' + ''.join(f'X{C(x, u)}Y{C(y, u)}D01*\n' for x, y in e[1:]), unit=u)
        self.write_drill(d)
        return d

    def write_drill(self, d):
        k = 1.0 if self.drill_unit == 'METRIC' else 1 / 25.4
        groups = {}
        for hl in self.holes: groups.setdefault(tuple(hl['span']), []).append(hl)
        files = []
        for span, hs in groups.items():
            nm = 'sb.drl' if span == (1, self.n) else f'sb-{span[0]}-{span[1]}.drl'
            L = ['M48', f'; #@! TF.FileFunction,Plated,{span[0]},{span[1]},PTH', 'FMAT,2', self.drill_unit]
            tools = sorted({h['d'] for h in hs})
            for i, t in enumerate(tools): L.append(f'T{i + 1}C{t * k:.4f}')
            L += ['%', 'G90', 'G05']
            for i, t in enumerate(tools):
                L.append(f'T{i + 1}')
                L += [f"X{h['x'] * k:.4f}Y{h['y'] * k:.4f}" for h in hs if h['d'] == t]
            L.append('M30'); open(os.path.join(d, nm), 'w').write('\n'.join(L) + '\n'); files.append(nm)
        if self.npth:
            L = ['M48', '; #@! TF.FileFunction,NonPlated,1,%d,NPTH' % self.n, 'FMAT,2', self.drill_unit]
            for i, (x, y, dd) in enumerate(self.npth): L.append(f'T{i + 1}C{dd * k:.4f}')
            L += ['%', 'G90', 'G05']
            for i, (x, y, dd) in enumerate(self.npth): L += [f'T{i + 1}', f'X{x * k:.4f}Y{y * k:.4f}']
            L.append('M30'); open(os.path.join(d, 'sb-NPTH.drl'), 'w').write('\n'.join(L) + '\n')

    # -------- 正解 (IPC-D-356、UNITS CUST 1 = mm/µm)
    def write_d356(self, path):
        L = ['C  synth', 'P  UNITS CUST 1']
        def rec(t, net, ref, pin, acc, x, y, sx, sy, hole=None):
            s = f'{t}{net[:14]:<14}   {ref:<6} {pin:<4} '
            s = s.ljust(32)
            hs = f'D{round(hole * 1000):04d}P' if hole else '     '
            L.append(f'{s}{hs}A{acc:02d}X{round(x * 1000):+07d}Y{round(y * 1000):+07d}X{round(sx * 1000):04d}Y{round(sy * 1000):04d}R000S0')
        for p in self.pads: rec('327', p['net'], p['ref'], p['pin'], p['layer'], p['x'], p['y'], p['w'], p['h'])
        for h in self.holes:
            acc = 0 if tuple(h['span']) == (1, self.n) else h['span'][0]
            rec('317', h['net'], h.get('ref', 'VIA'), h.get('pin', ''), acc, h['x'], h['y'], h['ring'], h['ring'], h['d'])
        for x, y, dd in self.npth:
            L.append(f"367{'':<14}   {'H1':<6} {'':<4} ".ljust(32) + f'D{round(dd * 1000):04d}UA00X{round(x * 1000):+07d}Y{round(y * 1000):+07d}X{round(dd * 1000):04d}Y{round(dd * 1000):04d}R000S0')
        L.append('999'); open(path, 'w').write('\n'.join(L) + '\n'); return path


def base2():
    """2 層: SIG_A = U1-1 (表) → via → U2-1 (裏)、SIG_B = U1-2 → U2-2 (表のみ)、GND = J1-1 (スルー) と表のベタ"""
    b = Board(2)
    b.pads += [dict(ref='U1', pin='1', net='SIG_A', layer=1, x=3, y=4, w=1, h=1), dict(ref='U2', pin='1', net='SIG_A', layer=2, x=15, y=4, w=1, h=1),
               dict(ref='U1', pin='2', net='SIG_B', layer=1, x=3, y=7, w=1, h=1), dict(ref='U2', pin='2', net='SIG_B', layer=1, x=15, y=7, w=1, h=1),
               dict(ref='U3', pin='1', net='GND', layer=1, x=3, y=10, w=1, h=1)]
    b.holes += [dict(net='SIG_A', x=9, y=4, d=0.3, ring=0.6, span=(1, 2)),
                dict(ref='J1', pin='1', net='GND', x=15, y=10, d=1.0, ring=1.7, span=(1, 2))]
    b.tracks += [(1, 'SIG_A', (3, 4), (9, 4), 0.25), (2, 'SIG_A', (9, 4), (15, 4), 0.25), (1, 'SIG_B', (3, 7), (15, 7), 0.25)]
    b.zones += [(1, 'GND', [(2, 9.2), (17, 9.2), (17, 11), (2, 11)])]
    b.npth += [(18.5, 2, 1.0)]
    return b


def base4_blind():
    """4 層: ブラインド via L1-L2 と ベリード via L2-L3、スルー via。SIG_C = U1-1(L1) →blind→ L2 →buried→ L3 →thru→ U2-1(L4)"""
    b = Board(4)
    b.pads += [dict(ref='U1', pin='1', net='SIG_C', layer=1, x=3, y=4, w=1, h=1), dict(ref='U2', pin='1', net='SIG_C', layer=4, x=17, y=4, w=1, h=1),
               dict(ref='U1', pin='2', net='SIG_D', layer=1, x=3, y=8, w=1, h=1), dict(ref='U2', pin='2', net='SIG_D', layer=4, x=17, y=8, w=1, h=1)]
    b.holes += [dict(net='SIG_C', x=6, y=4, d=0.2, ring=0.45, span=(1, 2)), dict(net='SIG_C', x=10, y=4, d=0.2, ring=0.45, span=(2, 3)),
                dict(net='SIG_C', x=14, y=4, d=0.3, ring=0.6, span=(1, 4)), dict(net='SIG_D', x=10, y=8, d=0.3, ring=0.6, span=(1, 4))]
    b.tracks += [(1, 'SIG_C', (3, 4), (6, 4), 0.2), (2, 'SIG_C', (6, 4), (10, 4), 0.2), (3, 'SIG_C', (10, 4), (14, 4), 0.2), (4, 'SIG_C', (14, 4), (17, 4), 0.2),
                 (1, 'SIG_D', (3, 8), (10, 8), 0.2), (4, 'SIG_D', (10, 8), (17, 8), 0.2)]
    return b
