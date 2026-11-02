#!/usr/bin/env python3
"""ルール YAML → KiCad custom rules (.kicad_dru) 書き出し。
  rules2dru.py <rules.yaml ...> [-o out.kicad_dru]   (省略時は標準出力)
  rules2dru.py --project <project_dir> <name>        make_out.sh から: <project_dir>/gerber_drc_rules.yaml → <name>.kicad_dru を毎回作り直す
    - gerber_drc_rules.yaml が無ければ何もしない (exit 0、既存の .kicad_dru はそのまま)
    - 既存の <name>.kicad_dru が手書き (先頭に MARK が無い) なら上書きしない: 警告を出し、生成物は out/<name>.kicad_dru.generated に置く (安全側、2026-10-14)
    - 生成物 (MARK あり) なら上書き。内容が同じなら書かない
backend が kicad_dru / both の有効ルールを、KiCad 10 の (rule ...) 構文で書く。書けない check はコメントで残す。
kicad-cli 10.0.7 の pcb drc で構文が通ることを tests/run_tests.py で確認している。"""
import argparse, fnmatch, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rules as rl

SIMPLE = {'track_width', 'clearance', 'hole_size', 'annular_width', 'via_diameter', 'hole_to_hole', 'hole_clearance',
          'edge_clearance', 'silk_clearance', 'text_height', 'text_thickness'}
MARK = '# gerber-drc rules2dru.py が生成'
PROJECT_RULES = 'gerber_drc_rules.yaml'
HOLE_COND = {'npth': "A.Pad_Type == 'NPTH, mechanical'", 'pth': "A.isPlated()", 'via': "A.Type == 'Via'"}


def mm(v): return f'{float(v):g}mm'


def fp_cond(globs, neg=False):
    """footprint の glob → memberOfFootprint (reference と footprint id の両方にワイルドカードで当たる)"""
    c = ' || '.join(f"A.memberOfFootprint('{g}')" for g in globs)
    return (f'!({c})' if neg else f'({c})') if c else ''


def minmax(v):
    if isinstance(v, dict):
        if 'allowed' in v:
            al = v['allowed']; t = float(v.get('tol', 0))
            if len(al) != 1: return None
            return f"(min {mm(al[0] - t)}) (max {mm(al[0] + t)})"
        return ' '.join(f'({k} {mm(v[k])})' for k in ('min', 'max') if isinstance(v.get(k), (int, float)))
    return f'(min {mm(v)})'


def rule_sexpr(r):
    """1 ルール → (rule ...) の文字列。書けないなら (None, 理由)"""
    c, sc, ex = r.get('check'), r.get('scope') or {}, r.get('except') or {}
    v = r.get('value'); conds = []; layer = None
    if c == 'hole_diameter_set': c = 'hole_size'
    if c == 'edge_keepout_courtyard':
        # 部品 (の pad) と外形の距離。courtyard 自体は edge_clearance の対象外のため pad で近似 (DESIGN.md)
        c = 'edge_clearance'; conds.append("A.Type == 'Pad'")
    if c == 'silk_overlap': c = 'silk_clearance'
    if c not in SIMPLE: return None, f'check {c} は .kicad_dru に書けない'
    lays = sc.get('layers') or []
    if any('Mask' in l for l in lays): return None, 'マスク層の clearance は .kicad_dru の対象外 (solder_mask_min_width は .kicad_pro)'
    if sc.get('hole') in HOLE_COND and c in ('hole_size', 'hole_clearance', 'annular_width', 'hole_to_hole'):
        conds.append(HOLE_COND[sc['hole']])
    if sc.get('footprints'): conds.append(fp_cond(sc['footprints']))
    if ex.get('footprints'): conds.append(fp_cond(ex['footprints'], neg=True))
    if ex.get('nets'): conds.append('!(' + ' || '.join(f"A.NetName == '{n}'" for n in ex['nets']) + ')')
    if c in ('silk_clearance', 'text_height', 'text_thickness'):
        ls = [l for l in lays if 'Silk' in l]; layer = '?.Silkscreen' if not ls or len(ls) > 1 else ls[0]
    if c == 'silk_clearance' and r['id'].startswith('user.silk_part'): conds.append("B.Type == 'Pad'")
    lim = minmax(v)
    if not lim: return None, f'value {v} を min/max にできない'
    sev = {'error': 'error', 'warning': 'warning', 'info': 'warning'}.get(r.get('severity', 'error'), 'error')
    L = [f'(rule "{r["id"]}"', f'  (constraint {c} {lim})']
    if layer: L.append(f'  (layer "{layer}")')
    if conds: L.append('  (condition "' + ' && '.join(conds).replace('"', "'") + '")')
    L.append(f'  (severity {sev}))')
    return '\n'.join(L), None


def to_dru(rs):
    out = ['(version 1)', f"{MARK}。手で編集しない (gerber_drc_rules.yaml を直す)。元: {', '.join(rs.summary()['files'])}"]
    out += [f'# 注意: {w}' for w in rs.warnings]
    skip = []
    for r in rs.for_backend('kicad_dru'):
        s, why = rule_sexpr(r)
        if s: out += ['', f"# {r['id']} ({r.get('_src')[-1][0]}) {r.get('ref') or r.get('note') or ''}".rstrip(), s]
        else: skip.append(f"# 書き出さない: {r['id']}: {why}")
    return '\n'.join(out + [''] + skip) + '\n'


def project(P, N, log=lambda m: print(m, file=sys.stderr)):
    """make_out.sh 用。戻り値 = 'none' / 'same' / 'written' / 'manual' (手書きを残した)"""
    y = os.path.join(P, PROJECT_RULES); dru = os.path.join(P, N + '.kicad_dru')
    if not os.path.isfile(y): log(f'rules2dru: {PROJECT_RULES} が無いので .kicad_dru は作らない'); return 'none'
    txt = to_dru(rl.load([y]))
    if os.path.exists(dru):
        old = open(dru, encoding='utf-8', errors='replace').read()
        if MARK not in '\n'.join(old.splitlines()[:3]):
            g = os.path.join(P, 'out', N + '.kicad_dru.generated'); os.makedirs(os.path.dirname(g), exist_ok=True); open(g, 'w').write(txt)
            log(f'rules2dru: 警告: {dru} は手書き (rules2dru の生成物ではない) なので上書きしない。KiCad DRC は手書きのルールで動く。'
                f'YAML からの生成物は {g}。YAML に移したら手書きを消すか別名にする')
            return 'manual'
        if old == txt: log(f'rules2dru: {dru} は最新'); return 'same'
    open(dru, 'w').write(txt); log(f'rules2dru: {y} → {dru}')
    for w in rl.load([y]).warnings: log(f'rules2dru: 注意: {w}')
    return 'written'


def main(argv=None):
    ap = argparse.ArgumentParser(description='ルール YAML → .kicad_dru')
    ap.add_argument('rules', nargs='*'); ap.add_argument('-o', '--out')
    ap.add_argument('--project', nargs=2, metavar=('PROJECT_DIR', 'NAME'))
    a = ap.parse_args(argv)
    if a.project:
        try: project(*a.project); return 0
        except Exception as e: print(f'rules2dru: 失敗: {e}', file=sys.stderr); return 2
    if not a.rules: ap.error('rules か --project が必要')
    txt = to_dru(rl.load(a.rules))
    if a.out: open(a.out, 'w').write(txt); print(f'rules2dru: → {a.out}', file=sys.stderr)
    else: sys.stdout.write(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
