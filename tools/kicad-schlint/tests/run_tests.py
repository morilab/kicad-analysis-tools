#!/usr/bin/env python3
"""schlint / netcmp の回帰テスト。
使い方: run_tests.py [--keep] [--no-kicad]
  - make_fixtures.py のフィクスチャを一時ディレクトリに作り、各ケースで期待する検査コードが出るか確かめる
  - kicad-cli があれば、全フィクスチャで schlint の接続判定と kicad-cli のネットリストを照合 (--verify-netlist)
  - netcmp: 同一コピーで「同一」、接続を変えたコピーで「違いあり」になるか
終了コード 0 = 全部合格"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_fixtures  # noqa: E402

SEV = {"error": 0, "warning": 1, "info": 2}


def run_lint(path, verify):
    out = path + ".lint.json"
    cmd = [sys.executable, os.path.join(TOOL, "schlint.py"), path, "--json", out]
    if verify:
        cmd.append("--verify-netlist")
    r = subprocess.run(cmd, capture_output=True, text=True)
    doc = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else None
    return r.returncode, doc, r.stdout + r.stderr


def main():
    keep = "--keep" in sys.argv
    use_kicad = "--no-kicad" not in sys.argv and shutil.which("kicad-cli")
    tmp = tempfile.mkdtemp(prefix="schlint-tests-")
    made = make_fixtures.build(os.path.join(tmp, "fx"))
    fails = 0
    fired_codes = set()
    for name, path in made.items():
        rc, doc, log = run_lint(path, verify=use_kicad and name != "parse_error")
        if name == "parse_error":
            ok = rc == 2 and doc and any(f["code"] == "SCHEMATIC_PARSE" for f in doc["findings"])
            print(f"{'OK ' if ok else 'NG '} {name}: 終了コード {rc}")
            fails += not ok
            continue
        if doc is None:
            print(f"NG  {name}: JSON が出ない\n{log}")
            fails += 1
            continue
        codes = {f["code"] for f in doc["findings"]}
        fired_codes |= codes
        exp, forbid = make_fixtures.CASES[name][1], make_fixtures.CASES[name][2]
        missing = exp - codes
        bad = [f for f in doc["findings"] if f["severity"] in forbid]
        nv = doc["stats"].get("netlist_verify")
        nv_ok = (nv is None) or nv.get("ok")
        ok = not missing and not bad and nv_ok
        fails += not ok
        msg = f"{'OK ' if ok else 'NG '} {name}: 期待 {sorted(exp) or '-'}"
        if missing:
            msg += f" / 出なかった {sorted(missing)}"
        if bad:
            msg += " / 出てはいけない指摘 " + "; ".join(f"{f['severity']} {f['code']} {f['message'][:60]}" for f in bad[:5])
        if nv is not None:
            msg += f" / ネットリスト照合 {'OK' if nv.get('ok') else 'NG ' + str(nv)[:200]}"
        print(msg)
    # 全コードのうちテストで一度も出なかったもの
    sys.path.insert(0, TOOL)
    import schlint
    never = sorted(set(schlint.CODES) - fired_codes - {"SCHEMATIC_PARSE"})
    print("テストで一度も出なかった検査コード:", never or "なし")

    # netcmp
    if use_kicad:
        src = made["clean"]
        d2 = os.path.join(tmp, "netcmp")
        os.makedirs(d2)
        same = os.path.join(d2, "same.kicad_sch")
        diff = os.path.join(d2, "diff.kicad_sch")
        txt = open(src, encoding="utf-8").read()
        open(same, "w", encoding="utf-8").write(txt.replace("安定化電源 5V", "安定化電源 (5V)"))
        # 出力側の接続点を 1 つ消す → T 分岐が切れて接続が変わる
        i = txt.index("(junction (at 127.0 100.33)")
        j = txt.index("\t)\n", i) + 3
        open(diff, "w", encoding="utf-8").write(txt[:i] + txt[j:])
        for p in (same, diff):
            shutil.copy(os.path.join(os.path.dirname(src), "clean.kicad_pro"), p[:-10] + ".kicad_pro")
        nc = os.path.join(TOOL, "netcmp.py")
        r1 = subprocess.run([sys.executable, nc, src, same], capture_output=True, text=True)
        r2 = subprocess.run([sys.executable, nc, src, diff], capture_output=True, text=True)
        ok1, ok2 = r1.returncode == 0, r2.returncode == 1
        print(f"{'OK ' if ok1 else 'NG '} netcmp 見た目だけの変更 → 同一 (終了コード {r1.returncode})")
        print(f"{'OK ' if ok2 else 'NG '} netcmp 接続点を削除 → 違いあり (終了コード {r2.returncode})")
        if not ok2 or not ok1:
            print(r1.stdout, r2.stdout)
        fails += (not ok1) + (not ok2)
    if keep:
        print("フィクスチャ:", tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    print("結果:", "全部合格" if not fails else f"{fails} 件不合格")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
