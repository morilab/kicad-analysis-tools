#!/usr/bin/env python3
"""uuid_check — kreview P25_uuid_unique: 基板・回路図の uuid 重複と、フットプリント (path) と回路図シンボルの対応 (2026-10-16 作者 決定)。

  python3 uuid_check.py <project_dir | x.kicad_pcb> [--netlist netlist.xml] [--json out.json]

判定:
  FAIL: フットプリント本体の uuid 重複 / 回路図ファイル内のシンボル uuid 重複 /
        footprint の (path) が空・重複・回路図 (ネットリスト) に対応するシンボルなし
  WARN: 子要素 (パッド・文字・図形) だけの uuid 重複 / 本体以外 (配線・via・ゾーン・図形) の uuid 重複 /
        path の対応先と部品番号・値が食い違う
  例外: (attr ... board_only) のフットプリントは path 空でよい (info に出す)。
DRC は uuid で項目を引くので (除外・注釈画像・相互参照)、重複すると DRC の部品名が取り違えられる
(2026-10-16 usb-jtag-ft2232h で C23/C24/C25・H1〜H4 が同じ uuid、C25 の違反が C23 と表示された)。
KiCad の schematic parity は path で照合するので uuid 重複は検出しない。
"""
import argparse, collections, glob, json, os, re, sys
import xml.etree.ElementTree as ET

UUID = re.compile(r'\(uuid "?([0-9a-fA-F-]{8,})"?\)')


def top_blocks(s, depth=2):
    """s-expr のうち指定の深さ (ルート=1) の要素を (開始位置, 文字列) で返す。文字列中の括弧は無視"""
    out = []; d = 0; st = None; j = 0; n = len(s)
    while j < n:
        c = s[j]
        if c == '"':
            j += 1
            while j < n and s[j] != '"':
                if s[j] == '\\':
                    j += 1
                j += 1
        elif c == '(':
            d += 1
            if d == depth:
                st = j
        elif c == ')':
            if d == depth and st is not None:
                out.append((st, s[st:j + 1])); st = None
            d -= 1
        j += 1
    return out


def _head(blk):
    m = re.match(r'\((\w+)', blk); return m.group(1) if m else ''


def _own_uuid(blk):
    """要素自身の uuid (子要素の uuid ではなく、直下の (uuid ...))"""
    for _, c in top_blocks(blk, 2):
        if _head(c) == 'uuid':
            m = UUID.match(c); return m.group(1) if m else None
    return None


def board_uuids(pcb_text):
    """footprints: [dict(ref, value, uuid, path, board_only, child_uuids)]、others: [(種類, uuid)]"""
    fps = []; others = []
    for _, blk in top_blocks(pcb_text, 2):
        h = _head(blk)
        if h == 'footprint':
            ref = re.search(r'\(property "Reference" "((?:[^"\\]|\\.)*)"', blk)
            val = re.search(r'\(property "Value" "((?:[^"\\]|\\.)*)"', blk)
            p = re.search(r'\(path "([^"]*)"\)', blk)
            own = _own_uuid(blk)
            child = [u for u in UUID.findall(blk)]
            if own in child:
                child.remove(own)
            fps.append(dict(ref=ref.group(1) if ref else '?', value=val.group(1) if val else '', uuid=own,
                            path=p.group(1) if p else '', board_only=bool(re.search(r'\(attr[^)]*\bboard_only\b', blk)),
                            child_uuids=child))
        else:
            for u in UUID.findall(blk):
                others.append((h, u))
    return fps, others


def sch_symbol_dups(sch_files):
    """回路図ファイルごとのシンボル uuid 重複 {file: [(uuid, n)]}"""
    res = {}
    for f in sch_files:
        s = open(f, encoding='utf-8').read()
        us = [_own_uuid(b) for _, b in top_blocks(s, 2) if _head(b) == 'symbol']
        d = [(u, n) for u, n in collections.Counter(u for u in us if u).items() if n > 1]
        if d:
            res[os.path.basename(f)] = d
    return res


def netlist_paths(xml_path):
    """ネットリストの部品: path (sheetpath tstamps + tstamps) -> (ref, value)"""
    r = ET.parse(xml_path).getroot(); m = {}
    for c in r.find('components'):
        sp = c.find('sheetpath'); base = sp.get('tstamps') if sp is not None else '/'
        if not base.endswith('/'):
            base += '/'
        for t in (c.findtext('tstamps') or '').split():
            m[base + t] = (c.get('ref'), c.findtext('value') or '')
    return m


def check(pcb_path, sch_files=(), netlist_xml=None):
    s = open(pcb_path, encoding='utf-8').read()
    fps, others = board_uuids(s)
    R = dict(fp_dup=[], child_dup=[], other_dup=[], sch_dup={}, path_empty=[], path_dup=[], path_missing=[],
             mismatch=[], board_only=[], counts={})
    # 本体の重複
    by = collections.defaultdict(list)
    for f in fps:
        by[f['uuid']].append(f['ref'])
    R['fp_dup'] = [dict(uuid=u, refs=sorted(rs)) for u, rs in by.items() if u and len(rs) > 1]
    # 子要素: 部品をまたいで同じ uuid (同じ部品内の重複も数える)
    cby = collections.defaultdict(list)
    for f in fps:
        for u in f['child_uuids']:
            cby[u].append(f['ref'])
    fpu = set(by)
    R['child_dup'] = [dict(uuid=u, refs=sorted(set(rs)), n=len(rs)) for u, rs in cby.items() if len(rs) > 1 or u in fpu]
    # 本体以外 (配線・via・ゾーン・図形・グループなど) と、それらと部品の間の重複
    allfp = fpu | set(cby)
    oby = collections.defaultdict(list)
    for h, u in others:
        oby[u].append(h)
    R['other_dup'] = [dict(uuid=u, kinds=sorted(set(hs)), n=len(hs) + (1 if u in allfp else 0)) for u, hs in oby.items() if len(hs) > 1 or u in allfp]
    # 回路図
    R['sch_dup'] = sch_symbol_dups(sch_files)
    # path
    pby = collections.defaultdict(list)
    for f in fps:
        if not f['path']:
            (R['board_only'] if f['board_only'] else R['path_empty']).append(f['ref'])
        else:
            pby[f['path']].append(f['ref'])
    R['path_dup'] = [dict(path=p, refs=sorted(rs)) for p, rs in pby.items() if len(rs) > 1]
    if netlist_xml and os.path.exists(netlist_xml):
        nm = netlist_paths(netlist_xml)
        for f in fps:
            if not f['path']:
                continue
            if f['path'] not in nm:
                R['path_missing'].append(dict(ref=f['ref'], path=f['path']))
                continue
            sref, sval = nm[f['path']]
            if sref != f['ref'] or sval != f['value']:
                R['mismatch'].append(dict(ref=f['ref'], value=f['value'], sch_ref=sref, sch_value=sval))
        R['netlist'] = True
    else:
        R['netlist'] = False
    R['counts'] = dict(footprints=len(fps), uuids=len(UUID.findall(s)))
    fail = R['fp_dup'] or any(R['sch_dup'].values()) or R['path_empty'] or R['path_dup'] or R['path_missing']
    warn = R['child_dup'] or R['other_dup'] or R['mismatch']
    R['grade'] = 'fail' if fail else 'warn' if warn else 'pass'
    R['value'] = f"{len(R['fp_dup'])}/{len(R['child_dup'])}/{len(R['path_empty']) + len(R['path_dup']) + len(R['path_missing'])}"
    return R


def detail(R, lim=12):
    d = []
    for x in R['fp_dup'][:lim]:
        d.append(f"本体 uuid 重複 {x['uuid'][:8]}…: {', '.join(x['refs'])}")
    if R['child_dup']:
        refs = collections.Counter(r for x in R['child_dup'] for r in x['refs'])
        d.append(f"子要素 (パッド・文字・図形) の uuid 重複 {len(R['child_dup'])} 個: " + ', '.join(f'{r} {n}' for r, n in sorted(refs.items())))
    if R['other_dup']:
        k = collections.Counter(h for x in R['other_dup'] for h in x['kinds'])
        d.append(f"本体以外の uuid 重複 {len(R['other_dup'])} 個: " + ', '.join(f'{h} {n}' for h, n in k.most_common(6)))
    for f, xs in R['sch_dup'].items():
        d.append(f"回路図 {f}: シンボル uuid 重複 {len(xs)} 個 ({', '.join(u[:8] for u, _ in xs[:4])}…)")
    if R['path_empty']:
        d.append(f"path が空 (回路図と未対応): {', '.join(R['path_empty'][:lim])}")
    for x in R['path_dup'][:lim]:
        d.append(f"path 重複 {x['path']}: {', '.join(x['refs'])}")
    for x in R['path_missing'][:lim]:
        d.append(f"path の対応シンボルなし: {x['ref']} {x['path']}")
    for x in R['mismatch'][:lim]:
        d.append(f"回路図と食い違い: {x['ref']} {x['value']} ↔ 回路図 {x['sch_ref']} {x['sch_value']}")
    if R['board_only']:
        d.append(f"board_only (path なしで可): {', '.join(R['board_only'][:lim])}")
    if not R['netlist']:
        d.append('ネットリストなし: path の対応先・部品番号・値は未確認')
    d.append(f"フットプリント {R['counts']['footprints']} 個 / uuid {R['counts']['uuids']} 個")
    return d


def project_files(arg):
    if arg.endswith('.kicad_pcb'):
        pcb = arg; pdir = os.path.dirname(os.path.abspath(arg))
    else:
        pdir = arg; pros = glob.glob(os.path.join(pdir, '*.kicad_pro'))
        name = os.path.splitext(os.path.basename(pros[0]))[0] if pros else os.path.basename(os.path.abspath(pdir))
        pcb = os.path.join(pdir, name + '.kicad_pcb')
    return pcb, sorted(glob.glob(os.path.join(pdir, '*.kicad_sch')))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('target'); ap.add_argument('--netlist'); ap.add_argument('--json')
    a = ap.parse_args()
    pcb, schs = project_files(a.target)
    nl = a.netlist
    if not nl and schs:
        import tempfile, subprocess
        root = [f for f in schs if os.path.splitext(os.path.basename(f))[0] == os.path.splitext(os.path.basename(pcb))[0]]
        if root:
            nl = os.path.join(tempfile.mkdtemp(), 'netlist.xml')
            subprocess.run(['kicad-cli', 'sch', 'export', 'netlist', '--format', 'kicadxml', '-o', nl, root[0]], capture_output=True)
    R = check(pcb, schs, nl)
    print(f"P25_uuid_unique {R['grade']} {R['value']}")
    for x in detail(R):
        print(' ', x)
    if a.json:
        json.dump(R, open(a.json, 'w'), indent=1, ensure_ascii=False)
    return 0


if __name__ == '__main__':
    sys.exit(main())
