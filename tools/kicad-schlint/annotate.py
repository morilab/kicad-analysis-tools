#!/usr/bin/env python3
"""annotate: 回路図 PDF を PNG にし、schlint の指摘位置に番号付きの丸を描く (目視確認用)。

使い方: annotate.py --pdf schematic.pdf --json schlint.json --outdir DIR [--dpi 150] [--min-severity warning]
出力: DIR/page-NN.png (そのまま)、DIR/page-NN-annotated.png (指摘入り)、DIR/annotations.txt (番号と内容)
PIL (python3-pil) が無い環境では annotated は作らず、素の PNG だけを作る。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

SEV = {"error": 0, "warning": 1, "info": 2}
COL = {"error": (220, 0, 0), "warning": (240, 140, 0), "info": (0, 90, 220)}
JA = {"error": "エラー", "warning": "警告", "info": "情報"}


def page_info(pdf):
    out = subprocess.run(["pdfinfo", "-f", "1", "-l", "9999", pdf], capture_output=True, text=True).stdout
    pages = []
    for m in re.finditer(r"Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts", out):
        pages.append((int(m.group(1)), float(m.group(2)) / 72 * 25.4, float(m.group(3)) / 72 * 25.4))
    if not pages:
        m = re.search(r"Pages:\s+(\d+)", out)
        s = re.search(r"Page size:\s+([\d.]+) x ([\d.]+) pts", out)
        n = int(m.group(1)) if m else 1
        pages = [(i + 1, float(s.group(1)) / 72 * 25.4, float(s.group(2)) / 72 * 25.4) for i in range(n)]
    return pages


def page_file(pdf, n):
    t = subprocess.run(["pdftotext", "-f", str(n), "-l", str(n), "-layout", pdf, "-"], capture_output=True, text=True).stdout
    m = re.search(r"File:\s*(.+?\.kicad_sch)", t)
    return os.path.basename(m.group(1).strip()) if m else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--min-severity", default="warning", choices=list(SEV))
    o = ap.parse_args(argv)
    os.makedirs(o.outdir, exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-r", str(o.dpi), o.pdf, os.path.join(o.outdir, "page")], check=True)
    pngs = sorted(glob.glob(os.path.join(o.outdir, "page-*.png")))
    pngs = [p for p in pngs if not p.endswith("-annotated.png")]
    doc = json.load(open(o.json, encoding="utf-8"))
    finds = [f for f in doc["findings"] if "x" in f and SEV[f["severity"]] <= SEV[o.min_severity]]
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("PIL が無いので指摘入り PNG は作りません (素の PNG のみ)", file=sys.stderr)
        return 0
    font = None
    for fp in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(fp):
            font = ImageFont.truetype(fp, max(12, o.dpi // 8))
            break
    lines = []
    infos = page_info(o.pdf)
    for (n, wmm, hmm), png in zip(infos, pngs):
        fname = page_file(o.pdf, n)
        im = Image.open(png).convert("RGB")
        sx, sy = im.width / wmm, im.height / hmm
        dr = ImageDraw.Draw(im)
        k = 0
        mine = [f for f in finds if fname and os.path.basename(f["sheet"]) == fname]
        mine.sort(key=lambda f: (SEV[f["severity"]], f["y"], f["x"]))
        for f in mine:
            k += 1
            x, y = f["x"] * sx, f["y"] * sy
            r = 3.0 * sx
            c = COL[f["severity"]]
            dr.ellipse([x - r, y - r, x + r, y + r], outline=c, width=max(2, o.dpi // 60))
            dr.text((x + r + 2, y - r - 2), str(k), fill=c, font=font)
            lines.append(f"page {n} ({fname}) #{k} [{JA[f['severity']]}] {f['code']} ({f['x']:.2f}, {f['y']:.2f}) {f['message']}")
        out = png[:-4] + "-annotated.png"
        im.save(out)
    with open(os.path.join(o.outdir, "annotations.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"PNG {len(pngs)} ページ、指摘 {len(lines)} 個を描画 → {o.outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
