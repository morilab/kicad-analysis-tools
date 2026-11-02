#!/usr/bin/env python3
"""schlint: KiCad 回路図 (.kicad_sch) の作図ルール検査 (人が読みやすい図面のための lint)

使い方:
  schlint.py TOP.kicad_sch [--json out.json] [--format text|json] [--grid 1.27]
             [--disable CODE,...] [--verify-netlist] [--strict]

終了コード: 0 = error なし / 1 = error あり (--strict なら warning でも 1) / 2 = 読み込み失敗
標準ライブラリだけで動く (kicad-cli は --verify-netlist のときだけ使う)。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kisch  # noqa: E402
from kisch import Q, qi, qp  # noqa: E402

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))

SEV_ORDER = {"error": 0, "warning": 1, "info": 2}
SEV_JA = {"error": "エラー", "warning": "警告", "info": "情報"}

# 検査コードの一覧 (README にも記載)
CODES = {
    "GRID_PIN": "ピンの接続点がグリッドからずれている",
    "GRID_SYMBOL": "シンボル原点がグリッドからずれている (ピンは乗っている)",
    "GRID_WIRE": "ワイヤ端点がグリッドからずれている",
    "GRID_LABEL": "ラベルがグリッドからずれている",
    "GRID_OTHER": "接続点・非接続フラグ・シートピンがグリッドからずれている",
    "WIRE_CROSSES_SYMBOL": "ワイヤがシンボル本体を横切る",
    "WIRE_CROSSES_PIN": "ワイヤが他のピンを横切る/重なる",
    "JUNCTION_MISSING": "T 分岐に接続点がない (KiCad では未接続になる)",
    "JUNCTION_DOT_MISSING": "3 本以上が 1 点に集まるのに接続点の表示がない",
    "PIN_ON_WIRE_MIDDLE": "ピンがワイヤの途中に乗っているだけ (未接続)",
    "JUNCTION_UNNEEDED": "不要な接続点",
    "JUNCTION_4WAY": "4 方向の接続 (読み間違いやすい)",
    "WIRE_CROSSING": "ワイヤの交差 (非接続)",
    "WIRE_OVERLAP": "ワイヤが重なっている",
    "WIRE_DANGLING": "どこにもつながらないワイヤ端",
    "WIRE_STUB": "ラベル付きワイヤの先端が開いている (余ったワイヤ)",
    "WIRE_ISOLATED": "ピンにもラベルにもつながらないワイヤ",
    "STRAY_GRAPHIC": "長さ 0 の図形線など見えないゴミ",
    "LABEL_UNCONNECTED": "ワイヤにもピンにも付いていないラベル",
    "LABEL_ONLY_RATIO": "ラベルだけでつながるピンの比率が高い",
    "LABEL_ONLY_PIN": "ワイヤでなくラベルだけでつながっているピン",
    "LABEL_SINGLE_USE": "1 回しか使われないラベル",
    "POWER_ORIENTATION": "電源記号の向き (電源は上向き / GND は下向き)",
    "TITLE_EMPTY": "タイトル欄の空欄",
    "PAPER_OCCUPANCY_LOW": "用紙に対して図が小さすぎる",
    "PAPER_DENSE": "用紙に対して図が詰まりすぎている",
    "PAPER_OVERFLOW": "図が用紙・図枠・タイトル欄にはみ出している",
    "TEXT_HALFWIDTH_KANA": "半角カナ",
    "TEXT_KANA_STROKE": "既定フォントでカナが細く崩れて描画される",
    "TEXT_MISSING_GLYPH": "既定フォントに無い文字 (□ になる)",
    "TEXT_UNTESTED_CHAR": "描画を未確認の文字",
    "TEXT_CONTROL": "タブ等の制御文字 (タブは表の位置揃えなら可)",
    "TEXT_FONT_FACE": "外部フォント指定 (環境依存)",
    "TEXT_SMALL": "文字が小さい",
    "TEXT_OVERLAP": "注記テキストがワイヤ・枠線・記号に重なる/枠からはみ出す",
    "FLOW_IO_REVERSED": "入力ピンが出力ピンより右にある (信号が右→左)",
    "FLOW_SOURCE_RIGHT": "電源・入力コネクタが図の右側にある",
    "FLOW_HLABEL_SIDE": "階層ラベル/シートピンの入出力の向きと左右位置が逆",
    "REF_UNANNOTATED": "部品番号が未確定 (R? など)",
    "REF_MISMATCH": "Reference と instances の部品番号が食い違う",
    "REF_DUPLICATE": "部品番号の重複",
    "FIELD_HIDDEN": "部品番号/定数が非表示",
    "FIELD_FAR": "部品番号/定数がシンボルから離れている",
    "FIELD_OVERLAP": "部品番号/定数の文字がワイヤ・シンボル・他の文字に重なる",
    "FIELD_VERTICAL": "部品番号/定数が縦向きに表示される",
    "BLOCK_NO_FRAME": "機能ブロックの枠・注記がない",
    "BLOCK_FRAME_NO_TEXT": "説明のないブロック枠",
    "BLOCK_SYMBOL_STRADDLE": "シンボルがブロック枠の線をまたいでいる",
    "SCHEMATIC_PARSE": "回路図の読み込み",
}


class Report:
    def __init__(self, disabled=()):
        self.items = []
        self.disabled = set(disabled)
        self.stats = {}

    def add(self, sev, code, sheet, msg, x=None, y=None, hint=None, **extra):
        if code in self.disabled:
            return
        d = dict(severity=sev, code=code, sheet=sheet, message=msg)
        if x is not None:
            d["x"] = round(x, 3)
            d["y"] = round(y, 3)
        if hint:
            d["hint"] = hint
        d.update(extra)
        self.items.append(d)

    def count(self):
        c = {"error": 0, "warning": 0, "info": 0}
        for i in self.items:
            c[i["severity"]] += 1
        return c


# ---------------------------------------------------------------- 文字
_cov = None


def coverage():
    global _cov
    if _cov is None:
        p = os.path.join(HERE, "stroke_coverage.json")
        try:
            d = json.load(open(p))
        except OSError:
            d = dict(missing=[], narrow=[], tested_ranges=[[0, 0x10FFFF]])
        _cov = d
    return _cov


def _in(ranges, c):
    for a, b in ranges:
        if a <= c <= b:
            return True
    return False


def classify_text(text, face):
    """問題のある文字を分類して {kind: [chars]} を返す"""
    cov = coverage()
    out = {}
    for ch in str(text):
        c = ord(ch)
        if ch in "\n":
            continue
        if ch == "\t" or (unicodedata.category(ch) == "Cc"):
            out.setdefault("control", []).append(ch)
            continue
        if 0xFF61 <= c <= 0xFF9F:
            out.setdefault("halfkana", []).append(ch)
            continue
        if face:
            continue  # 外部フォントは別途 info
        if c < 0x80:
            continue
        if _in(cov.get("missing", []), c):
            out.setdefault("missing", []).append(ch)
        elif 0x30A0 <= c <= 0x30FF and _in(cov.get("narrow", []), c):
            out.setdefault("katakana", []).append(ch)
        elif 0x3040 <= c <= 0x309F and _in(cov.get("narrow", []), c):
            out.setdefault("hiragana", []).append(ch)
        elif not _in(cov.get("tested_ranges", []), c):
            out.setdefault("untested", []).append(ch)
    return out


def uniq(chs):
    seen = []
    for c in chs:
        if c not in seen:
            seen.append(c)
    return "".join(seen)


# ---------------------------------------------------------------- 補助
def on_grid(v, g):
    n = round(v / g)
    return abs(v - n * g) < 0.002


def bbox_union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def shrink(b, d):
    return (b[0] + d, b[1] + d, b[2] - d, b[3] - d)


def rect_overlap(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def rect_area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def inter(a, b):
    return (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))


def fmt_xy(x, y):
    return f"({x:.2f}, {y:.2f})"


def _on_any_seg(p, segs):
    """量子化座標 p がいずれかの線分 (端点含む) 上にあるか"""
    for a, b in segs:
        qa, qb = qp(a), qp(b)
        if p == qa or p == qb or kisch.on_segment_interior(p, qa, qb):
            return True
    return False


def symdesc(s):
    ref = s.instances[0][1] if s.instances and not s.instances[0][1].endswith("?") else s.ref
    return f"{ref} ({s.lib_id.split(':')[-1]})"


def power_class(s):
    name = s.value or (s.lib.name.split(":")[-1] if s.lib else "")
    libn = s.lib.name.split(":")[-1] if s.lib else ""
    if "PWR_FLAG" in libn or "PWR_FLAG" in name:
        return None
    if kisch.is_ground_name(name) or kisch.is_ground_name(libn):
        return "gnd"
    if name.startswith("-") or re.match(r"(?i)^(VEE|VNEG|V-)", name):
        return "neg"
    return "pos"


def pin_count_at(conn, p):
    return len(conn.pin_pts.get(p, []))


# ---------------------------------------------------------------- 検査本体 (1 シートファイル)
def lint_sheet(sh, rep, opt, sheet_label, gctx):
    g = opt.grid
    conn = kisch.Connectivity(sh)
    S = sheet_label
    nonpower = [s for s in sh.symbols if not s.is_power]

    # ---- グリッド
    for s in sh.symbols:
        bad = [p for p in s.pins if not (on_grid(p.pt[0], g) and on_grid(p.pt[1], g))]
        if bad:
            p = bad[0]
            rep.add("warning", "GRID_PIN", S, f"{symdesc(s)} のピン {p.number} の接続点 {fmt_xy(*p.pt)} が {g} mm グリッドに乗っていない"
                    + (f" (他 {len(bad) - 1} 本)" if len(bad) > 1 else ""), p.pt[0], p.pt[1],
                    hint="シンボルを移動してグリッドに合わせる (配線もずれる)")
        elif not (on_grid(s.x, g) and on_grid(s.y, g)):
            rep.add("info", "GRID_SYMBOL", S, f"{symdesc(s)} の原点 {fmt_xy(s.x, s.y)} がグリッド外 (ピンはグリッド上)", s.x, s.y)
    offw = []
    for a, b in sh.wires:
        for p in (a, b):
            if not (on_grid(p[0], g) and on_grid(p[1], g)):
                offw.append(p)
    for p in sorted(set(offw)):
        rep.add("warning", "GRID_WIRE", S, f"ワイヤ端点 {fmt_xy(*p)} がグリッドに乗っていない", p[0], p[1])
    for L in sh.labels:
        if not (on_grid(L.x, g) and on_grid(L.y, g)):
            rep.add("warning", "GRID_LABEL", S, f"ラベル「{L.text}」{fmt_xy(L.x, L.y)} がグリッドに乗っていない", L.x, L.y)
    for kind, pts in (("接続点", sh.junctions), ("非接続フラグ", sh.no_connects),
                      ("シートピン", [sp.pt for S2 in sh.sheets for sp in S2.pins])):
        for p in pts:
            if not (on_grid(p[0], g) and on_grid(p[1], g)):
                rep.add("warning", "GRID_OTHER", S, f"{kind} {fmt_xy(*p)} がグリッドに乗っていない", p[0], p[1])

    # ---- ワイヤとシンボルの重なり
    for wi, (a, b) in enumerate(sh.wires):
        for s in sh.symbols:
            if s.body is None:
                continue
            body = shrink(s.body, 0.05)
            if body[2] <= body[0] or body[3] <= body[1]:
                continue
            L = kisch.seg_rect_intersect_len(a, b, body)
            if L > 0.3:
                # ワイヤがこのシンボルのピン接続点で終わり、その点が本体の中にある (長さ 0 ピン) 場合は除外
                own = [p for p in s.pins if qp(p.pt) in (qp(a), qp(b))]
                if own and s.is_power:
                    continue
                if own and all(kisch.seg_rect_intersect_len(p.pt, p.end, body) > 0 or p.pt == p.end for p in own) and L < 2.6:
                    continue
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                rep.add("error", "WIRE_CROSSES_SYMBOL", S, f"ワイヤ {fmt_xy(*a)}–{fmt_xy(*b)} が {symdesc(s)} の本体を横切っている ({L:.1f} mm)",
                        mx, my, hint="シンボルの外を回して配線する")
        # 他シンボルのピン (線分) を横切る/重なる
        for p in []:
            pass
    pin_segs = [(s, p) for s in sh.symbols for p in s.pins if p.pt != p.end and not p.hidden]
    wq = conn.W
    pin_q = [(s, p, qp(p.pt), qp(p.end)) for s, p in pin_segs]
    for wi, (a, b) in enumerate(sh.wires):
        qa, qb = wq[wi]
        wx0, wx1 = min(qa[0], qb[0]), max(qa[0], qb[0])
        wy0, wy1 = min(qa[1], qb[1]), max(qa[1], qb[1])
        for s, p, pa, pe in pin_q:
            if max(pa[0], pe[0]) < wx0 or min(pa[0], pe[0]) > wx1 or max(pa[1], pe[1]) < wy0 or min(pa[1], pe[1]) > wy1:
                continue
            # ピン線分の内部 (接続点以外) にワイヤが触れるか
            if _segs_touch_interior(qa, qb, pa, pe):
                rep.add("warning", "WIRE_CROSSES_PIN", S, f"ワイヤ {fmt_xy(*a)}–{fmt_xy(*b)} が {symdesc(s)} のピン {p.number} に重なる/横切る",
                        p.end[0], p.end[1], hint="ピンの先端 (接続点) でだけ接続する")
                break

    # ---- 接続点・T 分岐・交差
    jset = set(conn.junction_pts)
    ends = conn.ends
    for p, mids in conn.t_points.items():
        if p in jset:
            continue
        x, y = p[0] / Q, p[1] / Q
        rep.add("error", "JUNCTION_MISSING", S, f"T 分岐 {fmt_xy(x, y)} に接続点がない。KiCad ではこのワイヤは途中のワイヤに接続されない", x, y,
                hint="接続点 (junction) を置く")
    for pin, wi in conn.pin_mid_wire:
        x, y = pin.pt
        rep.add("error", "PIN_ON_WIRE_MIDDLE", S, f"{symdesc(pin.sym)} のピン {pin.number} がワイヤの途中 {fmt_xy(x, y)} に乗っているだけで未接続",
                x, y, hint="ワイヤをピンの位置で分割するか、ワイヤの端点にピンを置く (電源フラグで起きやすい)")
    # 次数 (接続点が必要か) の計算
    pts = set(ends) | jset | set(conn.pin_pts)
    for p in pts:
        nends = len(ends.get(p, []))
        nmid = len(conn.wires_through(p))
        npin = len(conn.pin_pts.get(p, []))
        deg = nends + 2 * nmid + npin
        x, y = p[0] / Q, p[1] / Q
        if p in jset:
            if nends + nmid == 0 and _on_any_seg(p, sh.buses):
                continue  # バス上の接続点 (バスの分岐) は対象外
            if nends + nmid == 0:
                rep.add("warning", "JUNCTION_UNNEEDED", S, f"接続点 {fmt_xy(x, y)} がどのワイヤにも乗っていない", x, y)
            elif deg <= 2:
                rep.add("warning", "JUNCTION_UNNEEDED", S, f"不要な接続点 {fmt_xy(x, y)} (分岐していない)", x, y)
            elif deg >= 4:
                rep.add("warning", "JUNCTION_4WAY", S, f"4 方向の接続 {fmt_xy(x, y)}。印刷や縮小で交差と見分けにくい", x, y,
                        hint="分岐位置を 1 グリッド以上ずらして T 字 2 つにする")
        else:
            if nmid == 0 and nends + npin >= 3 and nends >= 2:
                rep.add("warning", "JUNCTION_DOT_MISSING", S, f"{fmt_xy(x, y)} に {nends + npin} 本が集まるが接続点の表示がない", x, y,
                        hint="接続点を置く (KiCad 上は接続済みでも、紙の上で接続が分からない)")
    # 交差 (端点以外どうし)
    crossings = _crossings(conn)
    if crossings:
        for (x, y) in crossings:
            rep.add("info", "WIRE_CROSSING", S, f"ワイヤが交差 {fmt_xy(x, y)} (非接続)。交差は最小限に", x, y)
    # 重なり
    for (i, j) in _overlaps(conn):
        a, b = sh.wires[i]
        rep.add("warning", "WIRE_OVERLAP", S, f"ワイヤ {fmt_xy(*a)}–{fmt_xy(*b)} が別のワイヤと重なっている", (a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    # ---- ぶら下がり・孤立
    label_pts = set(qp(L.pt) for L in sh.labels)
    sheetpin_pts = set(qp(sp.pt) for S2 in sh.sheets for sp in S2.pins)
    label_roots = set(conn.uf.find(("l", li)) for li in range(len(sh.labels)))
    stub = []
    for p, ws in ends.items():
        if len(ws) >= 2 or p in conn.pin_pts or p in label_pts or p in jset or p in conn.nc_pts or p in sheetpin_pts \
                or p in conn.bus_entry_pts or p in conn.t_points:
            continue
        x, y = p[0] / Q, p[1] / Q
        r = conn.uf.find(("w", ws[0]))
        if r in label_roots:
            stub.append((x, y))
        else:
            rep.add("error", "WIRE_DANGLING", S, f"ワイヤ端 {fmt_xy(x, y)} がどこにもつながっていない", x, y,
                    hint="ピンの先端・他のワイヤの端点・接続点まで正しく伸ばすか、不要なら削除する")
    for (x, y) in stub:
        rep.add("warning", "WIRE_STUB", S, f"ラベル付きワイヤの先端 {fmt_xy(x, y)} が開いている (ラベルの先にワイヤが余っている。KiCad 既定の ERC では unconnected_wire_endpoint)", x, y,
                hint="ワイヤはラベルの位置で終える。同じシート内なら実際に配線する")
    uf = conn.uf
    comp = {}
    for i in range(len(sh.wires)):
        comp.setdefault(uf.find(("w", i)), []).append(i)
    has = set()
    for si, s in enumerate(sh.symbols):
        for pin in s.pins:
            has.add(uf.find(conn.pin_key(si, pin)))
    for li in range(len(sh.labels)):
        has.add(uf.find(("l", li)))
    for sj, S2 in enumerate(sh.sheets):
        for pi in range(len(S2.pins)):
            has.add(uf.find(("s", sj, pi)))
    for r, ws in comp.items():
        if r in has:
            continue
        if any(qp(sh.wires[i][0]) in conn.bus_entry_pts or qp(sh.wires[i][1]) in conn.bus_entry_pts for i in ws):
            continue
        a, b = sh.wires[ws[0]]
        rep.add("error", "WIRE_ISOLATED", S, f"ピンにもラベルにもつながらないワイヤ ({len(ws)} 本) {fmt_xy(*a)}–{fmt_xy(*b)}", a[0], a[1],
                hint="削除するか、正しく接続する")

    # ---- ラベル
    for li, L in enumerate(sh.labels):
        if L.kind not in ("label", "global_label", "hierarchical_label"):
            continue
        p = qp(L.pt)
        if not (ends.get(p) or conn.wires_through(p) or p in conn.pin_pts or p in sheetpin_pts):
            if not any(qp(a) == p or qp(b) == p or kisch.on_segment_interior(p, qp(a), qp(b)) for a, b in sh.buses):
                rep.add("error", "LABEL_UNCONNECTED", S, f"ラベル「{L.text}」{fmt_xy(L.x, L.y)} がワイヤにもピンにも付いていない", L.x, L.y)
    # ラベルだけでつながるピン
    wired = labelonly = 0
    lo_list = []
    local_names = {}
    for L in sh.labels:
        if L.kind == "label":
            local_names[kisch.net_name(L.text)] = local_names.get(kisch.net_name(L.text), 0) + 1
    glob_here = {}
    for L in sh.labels:
        if L.kind == "global_label":
            glob_here[L.text] = glob_here.get(L.text, 0) + 1
    # 各グループのつながり先の数 (ピン + シートピン + 階層/グローバルラベル)
    group_links = {}
    for sj, S2 in enumerate(sh.sheets):
        for pi in range(len(S2.pins)):
            r0 = uf.find(("s", sj, pi))
            group_links[r0] = group_links.get(r0, 0) + 1
    for li, L in enumerate(sh.labels):
        if L.kind in ("hierarchical_label", "global_label"):
            r0 = uf.find(("l", li))
            group_links[r0] = group_links.get(r0, 0) + 1
    group_pins = {}
    for si, s in enumerate(sh.symbols):
        for pin in s.pins:
            group_pins.setdefault(uf.find(conn.pin_key(si, pin)), []).append((si, pin))
    group_labels = {}
    for li, L in enumerate(sh.labels):
        group_labels.setdefault(uf.find(("l", li)), []).append(L)
    for si, s in enumerate(sh.symbols):
        if s.is_power:
            continue
        for pin in s.pins:
            if pin.hidden:
                continue
            p = qp(pin.pt)
            if p in conn.nc_pts:
                continue
            r = uf.find(conn.pin_key(si, pin))
            others = [(sj, q) for sj, q in group_pins.get(r, []) if not (sj == si and q is pin)]
            if any(not sh.symbols[sj].is_power for sj, q in others):
                wired += 1
                continue
            if any(sh.symbols[sj].is_power for sj, q in others):
                continue  # 電源記号でつながる (許容)
            labs = group_labels.get(r, [])
            # 同じシート内でワイヤの代わりに使われているラベル (ローカル、または同シートで 2 回以上出るグローバル)
            same_sheet = [L for L in labs if (L.kind == "label" and local_names.get(kisch.net_name(L.text), 0) >= 2)
                          or (L.kind == "global_label" and glob_here.get(L.text, 0) >= 2)]
            if same_sheet:
                labelonly += 1
                lo_list.append((s, pin, same_sheet[0]))
    total = wired + labelonly
    ratio = labelonly / total if total else 0.0
    rep.stats.setdefault("label_only", {})[S] = dict(wired=wired, label_only=labelonly, ratio=round(ratio, 3))
    if total and labelonly >= opt.label_min and ratio > opt.label_ratio:
        rep.add("warning", "LABEL_ONLY_RATIO", S,
                f"同じシート内でワイヤの代わりにラベルだけでつながるピンが {labelonly}/{total} ({ratio:.0%}) ある (しきい値 {opt.label_ratio:.0%})",
                hint="同じシート内の接続は実際の配線で描く。ラベルはシート間・遠距離・バスなど意味のあるところだけ")
    for s, pin, L in lo_list:
        rep.add("info", "LABEL_ONLY_PIN", S, f"{symdesc(s)} のピン {pin.number}{('(' + pin.name + ')') if pin.name and pin.name != '~' else ''} はラベル「{L.text}」だけでつながっている",
                pin.pt[0], pin.pt[1])
    # 1 回しか使われないラベル
    other_names = set(kisch.net_name(L.text) for L in sh.labels if L.kind in ("hierarchical_label", "global_label"))
    other_names |= set(s.value for s in sh.symbols if s.is_power)
    other_names |= set(p.name for s in sh.symbols for p in s.pins if p.hidden and p.etype == "power_in")
    for L in sh.labels:
        if L.kind in ("label", "global_label", "hierarchical_label"):
            for short, full in kisch.bus_members(kisch.net_name(L.text), gctx["aliases"]):
                other_names.add(full)
    nc_roots = set()
    for p in conn.nc_pts:
        for w in ends.get(p, []):
            nc_roots.add(uf.find(("w", w)))
        for si, pin in conn.pin_pts.get(p, []):
            nc_roots.add(uf.find(conn.pin_key(si, pin)))
    for li, L in enumerate(sh.labels):
        if L.kind == "label" and local_names.get(kisch.net_name(L.text), 0) == 1 and kisch.net_name(L.text) not in other_names:
            r = uf.find(("l", li))
            npins = len([1 for sj, q in group_pins.get(r, []) if not sh.symbols[sj].is_power]) + group_links.get(r, 0)
            if kisch.bus_members(L.text):
                continue
            if r in nc_roots:
                rep.add("info", "LABEL_SINGLE_USE", S, f"ローカルラベル「{L.text}」は未接続 (×) のピン/ワイヤに付いているだけ。不要なら削除", L.x, L.y)
                continue
            sev = "warning" if npins <= 1 else "info"
            rep.add(sev, "LABEL_SINGLE_USE", S,
                    f"ローカルラベル「{L.text}」はこのシートで 1 回しか使われていない" + ("。つながる相手がなく、綴り違いの可能性" if npins <= 1 else "。ネット名を付ける目的なら可 (意味のある名前か確認)"),
                    L.x, L.y)
        elif L.kind == "global_label" and gctx["global_count"].get(L.text, 0) == 1 :
            r = uf.find(("l", li))
            npins = len([1 for sj, q in group_pins.get(r, []) if not sh.symbols[sj].is_power]) + group_links.get(r, 0) - 1
            if npins >= 2:
                rep.add("info", "LABEL_SINGLE_USE", S, f"グローバルラベル「{L.text}」が階層全体で 1 回しか使われていない (ネット名を付ける目的ならローカルラベルで足りる)", L.x, L.y)
            else:
                rep.add("warning", "LABEL_SINGLE_USE", S, f"グローバルラベル「{L.text}」が階層全体で 1 回しか使われていない。つながる相手がなく、綴り違いの可能性", L.x, L.y)

    # ---- 電源記号の向き
    for s in sh.symbols:
        if not s.is_power or not s.pins or s.body is None:
            continue
        cls = power_class(s)
        if cls is None:
            continue
        p = s.pins[0].pt
        cx, cy = (s.body[0] + s.body[2]) / 2, (s.body[1] + s.body[3]) / 2
        dx, dy = cx - p[0], cy - p[1]
        if abs(dx) < 0.01 and abs(dy) < 0.01:
            continue
        d = ("上" if dy < 0 else "下") if abs(dy) >= abs(dx) else ("右" if dx > 0 else "左")
        want = {"pos": "上", "gnd": "下", "neg": "下"}[cls]
        if d != want:
            sev = "info" if cls == "neg" else "warning"
            nm = {"pos": "電源", "gnd": "GND", "neg": "負電源"}[cls]
            rep.add(sev, "POWER_ORIENTATION", S, f"{nm}記号「{s.value}」{fmt_xy(s.x, s.y)} が{d}向き ({want}向きにする)", s.x, s.y,
                    hint="電源は記号を上向きにして回路の上側に、GND は下向きにして下側に置く")

    # ---- タイトル欄
    tb = sh.title_block
    for k, ja in (("title", "表題 (title)"), ("date", "日付 (date)"), ("rev", "版数 (rev)"), ("company", "会社/作成者 (company)")):
        if not tb.get(k, "").strip():
            rep.add("warning", "TITLE_EMPTY", S, f"タイトル欄の {ja} が空", hint="ページ設定 (Page Settings) で入力する。階層シートはシートごとに入力")
    if not any(tb.get(f"comment{i}", "").strip() for i in range(1, 10)):
        rep.add("info", "TITLE_EMPTY", S, "タイトル欄のコメント欄がすべて空 (図面の説明・設計者・確認者などを書くとよい)")

    # ---- 用紙と占有率
    pname, W, H = sh.paper
    boxes = []
    for s in sh.symbols:
        boxes.append(s.pinbox or s.body)
        for fname, f in s.props.items():
            if f["hidden"] or not f["value"] or fname not in ("Reference", "Value"):
                continue
            boxes.append(_field_bbox(s, f))
    for a, b in sh.wires + sh.buses:
        boxes.append((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
    for L in sh.labels:
        boxes.append(kisch.text_bbox(L.text, L.x, L.y, L.angle, L.size, [], L.kind))
    for t in sh.texts:
        boxes.append(t["bbox"])
    boxes += sh.rects
    for pl in sh.polylines:
        if pl:
            b = (min(p[0] for p in pl), min(p[1] for p in pl), max(p[0] for p in pl), max(p[1] for p in pl))
            if b[2] - b[0] < 0.01 and b[3] - b[1] < 0.01:
                rep.add("info", "STRAY_GRAPHIC", S, f"長さ 0 の図形線 {fmt_xy(b[0], b[1])} (見えないゴミ。削除する)", b[0], b[1])
                continue
            boxes.append(b)
    for S2 in sh.sheets:
        boxes.append((S2.x, S2.y, S2.x + S2.w, S2.y + S2.h))
    bb = bbox_union(boxes)
    frame = (12.0, 12.0, W - 12.0, H - 12.0)
    tblock = (W - 10 - 110, H - 10 - 34, W - 12, H - 12)
    if bb:
        cw, ch = bb[2] - bb[0], bb[3] - bb[1]
        fw, fh = frame[2] - frame[0], frame[3] - frame[1]
        occ = rect_area(bb) / (fw * fh)
        rep.stats.setdefault("occupancy", {})[S] = dict(paper=pname, content_bbox=[round(v, 2) for v in bb],
                                                        width_ratio=round(cw / fw, 3), height_ratio=round(ch / fh, 3), area_ratio=round(occ, 3))
        if bb[0] < 0 or bb[1] < 0 or bb[2] > W or bb[3] > H:
            outs = [b for b in boxes if b and (b[0] < 0 or b[1] < 0 or b[2] > W or b[3] > H)]
            ob = bbox_union(outs) or bb
            ox = min(max(ob[0], 0.0), W)
            oy = min(max(ob[1], 0.0), H)
            rep.add("error", "PAPER_OVERFLOW", S, f"図が用紙 {pname} ({W:g}×{H:g} mm) の外にはみ出している: {len(outs)} 個の要素、はみ出し部分の範囲 {fmt_xy(ob[0], ob[1])}–{fmt_xy(ob[2], ob[3])} (PDF/印刷に出ない)",
                    ox, oy, hint="用紙を大きくするか、シートを分割する。用紙外に置いた参考図やゴミも印刷されない")
        elif bb[0] < frame[0] - 0.5 or bb[1] < frame[1] - 0.5 or bb[2] > frame[2] + 0.5 or bb[3] > frame[3] + 0.5:
            rep.add("warning", "PAPER_OVERFLOW", S, f"図が図枠にかかっている (範囲 {fmt_xy(bb[0], bb[1])}–{fmt_xy(bb[2], bb[3])})", bb[0], bb[1])
        tb_hits = [b for b in boxes if b and rect_area(inter(b, shrink(tblock, 0.5))) > 0.5]
        if tb_hits:
            b = tb_hits[0]
            rep.add("warning", "PAPER_OVERFLOW", S, f"図の一部 ({len(tb_hits)} 個) がタイトル欄に重なっている ({fmt_xy(b[0], b[1])} 付近)", b[0], b[1])
        if occ < opt.occ_min:
            smaller = _smaller_paper(pname, W, H, cw, ch)
            rep.add("warning", "PAPER_OCCUPANCY_LOW", S,
                    f"用紙 {pname} の作図領域に対して図が小さい: 外接矩形 {cw:.0f}×{ch:.0f} mm、幅 {cw / fw:.0%}・高さ {ch / fh:.0%}・面積 {occ:.0%} (しきい値 {opt.occ_min:.0%})",
                    bb[0], bb[1], hint="部品間隔・ブロック間隔を広げて用紙全体を使う" + (f"、または用紙を {smaller} にする" if smaller else ""))
        elif occ > opt.occ_max:
            rep.add("info", "PAPER_DENSE", S, f"図が用紙 {pname} の作図領域の {occ:.0%} を占めている。詰まりすぎていないか確認 (大きい用紙かシート分割)", bb[0], bb[1])

    # ---- 文字
    def check_text(txt, face, size, where, x, y, min_size=1.27):
        if not txt:
            return
        cl = classify_text(txt, face)
        if "halfkana" in cl:
            rep.add("warning", "TEXT_HALFWIDTH_KANA", S, f"{where}「{txt}」に半角カナ「{uniq(cl['halfkana'])}」がある", x, y,
                    hint="全角にするか、英語・漢字で書く")
        if "katakana" in cl:
            rep.add("warning", "TEXT_KANA_STROKE", S,
                    f"{where}「{txt}」の全角カタカナ「{uniq(cl['katakana'])}」は KiCad 既定フォントでは幅が漢字の約 7 割で描かれ、半角カナのように崩れて見える",
                    x, y, hint="漢字・英語に言い換える、または (font (face ...)) で表示フォントを指定し (embedded_fonts yes) で埋め込む")
        if "hiragana" in cl:
            rep.add("info", "TEXT_KANA_STROKE", S, f"{where}「{txt}」のひらがなは既定フォントで細く描かれる (読めるが PDF で確認)", x, y)
        if "missing" in cl:
            rep.add("error", "TEXT_MISSING_GLYPH", S, f"{where}「{txt}」の文字「{uniq(cl['missing'])}」は KiCad 既定フォントに無く □ で表示される", x, y,
                    hint="①→(1)、㎜→mm、㈱→(株) のように置き換える")
        if "untested" in cl:
            rep.add("warning", "TEXT_UNTESTED_CHAR", S, f"{where}「{txt}」に描画未確認の文字「{uniq(cl['untested'])}」がある", x, y)
        if "control" in cl:
            rep.add("info" if set(cl["control"]) <= {"\t"} else "warning", "TEXT_CONTROL", S, f"{where}「{txt.encode('unicode_escape').decode()}」にタブ等の制御文字がある", x, y)
        if face:
            rep.add("info", "TEXT_FONT_FACE", S, f"{where}「{txt}」はフォント「{face}」を指定している" +
                    ("" if gctx["embedded_fonts"].get(sh.path) else "。フォントが埋め込まれていない (embedded_fonts no) ので、ない環境では置換される"), x, y)
        if size and size < min_size - 0.01:
            rep.add("warning", "TEXT_SMALL", S, f"{where}「{txt}」の文字サイズ {size:g} mm は小さい (目安 {min_size} mm 以上)", x, y)

    for t in sh.texts:
        check_text(t["text"], t["face"], t["size"], "テキスト" if t["kind"] == "text" else "テキストボックス", t["x"], t["y"])
    for L in sh.labels:
        check_text(L.text, L.face, L.size, {"label": "ラベル", "global_label": "グローバルラベル", "hierarchical_label": "階層ラベル"}.get(L.kind, "ラベル"), L.x, L.y)
    for s in sh.symbols:
        for fname, f in s.props.items():
            if f["hidden"] or s.is_power and fname != "Value":
                continue
            check_text(f["value"], f["face"], f["size"], f"{s.ref} の {fname}", f["x"], f["y"], min_size=1.0)
    for k, v in tb.items():
        check_text(v, None, None, f"タイトル欄 {k}", None, None)
    for S2 in sh.sheets:
        check_text(S2.name, None, None, "シート名", S2.x, S2.y)
        for sp in S2.pins:
            check_text(sp.name, None, None, "シートピン", sp.x, sp.y)

    # ---- 信号の流れ (左→右) のヒューリスティック
    for s in nonpower:
        ins = [p for p in s.pins if p.etype in ("input",) and abs(p.dir[0]) > 0.7 and not p.hidden]
        outs = [p for p in s.pins if p.etype in ("output", "tri_state") and abs(p.dir[0]) > 0.7 and not p.hidden]
        if not ins or not outs:
            ins = [p for p in s.pins if p.etype == "power_in" and abs(p.dir[0]) > 0.7 and not p.hidden]
            outs = [p for p in s.pins if p.etype == "power_out" and abs(p.dir[0]) > 0.7 and not p.hidden]
        if ins and outs:
            xi = sum(p.pt[0] for p in ins) / len(ins)
            xo = sum(p.pt[0] for p in outs) / len(outs)
            # ライブラリ上で入力が左・出力が右に描かれているシンボルを、配置で反転させた場合だけ指摘する
            lib_ok = _lib_io_left_to_right(s, ins, outs)
            if xi > xo + 2.0 and lib_ok:
                rep.add("warning", "FLOW_IO_REVERSED", S, f"{symdesc(s)} の入力ピンが出力ピンより右にある (信号が右→左に流れる)", s.x, s.y,
                        hint="左右反転 (mirror) をやめ、入力を左・出力を右にする")
    if bb and nonpower:
        cw = bb[2] - bb[0]
        for s in nonpower:
            ident = (s.lib_id + " " + s.value).lower()
            if not re.match(r"^(J|P|CN|BT|X)\d", s.ref):
                continue
            strong = re.search(r"barrel|dc_?jack|jack_dc|battery|batt|poweronly|power_?in|pwr_?in|vin|電源入力|ac_?in", ident)
            weak = re.search(r"usb", ident)
            if strong or weak:
                rel = (s.x - bb[0]) / cw if cw > 1 else 0.0
                if rel > 0.66:
                    rep.add("warning" if strong else "info", "FLOW_SOURCE_RIGHT", S,
                            f"{'電源入力' if strong else 'USB'}コネクタ {symdesc(s)} が図の右側 ({rel:.0%} の位置) にある" + ("" if strong else " (給電・上流側なら左へ。下流ポートなら右で正しい)"),
                            s.x, s.y, hint="入力 (電源・信号の入口) は左、出力は右に置く")
        for L in sh.labels:
            if L.kind != "hierarchical_label":
                continue
            rel = (L.x - bb[0]) / cw if cw > 1 else 0.5
            if L.shape == "input" and rel > 0.75:
                rep.add("info", "FLOW_HLABEL_SIDE", S, f"入力の階層ラベル「{L.text}」が図の右端側にある", L.x, L.y)
            if L.shape == "output" and rel < 0.25:
                rep.add("info", "FLOW_HLABEL_SIDE", S, f"出力の階層ラベル「{L.text}」が図の左端側にある", L.x, L.y)
    for S2 in sh.sheets:
        for sp in S2.pins:
            right = abs(sp.x - (S2.x + S2.w)) < 0.01
            left = abs(sp.x - S2.x) < 0.01
            if sp.shape == "input" and right:
                rep.add("info", "FLOW_HLABEL_SIDE", S, f"シート「{S2.name}」の入力ピン「{sp.name}」が右辺にある", sp.x, sp.y)
            if sp.shape == "output" and left:
                rep.add("info", "FLOW_HLABEL_SIDE", S, f"シート「{S2.name}」の出力ピン「{sp.name}」が左辺にある", sp.x, sp.y)

    # ---- 部品番号・フィールド
    for s in sh.symbols:
        ref = s.ref
        if s.is_power:
            continue
        rf = s.props.get("Reference")
        if rf and rf["hidden"] and s.on_board:
            rep.add("warning", "FIELD_HIDDEN", S, f"{symdesc(s)} の部品番号が非表示", s.x, s.y)
        vf = s.props.get("Value")
        if vf and vf["hidden"] and s.on_board and not s.lib_id.startswith("Mechanical:"):
            rep.add("info", "FIELD_HIDDEN", S, f"{symdesc(s)} の定数 (Value) が非表示", s.x, s.y)
        box = s.pinbox or s.body
        if box:
            for fname in ("Reference", "Value"):
                f = s.props.get(fname)
                if not f or f["hidden"] or not f["value"]:
                    continue
                fb = _field_bbox(s, f)
                dx = max(box[0] - fb[2], fb[0] - box[2], 0)
                dy = max(box[1] - fb[3], fb[1] - box[3], 0)
                dist = math.hypot(dx, dy)
                if dist > opt.field_far:
                    rep.add("info", "FIELD_FAR", S, f"{symdesc(s)} の {fname}「{f['value']}」がシンボルから {dist:.1f} mm 離れている", f["x"], f["y"])
                # ワイヤとの重なり
                fbs = shrink(fb, min(0.25, (fb[3] - fb[1]) * 0.2))
                for a, b in sh.wires:
                    if kisch.seg_rect_intersect_len(a, b, fbs) > 0.5:
                        rep.add("warning", "FIELD_OVERLAP", S, f"{symdesc(s)} の {fname}「{f['value']}」がワイヤに重なっている", f["x"], f["y"],
                                hint="文字をワイヤから離す (配線と文字は重ねない)")
                        break
                else:
                    # 枠線 (矩形の辺) との重なり
                    for r in sh.rects:
                        edges = (((r[0], r[1]), (r[2], r[1])), ((r[2], r[1]), (r[2], r[3])), ((r[2], r[3]), (r[0], r[3])), ((r[0], r[3]), (r[0], r[1])))
                        if any(kisch.seg_rect_intersect_len(a, b, fbs) > 0.5 for a, b in edges):
                            rep.add("warning", "FIELD_OVERLAP", S, f"{symdesc(s)} の {fname}「{f['value']}」がブロック枠の線に重なっている", f["x"], f["y"])
                            break
                    # 自分の本体との重なり (2〜3 ピンの小さな部品だけ。IC は本体内に文字を置く設計がある)
                    if s.body and len(s.pins) <= 3 and rect_area(s.body) < 60 and rect_area(inter(fbs, shrink(s.body, 0.3))) > 0.5:
                        rep.add("info", "FIELD_OVERLAP", S, f"{symdesc(s)} の {fname}「{f['value']}」が自分の記号の中/上にある (抵抗の箱の中に定数を書く流儀なら可。線に重なって読めなければ直す)", f["x"], f["y"],
                                hint="文字を記号の横に出す")
                        continue
                    for s2 in sh.symbols:
                        if s2 is s or s2.body is None:
                            continue
                        if rect_area(inter(fbs, shrink(s2.body, 0.1))) > 0.5:
                            rep.add("warning", "FIELD_OVERLAP", S, f"{symdesc(s)} の {fname}「{f['value']}」が {symdesc(s2)} に重なっている", f["x"], f["y"])
                            break

    # 注記テキストの重なり (ワイヤ・枠線・記号)
    for t in sh.texts:
        if t["kind"] != "text" or not t["text"].strip():
            continue
        tb_ = t["bbox"]
        tbs = shrink(tb_, min(0.15, (tb_[3] - tb_[1]) * 0.1))
        what = None
        for r in sh.rects:
            edges = (((r[0], r[1]), (r[2], r[1])), ((r[2], r[1]), (r[2], r[3])), ((r[2], r[3]), (r[0], r[3])), ((r[0], r[3]), (r[0], r[1])))
            if any(kisch.seg_rect_intersect_len(a, b, tb_) > 0.0 for a, b in edges):
                what = "ブロック枠の線に重なっている (枠からはみ出している)"
                break
        if not what:
            for a, b in sh.wires:
                if kisch.seg_rect_intersect_len(a, b, tbs) > 0.5:
                    what = "ワイヤに重なっている"
                    break
        if not what:
            for s2 in sh.symbols:
                if s2.body is not None and rect_area(inter(tbs, shrink(s2.body, 0.1))) > 0.5:
                    what = f"{symdesc(s2)} に重なっている"
                    break
        if what:
            short = t["text"].split("\n")[0][:30]
            rep.add("warning", "TEXT_OVERLAP", S, f"テキスト「{short}」が{what}", t["x"], t["y"],
                    hint="文字を短くする・改行する・位置をずらす・枠を広げる")

    # 電源記号の名前とブロック枠の線
    for s in sh.symbols:
        f = s.props.get("Value")
        if not s.is_power or not f or f["hidden"] or not f["value"]:
            continue
        fbs = shrink(_field_bbox(s, f), 0.2)
        for r in sh.rects:
            edges = (((r[0], r[1]), (r[2], r[1])), ((r[2], r[1]), (r[2], r[3])), ((r[2], r[3]), (r[0], r[3])), ((r[0], r[3]), (r[0], r[1])))
            if any(kisch.seg_rect_intersect_len(a, b, fbs) > 0.3 for a, b in edges):
                rep.add("warning", "FIELD_OVERLAP", S, f"電源記号「{f['value']}」{fmt_xy(s.x, s.y)} の文字がブロック枠の線に重なっている", f["x"], f["y"])
                break

    # フィールド同士の重なり・縦書き
    fboxes = []
    for s in sh.symbols:
        for fname in ("Reference", "Value"):
            f = s.props.get(fname)
            if not f or f["hidden"] or not f["value"] or (s.is_power and fname == "Reference"):
                continue
            fb = _field_bbox(s, f)
            fboxes.append((fb, s, fname, f))
            if not s.is_power and len(f["value"]) >= 2 and (fb[3] - fb[1]) > (fb[2] - fb[0]) * 1.2:
                rep.add("info", "FIELD_VERTICAL", S, f"{symdesc(s)} の {fname}「{f['value']}」が縦向きに表示される", f["x"], f["y"],
                        hint="シンボルを回転したときはフィールドの角度も直して横書きにする (エディタでは R キーでフィールドを回転)")
    fboxes.sort(key=lambda t: t[0][0])
    for i, (b1, s1, n1, f1) in enumerate(fboxes):
        b1s = shrink(b1, min(0.2, (b1[3] - b1[1]) * 0.15))
        for b2, s2, n2, f2 in fboxes[i + 1:]:
            if b2[0] > b1[2]:
                break
            if rect_area(inter(b1s, shrink(b2, min(0.2, (b2[3] - b2[1]) * 0.15)))) > 0.3:
                rep.add("warning", "FIELD_OVERLAP", S, f"{symdesc(s1)} の {n1}「{f1['value']}」と {symdesc(s2)} の {n2}「{f2['value']}」の文字が重なっている", f1["x"], f1["y"],
                        hint="文字同士が重ならないように位置をずらす")

    # ---- ブロック枠と注記
    if len(nonpower) >= opt.block_min and not sh.rects and not [t for t in sh.texts if t["text"].strip()]:
        rep.add("info", "BLOCK_NO_FRAME", S, f"部品が {len(nonpower)} 個あるが、ブロック枠も注記もない",
                hint="機能ブロックごとに枠 (矩形) と見出し・説明のテキストを置く")
    for r in sh.rects:
        inside = [t for t in sh.texts if r[0] - 0.5 <= t["x"] <= r[2] + 0.5 and r[1] - 6 <= t["y"] <= r[3] + 0.5]
        if not inside and (r[2] - r[0]) * (r[3] - r[1]) > 100:
            rep.add("info", "BLOCK_FRAME_NO_TEXT", S, f"ブロック枠 {fmt_xy(r[0], r[1])}–{fmt_xy(r[2], r[3])} に見出し/説明のテキストがない", r[0], r[1])
        for s in sh.symbols:
            if s.body is None:
                continue
            a = rect_area(s.body)
            if a <= 0:
                continue
            ov = rect_area(inter(s.body, r)) / a
            if 0.15 < ov < 0.85:
                rep.add("warning", "BLOCK_SYMBOL_STRADDLE", S, f"{symdesc(s)} がブロック枠 {fmt_xy(r[0], r[1])}–{fmt_xy(r[2], r[3])} の線をまたいでいる", s.x, s.y)
    return conn


def _lib_io_left_to_right(s, ins, outs):
    """配置前 (ライブラリ座標) で入力ピンの平均 X が出力ピンより左か"""
    if s.lib is None:
        return False
    num_in = set(p.number for p in ins)
    num_out = set(p.number for p in outs)
    li = [lp.x for lp in s.lib.pins if lp.number in num_in and lp.unit in (0, s.unit)]
    lo = [lp.x for lp in s.lib.pins if lp.number in num_out and lp.unit in (0, s.unit)]
    return bool(li and lo) and sum(li) / len(li) < sum(lo) / len(lo) - 2.0


def _field_bbox(s, f):
    """フィールド文字の外接矩形。KiCad はフィールドの文字枠を (フィールド角度・揃えで) 求めてから
    シンボルの回転・ミラーをかけ、その枠の中央に文字を描く (常に読める向き)。"""
    x, y = f["x"], f["y"]
    ang = 90.0 if int(round(f["angle"])) % 180 == 90 else 0.0  # フィールドの角度は 0 / 90 だけ (270 は 90 扱い)
    b = kisch.text_bbox(f["value"], 0.0, 0.0, ang, f["size"], f["justify"])
    a, bb, c, d = kisch.make_transform(s.rot, s.mirror)
    a, bb, c, d = a, -bb, c, -d  # Y 下向き座標系での線形変換
    pts = [(b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])]
    tp = [(x + a * px + bb * py, y + c * px + d * py) for px, py in pts]
    return (min(p[0] for p in tp), min(p[1] for p in tp), max(p[0] for p in tp), max(p[1] for p in tp))


def _segs_touch_interior(a, b, c, d):
    """線分 a-b が線分 c-d (ピン) の c 以外の点に触れるか (水平/垂直のみ厳密)"""
    # c-d 上の点で a-b に乗るもの
    def on(p, s0, s1):
        return kisch.on_segment_interior(p, s0, s1) or p == s0 or p == s1
    # 平行で重なる
    if (a[0] == b[0] == c[0] == d[0]) or (a[1] == b[1] == c[1] == d[1]):
        lo1, hi1 = sorted((a[1], b[1])) if a[0] == b[0] else sorted((a[0], b[0]))
        lo2, hi2 = sorted((c[1], d[1])) if c[0] == d[0] else sorted((c[0], d[0]))
        ov = min(hi1, hi2) - max(lo1, lo2)
        return ov > qi(0.3)
    # 交差
    if (a[0] == b[0]) and (c[1] == d[1]):
        x, y = a[0], c[1]
    elif (a[1] == b[1]) and (c[0] == d[0]):
        x, y = c[0], a[1]
    else:
        return False
    p = (x, y)
    if p == c:
        return False
    return on(p, a, b) and on(p, c, d) and p != a and p != b or (on(p, c, d) and p != c and (p == a or p == b) and False)


def _crossings(conn):
    W = conn.W
    jset = set(conn.junction_pts)
    hs = [(i, a, b) for i, (a, b) in enumerate(W) if a[1] == b[1] and a[0] != b[0]]
    vs = [(i, a, b) for i, (a, b) in enumerate(W) if a[0] == b[0] and a[1] != b[1]]
    out = []
    vs_sorted = sorted(vs, key=lambda t: t[1][0])
    xs = [t[1][0] for t in vs_sorted]
    import bisect
    for i, a, b in hs:
        x0, x1 = sorted((a[0], b[0]))
        y = a[1]
        lo = bisect.bisect_right(xs, x0)
        hi = bisect.bisect_left(xs, x1)
        for j, c, d in vs_sorted[lo:hi]:
            y0, y1 = sorted((c[1], d[1]))
            if y0 < y < y1 and (c[0], y) not in jset:
                out.append((c[0] / Q, y / Q))
    return out


def _overlaps(conn):
    W = conn.W
    out = []
    by_line = {}
    for i, (a, b) in enumerate(W):
        if a[1] == b[1]:
            by_line.setdefault(("h", a[1]), []).append((min(a[0], b[0]), max(a[0], b[0]), i))
        elif a[0] == b[0]:
            by_line.setdefault(("v", a[0]), []).append((min(a[1], b[1]), max(a[1], b[1]), i))
    for k, segs in by_line.items():
        segs.sort()
        for n in range(1, len(segs)):
            for m in range(n):
                if segs[m][1] > segs[n][0]:
                    out.append((segs[n][2], segs[m][2]))
                    break
    return out


def _smaller_paper(pname, W, H, cw, ch):
    order = ["A0", "A1", "A2", "A3", "A4", "A5"]
    base = pname.split()[0]
    if base not in order:
        return None
    i = order.index(base)
    if i + 1 >= len(order):
        return None
    w2, h2 = kisch.PAPER[order[i + 1]]
    if W < H:
        w2, h2 = h2, w2
    # 作図領域 (枠 12 mm、タイトル欄の高さ 36 mm を避ける) に 1.3 倍の余白で入るか
    if cw * 1.3 <= w2 - 24 and ch * 1.3 <= h2 - 24 - 36:
        return order[i + 1]
    return None


# ---------------------------------------------------------------- 全体
def run(top, opt):
    rep = Report(opt.disable)
    try:
        hier, files = kisch.load_hierarchy(top)
    except Exception as e:  # noqa: BLE001
        rep.add("error", "SCHEMATIC_PARSE", os.path.basename(top), f"回路図を読めない: {e}")
        return rep, None
    gctx = dict(global_count={}, embedded_fonts={}, aliases={})
    for sh in files.values():
        gctx["aliases"].update(sh.bus_aliases)
    for sh, ip, npath in hier:
        for L in sh.labels:
            if L.kind == "global_label":
                gctx["global_count"][L.text] = gctx["global_count"].get(L.text, 0) + 1
    for p, sh in files.items():
        ef = kisch.find(sh.root, "embedded_fonts")
        gctx["embedded_fonts"][p] = bool(ef is not None and len(ef) > 1 and ef[1] == "yes")
    # 部品番号: 未確定・食い違い・重複 (階層インスタンスごと)
    seen = {}
    for sh, ip, npath in hier:
        lbl = os.path.relpath(sh.path, os.path.dirname(os.path.abspath(top)))
        for s in sh.symbols:
            if s.is_power:
                continue
            r = s.ref_for(ip)
            mine = [x for x in s.instances if x[0] == ip]
            if r.endswith("?") or (not mine and s.ref.endswith("?")):
                rep.stats.setdefault("skip_refs", []).append(r)
                rep.add("error", "REF_UNANNOTATED", lbl, f"{symdesc(s)} の部品番号が未確定 (Reference「{s.ref}」, このシートのインスタンス「{r}」)",
                        s.x, s.y, hint="アノテーションする。MCP の sch_annotate は instances を更新しないので部品番号は明示する")
                continue
            if mine and len(hier) == 1 and mine[0][1] != s.ref:
                rep.add("info", "REF_MISMATCH", lbl, f"{symdesc(s)} の Reference「{s.ref}」と instances の部品番号「{mine[0][1]}」が違う (画面・kicad-cli・ネットリストは instances 側を使う。スクリプトで Reference だけ書き換えても反映されない)",
                        s.x, s.y)
            key = (r, s.unit)
            if key in seen and seen[key] != (sh.path, s.uuid, ip):
                rep.stats.setdefault("skip_refs", []).append(r)
                rep.add("error", "REF_DUPLICATE", lbl,
                        f"部品番号 {r} (ユニット {s.unit}) が重複している ({npath})", s.x, s.y)
            seen[key] = (sh.path, s.uuid, ip)
    done = set()
    names = {}
    for sh, ip, npath in hier:
        names.setdefault(sh.path, npath)
    conns = {}
    for sh, ip, npath in hier:
        if sh.path in done:
            continue
        done.add(sh.path)
        label = os.path.relpath(sh.path, os.path.dirname(os.path.abspath(top)))
        conns[sh.path] = lint_sheet(sh, rep, opt, label, gctx)
    rep.stats["sheets"] = [dict(file=os.path.relpath(sh.path, os.path.dirname(os.path.abspath(top))), path=npath, paper=sh.paper[0],
                                symbols=len([s for s in sh.symbols if not s.is_power]), wires=len(sh.wires), labels=len(sh.labels))
                           for sh, ip, npath in hier]
    return rep, conns


def verify_netlist(top, conns, rep):
    """kicad-cli のネットリストと schlint の接続モデルを照合"""
    tmp = tempfile.mkdtemp(prefix="schlint-")
    net = os.path.join(tmp, "netlist.net")
    try:
        r = subprocess.run(["kicad-cli", "sch", "export", "netlist", "-o", net, top], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        rep.stats["netlist_verify"] = dict(ok=False, error=str(e))
        return
    if r.returncode != 0:
        rep.stats["netlist_verify"] = dict(ok=False, error=r.stderr[-300:])
        return
    pads, _ = kisch.global_partition(top)
    nets, _ = kisch.parse_netlist(net)
    skip = set(rep.stats.get("skip_refs", []))
    if skip:  # 重複・未確定の部品番号はピンを特定できないので照合から外す
        pads = {k: v for k, v in pads.items() if k[0] not in skip}
        nets = {n: [p for p in nodes if p[0] not in skip] for n, nodes in nets.items()}
        nets = {n: nodes for n, nodes in nets.items() if nodes}
    ok, bad = kisch.compare_partition(pads, nets)
    rep.stats["netlist_verify"] = dict(ok=not bad, nets=len(nets), matched=ok, mismatched=[dict(net=b[0], reason=b[1], pins=b[2]) for b in bad[:20]],
                                       mismatched_count=len(bad), skipped_refs=sorted(skip))


def format_text(rep, top, opt):
    out = []
    c = rep.count()
    out.append(f"schlint {VERSION}: {top}")
    for st in rep.stats.get("sheets", []):
        out.append(f"  シート {st['path']} ({st['file']}, {st['paper']}): 部品 {st['symbols']} / ワイヤ {st['wires']} / ラベル {st['labels']}")
    items = sorted(rep.items, key=lambda d: (d["sheet"], SEV_ORDER[d["severity"]], d["code"], d.get("y", 0), d.get("x", 0)))
    cur = None
    shown = {}
    hidden = {}
    for d in items:
        k = (d["sheet"], d["severity"], d["code"])
        shown[k] = shown.get(k, 0) + 1
        if shown[k] > opt.max_list:
            hidden[k] = hidden.get(k, 0) + 1
    shown = {}
    for d in items:
        if opt.min_sev and SEV_ORDER[d["severity"]] > SEV_ORDER[opt.min_sev]:
            continue
        k = (d["sheet"], d["severity"], d["code"])
        shown[k] = shown.get(k, 0) + 1
        if shown[k] > opt.max_list:
            if shown[k] == opt.max_list + 1:
                out.append(f"[{SEV_JA[d['severity']]}] {d['code']}: ……ほか {hidden[k]} 件 (全件は JSON を参照)")
            continue
        if d["sheet"] != cur:
            cur = d["sheet"]
            out.append(f"\n== {cur} ==")
        pos = f" {fmt_xy(d['x'], d['y'])}" if "x" in d else ""
        out.append(f"[{SEV_JA[d['severity']]}] {d['code']}{pos}: {d['message']}")
        if d.get("hint") and opt.hints:
            out.append(f"        → {d['hint']}")
    out.append("")
    for S, v in rep.stats.get("occupancy", {}).items():
        out.append(f"占有率 {S}: 用紙 {v['paper']}, 図の外接矩形 幅 {v['width_ratio']:.0%} × 高さ {v['height_ratio']:.0%} (面積 {v['area_ratio']:.0%})")
    for S, v in rep.stats.get("label_only", {}).items():
        out.append(f"配線/ラベル {S}: ワイヤで接続 {v['wired']} ピン、ラベルだけ {v['label_only']} ピン ({v['ratio']:.0%})")
    nv = rep.stats.get("netlist_verify")
    if nv:
        if nv.get("error"):
            out.append(f"ネットリスト照合: 実行できず ({nv['error']})")
        else:
            out.append(f"ネットリスト照合 (kicad-cli): {nv['matched']}/{nv['nets']} ネット一致" + ("" if nv["ok"] else f"、不一致 {nv['mismatched_count']} 件 (schlint の接続判定に誤りの可能性)"))
            for m in nv.get("mismatched", [])[:10]:
                out.append(f"    {m['net']}: {m['reason']} {m['pins']}")
    out.append(f"集計: エラー {c['error']} / 警告 {c['warning']} / 情報 {c['info']}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="KiCad 回路図の作図ルール検査")
    ap.add_argument("schematic", help="トップの .kicad_sch (階層シートもたどる)")
    ap.add_argument("--json", help="JSON の出力先")
    ap.add_argument("--format", choices=["text", "json"], default="text", help="標準出力の形式")
    ap.add_argument("--grid", type=float, default=1.27, help="グリッド (mm, 既定 1.27)")
    ap.add_argument("--label-ratio", type=float, default=0.30, help="ラベルだけでつながるピンの比率のしきい値 (既定 0.30)")
    ap.add_argument("--label-min", type=int, default=3, help="LABEL_ONLY_RATIO を出す最小ピン数 (既定 3)")
    ap.add_argument("--occ-min", type=float, default=0.25, help="占有率 (面積) の下限 (既定 0.25)")
    ap.add_argument("--occ-max", type=float, default=0.85, help="占有率 (面積) の上限 (既定 0.85)")
    ap.add_argument("--field-far", type=float, default=7.62, help="フィールドがシンボルから離れている判定距離 mm (既定 7.62)")
    ap.add_argument("--block-min", type=int, default=6, help="BLOCK_NO_FRAME を出す最小部品数 (既定 6)")
    ap.add_argument("--max-list", type=int, default=12, help="テキスト出力で 1 シート・1 コードあたりに並べる上限 (JSON は全件)")
    ap.add_argument("--disable", default="", help="無効にする検査コード (カンマ区切り)")
    ap.add_argument("--min-severity", dest="min_sev", choices=["error", "warning", "info"], default="info", help="テキスト出力に含める最低重要度")
    ap.add_argument("--no-hints", dest="hints", action="store_false", help="対処のヒントを出さない")
    ap.add_argument("--verify-netlist", action="store_true", help="kicad-cli のネットリストと接続判定を照合する")
    ap.add_argument("--strict", action="store_true", help="warning でも終了コード 1")
    ap.add_argument("--list-codes", action="store_true", help="検査コードの一覧を表示")
    opt = ap.parse_args(argv)
    if opt.list_codes:
        for k, v in CODES.items():
            print(f"{k:24s} {v}")
        return 0
    opt.disable = [x.strip() for x in opt.disable.split(",") if x.strip()]
    if not os.path.exists(opt.schematic):
        print(f"ファイルがない: {opt.schematic}", file=sys.stderr)
        return 2
    rep, conns = run(opt.schematic, opt)
    if conns is not None and opt.verify_netlist:
        verify_netlist(opt.schematic, conns, rep)
    c = rep.count()
    doc = dict(tool="schlint", version=VERSION, schematic=os.path.abspath(opt.schematic), grid=opt.grid, summary=c,
               stats=rep.stats, findings=sorted(rep.items, key=lambda d: (SEV_ORDER[d["severity"]], d["sheet"], d["code"])))
    if opt.json:
        with open(opt.json, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    if opt.format == "json":
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print(format_text(rep, opt.schematic, opt))
    if conns is None:
        return 2
    if c["error"] or (opt.strict and c["warning"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
