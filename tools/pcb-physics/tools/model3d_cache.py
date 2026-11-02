"""data/model3d/bbox.json に 3D モデル (STEP) の箱を入れる。KiCad の 3D ライブラリが無い環境 (WSL など) でも同じ Tj になるように。
使い方: python tools/model3d_cache.py <KiCad プロジェクト>...   (STEP が見つかるマシンで実行)"""
import glob, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from core import model3d, pcbread

c = model3d._cache()
for d in sys.argv[1:]:
    pcb = [p for p in glob.glob(os.path.join(d, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
    for fp in pcbread.read_pcb(pcb)['footprints']:
        for m in fp.get('models', []):
            if '${KIPRJMOD}' in m['path']:
                continue   # 案件内のモデルは案件と一緒に運ばれる
            f = model3d.resolve(m['path'], d)
            if f and m['path'] not in c:
                r = model3d.raw(f); r = {k: (float(v) if k != 'n_points' else int(v)) for k, v in r.items()}
                r['file'] = os.path.relpath(f, os.path.dirname(os.path.dirname(f))); c[m['path']] = r
                print('追加', fp['ref'], m['path'])
os.makedirs(os.path.dirname(model3d.CACHE), exist_ok=True)
json.dump(dict(sorted(c.items())), open(model3d.CACHE, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
print(len(c), '件 →', model3d.CACHE)
