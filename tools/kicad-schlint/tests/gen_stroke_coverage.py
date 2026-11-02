#!/usr/bin/env python3
"""KiCad ストロークフォント (既定フォント) が描ける文字を実測して stroke_coverage.json を作る。

方法: 候補の文字をすべて並べた回路図を kicad-cli で PDF にし、PDF の Type3 フォントの
グリフ手続き (CharProcs) を取り出す。'①' 等の欠字と同じ手続き (豆腐 □) になった文字を「描けない」、
送り幅 (Widths) が漢字の 0.75 倍未満の全角文字を「幅が狭く崩れる」と判定する。
KiCad を更新したら作り直すこと:  python3 tests/gen_stroke_coverage.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from schbuilder import Sch  # noqa: E402

RANGES = [(0x20, 0x7E), (0xA0, 0x2FFF), (0x3000, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
          (0xFE30, 0xFE4F), (0xFF00, 0xFFEF)]
EXTRA = [0x1F600, 0x1F44D, 0x1F50C, 0x2B740]


def chars():
    for a, b in RANGES:
        for c in range(a, b + 1):
            if 0xD800 <= c <= 0xDFFF or c in (0x22, 0x5C):
                continue
            ch = chr(c)
            if ch.isprintable():
                yield c
    yield from EXTRA


def main():
    cps = list(chars())
    tmp = tempfile.mkdtemp(prefix="strokecov-")
    s = Sch("cov", paper="A0", title="cov", date="-", rev="-", company="-")
    per = 120
    lines = [cps[i:i + per] for i in range(0, len(cps), per)]
    for i, ln in enumerate(lines):
        s.text("".join(chr(c) for c in ln), 15, 15 + (i % 380) * 2.0, size=1.27)
        # A0 は 841mm 高さ。足りない分は右にずらす
        if i % 380 == 379:
            pass
    # 380 行ごとに列を変える
    s.items = []
    for i, ln in enumerate(lines):
        col, row = divmod(i, 390)
        s.text("".join(chr(c) for c in ln), 15 + col * 380, 15 + row * 2.0, size=1.27)
    p = s.save(tmp)
    pdf = os.path.join(tmp, "cov.pdf")
    subprocess.run(["kicad-cli", "sch", "export", "pdf", "-o", pdf, p], check=True, capture_output=True)
    d = open(pdf, "rb").read()
    # xref のオフセットで各オブジェクトを切り出す (ストリーム内のバイナリに惑わされない)
    offs = sorted((m.start(), int(m.group(1))) for m in re.finditer(rb"(?:^|[\r\n])(\d+) 0 obj", d))
    objs = {}
    for (o, n), nxt in zip(offs, [x[0] for x in offs[1:]] + [len(d)]):
        body = d[o:nxt]
        objs[n] = body[body.index(b"obj") + 3:]

    def stream(o):
        v = objs[o]
        m = re.search(rb"/Length (\d+)( 0 R)?", v)
        n = int(m.group(1))
        if m.group(2):
            n = int(objs[n].split(b"endobj")[0].strip())
        i = v.index(b"stream") + len(b"stream")
        if v[i:i + 2] == b"\r\n":
            i += 2
        elif v[i:i + 1] == b"\n":
            i += 1
        raw = v[i:i + n]
        return zlib.decompress(raw) if b"FlateDecode" in v[:v.index(b"stream")] else raw

    glyph = {}  # cp -> (proc bytes, width)
    for k, v in objs.items():
        if b"/Subtype /Type3" not in v:
            continue
        cp_ref = int(re.search(rb"/CharProcs (\d+) 0 R", v).group(1))
        tu_ref = int(re.search(rb"/ToUnicode (\d+) 0 R", v).group(1))
        first = int(re.search(rb"/FirstChar (\d+)", v).group(1))
        widths = [float(x) for x in re.search(rb"/Widths \[([^\]]*)\]", v).group(1).split()]
        diffs = re.search(rb"/Differences \[([^\]]*)\]", v).group(1).split()
        code2name = {}
        cur = 0
        for t in diffs:
            if t.startswith(b"/"):
                code2name[cur] = t[1:].decode()
                cur += 1
            else:
                cur = int(t)
        procs = {m.group(1).decode(): int(m.group(2)) for m in re.finditer(rb"/(\S+) (\d+) 0 R", objs[cp_ref])}
        cmap = stream(tu_ref).decode("latin-1")
        code2uni = {}
        for blk in re.findall(r"beginbfchar(.*?)endbfchar", cmap, re.S):
            for a, b in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                code2uni[int(a, 16)] = int(b, 16) if len(b) <= 4 else int(b, 16)
        for blk in re.findall(r"beginbfrange(.*?)endbfrange", cmap, re.S):
            for a, b, c in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                for i, code in enumerate(range(int(a, 16), int(b, 16) + 1)):
                    code2uni[code] = int(c, 16) + i
        for code, uni in code2uni.items():
            if len(hex(uni)) > 6 and uni > 0x10FFFF:  # UTF-16 サロゲート
                hi, lo = uni >> 16, uni & 0xFFFF
                uni = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
            nm = code2name.get(code)
            if nm and nm in procs:
                w = widths[code - first] if 0 <= code - first < len(widths) else 0
                glyph[uni] = (stream(procs[nm]), w)
    tofu = glyph.get(0x2460, (None,))[0]  # ①
    kanji_w = glyph.get(0x96FB, (None, 1000))[1]  # 電
    missing, narrow = [], []
    for c in cps:
        g = glyph.get(c)
        if g is None:
            continue  # 空白など
        if g[0] == tofu:
            missing.append(c)
        elif (0x3040 <= c <= 0x30FF or 0xFF61 <= c <= 0xFF9F) and g[1] < 0.75 * kanji_w:
            narrow.append(c)

    def ranges(lst):
        out = []
        for c in sorted(lst):
            if out and out[-1][1] == c - 1:
                out[-1][1] = c
            else:
                out.append([c, c])
        return out

    ver = subprocess.run(["kicad-cli", "version"], capture_output=True, text=True).stdout.strip()
    res = dict(kicad_version=ver, tested_ranges=[[a, b] for a, b in RANGES] + [[c, c] for c in EXTRA],
               missing=ranges(missing), narrow=ranges(narrow), kanji_width=kanji_w,
               widths_sample={hex(c): glyph[c][1] for c in (0x41, 0x3042, 0x30C7, 0x30D5, 0xFF83, 0x96FB, 0xFF21) if c in glyph})
    out = os.path.join(os.path.dirname(HERE), "stroke_coverage.json")
    json.dump(res, open(out, "w"), indent=1)
    print(f"glyphs={len(glyph)} missing={len(missing)} narrow={len(narrow)} -> {out}")
    print(res["widths_sample"])


if __name__ == "__main__":
    main()
