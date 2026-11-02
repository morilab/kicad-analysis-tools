#!/usr/bin/env python3
"""gerber-lvs: 製造データ (銅層 Gerber + Excellon) から実際の銅の導通を求め、
KiCad の正解接続 (kicad-cli pcb export ipcd356) と pad 単位で比べる。
  short / open / pad・via の銅や穴の欠落 + 補助検査 (NPTH 上の銅、外形 clearance、外形外の銅、paste 下の銅)

使い方:
  gerber_lvs.py <project_dir> [name]                         # <dir>/out/gerber を検査、結果は <dir>/out/gerber_lvs.{txt,json}
  gerber_lvs.py <project_dir> [name] --png                   # 問題箇所の切り抜き PNG を <dir>/out/gerber_lvs/ に
  gerber_lvs.py --gerber-dir G --d356 F.d356 [--pro P.kicad_pro] --out-dir O
終了コード: 0 = error 0 件 / 1 = error あり (--strict なら warning も) / 2 = 実行できなかった
元の .kicad_pcb は読むだけ (IPC-D-356 は一時ディレクトリのコピーから出す)。"""
import argparse, glob, json, math, os, re, shutil, subprocess, sys, tempfile, time, traceback
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gerberlib as gl

VERSION = '1.1'
AUX_DEFAULT = False  # DRC 系の補助検査の既定: 省く (2026-10-14 に tools/gerber-drc へ移設、--aux で旧動作)
LEVELS = ('error', 'warning', 'info')


# ---------------------------------------------------------------- 入力の発見
def head_attr(path, n=40):
    with open(path, encoding='utf-8', errors='replace') as f:
        for _ in range(n):
            ln = f.readline()
            if not ln: break
            m = re.search(r'%TF\.FileFunction,([^*]*)\*%', ln)
            if m: return m.group(1).split(',')
    return None


def discover(gdir, info=None):
    """info (dict) を渡すと層の整合チェック用に {path: (FileFunction の層番号 or None, ファイル名の層名 or None)} を入れる"""
    copper, edge, paste, drills = {}, None, {}, []
    for f in sorted(os.listdir(gdir)):
        p = os.path.join(gdir, f)
        if not os.path.isfile(p): continue
        if f.lower().endswith(('.drl', '.xln', '.exc')):
            drills.append(p); continue
        if f.endswith(('.gbrjob', '.zip')) or 'drl_map' in f: continue
        ff = head_attr(p)
        if info is not None:
            mm = re.search(r'-(F_Cu|B_Cu|In\d+_Cu)\.', f)
            if (ff and ff[0] == 'Copper') or mm:
                info[p] = (int(ff[1][1:]) if ff and ff[0] == 'Copper' else None, ff[2] if ff and ff[0] == 'Copper' and len(ff) > 2 else None,
                           mm.group(1) if mm else None)
        if ff:
            if ff[0] == 'Copper':
                copper[int(ff[1][1:])] = p
            elif ff[0] == 'Profile':
                edge = p
            elif ff[0] == 'Paste':
                paste['top' if ff[1] == 'Top' else 'bot'] = p
            continue
        m = re.search(r'-(F_Cu|B_Cu|In(\d+)_Cu|Edge_Cuts|F_Paste|B_Paste)\.', f)
        if m:
            k = m.group(1)
            if k == 'F_Cu': copper[1] = p
            elif k == 'B_Cu': copper[99] = p
            elif k.startswith('In'): copper[int(m.group(2)) + 1] = p
            elif k == 'Edge_Cuts': edge = p
            else: paste['top' if k == 'F_Paste' else 'bot'] = p
    if 99 in copper:  # ファイル名だけで判断したとき
        copper[max([k for k in copper if k != 99] + [1]) + 1] = copper.pop(99)
    layers = [copper[k] for k in sorted(copper)]
    return layers, edge, paste, drills


def find_gerber_dir(P):
    """<P>/out/gerber、無ければ版ごとの <P>/out/<rev>/gerber のうち新しいもの (templates/board-project の形)"""
    g = os.path.join(P, 'out', 'gerber')
    if os.path.isdir(g): return g
    revs = [d for d in glob.glob(os.path.join(P, 'out', '*', 'gerber')) if os.path.isdir(d)]
    if not revs: return g
    def ver(d):
        nums = re.findall(r'\d+', os.path.basename(os.path.dirname(d)))
        return ([int(x) for x in nums], os.path.getmtime(d))
    return max(revs, key=ver)


def lname(path):
    m = re.search(r'-(F_Cu|B_Cu|In\d+_Cu)\.', os.path.basename(path))
    return m.group(1).replace('_', '.') if m else os.path.basename(path)


def read_rules(pro):
    rules = dict(edge=None, hole=None, src=None)
    if pro and os.path.exists(pro):
        r = json.load(open(pro)).get('board', {}).get('design_settings', {}).get('rules', {})
        rules.update(edge=r.get('min_copper_edge_clearance'), hole=r.get('min_hole_clearance'), src=os.path.basename(pro))
    return rules


def aux_origin(pcb):
    if not pcb or not os.path.exists(pcb): return None
    with open(pcb, encoding='utf-8', errors='replace') as f:
        for ln in f:
            m = re.search(r'\(aux_axis_origin\s+([-\d.]+)\s+([-\d.]+)\)', ln)
            if m: return float(m.group(1)), float(m.group(2))
            if '(footprint' in ln: break
    return (0.0, 0.0)


def export_d356(pcb, pro):
    """元ファイルを汚さないよう一時ディレクトリにコピーして出す"""
    tmp = tempfile.mkdtemp(prefix='glvs-')
    base = os.path.splitext(os.path.basename(pcb))[0]
    shutil.copy2(pcb, os.path.join(tmp, base + '.kicad_pcb'))
    if pro and os.path.exists(pro): shutil.copy2(pro, os.path.join(tmp, base + '.kicad_pro'))
    out = os.path.join(tmp, base + '.d356')
    r = subprocess.run(['kicad-cli', 'pcb', 'export', 'ipcd356', '-o', out, os.path.join(tmp, base + '.kicad_pcb')],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f'kicad-cli pcb export ipcd356 失敗: {r.stderr.strip() or r.stdout.strip()}')
    return out, tmp


# ---------------------------------------------------------------- 本体
class LVS:
    def __init__(self, gdir, d356, rules, aux=None, px=0.01, edge_clr=None, hole_clr=None, log=print, aux_checks=True):
        """aux_checks=False: 製造データ DRC 系の補助検査 (NPTH 上の銅・NPTH/外形 clearance・外形外の銅・paste 下の銅・外形が閉じていない) を省く。
        これらは tools/gerber-drc に移した (2026-10-14)。CLI の既定は省く (--aux で旧動作)"""
        self.gdir, self.d356, self.px, self.log = gdir, d356, px, log; self.aux_checks = aux_checks
        self.findings = []; self.T = {}; self.notes = []
        self.edge_clr = edge_clr if edge_clr is not None else (rules['edge'] if rules['edge'] is not None else 0.2)
        self.hole_clr = hole_clr if hole_clr is not None else (rules['hole'] if rules['hole'] is not None else 0.2)
        if edge_clr is None and rules['edge'] is None: self.notes.append('.kicad_pro が無いので外形 clearance は既定 0.2 mm')
        if hole_clr is None and rules['hole'] is None: self.notes.append('.kicad_pro が無いので穴 clearance は既定 0.2 mm')
        self.rules_src = rules['src']; self.aux = aux
        self.tol = 2 * px + 0.005  # ラスタの太り (約 1 px) + 円弧の折れ線化

    # -- 報告
    def add(self, level, code, msg, at=None, **kw):
        f = dict(level=level, code=code, msg=msg); f.update(kw)
        if at is not None:
            f['at'] = [round(at[0], 3), round(at[1], 3)]
            if self.aux is not None:
                dx, dy = at[0] - self.off[0], at[1] - self.off[1]
                f['at_kicad'] = [round(self.aux[0] + dx, 3), round(self.aux[1] - dy, 3)]
        self.findings.append(f); return f

    def padname(self, p):
        if p['ref'] == 'VIA': return f"VIA({p['x'] + self.off[0]:.3f},{p['y'] + self.off[1]:.3f})"
        return f"{p['ref']}-{p['pin']}" if p['pin'] else p['ref']

    def run(self):
        t0 = time.time()
        files, edgef, pastef, drills = discover(self.gdir)
        if not files: raise RuntimeError(f'銅層 Gerber が無い: {self.gdir}')
        if not edgef: raise RuntimeError(f'Edge_Cuts (Profile) Gerber が無い: {self.gdir}')
        if not drills: raise RuntimeError(f'drill ファイル (.drl) が無い: {self.gdir}')
        self.layer_names = [lname(f) for f in files]; n = len(files)
        gs = [gl.Gerber(f) for f in files]; edge = gl.Gerber(edgef)
        self.holes = [h for d in drills for h in gl.parse_drill(d, n)]
        self._layer_consistency(files, gs, edge)
        self._gbrjob(files)
        self.pts = gl.parse_d356(self.d356)
        self.T['parse'] = time.time() - t0
        self._origin()
        # ラスタの範囲: 外形 + 銅 + 余白
        bbs = [b for b in [edge.bbox()] + [g.bbox() for g in gs] if b]
        x0 = min(b[0] for b in bbs) - 0.5; y0 = min(b[1] for b in bbs) - 0.5
        x1 = max(b[2] for b in bbs) + 0.5; y1 = max(b[3] for b in bbs) + 0.5
        R = self.R = gl.Raster(x0, y0, x1, y1, self.px)
        self.log(f'raster {R.W}x{R.H} px ({self.px} mm/px), {n} 層, 穴 {len(self.holes)}, test point {len(self.pts)}')
        t1 = time.time()
        self._outline(edge)
        self.T['outline'] = time.time() - t1
        self.runs = []; self.gs = gs
        tr = tc = ta = 0.0
        for li, g in enumerate(gs):
            t1 = time.time(); img = gl.render(g, R); tr += time.time() - t1
            t1 = time.time()
            if self.aux_checks: self._npth_check(li, img)
            self._drill_out(li, img)
            if self.aux_checks: self._edge_check(li, img)
            ta += time.time() - t1
            t1 = time.time(); self.runs.append(gl.Runs(img)); tc += time.time() - t1
            del img
        del self.zone
        self.T.update(render=tr, aux=ta, ccl=tc)
        t1 = time.time()
        self._layer_offset(gs)
        self._connect(); self._compare(); self._x2_crosscheck(gs)
        if self.aux_checks: self._paste(pastef)
        self._floating()
        self.T['connect'] = time.time() - t1
        t1 = time.time(); self._locate_shorts(); self.T['locate'] = time.time() - t1
        if not self.aux_checks: self.notes.append('DRC 系の補助検査 (NPTH・外形 clearance・外形外の銅・paste) は省略 → tools/gerber-drc')
        self.T['total'] = time.time() - t0
        return self

    # -- 入力の整合: 層の欠落・取り違え・単位 (接続を正しく求める前提。合わなければ LVS 結果自体が信用できない)
    def _layer_consistency(self, files, gs, edge):
        info = {}; discover(self.gdir, info); n = len(files)
        nums = [info.get(f, (None,))[0] for f in files]
        if all(v is not None for v in nums):
            exp = list(range(1, max(nums) + 1))
            miss = [k for k in exp if k not in nums]
            if miss:
                self.add('error', 'LAYER_MISSING', f'銅層 L{",L".join(map(str, miss))} の Gerber が無い (FileFunction の層番号が {nums})', layers_found=nums)
        for f in files:
            num, side, nm = info.get(f, (None, None, None))
            if num is None or nm is None: continue
            want = 1 if nm == 'F_Cu' else (int(nm[2:-3]) + 1 if nm.startswith('In') else None)
            bad = (want is not None and want != num) or (nm == 'B_Cu' and (side not in (None, 'Bot') or num != n)) or (nm == 'F_Cu' and side not in (None, 'Top'))
            if bad:
                self.add('error', 'LAYER_NAME_MISMATCH', f'{os.path.basename(f)}: ファイル名は {nm} だが中身の FileFunction は L{num},{side} (層の取り違え?)', file=os.path.basename(f))
        span = max([h.span[1] for h in self.holes] + [0])
        if span > n:
            self.add('error', 'LAYER_MISSING', f'drill の層範囲は L1-L{span} だが銅層 Gerber は {n} 枚', drill_span=span, copper_layers=n)
        units = {os.path.basename(g.path): g.unit for g in gs + [edge]}
        if len(set(units.values())) > 1:
            self.notes.append(f'Gerber の単位が混在 ({units})。各ファイルの %MO に従って mm に換算した')

    # -- .gbrjob (KiCad の job file) との整合: 層数・ファイル一式・層機能
    def _gbrjob(self, files):
        jobs = [f for f in os.listdir(self.gdir) if f.endswith('.gbrjob')]
        if not jobs:
            self.notes.append('.gbrjob が無いので job file との整合は見ていない'); return
        if len(jobs) > 1:
            self.add('warning', 'GBRJOB_MISMATCH', f'.gbrjob が {len(jobs)} 個ある ({", ".join(sorted(jobs))})。最初のものだけ見る')
        jn = sorted(jobs)[0]
        try: j = json.load(open(os.path.join(self.gdir, jn), encoding='utf-8'))
        except Exception as e:
            self.add('warning', 'GBRJOB_MISMATCH', f'{jn} が読めない: {e}'); return
        n = len(files); ln = j.get('GeneralSpecs', {}).get('LayerNumber')
        if ln is not None and int(ln) != n:
            self.add('error', 'GBRJOB_MISMATCH', f'{jn}: 層数 {ln} だが銅層 Gerber は {n} 枚', job_layers=ln, copper_layers=n)
        cu_by_path = {os.path.basename(f): f for f in files}
        listed = set()
        for fa in j.get('FilesAttributes', []):
            p, ff, pol = fa.get('Path', ''), fa.get('FileFunction', ''), fa.get('FilePolarity', 'Positive')
            listed.add(p); full = os.path.join(self.gdir, p)
            cu = ff.startswith('Copper') or ff.startswith('Profile')
            if not os.path.exists(full):
                self.add('error' if cu else 'warning', 'GBRJOB_MISMATCH', f'{jn} にある {p} ({ff}) が無い', file=p); continue
            if ff.startswith('Copper'):
                own = head_attr(full)
                if own and ','.join(own[:3]) != ','.join(ff.split(',')[:3]):
                    self.add('error', 'GBRJOB_MISMATCH', f'{p}: job file では {ff} だがファイルの中身は {",".join(own)}', file=p)
                if p not in cu_by_path:
                    self.add('error', 'GBRJOB_MISMATCH', f'{p}: job file では銅層 ({ff}) だが銅層として読めなかった', file=p)
                if pol != 'Positive':
                    self.add('error', 'GBRJOB_MISMATCH', f'{p}: 銅層の極性が {pol} (負極性の銅層は未対応)', file=p)
        for p in cu_by_path:
            if p not in listed:
                self.add('warning', 'GBRJOB_MISMATCH', f'銅層 {p} が {jn} に載っていない (別の出力の残り?)', file=p)

    # -- 片層だけの原点ずれ: 各層の flash (pad / via ランド) とめっき穴の位置のずれを投票で求める
    def _layer_offset(self, gs, cell=0.01, near=5.0):
        for li, g in enumerate(gs):
            H = np.array([(h.x, h.y) for h in self.holes if h.plated and not h.is_slot and h.span[0] <= li + 1 <= h.span[1]])
            F = np.array([o[2][1] for o in g.objs if o[1] == 'flash' and o[0] == 'D'])
            if len(H) < 3 or not len(F): continue
            votes = {}
            for k in range(0, len(F), 2000):
                d = F[k:k + 2000, None, :] - H[None, :, :]
                m = np.abs(d).max(axis=2) < near
                for dx, dy in np.round(d[m] / cell).astype(int):
                    votes[(dx, dy)] = votes.get((dx, dy), 0) + 1
            if not votes: continue
            (dx, dy), c = max(votes.items(), key=lambda kv: kv[1])
            z = votes.get((0, 0), 0)
            if (dx, dy) != (0, 0) and c >= max(3, 0.5 * len(H)) and c > 3 * z and math.hypot(dx, dy) * cell > 0.02:
                self.add('error', 'LAYER_OFFSET', f'{self.layer_names[li]}: pad/ランドの flash が drill の穴から ({dx * cell:+.3f}, {dy * cell:+.3f}) mm ずれている '
                         f'(一致 {c} / 穴 {len(H)}、ずれなしの一致 {z})。この層だけ原点・座標形式が違う?', layer=self.layer_names[li],
                         offset=[round(int(dx) * cell, 3), round(int(dy) * cell, 3)], votes=int(c), holes=len(H))

    # -- Gerber X2 のネット属性 (TO.N) との突き合わせ: 説明の補強だけ (正解には使わない。検査対象自身の属性なので)
    def _x2_crosscheck(self, gs):
        if not any(o[3] for g in gs for o in g.objs):
            self.notes.append('Gerber に X2 のネット属性が無いので X2 との突き合わせはしていない'); return
        x2 = {}
        for li, (g, ru) in enumerate(zip(gs, self.runs)):
            for pol, kind, d, net in g.objs:
                if not net or pol != 'D' or kind == 'region': continue
                pts = [d[1]] if kind == 'flash' else [((d[1][0] + d[2][0]) / 2, (d[1][1] + d[2][1]) / 2)]
                for x, y in pts:
                    c, r = self.R.xy2pix(x, y); lb = ru.label_at(int(round(r)), int(round(c)))
                    if lb >= 0: x2.setdefault(self.dsu.find((li, lb)), set()).add(net.upper())
        self.x2 = x2
        for f in self.findings:
            if f['code'] == 'SHORT' and f.get('root') in x2:
                f['x2_nets'] = sorted(x2[f['root']])[:12]
            elif f['code'] == 'OPEN':
                f['x2_nets_per_group'] = [sorted(x2.get(r, ()))[:6] for r in f.get('roots', [])]
        bad = []
        for p, root in self.pad_root:
            if root is None or root not in x2: continue
            net = p['net'].upper()
            if net in ('N/C', '') or net.startswith('UNCONNECTED-'): continue
            if net not in x2[root]: bad.append((p, sorted(x2[root])))
        for p, nets in bad[:10]:
            self.add('info', 'X2_NET_DIFFERS', f'{self.padname(p)}: 正解のネット {p["net"]} だが、この銅の Gerber X2 属性は {nets[:4]} (gerber が古い / ネット名変更の反映漏れ?)',
                     at=(p['x'], p['y']), pad=self.padname(p), net=p['net'], x2_nets=nets[:6])
        if len(bad) > 10: self.notes.append(f'X2_NET_DIFFERS は {len(bad)} 件 (先頭 10 件だけ表示)')

    # -- 原点: drill と D356 の穴位置を突き合わせてずれを求める
    def _origin(self):
        thr = [p for p in self.pts if p['acc'] == 0 and p['hole'] > 0]
        self.off = (0.0, 0.0)
        if not thr:
            self.notes.append('D356 にスルーホールが無いので原点の自己チェックをしていない'); return
        votes = {}
        for p in thr[:400]:
            for h in self.holes:
                if abs(h.d - p['hole']) < 0.06:
                    k = (round(h.x - p['x'], 2), round(h.y - p['y'], 2)); votes[k] = votes.get(k, 0) + 1
        if not votes: raise RuntimeError('drill と IPC-D-356 の穴径が一つも合わない (別の基板?)')
        k, c = max(votes.items(), key=lambda kv: kv[1])
        if c < 0.5 * len(thr[:400]):
            raise RuntimeError(f'drill と IPC-D-356 の穴位置が合わない (一致 {c}/{len(thr[:400])})。別の基板か原点の設定違い')
        if k != (0.0, 0.0):
            self.notes.append(f'Gerber/drill と IPC-D-356 の原点が ({k[0]}, {k[1]}) mm ずれているので補正した (--use-drill-file-origin なし?)')
        self.off = k
        for p in self.pts: p['x'] += k[0]; p['y'] += k[1]

    # -- 外形: 閉路を作って内外を塗り分け、clearance の帯を作る
    def _outline(self, edge):
        R = self.R
        segs = []
        for pol, kind, d, _ in edge.objs:
            if kind == 'stroke': segs.append((d[1], d[2]))
            elif kind == 'region':
                for c in d: segs += [(c[i - 1], c[i]) for i in range(1, len(c))]
        segs = [s for s in segs if s[0] != s[1]]
        self.segs = np.array([(a[0], a[1], b[0], b[1]) for a, b in segs])
        loops, nopen = gl.outline_loops(segs)
        self.loops = loops
        zone = R.new(255); dr = ImageDraw.Draw(zone)
        self.board_ok = bool(loops) and nopen == 0
        if nopen and self.aux_checks:
            self.add('warning', 'OUTLINE_OPEN', f'Edge_Cuts に閉じていない線が {nopen} 本ある (外形外の銅の検査を省略)')
        if self.board_ok:
            order = sorted(range(len(loops)), key=lambda i: -abs(gl.poly_area(loops[i])))
            for i in order:
                p = loops[i]; depth = sum(gl.point_in_poly(p[0][0], p[0][1], loops[j]) for j in range(len(loops)) if j != i)
                dr.polygon([R.xy2pix(*q) for q in p], fill=0 if depth % 2 == 0 else 255)
            self.n_cutouts = sum(1 for i in range(len(loops)) if sum(gl.point_in_poly(loops[i][0][0], loops[i][0][1], loops[j]) for j in range(len(loops)) if j != i) % 2)
        else:
            dr.rectangle([0, 0, R.W, R.H], fill=0); self.n_cutouts = 0
        rad = self.edge_clr + 2 * self.px
        circ = gl.circle_poly(0, 0, rad, step=0.02)
        for a, b in segs:
            for exp, poly in gl.stroke_polys([(1, circ)], rad, a, b):
                dr.polygon([R.xy2pix(*q) for q in poly], fill=128)
        self.zone = zone

    # -- 補助: NPTH (穴あけ前の銅で見る)
    def _npth_check(self, li, img):
        R, px = self.R, self.px
        for h in self.holes:
            if h.plated or not (h.span[0] <= li + 1 <= h.span[1]): continue
            reach = h.d / 2 + self.hole_clr + 2 * px
            (ca, ra), (cb, rb) = R.xy2pix(min(h.x0, h.x1) - reach, max(h.y0, h.y1) + reach), R.xy2pix(max(h.x0, h.x1) + reach, min(h.y0, h.y1) - reach)
            ra, rb, ca, cb = max(0, int(ra)), min(R.H, int(rb) + 2), max(0, int(ca)), min(R.W, int(cb) + 2)
            a = np.asarray(img.crop((ca, ra, cb, rb))) > 127
            rr, cc = np.nonzero(a)
            if not len(rr): continue
            X = R.x0 + (cc + ca + 0.5) * px; Y = R.y1 - (rr + ra + 0.5) * px
            dist = h.dist(X, Y); i = int(np.argmin(dist)); dmin = float(dist[i])
            gap = dmin - h.d / 2  # 穴縁から銅まで (負 = 穴の中に銅)
            lay = self.layer_names[li]
            if gap < -self.tol:
                self.add('error', 'COPPER_OVER_NPTH', f'{lay}: NPTH φ{h.d:.2f} の穴の中に銅がある (穴の中心から {dmin:.3f} mm)',
                         at=(h.x, h.y), layer=lay, drill=h.d, min_from_center=round(dmin, 3), copper_at=[round(float(X[i]), 3), round(float(Y[i]), 3)])
            elif gap < self.hole_clr - self.tol:
                self.add('warning', 'NPTH_CLEARANCE', f'{lay}: NPTH φ{h.d:.2f} の穴縁から銅まで {gap:.3f} mm (規則 {self.hole_clr} mm)',
                         at=(float(X[i]), float(Y[i])), layer=lay, drill=h.d, gap=round(gap, 3), rule=self.hole_clr)

    def _drill_out(self, li, img):
        dr = ImageDraw.Draw(img); R = self.R
        for h in self.holes:
            if not (h.span[0] <= li + 1 <= h.span[1]): continue
            circ = gl.circle_poly(0, 0, h.d / 2, step=0.01)
            for exp, poly in gl.stroke_polys([(1, circ)], h.d / 2, (h.x0, h.y0), (h.x1, h.y1)):
                dr.polygon([R.xy2pix(*q) for q in poly], fill=0)

    # -- 補助: 外形 clearance と外形外の銅 (帯の中の銅画素から外形線分までの距離を厳密に計算)
    def _edge_check(self, li, img):
        R, px = self.R, self.px; lay = self.layer_names[li]
        bad_x, bad_y, bad_d = [], [], []; out_n = 0; out_at = None
        for r0 in range(0, R.H, 512):
            r1 = min(R.H, r0 + 512)
            cu = np.asarray(img.crop((0, r0, R.W, r1))) > 127
            z = np.asarray(self.zone.crop((0, r0, R.W, r1)))
            rr, cc = np.nonzero(cu & (z > 0))
            if not len(rr): continue
            zz = z[rr, cc]
            o = zz == 255
            if o.any():
                out_n += int(o.sum())
                if out_at is None: out_at = R.pix2xy(int(cc[o][0]), int(rr[o][0] + r0))
            b = zz == 128
            if b.any():
                X = R.x0 + (cc[b] + 0.5) * px; Y = R.y1 - (rr[b] + r0 + 0.5) * px
                d = np.concatenate([gl.seg_dist(X[k:k + 20000], Y[k:k + 20000], self.segs) for k in range(0, len(X), 20000)])
                m = d < self.edge_clr - self.tol
                bad_x.append(X[m]); bad_y.append(Y[m]); bad_d.append(d[m])
        if out_n and self.board_ok:
            self.add('error', 'COPPER_OUTSIDE_BOARD', f'{lay}: 外形の外 (または切り欠きの中) に銅がある ({out_n * px * px:.3f} mm²)',
                     at=out_at, layer=lay, area_mm2=round(out_n * px * px, 4))
        if bad_x:
            X, Y, D = np.concatenate(bad_x), np.concatenate(bad_y), np.concatenate(bad_d)
            for cx, cy, d, cnt in cluster_min(X, Y, D, 1.0)[:20]:
                self.add('error', 'EDGE_CLEARANCE', f'{lay}: 銅と外形の距離 {d:.3f} mm (規則 {self.edge_clr} mm)',
                         at=(cx, cy), layer=lay, dist=round(d, 3), rule=self.edge_clr)

    # -- 導通: PTH の壁に触れる各層の島をまとめる
    def _connect(self):
        R, px = self.R, self.px
        dsu = self.dsu = gl.DSU(); self.hole_layers = {}
        for hi, h in enumerate(self.holes):
            if not h.plated: continue
            node = ('H', hi); dsu.find(node); hl = {}
            for li in range(h.span[0] - 1, min(h.span[1], len(self.runs))):
                labs = {}
                for x, y in h.samples(h.d / 4):
                    for k, v in self.runs[li].labels_in_disk(R, x, y, h.d / 2 + 2 * px).items(): labs[k] = labs.get(k, 0) + v
                hl[li] = labs
                for lb in labs: dsu.union((li, lb), node)
            self.hole_layers[hi] = hl
        for li, ru in enumerate(self.runs):
            for lb in ru.labels(): dsu.find((li, int(lb)))

    def _find_hole(self, p, plated):
        best = None
        for hi, h in enumerate(self.holes):
            if h.plated != plated: continue
            dd = math.hypot(h.x - p['x'], h.y - p['y'])
            if dd < 0.02 and (best is None or dd < best[1]): best = (hi, dd)
        return best[0] if best else None

    def _pad_coverage(self, p, nm, li, thr=0.9):
        """SMD pad の中心の内接正方形 (一辺 = 短辺/√2) がほぼ銅で埋まっているか。凸な pad (丸/長円/角丸/矩形) なら回転によらず全部銅。
        pad が消えて配線の端だけ残った・別の層に出た、を見つける (中心 1 点だけでは配線の端で埋まって見逃す)"""
        half = min(p['sx'], p['sy']) / 2 / math.sqrt(2)
        if half < 3 * self.px: return
        if any(h.plated and h.dist(p['x'], p['y']) < half + h.d / 2 for h in self.holes): return  # via in pad は穴で抜けるので見ない
        (c0, r0), (c1, r1) = self.R.xy2pix(p['x'] - half, p['y'] + half), self.R.xy2pix(p['x'] + half, p['y'] - half)
        r0, r1, c0, c1 = int(math.ceil(r0)), int(math.floor(r1)) + 1, int(math.ceil(c0)), int(math.floor(c1)) + 1
        win = self.runs[li].window(r0, r1, c0, c1)
        if not win.size: return
        f = float(win.mean())
        if f < thr:
            self.add('warning', 'PAD_COPPER_INCOMPLETE', f'{nm} [{p["net"]}]: {self.layer_names[li]} の pad の中心部 {2 * half:.2f} mm 角のうち銅は {f * 100:.0f}% (pad が欠けている / 別の層に出ている?)',
                     at=(p['x'], p['y']), pad=nm, net=p['net'], layer=self.layer_names[li], fill=round(f, 3))

    def _compare(self):
        n = len(self.runs); self.pad_root = []; used = set()
        for p in self.pts:
            nm = self.padname(p)
            if p['type'] == '367' or (p['type'] == '317' and p['plated'] is False):
                hi = self._find_hole(p, False)
                if hi is None:
                    self.add('warning', 'MISSING_NPTH', f'{nm}: IPC-D-356 の NPTH (φ{p["hole"]:.2f}) が drill に無い', at=(p['x'], p['y']), pad=nm)
                else: used.add(hi)
                continue
            if p['acc'] == 0:
                hi = self._find_hole(p, True)
                if hi is None:
                    alt = self._find_hole(p, False)
                    why = ' (同じ位置に NPTH ならある)' if alt is not None else ''
                    self.add('error', 'MISSING_HOLE', f'{nm} [{p["net"]}]: めっき穴が drill に無い{why}', at=(p['x'], p['y']), pad=nm, net=p['net'])
                    self.pad_root.append((p, None)); continue
                used.add(hi); h = self.holes[hi]
                if p['hole'] and abs(h.d - p['hole']) > 0.03 + 0.00254:
                    self.add('warning', 'HOLE_SIZE_MISMATCH', f'{nm}: 穴径 drill φ{h.d:.3f} / IPC-D-356 φ{p["hole"]:.3f}', at=(p['x'], p['y']), pad=nm)
                if not any(self.hole_layers[hi].values()):
                    self.add('error', 'NO_COPPER_AT_HOLE', f'{nm} [{p["net"]}]: 穴はあるがどの層にも銅が無い', at=(p['x'], p['y']), pad=nm, net=p['net'])
                    self.pad_root.append((p, None)); continue
                self.pad_root.append((p, self.dsu.find(('H', hi))))
            else:
                if p['acc'] > n:
                    self.add('error', 'NO_COPPER_AT_PAD', f'{nm}: access 層 {p["acc"]} の Gerber が無い', at=(p['x'], p['y']), pad=nm, net=p['net'])
                    self.pad_root.append((p, None)); continue
                li = p['acc'] - 1; ru = self.runs[li]
                c, r = self.R.xy2pix(p['x'], p['y'])
                lb = ru.label_at(int(round(r)), int(round(c)))
                if lb < 0:
                    for f in (0.25, 0.45):
                        labs = ru.labels_in_disk(self.R, p['x'], p['y'], f * min(p['sx'], p['sy']))
                        if labs: break
                    if len(labs) > 1:
                        self.add('warning', 'PAD_AMBIGUOUS', f'{nm}: pad 中心に銅が無く、近くに島が {len(labs)} 個', at=(p['x'], p['y']), pad=nm, layer=self.layer_names[li])
                    lb = max(labs, key=labs.get) if labs else -1
                if lb < 0:
                    self.add('error', 'NO_COPPER_AT_PAD', f'{nm} [{p["net"]}]: {self.layer_names[li]} の pad 位置に銅が無い', at=(p['x'], p['y']), pad=nm, net=p['net'], layer=self.layer_names[li])
                    self.pad_root.append((p, None)); continue
                self._pad_coverage(p, nm, li)
                self.pad_root.append((p, self.dsu.find((li, lb))))
        for hi, h in enumerate(self.holes):
            if hi in used: continue
            if any(p['acc'] == 0 and math.hypot(h.x - p['x'], h.y - p['y']) < 0.02 for p in self.pts): continue
            self.add('warning', 'EXTRA_HOLE', f'drill にあって IPC-D-356 に無い{"めっき" if h.plated else " NPTH "}穴 φ{h.d:.2f}', at=(h.x, h.y), drill=h.d)
        # 比較
        key = lambda s: s.upper()
        names = {}
        for p, _ in self.pad_root: names.setdefault(key(p['net']), set()).add(p['net'])
        for k, v in names.items():
            if len(v) > 1: self.add('warning', 'NET_CASE_COLLISION', f'大文字小文字だけ違うネット名 {sorted(v)} (IPC-D-356 は大文字化するので区別できない)')
        comp_nets, net_comps = {}, {}
        for p, root in self.pad_root:
            if root is None: continue
            comp_nets.setdefault(root, {}).setdefault(key(p['net']), []).append(p)
            net_comps.setdefault(key(p['net']), {}).setdefault(root, []).append(p)
        self.comp_nets, self.net_comps = comp_nets, net_comps
        disp = lambda k: sorted(names[k])[0]
        for root, nets in comp_nets.items():
            real = {k: v for k, v in nets.items() if k != 'N/C'}
            if len(real) > 1:
                ks = sorted(real, key=lambda k: len(real[k]))
                self.add('error', 'SHORT', '別ネットが同じ銅でつながっている: ' + ' / '.join(disp(k) for k in ks),
                         at=(real[ks[0]][0]['x'], real[ks[0]][0]['y']), nets=[disp(k) for k in ks],
                         pads={disp(k): [self.padname(p) for p in real[k]][:12] for k in ks}, root=root)
            elif real and 'N/C' in nets:
                self.add('warning', 'NC_PAD_CONNECTED', f'ネット無しの pad {[self.padname(p) for p in nets["N/C"]][:6]} が {disp(next(iter(real)))} の銅に載っている',
                         at=(nets['N/C'][0]['x'], nets['N/C'][0]['y']))
        for k, comps in net_comps.items():
            if k.startswith('UNCONNECTED-') or k in ('N/C', '') or len(comps) < 2: continue
            groups = sorted(comps.items(), key=lambda kv: -len(kv[1]))
            self.add('error', 'OPEN', f'{disp(k)} が {len(comps)} つに分かれている: ' + ' | '.join(','.join(self.padname(p) for p in g[:6]) + ('…' if len(g) > 6 else '') for _, g in groups),
                     at=(groups[-1][1][0]['x'], groups[-1][1][0]['y']), net=disp(k),
                     groups=[[self.padname(p) for p in g][:12] for _, g in groups], roots=[r for r, _ in groups])

    # -- 補助: paste の下に銅があるか
    def _paste(self, pastef):
        for side, f in pastef.items():
            li = 0 if side == 'top' else len(self.runs) - 1
            g = gl.Gerber(f); ru = self.runs[li]; miss = []
            for pol, kind, d, _ in g.objs:
                if kind != 'flash' or pol != 'D': continue
                x, y = d[1]; c, r = self.R.xy2pix(x, y)
                bx = gl.flash_bbox(g, d[0], x, y); rad = max(0.05, 0.4 * min(bx[2] - bx[0], bx[3] - bx[1]))
                if ru.label_at(int(round(r)), int(round(c))) < 0 and not ru.labels_in_disk(self.R, x, y, rad):  # 中心が via 穴でも周りに銅があれば OK
                    miss.append((x, y))
            for x, y in miss[:20]:
                self.add('warning', 'PASTE_WITHOUT_COPPER', f'{self.layer_names[li]}: paste 開口の下に銅が無い', at=(x, y), layer=self.layer_names[li])

    # -- 情報: どの pad にもつながらない銅の島
    def _floating(self):
        roots_with_pad = {r for _, r in self.pad_root if r is not None}
        items = []
        for li, ru in enumerate(self.runs):
            area = np.bincount(ru.lab, weights=(ru.e - ru.s).astype(float))
            for lb in np.nonzero(area)[0]:
                if self.dsu.find((li, int(lb))) in roots_with_pad: continue
                i = int(np.argmax(ru.lab == lb))
                items.append((area[lb] * self.px ** 2, li, self.R.pix2xy(int(ru.s[i]), int(ru.row[i]))))
        items.sort(reverse=True)
        self.floating = len(items)
        for a, li, at in items[:10]:
            self.add('info', 'FLOATING_COPPER', f'{self.layer_names[li]}: どの pad にもつながらない銅 {a:.3f} mm²', at=at, layer=self.layer_names[li], area_mm2=round(a, 4))

    # -- short の位置: X2 の TO.N (診断用) で「そのネットの銅」と「ほかの銅」が接する画素を探す
    def _locate_shorts(self):
        for f in self.findings:
            if f['code'] != 'SHORT': continue
            target = f['nets'][0].upper(); root = f.pop('root'); contacts = []
            for li, ru in enumerate(self.runs):
                labs = [int(lb) for lb in ru.labels() if self.dsu.find((li, int(lb))) == root]
                if not labs: continue
                bb = ru.bbox_of(labs)
                r0, r1, c0, c1 = max(bb[0] - 2, 0), min(bb[1] + 2, self.R.H), max(bb[2] - 2, 0), min(bb[3] + 2, self.R.W)
                sub = gl.Raster(*self.R.pix2xy(c0 - 0.5, r1 - 0.5), *self.R.pix2xy(c1 - 0.5, r0 - 0.5), self.px)
                sub.x0, sub.y1 = self.R.x0 + c0 * self.px, self.R.y1 - r0 * self.px
                sub.W, sub.H = c1 - c0, r1 - r0
                g = self.gs[li]
                if not any(o[3] for o in g.objs): break  # X2 の net 属性が無い
                A = np.asarray(gl.render(g, sub, net_filter=lambda nt: nt is not None and nt.upper() == target)) > 127
                O = np.asarray(gl.render(g, sub, net_filter=lambda nt: nt is None or nt.upper() != target)) > 127
                isl = ru.window(r0, r1, c0, c1, labels=labs)
                Ad = A.copy(); Ad[1:] |= A[:-1]; Ad[:-1] |= A[1:]; Ad[:, 1:] |= A[:, :-1]; Ad[:, :-1] |= A[:, 1:]
                rr, cc = np.nonzero(Ad & O & ~A & isl)
                if len(rr):
                    X = sub.x0 + (cc + 0.5) * self.px; Y = sub.y1 - (rr + 0.5) * self.px
                    for cx, cy, _, cnt in cluster_min(X, Y, np.zeros(len(X)), 0.5)[:3]:
                        contacts.append(dict(layer=self.layer_names[li], at=[round(cx, 3), round(cy, 3)]))
            if contacts:
                f['contacts'] = contacts; f['at'] = contacts[0]['at']
                if self.aux is not None:
                    dx, dy = f['at'][0] - self.off[0], f['at'][1] - self.off[1]
                    f['at_kicad'] = [round(self.aux[0] + dx, 3), round(self.aux[1] - dy, 3)]
                f['msg'] += f' (接触: {contacts[0]["layer"]} {tuple(contacts[0]["at"])})'

    # -- PNG
    def png(self, outdir, limit=30):
        os.makedirs(outdir, exist_ok=True)
        for f in os.listdir(outdir):
            if f.endswith('.png'): os.remove(os.path.join(outdir, f))
        R, px = self.R, self.px; k = 0; written = []
        colors = [(255, 60, 60), (60, 160, 255), (60, 220, 60), (255, 200, 0), (220, 80, 255), (0, 220, 220)]
        for f in self.findings:
            if f['level'] != 'error' or 'at' not in f or k >= limit: continue
            k += 1
            li = self.layer_names.index(f['layer']) if f.get('layer') in self.layer_names else 0
            if f['code'] == 'SHORT' and f.get('contacts'): li = self.layer_names.index(f['contacts'][0]['layer'])
            pts = [f['at']]; hl = []
            if f['code'] == 'OPEN':
                for gi, root in enumerate(f.get('roots', [])):
                    ps = [p for p in self.pts if self.padname(p) in f['groups'][gi]]
                    pts += [(p['x'], p['y']) for p in ps]
                    hl.append(root)
                li = next((p['acc'] - 1 for p in self.pts if self.padname(p) in f['groups'][-1] and p['acc'] > 0), li)
            elif f['code'] == 'SHORT':
                hl = [next(r for r, nets in self.comp_nets.items() if {n.upper() for n in f['nets']} <= set(nets))]
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            (bx0, bx1), cx = crop_span(min(xs), max(xs), f['at'][0]); (by0, by1), cy = crop_span(min(ys), max(ys), f['at'][1])
            if cx or cy: f['png_crop'] = [round(bx0, 3), round(by0, 3), round(bx1, 3), round(by1, 3)]
            (c0, r0), (c1, r1) = R.xy2pix(bx0, by1), R.xy2pix(bx1, by0)
            r0, r1, c0, c1 = max(int(r0), 0), min(int(r1) + 1, R.H), max(int(c0), 0), min(int(c1) + 1, R.W)
            if r1 - r0 < 1 or c1 - c0 < 1: continue  # 切り抜きが raster の外 (起きないはずだが、作図で止めない)
            ru = self.runs[li]
            img = np.zeros((r1 - r0, c1 - c0, 3), np.uint8)
            img[ru.window(r0, r1, c0, c1)] = (110, 110, 110)
            for ci, root in enumerate(hl):
                labs = [int(lb) for lb in ru.labels() if self.dsu.find((li, int(lb))) == root]
                if labs: img[ru.window(r0, r1, c0, c1, labels=labs)] = colors[ci % len(colors)]
            im = Image.fromarray(img); sc = max(1, int(math.ceil(max(im.size) / 1200)))
            if sc > 1: im = im.resize((im.size[0] // sc, im.size[1] // sc), Image.NEAREST)
            up = max(1, 700 // max(im.size))  # 小さい切り抜きは拡大
            if up > 1: im = im.resize((im.size[0] * up, im.size[1] * up), Image.NEAREST); sc = sc / up
            dr = ImageDraw.Draw(im)
            def P(x, y): c, r = R.xy2pix(x, y); return ((c - c0) / sc, (r - r0) / sc)
            for x0, y0, x1, y1 in self.segs:
                dr.line([P(x0, y0), P(x1, y1)], fill=(255, 255, 0), width=1)
            for h in self.holes:
                if bx0 - 2 < h.x < bx1 + 2 and by0 - 2 < h.y < by1 + 2:
                    circ = gl.circle_poly(0, 0, h.d / 2, step=0.02)
                    allp = [q for _, poly in gl.stroke_polys([(1, circ)], h.d / 2, (h.x0, h.y0), (h.x1, h.y1)) for q in poly]
                    dr.polygon([P(*q) for q in gl.hull(allp)], outline=(255, 255, 255) if h.plated else (255, 128, 0))
            for x, y in pts[1:]:
                c, r = P(x, y); dr.line([c - 6, r, c + 6, r], fill=(255, 255, 255)); dr.line([c, r - 6, c, r + 6], fill=(255, 255, 255))
            c, r = P(*f['at']); dr.ellipse([c - 10, r - 10, c + 10, r + 10], outline=(255, 0, 255), width=2)
            at = f.get('at_kicad', f['at'])
            dr.text((4, 4), f"{f['code']} {self.layer_names[li]} @({at[0]:.3f},{at[1]:.3f}) " + ' '.join(str(f.get(k)) for k in ('net', 'dist', 'drill') if k in f), fill=(255, 255, 255))
            if f['code'] == 'SHORT': dr.text((4, 18), ' / '.join(f['nets'])[:120], fill=(255, 255, 255))
            if 'png_crop' in f: dr.text((4, 32 if f['code'] == 'SHORT' else 18), f"cropped to {CROP_MM:.0f} mm around the marker (some pads are outside)", fill=(255, 255, 255))  # 既定フォントは日本語を描けないので英語
            fn = os.path.join(outdir, f"{k:02d}_{f['code']}_{self.layer_names[li].replace('.', '_')}.png")
            im.save(fn); f['png'] = os.path.basename(fn); written.append(fn)
        return written

    def summary(self):
        cnt = {lv: sum(1 for f in self.findings if f['level'] == lv) for lv in LEVELS}
        return cnt


CROP_MM = 40.0  # PNG の切り抜きの最大幅 (mm)。これ以上は画素が大きすぎる (0.01 mm/px で 4000 px)


def crop_span(lo, hi, focus, margin=1.5, cap=CROP_MM):
    """PNG の切り抜き範囲 (1 軸)。[lo, hi] に余白を付け、cap より長ければ focus (印の位置) を中心に cap に切る。
    ((a, b), 切ったか) を返す。常に a < b で focus を含む (旧版は lo と hi が 40 mm 以上離れると a > b になり負の寸法で止まった)"""
    a, b = min(lo, focus) - margin, max(hi, focus) + margin
    if b - a <= cap: return (a, b), False
    a = min(max(focus - cap / 2, a), b - cap)
    return (a, a + cap), True


def cluster_min(X, Y, D, cell):
    """点群をセル (mm) でまとめ、塊ごとに (x, y, 最小 D, 点数) を最小 D の順で返す"""
    if not len(X): return []
    keys = {}
    for x, y, d in zip(np.floor(X / cell).astype(int), np.floor(Y / cell).astype(int), range(len(X))):
        keys.setdefault((x, y), []).append(d)
    dsu = gl.DSU()
    for (x, y) in keys:
        dsu.find((x, y))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (x + dx, y + dy) in keys: dsu.union((x, y), (x + dx, y + dy))
    groups = {}
    for kk, idx in keys.items(): groups.setdefault(dsu.find(kk), []).extend(idx)
    out = []
    for idx in groups.values():
        idx = np.array(idx); i = idx[np.argmin(D[idx])]
        out.append((float(X[i]), float(Y[i]), float(D[i]), len(idx)))
    return sorted(out, key=lambda t: t[2])


# ---------------------------------------------------------------- 出力
JP = {'SHORT': 'ショート', 'OPEN': 'オープン', 'NO_COPPER_AT_PAD': 'pad に銅が無い', 'MISSING_HOLE': '穴が無い',
      'NO_COPPER_AT_HOLE': '穴に銅が無い', 'COPPER_OVER_NPTH': 'NPTH の上の銅', 'NPTH_CLEARANCE': 'NPTH との間隔',
      'EDGE_CLEARANCE': '外形との間隔', 'COPPER_OUTSIDE_BOARD': '外形外の銅', 'OUTLINE_OPEN': '外形が閉じていない',
      'PASTE_WITHOUT_COPPER': 'paste の下に銅が無い', 'EXTRA_HOLE': '余分な穴', 'MISSING_NPTH': 'NPTH が無い',
      'HOLE_SIZE_MISMATCH': '穴径の違い', 'PAD_AMBIGUOUS': 'pad の島が曖昧', 'FLOATING_COPPER': '浮き銅',
      'NC_PAD_CONNECTED': 'ネット無し pad の接続', 'NET_CASE_COLLISION': 'ネット名の大文字小文字',
      'LAYER_MISSING': '銅層の欠落', 'LAYER_OFFSET': '層の原点ずれ', 'GBRJOB_MISMATCH': 'job file との不一致', 'X2_NET_DIFFERS': 'X2 ネット属性との違い', 'PAD_COPPER_INCOMPLETE': 'pad の銅が欠けている', 'LAYER_NAME_MISMATCH': '層の取り違え'}


def write_reports(lvs, out_txt, out_json, meta):
    cnt = lvs.summary()
    res = dict(tool='gerber-lvs', version=VERSION, **meta, summary=cnt,
               layers=lvs.layer_names, holes=len(lvs.holes), testpoints=len(lvs.pts),
               raster=dict(w=lvs.R.W, h=lvs.R.H, px_mm=lvs.px), rules=dict(edge_clearance=lvs.edge_clr, hole_clearance=lvs.hole_clr, tolerance=round(lvs.tol, 4), source=lvs.rules_src),
               origin_offset=list(lvs.off), outline=dict(loops=len(lvs.loops), cutouts=lvs.n_cutouts), floating_islands=lvs.floating,
               aux_checks=lvs.aux_checks, notes=lvs.notes, time_s={k: round(v, 2) for k, v in lvs.T.items()},
               findings=[{k: v for k, v in f.items() if k != 'roots'} for f in lvs.findings])
    # kreview F09 互換のキー
    res['status'] = 'fail' if cnt['error'] else 'ok'
    res['shorts'] = [f['msg'] for f in lvs.findings if f['code'] == 'SHORT']
    res['opens'] = [f['msg'] for f in lvs.findings if f['code'] == 'OPEN']
    json.dump(res, open(out_json, 'w'), ensure_ascii=False, indent=1)
    L = [f"# gerber-lvs {meta.get('board', '')}  ({meta['date']})",
         f"gerber: {meta['gerber_dir']}", f"正解: {meta['d356_src']}",
         f"層: {', '.join(lvs.layer_names)} / 穴 {len(lvs.holes)} / test point {len(lvs.pts)} / 外形の閉路 {len(lvs.loops)} (切り欠き {lvs.n_cutouts})",
         f"規則: 外形 {lvs.edge_clr} mm, NPTH {lvs.hole_clr} mm (出典 {lvs.rules_src or '既定値'}), 許容誤差 {lvs.tol:.3f} mm, 解像度 {lvs.px} mm/px",
         f"結果: error {cnt['error']} / warning {cnt['warning']} / info {cnt['info']}  ({lvs.T['total']:.1f} s)"]
    for n in lvs.notes: L.append(f"注: {n}")
    L.append('')
    for lv in LEVELS:
        fs = [f for f in lvs.findings if f['level'] == lv]
        if not fs: continue
        L.append(f'## {lv} ({len(fs)})')
        for f in fs:
            at = f" @gerber{tuple(f['at'])}" if 'at' in f else ''
            if 'at_kicad' in f: at += f" kicad{tuple(f['at_kicad'])}"
            L.append(f"- [{f['code']} {JP.get(f['code'], '')}] {f['msg']}{at}" + (f"  → {f['png']}" if 'png' in f else ''))
        L.append('')
    if not any(f['level'] == 'error' for f in lvs.findings): L.append('error なし: Gerber の銅の導通は IPC-D-356 と一致。')
    open(out_txt, 'w').write('\n'.join(L) + '\n')
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description='Gerber LVS: 製造データの銅の導通を IPC-D-356 と比べる')
    ap.add_argument('project_dir', nargs='?')
    ap.add_argument('name', nargs='?')
    ap.add_argument('--gerber-dir'); ap.add_argument('--d356'); ap.add_argument('--pcb'); ap.add_argument('--pro')
    ap.add_argument('--out-dir'); ap.add_argument('--prefix', default='gerber_lvs')
    ap.add_argument('--json', help='JSON の出力先 (kreview F09 互換: shorts / opens / status を含む)。--out-dir が無ければ txt も同じ場所に出す')
    ap.add_argument('--px', type=float, default=0.01, help='解像度 mm/px (既定 0.01)')
    ap.add_argument('--edge-clearance', type=float); ap.add_argument('--hole-clearance', type=float)
    ap.add_argument('--png', action='store_true', help='error の切り抜き PNG を出す')
    ap.add_argument('--strict', action='store_true', help='warning も終了コード 1 にする')
    ag = ap.add_mutually_exclusive_group()
    ag.add_argument('--aux', dest='aux_checks', action='store_true', help='旧動作: DRC 系の補助検査 (NPTH・外形 clearance・外形外の銅・paste) も行う')
    ag.add_argument('--no-aux', dest='aux_checks', action='store_false', help='DRC 系の補助検査を省く (既定。これらは tools/gerber-drc で行う)')
    ap.set_defaults(aux_checks=AUX_DEFAULT)
    ap.add_argument('-q', '--quiet', action='store_true')
    a = ap.parse_args(argv)
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, file=sys.stderr))
    tmp = None
    try:
        P = a.project_dir
        if P:
            N = a.name or os.path.basename(os.path.abspath(P))
            pcb = a.pcb or os.path.join(P, N + '.kicad_pcb'); pro = a.pro or os.path.join(P, N + '.kicad_pro')
            gdir = a.gerber_dir or find_gerber_dir(P); outd = a.out_dir or os.path.dirname(os.path.abspath(gdir))
        else:
            if not (a.gerber_dir and (a.d356 or a.pcb)): ap.error('project_dir か --gerber-dir と --d356/--pcb が必要')
            N = a.name or ''; pcb, pro, gdir = a.pcb, a.pro, a.gerber_dir; outd = a.out_dir or '.'
        if a.d356:
            d356, src = a.d356, a.d356
        else:
            if not pcb or not os.path.exists(pcb): raise RuntimeError(f'.kicad_pcb が無い: {pcb}')
            d356, tmp = export_d356(pcb, pro); src = f'kicad-cli pcb export ipcd356 {pcb}'
        rules = read_rules(pro)
        lvs = LVS(gdir, d356, rules, aux=aux_origin(pcb) if pcb else None, px=a.px,
                  edge_clr=a.edge_clearance, hole_clr=a.hole_clearance, log=log, aux_checks=a.aux_checks).run()
        if a.json:
            if not a.out_dir: outd = os.path.dirname(os.path.abspath(a.json))
        os.makedirs(outd, exist_ok=True)
        if a.png:  # PNG は補助。作図で何か起きても txt/json は出す
            try: lvs.png(os.path.join(outd, a.prefix))
            except Exception as e:
                lvs.notes.append(f'PNG を出せなかった ({type(e).__name__}: {e})。検査結果はこのレポートのとおり')
                log(f'gerber-lvs: PNG を出せなかった: {e}')
                if not a.quiet: traceback.print_exc()
        meta = dict(board=N, date=time.strftime('%Y-%m-%d %H:%M:%S %Z'), gerber_dir=os.path.abspath(gdir), d356_src=src)
        res = write_reports(lvs, os.path.join(outd, a.prefix + '.txt'), a.json or os.path.join(outd, a.prefix + '.json'), meta)
        cnt = res['summary']
        print(f"gerber-lvs {N}: error={cnt['error']} warning={cnt['warning']} info={cnt['info']} ({lvs.T['total']:.1f}s) → {os.path.join(outd, a.prefix + '.txt')}")
        for f in lvs.findings:
            if f['level'] == 'error' or (f['level'] == 'warning' and not a.quiet):
                print(f"  {f['level']} {f['code']}: {f['msg']}")
        return 1 if cnt['error'] or (a.strict and cnt['warning']) else 0
    except gl.Unsupported as e:
        print(f'gerber-lvs: 未対応の構文: {e}', file=sys.stderr); return 2
    except Exception as e:
        print(f'gerber-lvs: 実行できなかった: {e}', file=sys.stderr)
        if not a.quiet: traceback.print_exc()
        return 2
    finally:
        if tmp: shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
