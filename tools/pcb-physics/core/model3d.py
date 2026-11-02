"""model3d — footprint の 3D モデル (STEP) から外形の箱と上面を読む (CARTESIAN_POINT の座標だけを見る簡易版)。
上面 = z 最大から 0.05 mm 以内の点の xy 範囲 (リードを除いた本体の上面)。"""
import os, re
import numpy as np

SEARCH = [p for p in (os.environ.get('KICAD10_3DMODEL_DIR'), os.environ.get('KICAD_SHARE') and os.path.join(os.environ['KICAD_SHARE'], '3dmodels'), '/usr/share/kicad/3dmodels') if p]
_pt = re.compile(r"CARTESIAN_POINT\('[^']*',\(\s*([-\d.E+e]+),\s*([-\d.E+e]+),\s*([-\d.E+e]+)\)\)")


def resolve(path, project_dir):
    p = re.sub(r'\$\{KICAD\d*_3DMODEL_DIR\}', '{3D}', path).replace('${KIPRJMOD}', project_dir)
    cands = [p.replace('{3D}', d) for d in (os.environ.get('KICAD10_3DMODEL_DIR'),) + tuple(SEARCH) if d] if '{3D}' in p else [p]
    for c in cands:
        for ext in ('', '.step', '.stp'):
            q = os.path.splitext(c)[0] + ext if ext else c
            if os.path.exists(q) and q.lower().endswith(('.step', '.stp')):
                return q
    return None


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'model3d', 'bbox.json')


def _cache():
    import json
    try:
        return json.load(open(CACHE, encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def raw(step_file):
    """スケール・オフセット前の生の値 (キャッシュに入れる形)"""
    s = open(step_file, encoding='latin-1').read()
    a = np.array([tuple(map(float, m)) for m in _pt.findall(s)])
    if not len(a):
        return None
    zmax = a[:, 2].max(); top = a[a[:, 2] >= zmax - 0.05]
    return dict(n_points=len(a), xmin=a[:, 0].min(), xmax=a[:, 0].max(), ymin=a[:, 1].min(), ymax=a[:, 1].max(), zmin=a[:, 2].min(), zmax=zmax,
                top_x=top[:, 0].max() - top[:, 0].min(), top_y=top[:, 1].max() - top[:, 1].min())


def from_raw(r, scale=(1, 1, 1), offset=(0, 0, 0), file=None):
    sx, sy, sz = scale; zmin, zmax = r['zmin'] * sz + offset[2], r['zmax'] * sz + offset[2]
    tx, ty = r['top_x'] * sx, r['top_y'] * sy
    return dict(file=file, n_points=r['n_points'], x=float((r['xmax'] - r['xmin']) * sx), y=float((r['ymax'] - r['ymin']) * sy), zmin=float(zmin),
                height=float(zmax - min(0.0, zmin)), top_x=float(tx), top_y=float(ty), top_area=float(tx * ty))


def lookup(kicad_path, project_dir, scale=(1, 1, 1), offset=(0, 0, 0)):
    """STEP を探して箱を返す。見つからなければ data/model3d/bbox.json (KiCad のパス文字列がキー) を使う。どちらも無ければ None"""
    f = resolve(kicad_path, project_dir)
    if f:
        r = bbox(f, scale, offset)
        if r:
            r['source'] = 'step'
        return r
    c = _cache().get(kicad_path)
    if c:
        r = from_raw(c, scale, offset, file='data/model3d/bbox.json'); r['source'] = 'cache'
        return r
    return None


def bbox(step_file, scale=(1, 1, 1), offset=(0, 0, 0)):
    s = open(step_file, encoding='latin-1').read()
    a = np.array([tuple(map(float, m)) for m in _pt.findall(s)])
    if not len(a):
        return None
    a = a * np.array(scale) + np.array(offset)
    zmax = a[:, 2].max(); top = a[a[:, 2] >= zmax - 0.05]
    tx, ty = top[:, 0].max() - top[:, 0].min(), top[:, 1].max() - top[:, 1].min()
    return dict(file=step_file, n_points=len(a), x=float(a[:, 0].max() - a[:, 0].min()), y=float(a[:, 1].max() - a[:, 1].min()),
                zmin=float(a[:, 2].min()), height=float(zmax - min(0.0, a[:, 2].min())), top_x=float(tx), top_y=float(ty),
                top_area=float(tx * ty))
