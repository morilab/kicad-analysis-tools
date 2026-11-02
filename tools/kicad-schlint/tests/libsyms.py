#!/usr/bin/env python3
"""テスト用の lib_symbols を、手元の KiCad 10 標準シンボルライブラリから抜き出す。
KiCad のライブラリ (CC-BY-SA 4.0) は本リポに同梱しないため、テストのたびに手元のインストールから読む。

探す場所 (先に見つかったもの): 環境変数 KICAD10_SYMBOL_DIR、$KICAD_SHARE/symbols、/usr/share/kicad/symbols
  python3 libsyms.py [出力ファイル]   抜き出した結果を表示 (またはファイルに書く)
"""
import os
import re
import sys

# テストの回路図で使うシンボル (ライブラリ名:シンボル名)
NEEDED = [
    "power:GND", "power:+5V", "power:+3V3", "power:-5V", "power:VCC", "power:PWR_FLAG",
    "Device:R", "Device:R_Small", "Device:C", "Device:C_Small", "Device:LED", "Device:Polyfuse",
    "Connector_Generic:Conn_01x02", "Connector_Generic:Conn_01x03",
    "Connector:USB_C_Receptacle_PowerOnly_6P", "Mechanical:MountingHole",
    "Regulator_Linear:LM7805_TO220", "Amplifier_Operational:LM2904",
]


def symbol_dir():
    cands = [os.environ.get("KICAD10_SYMBOL_DIR"),
             os.environ.get("KICAD_SHARE") and os.path.join(os.environ["KICAD_SHARE"], "symbols"),
             "/usr/share/kicad/symbols"]
    for d in cands:
        if d and os.path.isdir(d):
            return d
    sys.exit("KiCad 10 の標準シンボルライブラリが見つからない。KICAD10_SYMBOL_DIR を設定する "
             "(例: /usr/share/kicad/symbols)")


def _block(s, start):
    """s[start] の '(' から対応する ')' までを返す (文字列中の括弧は数えない)"""
    depth, j, instr = 0, start, False
    while True:
        ch = s[j]
        if instr:
            if ch == "\\":
                j += 1
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
        j += 1


def _read_symbol(d, lib, name):
    """KiCad 10 の <lib>.kicad_symdir/<name>.kicad_sym、なければ従来形式の <lib>.kicad_sym から 1 シンボルを読む"""
    for path in (os.path.join(d, f"{lib}.kicad_symdir", f"{name}.kicad_sym"), os.path.join(d, f"{lib}.kicad_sym")):
        if not os.path.exists(path):
            continue
        s = open(path, encoding="utf-8").read()
        m = re.search(r'\n\t\(symbol "' + re.escape(name) + r'"', s)
        if not m:
            continue
        b = _block(s, m.start() + 2)
        if "(extends " in b:
            sys.exit(f"{lib}:{name} は extends を使っている (未対応)")
        return "\t" + b.replace(f'(symbol "{name}"', f'(symbol "{lib}:{name}"', 1)
    sys.exit(f"シンボルが見つからない: {lib}:{name} ({d})")


def libsyms_text(names=NEEDED):
    d = symbol_dir()
    return "\n".join(_read_symbol(d, *n.split(":", 1)) for n in names) + "\n"


if __name__ == "__main__":
    t = libsyms_text()
    if len(sys.argv) > 1:
        open(sys.argv[1], "w", encoding="utf-8").write(t)
    else:
        sys.stdout.write(t)
