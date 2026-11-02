#!/usr/bin/env python3
"""実装性チェック用: 工場へ出す F/B.Paste・F/B.Mask の gerber 実データから、パッドごとのペースト開口・マスク開口を求める。

- kicad-cli で `pcb export gerbers -l F.Paste,B.Paste,F.Mask,B.Mask` を nice 付きで出す (load average > 20 なら待つ)。
  結果は /tmp/kreview-cache/mount-gerber-<sha1(pcb)>/ にキャッシュ。基板ファイルは読むだけ。
- 読み込み・描画は gerber-lvs の gerberlib.py (gerber-drc と同じ解釈、環境変数 GERBER_LVS_DIR、既定 ../gerber-lvs)。
- パッドの開口 = パッドの銅の中にかかる開口の連結成分 (= フットプリントに足した「開口用パッド」(番号なし・銅なし) で
  延長した分も含む)。座標は gerber (Y 上向き、ドリル原点なし) → KiCad (Y 下向き) に戻す。
"""
import hashlib, math, os, subprocess, sys, time
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'pcb-score'))
import kpcb
LVS_DIR = os.environ.get('GERBER_LVS_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'gerber-lvs'))
PX = 0.01          # mm/画素 (テント余裕 0.10 mm を ±0.01 mm で見る)
LAYERS = ('F.Paste', 'B.Paste', 'F.Mask', 'B.Mask')


def _load_ok(limit=20.0, wait_s=600):
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            if float(open('/proc/loadavg').read().split()[0]) <= limit:
                return True
        except Exception:
            return True
        time.sleep(15)
    return False


def export(pcb, cache=None):
    """-> (dir, {layer: path}) 。失敗は (None, 理由)。cache の既定は 環境変数 KREVIEW_CACHE か /tmp/kreview-cache"""
    cache = cache or os.environ.get('KREVIEW_CACHE', '/tmp/kreview-cache')
    h = hashlib.sha1(open(pcb, 'rb').read()).hexdigest()[:16]
    d = os.path.join(cache, f'mount-gerber-{h}'); os.makedirs(d, exist_ok=True)
    def found():
        out = {}
        for L in LAYERS:
            k = L.replace('.', '_')
            for fn in os.listdir(d):
                if k in fn and not fn.endswith('.gbrjob'):
                    out[L] = os.path.join(d, fn)
        return out
    got = found()
    if len(got) == len(LAYERS):
        return d, got
    if not _load_ok():
        return None, 'load average > 20 が 10 分続いたので gerber を出さなかった'
    env = dict(os.environ, HOME=os.environ.get('KICAD_CLI_HOME', os.path.expanduser('~')))
    r = subprocess.run(['nice', '-n', '10', 'kicad-cli', 'pcb', 'export', 'gerbers', '-l', ','.join(LAYERS), '-o', d + '/', pcb],
                       capture_output=True, text=True, env=env, timeout=600)
    got = found()
    if len(got) != len(LAYERS):
        return None, f'kicad-cli の gerber 出力に失敗 ({r.returncode}): {(r.stderr or r.stdout)[-300:]}'
    return d, got


class Openings:
    """F/B の Paste・Mask を画素にし、連結成分を付ける。pad_open(f, p, 'Paste') でパッドの開口を返す"""

    def __init__(self, board, files, px=PX):
        sys.path.insert(0, LVS_DIR)
        import gerberlib as gl
        self.gl = gl; self.px = px; self.b = board
        outline, _, _ = board.outline()
        xs = [q[0] for q in outline]; ys = [q[1] for q in outline]
        # gerber 座標 (x, -y)
        self.R = gl.Raster(min(xs) - 1, -max(ys) - 1, max(xs) + 1, -min(ys) + 1, px)
        self.img, self.runs = {}, {}
        for L, path in files.items():
            g = gl.Gerber(path)
            im = gl.render(g, self.R)
            self.img[L] = im; self.runs[L] = gl.Runs(im)
        self.cache = {}

    def _pix(self, x, y):           # KiCad 座標 → (列, 行)
        c, r = self.R.xy2pix(x, -y)
        return c + 0.5, r + 0.5

    def pad_open(self, f, p, kind, grow=1.5):
        """-> dict(area_mm2, on_pad_mm2, out_mm2, ext_mm (パッド銅の外へ出た最大距離), mask (bool 窓), r0, c0) か None"""
        side = 'B' if f.side == 'B' else 'F'
        L = f'{side}.{kind}'
        key = (L, f.ref, p.num, round(p.x, 4), round(p.y, 4))
        if key in self.cache:
            return self.cache[key]
        if L not in self.runs:
            return None
        ru = self.runs[L]
        labs = ru.labels_in_disk(self.R, p.x, -p.y, max(0.02, min(p.w, p.h) * 0.45))
        if not labs:
            self.cache[key] = None; return None
        half = max(p.w, p.h) / 2 + grow
        c, r = self._pix(p.x, p.y); n = int(math.ceil(half / self.px))
        r0, c0 = max(0, int(r) - n), max(0, int(c) - n)
        win = ru.window(r0, int(r) + n, c0, int(c) + n, labels=set(labs))
        rr, cc = np.nonzero(win)
        X = self.R.x0 + (cc + c0 + 0.5) * self.px; Y = -(self.R.y1 - (rr + r0 + 0.5) * self.px)
        lx, ly = kpcb.rot(X - p.x, Y - p.y, -p.angle)   # 世界 → パッドの局所 (Pad.poly の逆)
        dx = np.maximum(np.abs(lx) - p.w / 2, 0); dy = np.maximum(np.abs(ly) - p.h / 2, 0)
        on = (dx == 0) & (dy == 0)
        a = self.px ** 2
        out = dict(area_mm2=round(float(len(X)) * a, 4), on_pad_mm2=round(float(on.sum()) * a, 4), out_mm2=round(float((~on).sum()) * a, 4),
                   ext_mm=round(float(np.hypot(dx, dy).max()) if len(X) else 0.0, 3), X=X, Y=Y,
                   labels=sorted(labs))
        self.cache[key] = out
        return out

    def via_rel(self, f, p, vias, reach=0.3):
        """マスク開口 (gerber) と via の関係 (mount_geom.via_relations と同じ形)。開口が無ければ None"""
        o = self.pad_open(f, p, 'Mask')
        if o is None or not len(o['X']):
            return None
        rows = []
        for v in vias:
            d = np.hypot(o['X'] - v['at'][0], o['Y'] - v['at'][1])
            dmin = float(d.min()) - self.px / 2
            land = dmin - v['size'] / 2
            if land > reach:
                continue
            r = v['drill'] / 2
            inside = d <= r
            nd = math.pi * r * r / self.px ** 2
            f_open = min(1.0, float(inside.sum()) / nd) if nd else 0.0
            rows.append(dict(at=tuple(v['at']), net=v['net'], drill=v['drill'], size=v['size'], center_in_pad=bool(dmin <= 0),
                             drill_edge=round(dmin - r, 3), land_edge=round(land, 3), f_open=round(f_open, 3)))
        return rows


if __name__ == '__main__':
    pcb = sys.argv[1]; refs = sys.argv[2].split(',') if len(sys.argv) > 2 else None
    d, files = export(pcb); print(d, files if d else '')
    b = kpcb.Board(pcb); O = Openings(b, files)
    for f in b.footprints:
        if refs and f.ref not in refs:
            continue
        for p in f.pads:
            if p.type == 'smd' and p.num:
                o = O.pad_open(f, p, 'Paste'); m = O.pad_open(f, p, 'Mask')
                print(f.ref, p.num, p.w, p.h, 'paste', o and (o['area_mm2'], o['on_pad_mm2'], o['ext_mm']), 'mask', m and (m['area_mm2'], m['ext_mm']))
