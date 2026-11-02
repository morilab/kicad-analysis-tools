# irdrop — IR ドロップ (直流の電圧降下) とジュール熱 (v0.1、2026-10-14)

対象は **アートワーク後 (配線済み .kicad_pcb) のみ**。格子は core の pcb-physics-grid/2 (IR は dx 0.1 mm 推奨)。

```sh
PY=python3
$PY tests/test_irdrop_analytic.py                                   # 手計算検証 (±5%)、約 2 s
$PY -m core.gridtool <project> --dx 0.1 --out out/grid              # 格子 (FT2232H rev0.2 で 30 s)
$PY -m irdrop.run <project> --cond irdrop/examples/<案件>/irdrop.yaml --out out/ir --grid out/grid/grid_artwork_dx0.1.npz
```

| ファイル | 内容 |
|---|---|
| `interface.py` | IrSolver / IrResult、ρ(T) = 1.72e-8 (1 + 0.00393 (T − 20)) |
| `solver.py` | `solve_net` (1 ネット、scipy spsolve)、`FdIrSolver` (IrSolver の実装、sources に `v_set` があればディリクレ)、`hot_spots` |
| `run.py` | 案件 YAML → typ / worst、GND を別に解いて合算、直列素子 (FB/PTC) はネット間抵抗、PNG・表 (irdrop.md)・kreview 形式 JSON (irdrop.json)・発熱マップ heat_*.npz |
| `plating.py` | ビアめっき厚の決定と grid.meta への記録 (plating_mm・plating_src) |
| `couple.py` | 電熱連成 (温度 → IR → ジュール熱 → thermal.pcbmodel.build_and_solve)。熱の格子は `--thermal-dx` (既定 = IR の dx)、違えば core/regrid で受け渡し (温度は双一次補間、発熱は面積で配分し合計を保存)。2 回目以降は前回温度を `t_init` に。途中経過 out/progress から再開 |
| `examples/usb-jtag-ft2232h/irdrop.yaml` | 系統 (源・入力・直列素子)、GND 基準、案件側のピン電流、合否基準 |

モデル: 節点 = 層ごとのセル (net_major)。同層は edge_*_net が同じネットの辺だけ G = σ t edge_w。ビアは筒 R = ρh/(π((r+tp)² − r²))、h = 誘電体 + 銅厚。
源の pad 全面をディリクレ、負荷は pad のセルへ面積重みで電流シンク。
降下 = (源の公称 − ピン電位) + (負荷の GND ピン電位 − 源の GND ピン電位)。GND は全負荷の帰りを各部品の GND ピンから注入し、基準 (USB コネクタの GND) を 0 V。
合否 (worst): 降下 ≤ 許容電圧余裕 × 20% (余裕 = 源の電圧 − ピンの許容最小、2026-10-14 作者 決定)。

精度 (tests/test_irdrop_analytic.py): 合成格子 (直線・1 □・ビア・並列・温度補正・ジュール熱) 0.0〜1.3%。実ラスタの 0.2 mm 線: 水平 dx 0.1 で −0.8%、
斜め 30°/45° は dx 0.05 で +3.4/+2.0%、**dx 0.1 では 4 近傍の階段近似で +8〜12% (抵抗大 = 安全側)**。FT2232H rev0.2 の最大降下は dx 0.1 と 0.05 で差 0.7% 以内。

ビアめっき厚の優先順 (高い順。決めた値と出典を grid.meta の plating_mm / plating_src と irdrop.md・irdrop.json の grid に記録):
1. 案件 YAML `plating: {value: <µm>, basis: ..., from: ...}`
2. 基板 YAML (data/board) `process.via_plating: {value: <µm>, ...}` (JLC の注文画面にめっき厚の項目は無いので order: ではなく process: に書く)
3. core 既定 18 µm (core/stackup.T_PLATING)

基板 YAML は `--board` > 案件 YAML の `board:` > data/board/<基板名>*.yaml の順で探す。
熱側 (thermal/pcbmodel) のビアは現状 core 既定 18 µm 固定 (core への要望として報告済み)。

連成の熱の面内伝導 (2026-10-19、pdn-edge): 熱は main の edge 方式 (f5c2ded〜、core/raster.path_conduct = セル中心間の経路の銅、thermal/pcbmodel の既定) で解く。旧 pdn-v0.1 (5527d36・929d23d) はセル方式 (セルの銅の面積率の調和平均) で、隙間をまたぐセルが熱的に短絡し、FT2232H 166e892 の F1 パッドが 0.1 mm で 4.85 K 低く出ていた (熱解析担当 kicad-thermal-results ft2232h-rev0.2-166e892/vs-irdrop)。旧方式に戻すときは thermal.yaml に `solver: {inplane: cell}`。使った方式は coupling.json の cases.<case>.thermal_inplane に残る。IR・PDN の電気の面内コンダクタンスは従来どおり (edge_*_w)。

直列素子の抵抗 (2026-10-19): 部品 YAML の `dc_resistance` (IR 側の書き方) が無ければ `resistance` + `resistance_min` (熱側の書き方) を読む (core/parts.series_resistance)。C207022・C468889 は main の版 (データシートの新しい版) に統一。

連成の目安 (2026-10-14、熱 AMG-CG): FT2232H rev0.2 (2 層) は熱も dx 0.1 mm で 197 s・最大 1.6 GB。4 層 100x80 mm (pitfall-4l) は熱 0.1 mm だと約 560 万未知数で載らないため `--thermal-dx 0.2` (107 s/反復 4 回、最大 3.1 GB)。
