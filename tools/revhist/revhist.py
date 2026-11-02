#!/usr/bin/env python3
"""revhist: CHANGELOG.md (正本) の改版履歴を回路図 (.kicad_sch) のテキストに書き写す / 一致を確かめる。

CHANGELOG.md の書式 (templates/board-project/CHANGELOG.md):

    ## rev0.2 | 2026-10-20 | AI 作成 / D. Mori 確認
    要約: 基板縮小 42x25mm silk 修正      <- 回路図に載る。漢字+英数字のみ (カナは既定フォントで崩れる)
    - 詳細 (箇条書き。回路図には書かない)

    ## rev0.3 | 未発行 | ...      <- 日付が 未発行 (または unreleased) の版は既定では書かない (--include-unreleased で書く)

回路図側: 先頭行が「改版履歴」(旧: "REVISION HISTORY") の (text ...) を 1 個だけ持つ。無ければ追加する。
本文は「改版履歴 / 版 / 日付 / 作成者 / 変更内容」の見出しと 1 版 1 行。人が読む文は日本語 (2026-10-09 の規則)。
あれば位置・文字サイズは保ったまま本文だけ置き換える。title_block の (rev ...) (date ...) も最新版に合わせる。
依存は標準ライブラリだけ。KiCad 10.0.7 の .kicad_sch で確認。

使い方:
    revhist.py CHANGELOG.md board.kicad_sch            # 書き込み (変更があれば上書き、元は board.kicad_sch.revhist-bak)
    revhist.py CHANGELOG.md board.kicad_sch --check    # 一致確認のみ (終了コード 0 = 一致、1 = 不一致、2 = 書式エラー)
    revhist.py CHANGELOG.md --print                    # 回路図に書く本文を表示
    オプション: --at X,Y (新規追加時の位置 mm, 既定 20,20)、--size 1.27、--include-unreleased
"""
import argparse, re, shutil, sys, uuid

HEAD = '改版履歴'                           # 回路図テキストの 1 行目 (目印)。人が読む文は日本語 (2026-10-09 規則)
COLS = '版 / 日付 / 作成者 / 変更内容'
LEGACY_HEADS = ('REVISION HISTORY',)        # 旧版の目印 (見つけたら日本語の見出しに置き換える)
UNRELEASED = ('未発行', 'unreleased')
H_RE = re.compile(r'^##\s+(rev\d+\.\d+)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*$')
S_RE = re.compile(r'^(?:要約|summary)\s*[:：]\s*(.+)$', re.I)


def parse_changelog(path):
    revs, cur = [], None
    for ln in open(path, encoding='utf-8'):
        ln = ln.rstrip('\n')
        m = H_RE.match(ln)
        if m:
            cur = dict(rev=m.group(1), date=m.group(2), author=m.group(3), summary='')
            revs.append(cur)
            continue
        if ln.startswith('## '):
            cur = None
            continue
        if cur is not None and not cur['summary']:
            s = S_RE.match(ln.strip())
            if s:
                cur['summary'] = s.group(1).strip()
    errs = [f"{r['rev']}: 要約: 行が無い" for r in revs if not r['summary']]
    errs += [f"{r['rev']}: 日付は YYYY-MM-DD か 未発行 (unreleased)" for r in revs
             if r['date'] not in UNRELEASED and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['date'])]
    return revs, errs


def rev_key(r):
    a, b = r['rev'][3:].split('.')
    return (int(a), int(b))


def body(revs, include_unreleased=False):
    rs = sorted([r for r in revs if include_unreleased or r['date'] not in UNRELEASED], key=rev_key)
    lines = [HEAD, COLS]
    lines += [f"{r['rev']}  {r['date']}  {r['author']}  {r['summary']}" for r in rs]
    return '\n'.join(lines), (rs[-1] if rs else None)


def q(s):  # KiCad の文字列: \ と " をエスケープ、改行は \n
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def find_block(src, start):
    """src[start] == '(' のブロック終端 (含む) の index+1。文字列内の括弧は数えない"""
    depth, i, n, ins = 0, start, len(src), False
    while i < n:
        c = src[i]
        if ins:
            if c == '\\':
                i += 1
            elif c == '"':
                ins = False
        elif c == '"':
            ins = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError('括弧が閉じていない')


def find_revtext(src):
    heads = '|'.join(re.escape(h) for h in LEGACY_HEADS) + '|' + re.escape(HEAD) + r'\\n'
    hits = [m.start() for m in re.finditer(r'\(text\s+"(?:' + heads + ')', src)]
    return hits


def unq(s):
    return s.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')


def current_text(src):
    hits = find_revtext(src)
    if len(hits) != 1:
        return None, hits
    m = re.match(r'\(text\s+"((?:[^"\\]|\\.)*)"', src[hits[0]:])
    return unq(m.group(1)), hits


def tb_value(src, key):
    m = re.search(r'\(title_block.*?\(' + key + r'\s+"((?:[^"\\]|\\.)*)"\)', src, re.S)
    return m.group(1) if m else None


def root_uuid(src):  # 新規テキストの uuid を回路図ごとに固定 (再生成しても差分が出ない)
    m = re.search(r'\(uuid\s+"([^"]+)"', src)
    return m.group(1) if m else 'none'


def apply(src, text, latest, at, size):
    hits = find_revtext(src)
    if len(hits) > 1:
        raise ValueError(f'改版履歴のテキストが {len(hits)} 個ある (1 個にする)')
    if hits:
        s = hits[0]
        m = re.match(r'\(text\s+"(?:[^"\\]|\\.)*"', src[s:])
        src = src[:s] + '(text ' + q(text) + src[s + m.end():]
    else:
        blk = (f'\t(text {q(text)}\n\t\t(exclude_from_sim no)\n\t\t(at {at[0]} {at[1]} 0)\n'
               f'\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {size} {size})\n\t\t\t)\n\t\t\t(justify left top)\n\t\t)\n'
               f'\t\t(uuid "{uuid.uuid5(uuid.NAMESPACE_URL, "kicad-knowhow/revhist/" + root_uuid(src))}")\n\t)\n')
        m = re.search(r'\n\t\(sheet_instances', src) or re.search(r'\n\t\(embedded_fonts', src)
        pos = m.start() + 1 if m else src.rstrip().rfind(')')
        src = src[:pos] + blk + src[pos:]
    if latest:
        tb = re.search(r'\(title_block', src)
        if tb:
            e = find_block(src, tb.start())
            t = src[tb.start():e]
            for key, val in (('rev', latest['rev'][3:]), ('date', latest['date'])):
                if re.search(r'\(' + key + r'\s+"', t):
                    t = re.sub(r'\(' + key + r'\s+"(?:[^"\\]|\\.)*"\)', f'({key} {q(val)})', t, count=1)
                else:
                    t = t[:-1].rstrip() + f'\n\t\t({key} {q(val)})\n\t)'
            src = src[:tb.start()] + t + src[e:]
    return src


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('changelog')
    ap.add_argument('sch', nargs='?')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--print', action='store_true')
    ap.add_argument('--include-unreleased', action='store_true')
    ap.add_argument('--at', default='20,20')
    ap.add_argument('--size', default='1.27')
    a = ap.parse_args()
    revs, errs = parse_changelog(a.changelog)
    if errs or not revs:
        print('CHANGELOG 書式エラー:', *(errs or ['## revX.Y | date | author の見出しが無い']), sep='\n  ', file=sys.stderr)
        return 2
    text, latest = body(revs, a.include_unreleased)
    if a.print or not a.sch:
        print(text)
        return 0
    src = open(a.sch, encoding='utf-8').read()
    if a.check:
        cur, hits = current_text(src)
        ng = []
        if cur is None:
            ng.append(f'改版履歴のテキストが {len(hits)} 個 (1 個であること)')
        elif cur != text:
            ng.append('改版履歴の本文が CHANGELOG と違う')
        if latest and tb_value(src, 'rev') != latest['rev'][3:]:
            ng.append(f"title_block rev={tb_value(src, 'rev')!r} != {latest['rev'][3:]!r}")
        if latest and tb_value(src, 'date') != latest['date']:
            ng.append(f"title_block date={tb_value(src, 'date')!r} != {latest['date']!r}")
        print('revhist: OK' if not ng else 'revhist: NG\n  ' + '\n  '.join(ng))
        return 0 if not ng else 1
    at = tuple(float(v) for v in a.at.split(','))
    new = apply(src, text, latest, at, a.size)
    if new != src:
        shutil.copy2(a.sch, a.sch + '.revhist-bak')
        open(a.sch, 'w', encoding='utf-8').write(new)
        print(f'revhist: {a.sch} を更新 (latest={latest["rev"] if latest else None})')
    else:
        print('revhist: 変更なし')
    return 0


if __name__ == '__main__':
    sys.exit(main())
