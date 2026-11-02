#!/usr/bin/env python3
"""revhist の自己テスト (最小の .kicad_sch で 書き込み → check OK → 冪等 → 不一致検出)"""
import os, subprocess, sys, tempfile
R = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'revhist.py')
SCH = '''(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(title_block
\t\t(title "t")
\t\t(date "2000-01-01")
\t\t(rev "0.0")
\t)
\t(lib_symbols)
\t(text "note (with \\"quote\\")"
\t\t(at 10 10 0)
\t\t(uuid "00000000-0000-0000-0000-000000000002")
\t)
\t(sheet_instances
\t\t(path "/"
\t\t\t(page "1")
\t\t)
\t)
)
'''
CL = '''# CHANGELOG
## rev0.2 | 未発行 | AI 作成
要約: next
## rev0.1 | 2026-10-09 | AI, review D. Mori
要約: first (paren) "q"
- detail
'''

def run(*a):
    return subprocess.run([sys.executable, R, *a], capture_output=True, text=True)

with tempfile.TemporaryDirectory() as d:
    s, c = os.path.join(d, 'a.kicad_sch'), os.path.join(d, 'CHANGELOG.md')
    open(s, 'w').write(SCH); open(c, 'w').write(CL)
    assert run(c, s, '--check').returncode == 1
    assert run(c, s).returncode == 0
    assert run(c, s, '--check').returncode == 0, run(c, s, '--check').stdout
    t1 = open(s).read()
    assert '(rev "0.1")' in t1 and '(date "2026-10-09")' in t1 and 'note (with' in t1
    assert run(c, s).returncode == 0 and open(s).read() == t1          # 冪等
    assert run(c, s, '--include-unreleased', '--check').returncode == 1
    assert run(c, s, '--include-unreleased').returncode == 0
    assert run(c, s, '--include-unreleased', '--check').returncode == 0
    t2 = open(s).read()
    assert t2.count('改版履歴\\n版 / 日付 / 作成者 / 変更内容') == 1 and '(date "未発行")' in t2
    # 旧版 (英語の見出し) を見つけたら日本語に置き換える
    open(s, 'w').write(t1.replace('改版履歴\\n版 / 日付 / 作成者 / 変更内容', 'REVISION HISTORY / 改版履歴\\nrev / date / author / summary'))
    assert run(c, s, '--check').returncode == 1
    assert run(c, s).returncode == 0 and run(c, s, '--check').returncode == 0
    assert 'REVISION HISTORY' not in open(s).read()
    open(c, 'w').write('## rev0.1 | 2026-10-09 | AI\n')                 # 要約なし
    assert run(c, s, '--check').returncode == 2
print('revhist tests: OK')
