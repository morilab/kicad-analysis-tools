#!/usr/bin/env python3
"""わざと違反を入れた小さな回路図 (テスト用) を作る。
使い方: make_fixtures.py [出力ディレクトリ (既定: tests/fixtures)]
各フィクスチャは別ディレクトリ (= 別プロジェクト) に置く。期待する検査コードは CASES に書いてあり、run_tests.py が使う。"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schbuilder import Sch, _u, q  # noqa: E402

TB = dict(title="試験回路", date="2026-10-08", rev="A", company="test")
R = "Device:R"


def base(name, **kw):
    a = dict(TB, comments=("schlint test fixture",))
    a.update(kw)
    return Sch(name, **a)


def r_at(s, ref, x, y, **kw):
    """縦置きの抵抗。戻り値は (上ピン, 下ピン) の座標"""
    s.sym(R, ref, "10k", x, y, **kw)
    return (x, round(y - 3.81, 2)), (x, round(y + 3.81, 2))


def gnd(s, n, x, y, rot=0):
    s.sym("power:GND", f"#PWR{n:02d}", "GND", x, y, rot=rot)


def vcc(s, n, x, y, rot=0):
    s.sym("power:+5V", f"#PWR{n:02d}", "+5V", x, y, rot=rot)


def stray(s, x, y):
    s.items.append(f"\t(polyline (pts (xy {x} {y}) (xy {x} {y}))\n\t\t(stroke (width 0) (type default))\n\t\t(uuid {q(_u())})\n\t)\n")


CASES = {}


def case(name, expect, forbid=()):
    def deco(fn):
        CASES[name] = (fn, set(expect), set(forbid))
        return fn
    return deco


# ---------------------------------------------------------------- 違反なし (基準)
@case("clean", expect=[], forbid=["error", "warning"])
def f_clean():
    s = base("clean")
    # 入力コネクタ (左) → 7805 → 出力コネクタ (右)。電源は上、GND は下
    s.sym("Connector_Generic:Conn_01x02", "J1", "DC_IN", 50.8, 100.33, mirror="y", ref_at=(48.26, 95.25), val_at=(48.26, 107.95))
    s.sym("Device:C", "C1", "10u", 76.2, 106.68, ref_at=(78.74, 105.41), val_at=(78.74, 107.95))
    s.sym("Regulator_Linear:LM7805_TO220", "U1", "LM7805", 101.6, 100.33, ref_at=(97.79, 93.98), val_at=(97.79, 96.52))
    s.sym("Device:C", "C2", "10u", 127.0, 106.68, ref_at=(129.54, 105.41), val_at=(129.54, 107.95))
    s.sym(R, "R1", "1k", 152.4, 106.68, ref_at=(154.94, 105.41), val_at=(154.94, 107.95))
    s.sym("Device:LED", "D1", "GREEN", 152.4, 119.38, rot=90, ref_at=(154.94, 118.11), val_at=(154.94, 120.65), field_angle=90, field_justify="right")
    s.sym("Connector_Generic:Conn_01x03", "J2", "OUT", 190.5, 102.87, ref_at=(186.69, 96.52), val_at=(186.69, 110.49))
    vcc(s, 1, 134.62, 95.25)
    for i, (x, y) in enumerate([(60.96, 115.57), (76.2, 115.57), (101.6, 115.57), (127.0, 115.57), (152.4, 128.27), (180.34, 115.57)]):
        gnd(s, 10 + i, x, y)
    s.wire((55.88, 100.33), (93.98, 100.33))
    s.wire((76.2, 100.33), (76.2, 102.87))
    s.junction(76.2, 100.33)
    s.wire((55.88, 102.87), (60.96, 102.87), (60.96, 115.57))
    s.wire((76.2, 110.49), (76.2, 115.57))
    s.wire((101.6, 107.95), (101.6, 115.57))
    s.wire((109.22, 100.33), (185.42, 100.33))
    s.wire((127.0, 100.33), (127.0, 102.87))
    s.junction(127.0, 100.33)
    s.wire((127.0, 110.49), (127.0, 115.57))
    s.wire((134.62, 95.25), (134.62, 100.33))
    s.junction(134.62, 100.33)
    s.wire((152.4, 100.33), (152.4, 102.87))
    s.junction(152.4, 100.33)
    s.wire((152.4, 110.49), (152.4, 115.57))
    s.wire((152.4, 123.19), (152.4, 128.27))
    s.wire((185.42, 102.87), (180.34, 102.87), (180.34, 115.57))
    s.nc(185.42, 105.41)
    # 電源フラグ (入力ネットと GND)。ワイヤの途中に置くと未接続になるので、分岐 + 接続点で付ける
    s.sym("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 66.04, 95.25)
    s.wire((66.04, 95.25), (66.04, 100.33))
    s.junction(66.04, 100.33)
    s.sym("power:PWR_FLAG", "#FLG02", "PWR_FLAG", 68.58, 110.49)
    s.wire((60.96, 110.49), (68.58, 110.49))
    s.junction(60.96, 110.49)
    # ブロック枠と見出し
    for (x1, x2, t) in ((30.48, 87.63, "入力端子"), (88.9, 144.78, "安定化電源 5V"), (146.05, 203.2, "表示灯と出力")):
        s.rect(x1, 63.5, x2, 139.7)
        s.text(t, x1 + 2.54, 67.31, size=1.524)
    return s


# ---------------------------------------------------------------- グリッド
@case("grid", expect=["GRID_PIN", "GRID_WIRE", "GRID_LABEL", "GRID_OTHER"])
def f_grid():
    s = base("grid")
    r_at(s, "R1", 51.3, 50.8)
    s.wire((70.0, 50.8), (80.01, 50.8))
    s.label("A", 70.0, 50.8)
    s.label("A", 80.0, 50.8)
    s.junction(90.5, 60.0)
    return s


# ---------------------------------------------------------------- 配線
@case("wire_cross_symbol", expect=["WIRE_CROSSES_SYMBOL"])
def f_wcs():
    s = base("wire_cross_symbol")
    r_at(s, "R1", 50.8, 50.8)
    s.wire((40.64, 50.8), (60.96, 50.8))
    s.label("A", 40.64, 50.8)
    s.label("A", 60.96, 50.8)
    return s


@case("wire_cross_pin", expect=["WIRE_CROSSES_PIN"])
def f_wcp():
    s = base("wire_cross_pin")
    s.sym("Regulator_Linear:LM7805_TO220", "U1", "LM7805", 101.6, 101.6)
    s.wire((95.25, 95.25), (95.25, 107.95))
    s.label("A", 95.25, 95.25)
    s.label("A", 95.25, 107.95)
    return s


@case("junction_missing", expect=["JUNCTION_MISSING"])
def f_jm():
    s = base("junction_missing")
    a, b = r_at(s, "R1", 30.48, 50.8)
    s.wire(b, (30.48, 60.96), (60.96, 60.96))
    r_at(s, "R2", 60.96, 64.77)  # 上ピン 60.96
    s.wire((45.72, 60.96), (45.72, 71.12))  # T (接続点なし)
    s.label("X", 45.72, 71.12)
    s.label("X", 30.48, 46.99)
    return s


@case("junction_dot_missing", expect=["JUNCTION_DOT_MISSING"])
def f_jdm():
    s = base("junction_dot_missing")
    s.wire((40.64, 50.8), (50.8, 50.8))
    s.wire((50.8, 50.8), (60.96, 50.8))
    s.wire((50.8, 50.8), (50.8, 60.96))
    s.label("A", 40.64, 50.8)
    s.label("A", 60.96, 50.8)
    s.label("A", 50.8, 60.96)
    return s


@case("pin_on_wire", expect=["PIN_ON_WIRE_MIDDLE"])
def f_pow():
    s = base("pin_on_wire")
    a, b = r_at(s, "R1", 50.8, 50.8)
    s.wire((40.64, a[1]), (60.96, a[1]))
    s.label("A", 40.64, a[1])
    s.label("A", 60.96, a[1])
    s.label("B", *b)
    s.label("B", 70.0 - 0.15, 60.96)
    return s


@case("junction_unneeded", expect=["JUNCTION_UNNEEDED"])
def f_ju():
    s = base("junction_unneeded")
    s.wire((40.64, 50.8), (50.8, 50.8), (50.8, 60.96))
    s.junction(50.8, 50.8)
    s.label("A", 40.64, 50.8)
    s.label("A", 50.8, 60.96)
    return s


@case("junction_4way", expect=["JUNCTION_4WAY"])
def f_j4():
    s = base("junction_4way")
    s.wire((40.64, 50.8), (60.96, 50.8))
    s.wire((50.8, 40.64), (50.8, 60.96))
    s.junction(50.8, 50.8)
    for p in ((40.64, 50.8), (60.96, 50.8), (50.8, 40.64), (50.8, 60.96)):
        s.label("A", *p)
    return s


@case("wire_crossing", expect=["WIRE_CROSSING"])
def f_wc():
    s = base("wire_crossing")
    s.wire((40.64, 50.8), (60.96, 50.8))
    s.wire((50.8, 40.64), (50.8, 60.96))
    for p, n in (((40.64, 50.8), "A"), ((60.96, 50.8), "A"), ((50.8, 40.64), "B"), ((50.8, 60.96), "B")):
        s.label(n, *p)
    return s


@case("wire_overlap", expect=["WIRE_OVERLAP"])
def f_wo():
    s = base("wire_overlap")
    s.wire((40.64, 50.8), (55.88, 50.8))
    s.wire((50.8, 50.8), (60.96, 50.8))
    s.label("A", 40.64, 50.8)
    s.label("A", 60.96, 50.8)
    return s


@case("wire_dangling", expect=["WIRE_DANGLING"])
def f_wd():
    s = base("wire_dangling")
    a, b = r_at(s, "R1", 50.8, 50.8)
    s.wire(a, (50.8, 40.64))
    s.nc(*b)
    return s


@case("wire_stub", expect=["WIRE_STUB"])
def f_ws():
    s = base("wire_stub")
    a, b = r_at(s, "R1", 50.8, 50.8)
    s.wire(a, (50.8, 38.1))
    s.label("SIG", 50.8, 41.91)
    r_at(s, "R2", 76.2, 50.8)
    s.label("SIG", 76.2, 46.99)
    s.nc(*b)
    s.nc(76.2, 54.61)
    return s


@case("wire_isolated", expect=["WIRE_ISOLATED"])
def f_wi():
    s = base("wire_isolated")
    s.wire((101.6, 101.6), (111.76, 101.6))
    return s


@case("stray_graphic", expect=["STRAY_GRAPHIC"])
def f_sg():
    s = base("stray_graphic")
    r_at(s, "R1", 50.8, 50.8)
    stray(s, 80.01, 80.01)
    return s


# ---------------------------------------------------------------- ラベル
@case("label_unconnected", expect=["LABEL_UNCONNECTED"])
def f_lu():
    s = base("label_unconnected")
    s.label("FLOAT", 50.8, 50.8)
    s.label("FLOAT", 60.96, 50.8)
    return s


@case("label_only", expect=["LABEL_ONLY_RATIO", "LABEL_ONLY_PIN"])
def f_lo():
    s = base("label_only")
    for i in range(4):
        a, b = r_at(s, f"R{i + 1}", 50.8 + 12.7 * i, 50.8)
        s.label(f"N{i}", *a)
        s.label(f"N{(i + 1) % 4}", *b)
    return s


@case("label_single_use", expect=["LABEL_SINGLE_USE"])
def f_lsu():
    s = base("label_single_use")
    a, b = r_at(s, "R1", 50.8, 50.8)
    s.wire(a, (50.8, 43.18))
    s.label("TYPO_NAME", 50.8, 43.18)
    s.nc(*b)
    return s


# ---------------------------------------------------------------- 電源記号
@case("power_orientation", expect=["POWER_ORIENTATION"])
def f_po():
    s = base("power_orientation")
    a, b = r_at(s, "R1", 50.8, 50.8)
    vcc(s, 1, 50.8, 46.99, rot=180)  # 下向きの電源
    gnd(s, 2, 50.8, 54.61, rot=180)  # 上向きの GND
    return s


# ---------------------------------------------------------------- 用紙・タイトル欄
@case("title_empty", expect=["TITLE_EMPTY"])
def f_te():
    s = Sch("title_empty")
    r_at(s, "R1", 50.8, 50.8)
    return s


@case("paper_low", expect=["PAPER_OCCUPANCY_LOW"])
def f_pl():
    s = base("paper_low", paper="A3")
    a, b = r_at(s, "R1", 101.6, 101.6)
    vcc(s, 1, *a)
    gnd(s, 2, *b)
    return s


@case("paper_overflow", expect=["PAPER_OVERFLOW"])
def f_pov():
    s = base("paper_overflow")
    r_at(s, "R1", 50.8, 50.8)
    r_at(s, "R2", 320.04, 50.8)
    return s


# ---------------------------------------------------------------- 文字
@case("text", expect=["TEXT_HALFWIDTH_KANA", "TEXT_KANA_STROKE", "TEXT_MISSING_GLYPH", "TEXT_CONTROL", "TEXT_FONT_FACE", "TEXT_SMALL", "TEXT_UNTESTED_CHAR", "TEXT_OVERLAP"])
def f_text():
    s = base("text")
    s.text("ﾃｽﾄ 回路", 50.8, 50.8)
    s.text("電源 フィルタ", 50.8, 55.88)
    s.text("注意 ① 10㎜", 50.8, 60.96)
    s.text("A\tB", 50.8, 66.04)
    s.text("見出し", 50.8, 71.12, face="Noto Sans CJK JP")
    s.text("small", 50.8, 76.2, size=0.8)
    s.text("전원", 50.8, 81.28)
    s.rect(101.6, 38.1, 116.84, 55.88)
    s.text("枠より長い見出しの文字列", 102.87, 40.64)
    return s


@case("paper_dense", expect=["PAPER_DENSE"])
def f_pd():
    s = base("paper_dense", paper="A5")
    for i in range(15):
        for j in range(9):
            a, b = r_at(s, f"R{i * 9 + j + 1}", 15.24 + 12.7 * i, 17.78 + 12.7 * j)
            s.nc(*a); s.nc(*b)
    return s


# ---------------------------------------------------------------- 信号の流れ
@case("flow_io", expect=["FLOW_IO_REVERSED"])
def f_fio():
    s = base("flow_io")
    s.sym("Amplifier_Operational:LM2904", "U1", "LM2904", 101.6, 101.6, mirror="y")
    for x, y in ((93.98, 101.6), (109.22, 99.06), (109.22, 104.14)):
        s.nc(x, y)
    return s


@case("flow_source", expect=["FLOW_SOURCE_RIGHT"])
def f_fs():
    s = base("flow_source")
    a, b = r_at(s, "R1", 50.8, 101.6)
    s.sym("Connector_Generic:Conn_01x02", "J1", "PWR_IN", 203.2, 101.6)
    s.wire(a, (50.8, 93.98), (190.5, 93.98), (190.5, 101.6), (198.12, 101.6))
    s.wire(b, (50.8, 109.22), (190.5, 109.22), (190.5, 104.14), (198.12, 104.14))
    return s


@case("hier", expect=["FLOW_HLABEL_SIDE"])
def f_hier():
    top = base("hier")
    r_at(top, "R1", 50.8, 101.6)
    su = top.sheet("Child", "child.kicad_sch", 101.6, 88.9, 25.4, 25.4,
                   pins=[("IN", "input", 127.0, 96.52, 0), ("OUT", "output", 101.6, 106.68, 180)])
    top.wire((50.8, 97.79), (50.8, 96.52), (127.0 + 7.62, 96.52))
    top.label("TOPNET", 134.62, 96.52)
    top.label("TOPNET", 50.8, 105.41)
    top.nc(101.6, 106.68)
    ch = base("child")
    ch.root_uuid = top.uuid
    ch.path = f"/{top.uuid}/{su}"
    ch.project = "hier"
    a, b = r_at(ch, "R2", 101.6, 101.6, )
    ch.label("IN", a[0], a[1], kind="hierarchical_label", shape="input", angle=90)
    ch.label("OUT", b[0], b[1], kind="hierarchical_label", shape="output", angle=270)
    return [top, ch]


# ---------------------------------------------------------------- 部品番号・フィールド
@case("refs", expect=["REF_UNANNOTATED", "REF_DUPLICATE", "REF_MISMATCH"])
def f_refs():
    s = base("refs")
    for i, (ref, inst) in enumerate((("R?", None), ("R1", None), ("R1", None), ("R5", "R6"))):
        a, b = r_at(s, ref, 50.8 + 12.7 * i, 50.8, inst_ref=inst)
        s.nc(*a)
        s.nc(*b)
    return s


@case("fields", expect=["FIELD_HIDDEN", "FIELD_FAR", "FIELD_OVERLAP", "FIELD_VERTICAL"])
def f_fields():
    s = base("fields")
    a, b = r_at(s, "R1", 50.8, 50.8, fields_hidden=True)
    s.nc(*a); s.nc(*b)
    a, b = r_at(s, "R2", 76.2, 50.8, ref_at=(101.6, 50.8))
    s.nc(*a); s.nc(*b)
    s.sym("Device:LED", "D1", "RED", 76.2, 76.2, rot=90)  # 回転したのにフィールドが 0 度 → 縦書き
    s.nc(76.2, 72.39); s.nc(76.2, 80.01)
    a, b = r_at(s, "R3", 50.8, 76.2, ref_at=(45.72, 88.9))
    s.wire((40.64, 88.9), (60.96, 88.9))
    s.label("A", 40.64, 88.9)
    s.label("A", 60.96, 88.9)
    s.nc(*a); s.nc(*b)
    return s


# ---------------------------------------------------------------- ブロック
@case("blocks", expect=["BLOCK_FRAME_NO_TEXT", "BLOCK_SYMBOL_STRADDLE"])
def f_blocks():
    s = base("blocks")
    for i in range(3):
        a, b = r_at(s, f"R{i + 1}", 50.8 + 12.7 * i, 50.8)
        s.nc(*a); s.nc(*b)
    s.rect(40.64, 38.1, 88.9, 63.5)
    a, b = r_at(s, "R9", 88.9, 50.8)  # 枠線をまたぐ
    s.nc(*a); s.nc(*b)
    return s


@case("no_frame", expect=["BLOCK_NO_FRAME"])
def f_nf():
    s = base("no_frame")
    for i in range(6):
        a, b = r_at(s, f"R{i + 1}", 50.8 + 12.7 * i, 50.8)
        s.nc(*a); s.nc(*b)
    return s


def build(outdir):
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    made = {}
    for name, (fn, exp, forbid) in CASES.items():
        d = os.path.join(outdir, name)
        r = fn()
        sheets = r if isinstance(r, list) else [r]
        paths = [x.save(d) for x in sheets]
        made[name] = paths[0]
    # 読み込めないファイル
    d = os.path.join(outdir, "parse_error")
    os.makedirs(d)
    open(os.path.join(d, "parse_error.kicad_sch"), "w").write("(kicad_sch (version 20260306) (paper \"A4\"\n")
    made["parse_error"] = os.path.join(d, "parse_error.kicad_sch")
    return made


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    for k, v in build(out).items():
        print(k, v)
