#!/usr/bin/env python3
"""gerber-drc: 製造データ (銅層 Gerber + Excellon + Paste + Edge_Cuts) の幾何の検査 (DRC 系)。
2026-10-14 に gerber-lvs (../gerber-lvs) の補助検査を移した。LVS (接続の一致) は gerber-lvs、こちらは幾何だけ。
  COPPER_OVER_NPTH (error)     NPTH の穴の中に銅
  NPTH_CLEARANCE (warning)     NPTH の穴縁から銅まで < min_hole_clearance
  EDGE_CLEARANCE (error)       銅と外形 (Edge_Cuts) の距離 < min_copper_edge_clearance
  COPPER_OUTSIDE_BOARD (error) 外形の外 / 切り欠きの中の銅
  PASTE_WITHOUT_COPPER (warning) paste 開口の下に銅が無い
  OUTLINE_OPEN (warning)       Edge_Cuts が閉じていない

使い方:
  gerber_drc.py <project_dir> [name] [--gerber-dir G] [--out-dir O] [--json J] [--png]
  gerber_drc.py --gerber-dir G [--pro P.kicad_pro] [--pcb P.kicad_pcb] --out-dir O
終了コード: 0 = error 0 件 / 1 = error あり (--strict なら warning も) / 2 = 実行できなかった
IPC-D-356 (正解の接続) は使わない。.kicad_pcb は aux 原点 (KiCad 座標の表示) を読むだけ、.kicad_pro は規則を読むだけ。

依存: Gerber/Excellon の読み込み・描画は gerber-lvs の gerberlib.py と gerber_lvs.py の入力発見部分をそのまま使う
(環境変数 GERBER_LVS_DIR、既定 ../gerber-lvs)。コピーにしないのは、構文対応やバグ修正を 1 か所で済ませ、
LVS と DRC が同じ解釈で gerber を読むことを保証するため。"""
import argparse, fnmatch, json, math, os, re, sys, time, traceback
import numpy as np
from PIL import Image, ImageDraw

LVS_DIR = os.environ.get('GERBER_LVS_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gerber-lvs'))
sys.path.insert(0, LVS_DIR)
try:
    import gerberlib as gl
    import gerber_lvs as gv
except ImportError as e:
    sys.exit(f'gerber-drc: gerber-lvs が見つからない ({LVS_DIR}): {e}。gerber-lvs の場所を GERBER_LVS_DIR で指定')
from gerber_lvs import discover, find_gerber_dir, read_rules, aux_origin, lname, cluster_min

VERSION = '1.0'
LEVELS = ('error', 'warning', 'info')
JP = {'HOLE_SIZE': '穴径 (工場の最小/最大)', 'HOLE_DIAMETER_SET': '穴径の指定 (project)', 'COPPER_OVER_NPTH': 'NPTH の上の銅', 'NPTH_CLEARANCE': 'NPTH との間隔', 'EDGE_CLEARANCE': '外形との間隔',
      'COPPER_OUTSIDE_BOARD': '外形外の銅', 'OUTLINE_OPEN': '外形が閉じていない', 'PASTE_WITHOUT_COPPER': 'paste の下に銅が無い'}


class DRC:
    def __init__(self, gdir, rules, aux=None, px=0.01, edge_clr=None, hole_clr=None, log=print, extra_edge=(), sev=None, hole_rules=(), fp_holes=None):
        """extra_edge = [(rule_id, clearance_mm, level)] 追加の外形 clearance (YAML の user/project ルール)。sev = {code: level} 重大度の上書き"""
        self.gdir, self.px, self.log = gdir, px, log
        self.findings = []; self.T = {}; self.notes = []
        self.edge_clr = edge_clr if edge_clr is not None else (rules['edge'] if rules['edge'] is not None else 0.2)
        self.hole_clr = hole_clr if hole_clr is not None else (rules['hole'] if rules['hole'] is not None else 0.2)
        if edge_clr is None and rules['edge'] is None: self.notes.append('.kicad_pro が無いので外形 clearance は既定 0.2 mm')
        if hole_clr is None and rules['hole'] is None: self.notes.append('.kicad_pro が無いので穴 clearance は既定 0.2 mm')
        self.rules_src = rules['src']; self.aux = aux
        self.off = (0.0, 0.0)  # gerber-lvs は D356 と照合して原点を補正するが、DRC は gerber 座標のまま
        self.tol = 2 * px + 0.005
        self.sev = dict(sev or {})
        self.hole_rules = list(hole_rules); self.fp_holes = fp_holes  # 穴径ルール (rules_params) と .kicad_pcb の footprint 穴 [(ref, fpid, x, y, d)] (gerber 座標)
        self.edge_rules = [(None, self.edge_clr, self.sev.get('EDGE_CLEARANCE', 'error'))] + [tuple(e) for e in extra_edge]
        self.edge_reach = max(v for _, v, _ in self.edge_rules)

    def run(self):
        t0 = time.time()
        files, edgef, pastef, drills = discover(self.gdir)
        if not files: raise RuntimeError(f'銅層 Gerber が無い: {self.gdir}')
        if not edgef: raise RuntimeError(f'Edge_Cuts (Profile) Gerber が無い: {self.gdir}')
        if not drills: raise RuntimeError(f'drill ファイル (.drl) が無い: {self.gdir}')
        self.layer_names = [lname(f) for f in files]; n = self.n = len(files)
        gs = [gl.Gerber(f) for f in files]; edge = gl.Gerber(edgef)
        self.holes = [h for d in drills for h in gl.parse_drill(d, n)]
        self._hole_size()
        bbs = [b for b in [edge.bbox()] + [g.bbox() for g in gs] if b]
        x0 = min(b[0] for b in bbs) - 0.5; y0 = min(b[1] for b in bbs) - 0.5
        x1 = max(b[2] for b in bbs) + 0.5; y1 = max(b[3] for b in bbs) + 0.5
        R = self.R = gl.Raster(x0, y0, x1, y1, self.px)
        self._outline(edge)
        self.runs = {}
        need = {0 if s == 'top' else n - 1 for s in pastef}
        for li, g in enumerate(gs):
            img = gl.render(g, R)
            self._npth_check(li, img)
            self._drill_out(li, img)
            self._edge_check(li, img)
            if li in need: self.runs[li] = gl.Runs(img)
            del img
        del self.zone
        self._paste(pastef)
        self.T['total'] = time.time() - t0
        return self

    def summary(self):
        return {lv: sum(1 for f in self.findings if f['level'] == lv) for lv in LEVELS}

    def add(self, level, code, msg, at=None, **kw):
        if 'rule_id' not in kw: level = self.sev.get(code, level)
        f = dict(level=level, code=code, msg=msg); f.update(kw)
        if at is not None:
            f['at'] = [round(at[0], 3), round(at[1], 3)]
            if self.aux is not None:
                dx, dy = at[0] - self.off[0], at[1] - self.off[1]
                f['at_kicad'] = [round(self.aux[0] + dx, 3), round(self.aux[1] - dy, 3)]
        self.findings.append(f); return f

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
        if nopen:
            self.add('warning', 'OUTLINE_OPEN', f'Edge_Cuts に閉じていない線が {nopen} 本ある (外形外の銅の検査を省略)')
        if self.board_ok:
            order = sorted(range(len(loops)), key=lambda i: -abs(gl.poly_area(loops[i])))
            for i in order:
                p = loops[i]; depth = sum(gl.point_in_poly(p[0][0], p[0][1], loops[j]) for j in range(len(loops)) if j != i)
                dr.polygon([R.xy2pix(*q) for q in p], fill=0 if depth % 2 == 0 else 255)
            self.n_cutouts = sum(1 for i in range(len(loops)) if sum(gl.point_in_poly(loops[i][0][0], loops[i][0][1], loops[j]) for j in range(len(loops)) if j != i) % 2)
        else:
            dr.rectangle([0, 0, R.W, R.H], fill=0); self.n_cutouts = 0
        rad = self.edge_reach + 2 * self.px
        circ = gl.circle_poly(0, 0, rad, step=0.02)
        for a, b in segs:
            for exp, poly in gl.stroke_polys([(1, circ)], rad, a, b):
                dr.polygon([R.xy2pix(*q) for q in poly], fill=128)
        self.zone = zone

    def _hole_size(self):
        """ドリルファイルの穴径 (gerber 解釈は gerberlib のまま、Hole.d / plated だけ使う)。
        hole_size: 工場の最小/最大 (scope.hole = pth/npth/any)。hole_diameter_set: 許す径の集合 (scope.footprints は .kicad_pcb の footprint の位置で穴を特定)。
        同じ (ルール, 径) は 1 件にまとめて個数を出す"""
        for r in self.hole_rules:
            kind = r['scope'].get('hole', 'any'); fps = r['scope'].get('footprints')
            hs = [h for h in self.holes if kind == 'any' or (kind == 'pth') == h.plated]
            if fps:
                if self.fp_holes is None:
                    self.notes.append(f"{r['id']}: footprint 条件があるが .kicad_pcb が無いので見ていない"); continue
                pos = [(x, y) for ref, fid, x, y, d in self.fp_holes if any(fnmatch.fnmatch(ref, g) or fnmatch.fnmatch(fid, g) for g in fps)]
                hs = [h for h in hs if any(abs(h.x - x) < 0.05 and abs(h.y - y) < 0.05 for x, y in pos)]
                if not pos: self.notes.append(f"{r['id']}: 対象の footprint ({', '.join(fps)}) が .kicad_pcb に無い")
            bad = {}
            for h in hs:
                d = h.d; v = r['value']
                if r['check'] == 'hole_diameter_set':
                    tol = float(v.get('tol', 0.0)) + 1e-6
                    ng = not any(abs(d - a) <= tol for a in v['allowed']); why = f"許す径 {v['allowed']} (±{v.get('tol', 0)})"
                else:
                    mn = v if isinstance(v, (int, float)) else v.get('min'); mx = None if isinstance(v, (int, float)) else v.get('max')
                    ng = (mn is not None and d < mn - 1e-6) or (mx is not None and d > mx + 1e-6)
                    why = ' '.join(x for x in (f'最小 {mn}' if mn is not None else '', f'最大 {mx}' if mx is not None else '') if x)
                if ng: bad.setdefault(round(d, 4), []).append(h)
            code = 'HOLE_DIAMETER_SET' if r['check'] == 'hole_diameter_set' else 'HOLE_SIZE'
            for d, lst in sorted(bad.items())[:20]:
                h = lst[0]; kd = 'PTH' if h.plated else 'NPTH'
                self.add(r['level'], code, f"{kd} φ{d:g} が {len(lst)} 個: {why} mm ({r['id']})", at=(h.x, h.y),
                         rule_id=r['id'], drill=d, count=len(lst), plated=h.plated)

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

    def _edge_check(self, li, img):
        R, px = self.R, self.px; lay = self.layer_names[li]
        bad = {k: ([], [], []) for k in range(len(self.edge_rules))}; out_n = 0; out_at = None
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
                for k, (_, clr, _) in enumerate(self.edge_rules):
                    m = d < clr - self.tol
                    bad[k][0].append(X[m]); bad[k][1].append(Y[m]); bad[k][2].append(d[m])
        if out_n and self.board_ok:
            self.add('error', 'COPPER_OUTSIDE_BOARD', f'{lay}: 外形の外 (または切り欠きの中) に銅がある ({out_n * px * px:.3f} mm²)',
                     at=out_at, layer=lay, area_mm2=round(out_n * px * px, 4))
        for k, (rid, clr, lv) in enumerate(self.edge_rules):
            bx, by, bd = bad[k]
            if not bx: continue
            X, Y, D = np.concatenate(bx), np.concatenate(by), np.concatenate(bd)
            if not len(X): continue
            for cx, cy, d, cnt in cluster_min(X, Y, D, 1.0)[:20]:
                kw = dict(rule_id=rid) if rid else {}
                self.add(lv, 'EDGE_CLEARANCE', f'{lay}: 銅と外形の距離 {d:.3f} mm (規則 {clr} mm' + (f' {rid})' if rid else ')'),
                         at=(cx, cy), layer=lay, dist=round(d, 3), rule=clr, **kw)

    def _paste(self, pastef):
        for side, f in pastef.items():
            li = 0 if side == 'top' else self.n - 1
            g = gl.Gerber(f); ru = self.runs[li]; miss = []
            for pol, kind, d, _ in g.objs:
                if kind != 'flash' or pol != 'D': continue
                x, y = d[1]; c, r = self.R.xy2pix(x, y)
                bx = gl.flash_bbox(g, d[0], x, y); rad = max(0.05, 0.4 * min(bx[2] - bx[0], bx[3] - bx[1]))
                if ru.label_at(int(round(r)), int(round(c))) < 0 and not ru.labels_in_disk(self.R, x, y, rad):  # 中心が via 穴でも周りに銅があれば OK
                    miss.append((x, y))
            for x, y in miss[:20]:
                self.add('warning', 'PASTE_WITHOUT_COPPER', f'{self.layer_names[li]}: paste 開口の下に銅が無い', at=(x, y), layer=self.layer_names[li])


CHECK_CODE = {'copper_over_npth': 'COPPER_OVER_NPTH', 'copper_outside_board': 'COPPER_OUTSIDE_BOARD',
              'paste_without_copper': 'PASTE_WITHOUT_COPPER', 'outline_closed': 'OUTLINE_OPEN'}
BASE_EDGE, BASE_NPTH = 'edge.copper_clearance', 'npth.copper_clearance'
PROJECT_RULES = 'gerber_drc_rules.yaml'


def rules_params(rs):
    """RuleSet (rules.py) → DRC の引数。gerber_drc が担当 (backend gerber_drc/both) の有効ルールだけ使う。
    外形: edge.copper_clearance が基本値 (従来の EDGE_CLEARANCE)、それ以外の edge_clearance ルール (例 user.edge_keepout_copper 1 mm) は別ルールとして両方出す。
    NPTH: hole_clearance で scope.hole=npth のもの。重大度は severity。未実装の check は notes に出す。"""
    p = dict(edge_clr=None, hole_clr=None, extra_edge=[], sev={}, notes=[], hole_rules=[])
    for r in rs.for_backend('gerber_drc'):
        c, rid, lv, v = r.get('check'), r['id'], r.get('severity', 'error'), r.get('value')
        if c == 'edge_clearance':
            if rid == BASE_EDGE: p['edge_clr'] = float(v); p['sev']['EDGE_CLEARANCE'] = lv
            else: p['extra_edge'].append((rid, float(v), lv))
        elif c == 'hole_clearance' and (r.get('scope') or {}).get('hole') == 'npth':
            p['hole_clr'] = float(v); p['sev']['NPTH_CLEARANCE'] = lv
        elif c in CHECK_CODE: p['sev'][CHECK_CODE[c]] = lv
        elif c in ('hole_size', 'hole_diameter_set'):
            sc = dict(r.get('scope') or {})
            if sc.get('hole') == 'via': p['notes'].append(f'YAML ルール {rid}: drill ファイルでは via と部品穴を区別しないので見ていない'); continue
            if c == 'hole_diameter_set' and not (isinstance(v, dict) and v.get('allowed')): p['notes'].append(f'YAML ルール {rid}: value.allowed が無い'); continue
            p['hole_rules'].append(dict(id=rid, check=c, value=v, level=lv, scope=sc))
        else: p['notes'].append(f'YAML ルール {rid} ({c}) は gerber-drc 未実装なので見ていない')
    p['notes'] += [f'YAML: {w}' for w in rs.warnings]
    return p


def _sexpr(txt):
    """最小の S 式 parser (.kicad_pcb の footprint を読むだけ)"""
    st = [[]]
    for m in re.finditer(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+', txt):
        t = m.group(0)
        if t == '(': st.append([])
        elif t == ')': x = st.pop(); st[-1].append(x)
        else: st[-1].append(t[1:-1] if t[0] == '"' else t)
    return st[0]


def footprint_holes(pcb, aux):
    """.kicad_pcb の footprint の穴 → [(reference, footprint id, gx, gy, drill)] (gerber 座標 = drill file origin = aux 原点基準、y 上向き)"""
    if not pcb or not os.path.exists(pcb): return None
    root = _sexpr(open(pcb, encoding='utf-8', errors='replace').read())
    root = root[0] if root and isinstance(root[0], list) else root
    ax, ay = aux or (0.0, 0.0); out = []
    for fp in (e for e in root if isinstance(e, list) and e and e[0] == 'footprint'):
        fid = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ''
        at = next((e for e in fp if isinstance(e, list) and e[0] == 'at'), ['at', 0, 0])
        fx, fy = float(at[1]), float(at[2]); rot = math.radians(float(at[3])) if len(at) > 3 else 0.0
        ref = next((e[2] for e in fp if isinstance(e, list) and e[0] == 'property' and len(e) > 2 and e[1] == 'Reference'), '')
        ref = ref or next((e[2] for e in fp if isinstance(e, list) and e[0] == 'fp_text' and len(e) > 2 and e[1] == 'reference'), '')
        for pd in (e for e in fp if isinstance(e, list) and e[0] == 'pad'):
            dr = next((e for e in pd if isinstance(e, list) and e[0] == 'drill'), None)
            if not dr: continue
            dv = [x for x in dr[1:] if isinstance(x, str) and re.match(r'^[\d.]+$', x)]
            if not dv: continue
            pa = next((e for e in pd if isinstance(e, list) and e[0] == 'at'), ['at', 0, 0])
            px, py = float(pa[1]), float(pa[2])
            kx = fx + px * math.cos(rot) + py * math.sin(rot); ky = fy - px * math.sin(rot) + py * math.cos(rot)  # KiCad は y 下向き・反時計回り
            out.append((ref, fid, kx - ax, ay - ky, float(dv[0])))
    return out


def png(drc, outdir, limit=30):
    """error の場所の切り抜き (銅は描き直す)"""
    os.makedirs(outdir, exist_ok=True)
    for f in os.listdir(outdir):
        if f.endswith('.png'): os.remove(os.path.join(outdir, f))
    R = drc.R; k = 0
    gs = {}
    for f in drc.findings:
        if f['level'] != 'error' or 'at' not in f or k >= limit: continue
        k += 1; li = drc.layer_names.index(f['layer']) if f.get('layer') in drc.layer_names else 0
        x, y = f['at']; m = 1.5
        sub = gl.Raster(x - m, y - m, x + m, y + m, drc.px)
        if li not in gs: gs[li] = gl.Gerber(discover(drc.gdir)[0][li])
        cu = np.asarray(gl.render(gs[li], sub)) > 127
        img = np.zeros(cu.shape + (3,), np.uint8); img[cu] = (110, 110, 110)
        im = Image.fromarray(img).resize((cu.shape[1] * 2, cu.shape[0] * 2), Image.NEAREST); dr = ImageDraw.Draw(im)
        P = lambda X, Y: tuple(2 * v for v in sub.xy2pix(X, Y))
        for a0, b0, a1, b1 in drc.segs: dr.line([P(a0, b0), P(a1, b1)], fill=(255, 255, 0))
        for h in drc.holes:
            if abs(h.x - x) < m + 2 and abs(h.y - y) < m + 2:
                c, r = P(h.x, h.y); rr = h.d / drc.px
                dr.ellipse([c - rr, r - rr, c + rr, r + rr], outline=(255, 255, 255) if h.plated else (255, 128, 0))
        c, r = P(x, y); dr.ellipse([c - 10, r - 10, c + 10, r + 10], outline=(255, 0, 255), width=2)
        at = f.get('at_kicad', f['at']); dr.text((4, 4), f"{f['code']} {drc.layer_names[li]} @({at[0]:.3f},{at[1]:.3f})", fill=(255, 255, 255))
        fn = os.path.join(outdir, f"{k:02d}_{f['code']}_{drc.layer_names[li].replace('.', '_')}.png"); im.save(fn); f['png'] = os.path.basename(fn)


def write_reports(drc, out_txt, out_json, meta):
    cnt = drc.summary()
    res = dict(tool='gerber-drc', version=VERSION, **meta, summary=cnt, layers=drc.layer_names, holes=len(drc.holes),
               rules=dict(edge_clearance=drc.edge_clr, hole_clearance=drc.hole_clr, tolerance=round(drc.tol, 4), source=drc.rules_src),
               outline=dict(loops=len(drc.loops), cutouts=drc.n_cutouts), notes=drc.notes, time_s=round(drc.T['total'], 2),
               status='fail' if cnt['error'] else 'ok', findings=drc.findings)
    if getattr(drc, 'rules_yaml', None): res['rules']['yaml'] = drc.rules_yaml; res['rules']['extra_edge'] = [list(e) for e in drc.edge_rules[1:]]
    json.dump(res, open(out_json, 'w'), ensure_ascii=False, indent=1)
    L = [f"# gerber-drc {meta.get('board', '')}  ({meta['date']})", f"gerber: {meta['gerber_dir']}",
         f"層: {', '.join(drc.layer_names)} / 穴 {len(drc.holes)} / 外形の閉路 {len(drc.loops)} (切り欠き {drc.n_cutouts})",
         f"規則: 外形 {drc.edge_clr} mm, NPTH {drc.hole_clr} mm (出典 {drc.rules_src or '既定値'}), 許容誤差 {drc.tol:.3f} mm, 解像度 {drc.px} mm/px",
         f"結果: error {cnt['error']} / warning {cnt['warning']} / info {cnt['info']}  ({drc.T['total']:.1f} s)"]
    L += [f'注: {n}' for n in drc.notes] + ['']
    for lv in LEVELS:
        fs = [f for f in drc.findings if f['level'] == lv]
        if not fs: continue
        L.append(f'## {lv} ({len(fs)})')
        for f in fs:
            at = f" @gerber{tuple(f['at'])}" if 'at' in f else ''
            if 'at_kicad' in f: at += f" kicad{tuple(f['at_kicad'])}"
            L.append(f"- [{f['code']} {JP.get(f['code'], '')}] {f['msg']}{at}" + (f"  → {f['png']}" if 'png' in f else ''))
        L.append('')
    if not cnt['error']: L.append('error なし。')
    open(out_txt, 'w').write('\n'.join(L) + '\n')
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description='gerber-drc: 製造データ (gerber/drill) の幾何の検査')
    ap.add_argument('project_dir', nargs='?'); ap.add_argument('name', nargs='?')
    ap.add_argument('--gerber-dir'); ap.add_argument('--pcb'); ap.add_argument('--pro')
    ap.add_argument('--out-dir'); ap.add_argument('--prefix', default='gerber_drc'); ap.add_argument('--json')
    ap.add_argument('--px', type=float, default=0.01)
    ap.add_argument('--edge-clearance', type=float); ap.add_argument('--hole-clearance', type=float)
    ap.add_argument('--rules', action='append', default=[], help='ルール YAML (複数可、後勝ち)。省略時は <project_dir>/gerber_drc_rules.yaml があれば使う')
    ap.add_argument('--no-rules', action='store_true', help='YAML を読まない (従来動作)')
    ap.add_argument('--png', action='store_true'); ap.add_argument('--strict', action='store_true'); ap.add_argument('-q', '--quiet', action='store_true')
    a = ap.parse_args(argv)
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, file=sys.stderr))
    try:
        P = a.project_dir
        if P:
            N = a.name or os.path.basename(os.path.abspath(P))
            pcb = a.pcb or os.path.join(P, N + '.kicad_pcb'); pro = a.pro or os.path.join(P, N + '.kicad_pro')
            gdir = a.gerber_dir or find_gerber_dir(P); outd = a.out_dir or os.path.dirname(os.path.abspath(gdir))
        else:
            if not a.gerber_dir: ap.error('project_dir か --gerber-dir が必要')
            N = a.name or ''; pcb, pro, gdir = a.pcb, a.pro, a.gerber_dir; outd = a.out_dir or '.'
        if a.json and not a.out_dir: outd = os.path.dirname(os.path.abspath(a.json))
        rpaths = [] if a.no_rules else list(a.rules)
        if not rpaths and not a.no_rules and P and os.path.isfile(os.path.join(P, PROJECT_RULES)): rpaths = [os.path.join(P, PROJECT_RULES)]
        rs = None; kw = {}; ec, hc = a.edge_clearance, a.hole_clearance
        kr = read_rules(pro)
        if rpaths:
            import importlib.util as iu  # gerber-lvs を sys.path 先頭に入れているので、同名衝突を避けてファイルから読む
            sp = iu.spec_from_file_location('gdrc_rules', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rules.py')); rl = iu.module_from_spec(sp); sp.loader.exec_module(rl)
            rs = rl.load(rpaths); rp = rules_params(rs)  # 優先: CLI > YAML > .kicad_pro > 0.2
            ec = ec if ec is not None else rp['edge_clr']; hc = hc if hc is not None else rp['hole_clr']
            kw = dict(extra_edge=rp['extra_edge'], sev=rp['sev'], hole_rules=rp['hole_rules'])
            if any(h['scope'].get('footprints') for h in rp['hole_rules']): kw['fp_holes'] = footprint_holes(pcb, aux_origin(pcb) if pcb else None)
            if rp['edge_clr'] is not None or rp['hole_clr'] is not None: kr = dict(kr, src='YAML ' + ', '.join(rs.summary()['files']))
        drc = DRC(gdir, kr, aux=aux_origin(pcb) if pcb else None, px=a.px, edge_clr=ec, hole_clr=hc, log=log, **kw)
        if rs is not None: drc.notes += rp['notes']; drc.rules_yaml = rs.summary()
        drc.run()
        os.makedirs(outd, exist_ok=True)
        if a.png: png(drc, os.path.join(outd, a.prefix))
        meta = dict(board=N, date=time.strftime('%Y-%m-%d %H:%M:%S %Z'), gerber_dir=os.path.abspath(gdir))
        res = write_reports(drc, os.path.join(outd, a.prefix + '.txt'), a.json or os.path.join(outd, a.prefix + '.json'), meta)
        cnt = res['summary']
        print(f"gerber-drc {N}: error={cnt['error']} warning={cnt['warning']} ({drc.T['total']:.1f}s) → {os.path.join(outd, a.prefix + '.txt')}")
        for f in drc.findings:
            if f['level'] == 'error' or not a.quiet: print(f"  {f['level']} {f['code']}: {f['msg']}")
        return 1 if cnt['error'] or (a.strict and cnt['warning']) else 0
    except gl.Unsupported as e:
        print(f'gerber-drc: 未対応の構文: {e}', file=sys.stderr); return 2
    except Exception as e:
        print(f'gerber-drc: 実行できなかった: {e}', file=sys.stderr)
        if not a.quiet: traceback.print_exc()
        return 2


if __name__ == '__main__':
    sys.exit(main())
