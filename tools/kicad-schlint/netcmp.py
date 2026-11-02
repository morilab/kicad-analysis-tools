#!/usr/bin/env python3
"""netcmp: 編集前後の回路図 (またはネットリスト) の「接続」が同じかを比べる。

見た目だけを直す作業 (枠・注記・部品番号の位置・配線の引き直し) のあとに、接続が変わっていないことを確かめる。

使い方:
  netcmp.py BEFORE.kicad_sch AFTER.kicad_sch [--names] [--json out.json]
  netcmp.py BEFORE.net AFTER.net            (kicad-cli sch export netlist の出力も可)

比較内容:
  * 部品の増減、Value / Footprint の変化
  * ピン (部品番号, ピン番号) の接続グループ (ネット名に依存しない比較)。分割・統合・ピンの移動
  * ネット名の変化 (同じピン集合で名前だけ違う)。--names を付けると名前の変化も不一致として扱う

終了コード: 0 = 接続は同一 / 1 = 違いあり / 2 = 実行エラー
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kisch  # noqa: E402


def load(path, tmp):
    if path.endswith(".kicad_sch"):
        out = os.path.join(tmp, os.path.basename(path) + f".{abs(hash(os.path.abspath(path)))}.net")
        r = subprocess.run(["kicad-cli", "sch", "export", "netlist", "-o", out, path], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"kicad-cli のネットリスト出力に失敗: {path}\n{r.stderr[-500:]}")
        path = out
    return kisch.parse_netlist(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="編集前後の接続 (ネットリスト) 比較")
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--names", action="store_true", help="ネット名の変化も不一致にする")
    ap.add_argument("--values", action="store_true", help="Value / Footprint の変化も不一致にする")
    ap.add_argument("--strict", action="store_true", help="--names と --values の両方")
    ap.add_argument("--json", help="結果 JSON の出力先")
    o = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="netcmp-")
    try:
        na, ca = load(o.before, tmp)
        nb, cb = load(o.after, tmp)
    except (RuntimeError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    res = dict(before=o.before, after=o.after, components_added=sorted(set(cb) - set(ca)), components_removed=sorted(set(ca) - set(cb)),
               value_changed=[], footprint_changed=[], connectivity=[], renamed=[])
    for r in sorted(set(ca) & set(cb)):
        if ca[r]["value"] != cb[r]["value"]:
            res["value_changed"].append(dict(ref=r, before=ca[r]["value"], after=cb[r]["value"]))
        if ca[r]["footprint"] != cb[r]["footprint"]:
            res["footprint_changed"].append(dict(ref=r, before=ca[r]["footprint"], after=cb[r]["footprint"]))
    pin_a = {p: n for n, ps in na.items() for p in ps}
    pin_b = {p: n for n, ps in nb.items() for p in ps}
    common = set(pin_a) & set(pin_b)
    for p in sorted(set(pin_a) - set(pin_b)):
        if p[0] in cb:
            res["connectivity"].append(dict(kind="pin_removed", pin=list(p), before=pin_a[p]))
    for p in sorted(set(pin_b) - set(pin_a)):
        if p[0] in ca:
            res["connectivity"].append(dict(kind="pin_added", pin=list(p), after=pin_b[p]))
    # ネット名に依存しない比較: 前のネットごとに、共通ピンが後で何個のネットに分かれたか
    seen_b = {}
    for n, ps in sorted(na.items()):
        ps = sorted(p for p in ps if p in common)
        if not ps:
            continue
        groups = {}
        for p in ps:
            groups.setdefault(pin_b[p], []).append(p)
        if len(groups) > 1:
            res["connectivity"].append(dict(kind="split", before=n, after={k: [list(x) for x in v] for k, v in groups.items()}))
        for k in groups:
            seen_b.setdefault(k, set()).add(n)
    for k, ns in sorted(seen_b.items()):
        if len(ns) > 1:
            res["connectivity"].append(dict(kind="merged", after=k, before=sorted(ns)))
        else:
            n = next(iter(ns))
            if n != k and set(p for p in na[n] if p in common) == set(p for p in nb[k] if p in common):
                res["renamed"].append(dict(before=n, after=k))
    diff_conn = bool(res["connectivity"] or res["components_added"] or res["components_removed"])
    diff_names = bool(res["renamed"])
    diff_vals = bool(res["value_changed"] or res["footprint_changed"])
    if o.strict:
        o.names = o.values = True
    res["identical_connectivity"] = not diff_conn
    if o.json:
        json.dump(res, open(o.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # 人向け
    print(f"netcmp: {o.before}  →  {o.after}")
    print(f"  部品 {len(ca)} → {len(cb)}、ネット {len(na)} → {len(nb)}")
    for r in res["components_added"]:
        print(f"  [差] 部品が増えた: {r}")
    for r in res["components_removed"]:
        print(f"  [差] 部品が減った: {r}")
    for d in res["value_changed"]:
        print(f"  [{'差' if o.values else '注'}] {d['ref']} の Value: {d['before']} → {d['after']}")
    for d in res["footprint_changed"]:
        print(f"  [{'差' if o.values else '注'}] {d['ref']} の Footprint: {d['before']} → {d['after']}")
    for d in res["connectivity"]:
        if d["kind"] == "split":
            print(f"  [差] ネット {d['before']} が分かれた: " + " / ".join(f"{k}: {', '.join(r + '.' + p for r, p in v)}" for k, v in d["after"].items()))
        elif d["kind"] == "merged":
            print(f"  [差] ネット {', '.join(d['before'])} が 1 つ ({d['after']}) にまとまった")
        elif d["kind"] == "pin_removed":
            print(f"  [差] ピン {d['pin'][0]}.{d['pin'][1]} が消えた (元 {d['before']})")
        elif d["kind"] == "pin_added":
            print(f"  [差] ピン {d['pin'][0]}.{d['pin'][1]} が増えた ({d['after']})")
    for d in res["renamed"]:
        print(f"  [{'差' if o.names else '注'}] ネット名の変化: {d['before']} → {d['after']}")
    if not diff_conn and not (o.names and diff_names) and not (o.values and diff_vals):
        notes = []
        if diff_names:
            notes.append(f"ネット名の変化 {len(res['renamed'])} 件")
        if diff_vals:
            notes.append(f"Value/Footprint の変化 {len(res['value_changed']) + len(res['footprint_changed'])} 件")
        print("結果: 接続は同一" + (f" ({'、'.join(notes)})" if notes else ""))
        return 0
    print("結果: 接続に違いあり")
    return 1


if __name__ == "__main__":
    sys.exit(main())
