"""格子だけを作って保存し、検査結果 (衝突・細線・未充填ゾーン・孤立銅・分断ネット) を表示する。IR ドロップ用は dx 0.1 mm を推奨。
  python -m core.gridtool <project_dir> [--mode artwork] [--dx 0.1] [--out DIR]
熱解析は既定 0.25 mm のまま (kthermal.py --grid)。FT2232H rev0.2 (52×41 mm): dx 0.1 mm で 3.8 s、dx 0.05 mm で 13 s・約 0.7 GB (2026-10-14、斜め補正込み)。"""
import argparse, glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import pcbread, grid as CG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project'); ap.add_argument('--mode', default='artwork'); ap.add_argument('--dx', type=float, default=0.1)
    ap.add_argument('--sub', type=int, default=4); ap.add_argument('--no-diag-corr', action='store_true', help='斜め配線の線幅ベース補正を切る (比較用)'); ap.add_argument('--out', default='out/grid')
    a = ap.parse_args()
    pcb = [p for p in glob.glob(os.path.join(a.project, '*.kicad_pcb')) if not os.path.basename(p).startswith('_')][0]
    t = time.time(); board = pcbread.read_pcb(pcb)
    g, st = CG.build(board, a.mode, a.dx, a.sub, diag_corr=not a.no_diag_corr)
    os.makedirs(a.out, exist_ok=True); path = os.path.join(a.out, f"grid_{a.mode}_dx{a.dx:g}.npz"); g.save(path)
    c = g.meta['checks']
    print(f"格子 {g.shape} (dx {a.dx} mm)、{time.time() - t:.1f} s → {path}")
    print(f"  衝突セル {c['conflict_cells']}、最細線 {c['min_track_mm']} mm、細線 {c['thin_tracks']} 本、未充填ゾーン {len(c['zones_unfilled'])}、"
          f"連結成分 {c['n_components']}、孤立銅 {len(c['islands'])}、分断ネット {len(c['split_nets'])}")
    for w in g.meta['warnings']:
        print('  警告:', w)


if __name__ == '__main__':
    main()
