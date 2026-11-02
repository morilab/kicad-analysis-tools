# pitfall-4l — 正解付き 4 層の試験基板の熱の区画 (2026-10-14)

基板と正解表はリポジトリの `testboards/pitfall-4l/` (expected.yaml、読むだけ)。
条件は `thermal.yaml` (区画ごとの `scenarios:`)、基板の製造条件は `data/board/pitfall-4l-rev0.1.yaml` (JLC04161H-7628 仮定)、AMS1117 は `data/parts/C6186.yaml` (θJC(タブ) 15・θJA 90 は expected.yaml の転記で PDF は未照合 → assumed)。

```sh
PY=python3
$PY examples/pitfall-4l/run_pitfall.py [--only ldo,dense,spread,relief,th1a,cp1a5]   # 熱 0.25 mm・IR 0.1 mm、全部で約 12 分 (4 層 0.25 mm は未知数 約 90 万、1 区画 約 90 s)
$PY examples/pitfall-4l/table.py                                                       # results.md (expected.yaml と同じ項目で表)
```

TH-1A・CP-1A5 のジュール熱は IR を解いて熱の外部発熱にする (`coupling.driver.run`、温度は補間・発熱は保存)。IR は `irdrop.solver.FdIrSolver` が main にあればそれを、
無ければ `run_pitfall.py` の SimpleIr (同じモデル) を使う。SimpleIr は FdIrSolver と 20 ℃ で降下・発熱とも 1e-8 の相対差で一致を確認。

結果: `results.json` (table.py で results.md を作る)。
