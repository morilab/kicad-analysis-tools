#!/usr/bin/env python3
"""比較スコア (score_profiles.py) の単体テスト。python3 tools/pcb-score/tests/test_score_profiles.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import score_profiles as SP

ok = True
def check(name, got, want):
    global ok
    r = got == want; ok &= r
    print('OK ' if r else 'NG ', name, got, '(期待', want, ')')

P = SP.load()
BASE = dict(area=3000, mst_sig=1200, hpwl_pwr=160, crossings=200, crossings_hdr=150, usb_mm=10, far_empty=0.2, density=0.5, pwr_clear=0.5)
W1 = {k: 1.0 for k in P['items']}
# 基準版自身: 各項目 100、上限なし
r = SP.score(BASE, BASE, W1, P)
check('基準版は各項目 100', all(v['s'] == 100 for v in r['items'].values()), True)
check('基準版の S = 100 × 項目数', r['S'], 100.0 * len(r['items']))
# 低いほど良い: 面積半分で 200、高いほど良い: 密度 2 倍で 200 (100 を超える)
check('面積半分 → 200', SP.score(dict(BASE, area=1500), BASE, {'area': 1}, P)['S'], 200.0)
check('密度 2 倍 → 200', SP.score(dict(BASE, density=1.0), BASE, {'density': 1}, P)['S'], 200.0)
check('far_empty 0 でも floor で有限 (400)', SP.score(dict(BASE, far_empty=0.0), BASE, {'far_empty': 1}, P)['S'], 400.0)
# 係数
check('係数 2.5 倍', SP.score(dict(BASE, area=1500), BASE, {'area': 2.5}, P)['S'], 500.0)
check('負の係数 (thermal の空き重視)', SP.score(dict(BASE, density=1.0), BASE, {'density': -0.5}, P)['S'], -100.0)
# 片方に無い指標は計算しない。同じ指標名で比べる (mst が片方だけなら hpwl へ)
r = SP.score(dict(hpwl_sig=500), dict(hpwl_sig=1000, mst_sig=1200), {'wirelen': 1}, P)
check('同じ指標名 (hpwl_sig) で比べる', (r['items']['wirelen']['key'], r['S']), ('hpwl_sig', 200.0))
check('via が無ければ項目なし', 'vias' in SP.score(BASE, BASE, W1, P)['items'], False)
# 違反: 軽い違反は 1 件 −1000、ハード制約は feasible=False
check('軽い違反 2 件 (p22 1 + j2)', SP.score(dict(BASE, p22_parts=1, j2=5.0), BASE, {'area': 1}, P)['S'], 100.0 - 2000)
check('open 0.05 は数えない', SP.soft_count(dict(open=0.05)), 0)
check('重なり → 作れない', SP.score(dict(BASE, overlap=0.5), BASE, W1, P)['feasible'], False)
check('穴 0.005 は誤差 → 作れる', SP.score(dict(BASE, hole=0.005), BASE, W1, P)['feasible'], True)
# 順位: 作れない案は S が高くても除外
rows = {'a': dict(BASE, area=2000), 'b': dict(BASE, area=1000, outside=1.0), 'c': BASE}
rk = SP.rank(rows, BASE, {'area': 1}, P)
check('順位 (作れない b は除外)', [(n, v['rank']) for n, v in rk.items()], [('a', 1), ('c', 2), ('b', None)])
check('表に除外が出る', '除外' in SP.table(rk, P), True)
# プロファイル
check('組み込み 5 種', sorted(P['profiles']), ['cost', 'default', 'signal', 'size', 'thermal'])
check('全プロファイルの項目が定義済み', all(set(w) <= set(P['items']) for w in P['profiles'].values()), True)
with tempfile.NamedTemporaryFile('w', suffix='.toml', delete=False) as f:
    f.write('[score]\nprofile = "p"\n[score.profiles.p]\nbase = "cost"\nweights = { thermal = 0.1, area = 2.5 }\n')
cfg = SP.project_config(f.name); w = SP.weights('p', cfg, P); os.unlink(f.name)
check('review.toml の上書き', (w['thermal'], w['area'], w['vias']), (0.1, 2.5, P['profiles']['cost']['vias']))
try:
    SP.weights('nai', {}, P); check('未知のプロファイルは KeyError', False, True)
except KeyError:
    check('未知のプロファイルは KeyError', True, True)
# CLI: pcb_score --profile (同じ基板 2 枚 → 両方 基準と同じ = 100 × 係数和)
import subprocess, json
KH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
pcb = os.path.join(KH, 'testboards', 'pitfall-4l', 'pitfall-4l.kicad_pcb')
if os.path.exists(pcb):
    out = tempfile.mktemp(suffix='.json')
    cp = subprocess.run([sys.executable, os.path.join(KH, 'tools', 'pcb-score', 'pcb_score.py'), pcb, pcb, '--profile', 'cost', '--json', out], capture_output=True, text=True)
    R = json.load(open(out)); os.unlink(out); pr = R[1]['profile']
    want = round(100 * sum(P['profiles']['cost'][i] for i in pr['items']), 1)
    check('pcb_score --profile cost (基準と同じ基板)', (cp.returncode, pr['S'], pr['feasible'], R[0]['profile']['rank'] + pr['rank']), (0, want, True, 3))
    check('配線後は via / detour も入る', {'vias', 'detour'} <= set(pr['items']), True)
print('ALL OK' if ok else 'FAILED'); sys.exit(0 if ok else 1)
