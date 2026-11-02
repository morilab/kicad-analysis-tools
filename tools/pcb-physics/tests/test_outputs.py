"""kthermal の出力ファイルのテスト (2026-10-19)。
--mode artwork / placement / both のどれでも、出力フォルダに thermal.md・parts_table.csv・thermal_<mode>.json・PNG がそろうこと。
(2026-10-19 まで thermal.md と parts_table.csv は両モードを解いた時だけ出ていた。--mode を 1 つにした時は出ず、thermal_<mode>.json の parts_table が無いファイルを指していた)
使い方: python tests/test_outputs.py [--project <KiCad プロジェクトのディレクトリ>] [--cond <thermal.yaml>]
  既定は同梱の testboards/pitfall-4l と examples/pitfall-4l/thermal.yaml (約 10 分)。--project を渡したときの既定の --cond は examples/usb-jtag-ft2232h/thermal.yaml。格子は 0.25 mm (値は見ない、ファイルと見出しだけ。0.5 mm 以上は core/diag.py の斜め配線補正が 0 除算で落ちるので使わない)。"""
import os, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.join(HERE, '..')
arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
# 既定はリポジトリ同梱の試験基板 testboards/pitfall-4l (条件 examples/pitfall-4l/thermal.yaml)
PROJ = arg('--project', os.path.join(ROOT, '..', '..', 'testboards', 'pitfall-4l'))
COND = arg('--cond', os.path.join(ROOT, 'examples', 'pitfall-4l' if '--project' not in sys.argv else 'usb-jtag-ft2232h', 'thermal.yaml'))
FAIL = []


def check(ok, msg):
    print(('ok   ' if ok else 'FAIL ') + msg)
    if not ok:
        FAIL.append(msg)


with tempfile.TemporaryDirectory(prefix='kthermal-outputs-', dir=os.environ.get('TEST_TMP')) as tmp:
    for mode in ('artwork', 'placement', 'both'):
        out = os.path.join(tmp, mode)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'kthermal.py'), PROJ, '--cond', COND, '--mode', mode, '--grid', '0.25', '--out', out],
                           capture_output=True, text=True)
        check(r.returncode in (0, 1), f'{mode}: 終了コード {r.returncode} (0 = fail なし、1 = worst に fail あり)' + ('' if r.returncode in (0, 1) else '\n' + r.stderr[-2000:]))
        ms = ['placement', 'artwork'] if mode == 'both' else [mode]
        for f in ['thermal.md', 'parts_table.csv'] + [f'thermal_{m}.json' for m in ms] + [f'temp_{c}_{m}_{s}.png' for c in ('typ', 'worst') for m in ms for s in 'FB']:
            check(os.path.exists(os.path.join(out, f)), f'{mode}: {f} がある')
        md = open(os.path.join(out, 'thermal.md'), encoding='utf-8').read() if os.path.exists(os.path.join(out, 'thermal.md')) else ''
        check(('## アートワーク後' in md) == ('artwork' in ms), f'{mode}: thermal.md のアートワークの節の有無')
        check(('## 配置案' in md) == ('placement' in ms), f'{mode}: thermal.md の配置案の節の有無')
        check('## 解析条件の部品表' in md, f'{mode}: thermal.md に部品表の節がある')
print('outputs test:', 'すべて ok' if not FAIL else f'FAIL {len(FAIL)} 件')
sys.exit(1 if FAIL else 0)
