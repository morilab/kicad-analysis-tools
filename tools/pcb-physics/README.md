# pcb-physics — KiCad 基板の物理解析 (熱・IR ドロップ・連成)

共通ライブラリ `core/` の上に、熱解析 `thermal/`、IR ドロップ `irdrop/` (雛形、IR ドロップ担当)、連成ドライバ `coupling/` を置く。
旧 kicad-thermal 0.1.0 を移植 (結果は bit 単位で同じ = `tests/test_regression.py`)。

```sh
PY=python3                  # numpy / scipy / matplotlib / PyYAML / pyamg (熱の既定ソルバー、無ければ直接法)
$PY kthermal.py --selftest                                   # 解析解 + 部品 YAML の規則 + 格子の交換形式 (約 3 s)
$PY tests/test_regression.py                                 # FT2232H rev0.2 の回帰 (約 1.5 分)
$PY kthermal.py <project_dir> --cond thermal.yaml --out out/<name>          # typ/worst × placement/artwork
$PY kthermal.py <project_dir> --cond thermal.yaml --mode artwork --heat heat.npz   # 外部発熱マップ (ジュール熱) を足す
$PY -m coupling.driver <project_dir> --cond thermal.yaml --case worst --out out/c  # 熱 ⇄ IR の連成 (IR 未実装の間はスタブ)
```

| 場所 | 中身 |
|---|---|
| `core/pcbread.py` | .kicad_pcb (KiCad 10) の S 式: 外形、層、stackup、footprint/pad (ネット)、track/arc、via、zone、3D モデルの参照 |
| `core/stackup.py` | 層構成 (stackup が無ければ JLCPCB 標準)、材料定数 |
| `core/raster.py` | 細分ラスタ化: 銅の面積率とセルごとのネット ID |
| `core/grid.py` | **2.5D 格子 = 交換形式** (PcbGrid npz + メタ JSON、温度/発熱マップ npz) |
| `core/model3d.py` | STEP から外形・高さ・上面 |
| `core/parts.py` | 部品 YAML / 案件 YAML の値オブジェクト (basis 必須、出典ページ・仮定の根拠と計算式必須、ピンごと電流) |
| `core/cases.py` | typ / worst (電流・ピン電流は typ/max 必須、環境の既定 typ 25 ℃ / worst 50 ℃) |
| `thermal/` | 熱回路網 (model)、h の相関式 (env)、基板 → 回路網 (pcbmodel、外部発熱マップ対応)、判定と出力 (report、partstable) |
| `irdrop/` | `interface.py` (IrSolver / IrResult、銅の抵抗の温度補正)、`stub.py` (発熱 0) |
| `coupling/driver.py` | 温度 → IR → ジュール熱 → 熱 を収束まで |
| `data/parts/<LCSC>.yaml` | 部品データ (熱・電流・ピン) |
| `data/board/<基板>.yaml` | 基板の製造条件 (JLCPCB 注文画面の項目名、core/boardspec.py) |
| `examples/usb-jtag-ft2232h/` | 案件 YAML (環境・typ/worst・部品の電力・ピン電流) |
| `tests/` | 解析解、データ規則、格子、回帰 (`regression/ref/` = kicad-thermal ac64d1b の結果) |

## 交換形式 (IR ドロップ担当へ)
`core/grid.py` の先頭に仕様。要点:
- 格子 `grid_<mode>.npz` (+ 同名 `.json` にメタ): `board_frac (Ny,Nx)`、`cu_frac (L,Ny,Nx)`、`net_id (L,Ny,Nx) int32` (meta.nets の添字、-1 = なし)、
  ビア接続リスト `via_x_mm, via_y_mm, via_drill_mm, via_j, via_i, via_net, via_l_from, via_l_to, via_kind` (0 via / 1 めっき穴)。
  meta: `format pcb-physics-grid/1`、`dx_mm, x0_mm, y0_mm, nx, ny`、`layers [{name, cu_mm}]`、`dielectric_mm`、`plating_mm`、`nets`、`grid_id`、`pcb_sha256`。
- 座標: KiCad mm (y 下向き)、配列は `[layer, j, i]`、セル中心 = `(x0 + (i+0.5) dx, y0 + (j+0.5) dx)`、層は F.Cu → 内層 → B.Cu。
- マップ `tmap_<case>_<mode>.npz` (温度 ℃) / `heat_*.npz` (発熱 W/セル): `data (L,Ny,Nx)`、`meta_json` (`kind`, `unit`, `grid_id`)。読み込み時に grid_id を照合。
- 電流源: 部品 YAML / 案件 YAML の `pins.<pin>.current` (typ/max、正 = 部品へ流れ込む)。`coupling.driver.pin_sources` が座標・ネット付きのリストにする。
- 銅の抵抗: ρ(T) = 1.72e-8 × (1 + 0.00393 (T − 20)) Ω·m (`irdrop.interface.rho_cu`)。
## typ / worst (標準動作)
毎回 **typ (標準: 既定 周囲 25 ℃、データシートの標準電流)** と **worst (最悪: 既定 周囲 50 ℃、最大電流)** の両方を計算し、thermal.md・parts_table.csv・JSON に並べる。**合否は worst**。
電流は `{typ: <値>, max: <値>}` の両方が必須 (それぞれ出典か仮定の根拠)。環境は `environment` (共通) + `cases.typ / cases.worst` (上書き)。

## 値の区分 (YAML スキーマ)
全数値に `basis`: `datasheet` (src.page と src.table 必須、資料名・版・URL は datasheet:)、`assumed` (reason と calc = 計算式と代入値 が必須)、`board` / `design` (from 必須)。満たさないと読み込みで止まる。

## 出力
`temp_<typ|worst>_<placement|artwork>_<F|B>.png` (下部に詳細条件欄)、`thermal.md`、`parts_table.csv` (解析条件の部品表: 部品×項目の 値 typ / worst・区分・備考)、`thermal_<mode>.json` (worst の metrics で判定 + typ は info)。

## カラーバー
全ケース共通で固定 (既定 25〜95 ℃、inferno)。95 ℃ 以上は紫 (#9b30ff、カラーバー上端の三角) + 白斜線と白い等温線 (inferno 中間の紫と区別するため)、25 ℃ 未満は最下端色。thermal.yaml の `plot: {tmin_c, tmax_c, cmap, over_color}` で変更。

## 判定
推定値 + 誤差 (基板 15%、θ が仮定/逆算の部品 30%、最小 5 ℃) の **上側** で、≤ Tj上限−25 ℃ pass、≤ Tj上限 warn、超えれば fail。θ が仮定/逆算の部品は warn 止まり。

## 精度 (selftest、2026-10-14)
1D 一様発熱 (2 層) 0.00%、1D フィン 0.5%、点熱源 K0 解 0.4〜1.3%、放射のみ 0.00%。格子 0.5 → 0.25 mm で FT2232H rev0.2 の Tj 差 1.1〜1.3 ℃。
相関式・材料定数・部品熱データの誤差はこれに含まれない (docs/design.md 6 章)。Elmer FEM との比較は未実施。

### 格子形式 pcb-physics-grid/2 (2026-10-14 追加、IR ドロップ向け)
- `net_id[L,Ny,Nx]`: 0 = 銅なし、-1 = セル内に複数ネット (衝突)、k ≥ 1 = `meta.nets[k-1]`。`net_major` (多数決ネット)・`net_count` (セル内ネット数) も同梱。
- 同層隣接の接続: `edge_x_w/edge_x_net [L,Ny,Nx-1]`、`edge_y_w/edge_y_net [L,Ny-1,Nx]`。w = 境界を横切る銅の幅率 (0..1)、net = そのネット (別ネット同士は 0 で非接続、-1 は境界で 2 ネット)。導電率 G = σ(T)·t·w (σ = 1/ρ、t = `meta.layers[].cu_mm`)。熱側は従来どおり cu_frac の調和平均で、この配列は使わない。
- ビア: `via_x_mm, via_y_mm, via_drill_mm, via_net (1 始まり), via_l0, via_l1`。
- pad→セル: `pad_ref, pad_pin, pad_net, pad_layer, pad_x_mm, pad_y_mm, pad_area_mm2` と CSR `pad_cell_ptr[P+1]` → `padcell_j, padcell_i, padcell_area_mm2, padcell_w` (面積重み、pad ごとに和 1)。`PcbGrid.pad_cells(k)` で取り出せる。
- 孤立銅: `island[L,Ny,Nx]` (同ネット辺 + ビアの連結成分番号)。`meta.checks` = conflict_cells, min_track_mm, thin_tracks / thin_widths_mm (線幅 < 2·dx), zones_unfilled, islands (pad に繋がらない銅), split_nets, n_components。`meta.warnings` に日本語の警告文。
- 部品 YAML のピン許容電圧 (任意): `pins.<n>.v_range: {min, max, kind: recommended|absolute}`、値は basis 付き値オブジェクト (式 "VIN+0.3" 可)。例 data/parts/C500795.yaml (AP7361C p.4)。
- 細かい格子: `python -m core.gridtool <project> --dx 0.1` (格子だけ作り検査表示)。FT2232H rev0.2 artwork で 2×410×520、32 s。衝突 F.Cu 8 セル、細線 0、孤立銅 0。熱は既定 0.25 mm のまま。
- 熱の回帰テスト: 変更後も 4 ケース最大差 0 ℃、Tj 20 指標一致。

### 基板の製造条件 YAML (data/board/<基板>.yaml、2026-10-14 追加、作者 決定)
- 部品 YAML (data/parts) と並べて `data/board/<基板>-rev<版>.yaml` に置く。`order:` の項目名と選択肢は JLCPCB 注文画面 (PCB Specifications / High-spec Options) のまま。Base Material, Layers, PCB Thickness, Material Type, Surface Finish, Outer Copper Weight, Inner Copper Weight, Specify Layer Sequence, Specify Stackup, Layer Stackup, Impedance Control, Via Covering, Min via hole size/diameter, PCB Color, Silkscreen, Board Outline Tolerance。選択肢は core/boardspec.OPTIONS (出典 https://jlcpcb.com/capabilities/pcb-capabilities と銅厚ガイド、2026-10-14 確認)。項目名や選択肢にない値は読み込みで拒否する。
- 各項目は値オブジェクト `{value, basis, ...}`。決定済みは basis: design と from、未定は basis: assumed と reason・calc (例「JLC 注文画面の既定値を選んだと仮定」)。必須項目: Base Material, Layers, PCB Thickness, Outer Copper Weight, Specify Stackup。
- 層構成の優先順位: thermal.yaml の `stackup` (明示上書き) > 基板 YAML > .kicad_pcb の stackup > JLC 標準。thermal.yaml の `board:` で指定、省略時は `data/board/<project>*.yaml` を自動で探す。
- 銅厚: 1 oz = 35 µm (外層)。内層 0.5 oz = 15.2 µm (JLC stackup 表)。Specify Stackup = Yes で登録済みの名前 (JLC04161H-7628) はその寸法、未登録の名前は警告して等分。
- 食い違いの警告: 層数 (基板 YAML は使わず .kicad_pcb 側に戻す)、板厚、.kicad_pcb stackup の銅厚、FR-4 以外の材質。kthermal の表示、格子 meta.stackup_warnings / meta.warnings、PNG 詳細条件欄、部品表に出す。
- 出典の表示: PNG 詳細条件欄の「層構成の出典」、部品表 (parts_table.csv / thermal.md) の先頭に「基板」行 (層構成の出典 + 基板 YAML の全項目と区分・備考)。
- FT2232H rev0.2: `data/board/usb-jtag-ft2232h-rev0.2.yaml` (JLC 標準 2 層 1.6 mm 外層 1 oz を design、材質・表面処理・ビア処理・色などは assumed)。層構成は従来の JLC 標準と同じ (銅 35 µm×2、誘電体 1.510 mm)。熱の回帰テスト: 4 ケース最大差 0 ℃、Tj 20 指標一致。

### core 改善 (2026-10-14 09:30〜、IR ドロップ担当要望 + 作者 決定)
- **斜め配線の線幅ベース補正** (core/diag.py、`build(..., diag_corr=True)` が既定、artwork のみ): 同じ角度・線幅/dx・格子内の始点位置 (0.05 dx 刻み) の直線を小格子に同じ方法で描いて単位長さ抵抗を数値で解き、真値 1/(σtW) との比 k を出す。その配線だけが横切るエッジの `edge_*_w` に k を掛ける (w > 1 になりうる)。水平・垂直 (±1°) と pad・ゾーンと重なるエッジは補正しない。meta.diag_corr = {enabled, edges, method}。`gridtool --no-diag-corr` で比較。
  - 0.2 mm 線 dx 0.1 (tests/test_grid.py、始点 4 通り): 30° 補正なし +8.6〜+8.9% → −1.2〜+0.3%、45° +2.5〜+13.6% → −0.6〜+1.2%、135° +12.8〜+13.6% → −1.3〜−0.6%。45° は始点の位置で誤差が大きく変わるので位置ごとに解く。
- **格子作成の高速化**: ゾーンの塗りつぶしを matplotlib contains_points から行ごとのトグル累積 XOR (raster.fill_polygon、agg と同じ交差式で FT2232H の 0.25/0.1 mm とも 1 画素も違わない) に、cell_nets を bincount 化。FT2232H: dx 0.1 で 32 s → 3.8 s、dx 0.05 で 270 s (IR 担当測定) → 13 s・0.7 GB。熱の回帰 82 s (変化なし)。
- **熱データなしの部品 YAML**: θ も tj_max も無い (ピン電流・信号だけ) 部品 YAML を data/parts に置ける。熱側は pad 直下に発熱を置いて pad 温度だけ出し、判定は info (`parts.has_thermal`)。部品表の「部品熱データ」の備考にも書く。
- **signal:** (部品の正本を 1 ファイルに): `data/signal/parts/<LCSC>.yaml` の `signal:` (default / pins / datasheet / notes、値は値オブジェクト、pins の kind・note は文字列) を部品 YAML に置いても検査を通す (`parts.check_signal`)。中身は pcb-physics では使わない。取り込みはしていない (テストは bd287ae を git show で読んで一時ディレクトリで検査)。熱データと資料の版が違う時は `signal.datasheet` に書く (例 C27882: 熱 v2.21、signal v2.10)。
- data/board/usb-jtag-ft2232h-rev0.2.yaml: Impedance Control "No" を 作者 2026-10-14 決定 (費用のため) に変更。
- 熱の回帰テスト: 4 ケース最大差 0 ℃、Tj 20 指標一致。

### 熱求解の高速化・格子間の受け渡し・連成ドライバ (2026-10-14 12:20〜、IR ドロップ担当要望)
- **ソルバー** (thermal/model.py): `auto` (既定) = pyamg の smoothed aggregation (強い結合のしきい値 θ 0.08) を前処理にした CG。放射の反復で変わるのは対角だけなので AMG の階層は 1 回目だけ作り、CG は前回の温度から始め、途中は相対残差 1e-6・最後に 1e-10 で解き直す。`direct` (従来の SuperLU)・`cg` (Jacobi) も選べる (thermal.yaml `solver: {method: ...}`、`kthermal.py --solver`)。pyamg が無い環境は 20 万未満を直接法。pyamg は venv-thermal に入れた (5.3.0)。
- 計測 (FT2232H rev0.2 worst/artwork、1 ケース、tests/bench_thermal.py。box の load 4〜13 で計測):

| 格子 | 未知数 | 従来 (直接法) | 新 (AMG+CG) |
|---|---|---|---|
| 0.25 mm | 10.2 万 | 18〜22 s、最大 RSS 507 MB | 4.1 s、191 MB |
| 0.1 mm | 63.9 万 | 仮想メモリ 4.5 GB 制限で SuperLU が失敗 (IR 担当測定 7〜10 分・メモリ不足) | 59 s、739 MB (CG 計 71 回、AMG 準備 13 s) |

  0.1 mm の Tj は 0.25 mm より U1 +6.2 ℃ (104.7)、U3 +5.8 ℃ (103.4)。
- **回帰テスト**: `--solver direct` は ac64d1b と 0 ℃ 差 (許容 1e-6)。既定 auto は温度場の最大差 6.9e-5 ℃ (放射反復の収束判定 1e-3 ℃ 以内、Tj など 20 指標は表示桁で一致)。
- **格子間の受け渡し** (core/regrid.py): `temperature(src, src_meta, dst_meta)` = セル中心の双一次補間 (基板外は最寄りで埋める)、`heat(src, src_meta, dst_meta, dst_mask)` = 面積の重なりで配る保存的写像 (合計を保存、基板外に落ちた分は最寄りの基板内セル)。層は名前で対応。
- **連成ドライバ** (coupling/driver.py): 熱と IR を別の格子で (`--grid 0.25 --grid-ir 0.1`)、基板 YAML を渡す、最初の IR の銅温度 = そのケースの周囲温度 (従来 20 ℃ 固定)、収束 = 温度変化 < 0.1 ℃ かつ 降下の相対変化 < 0.1%、熱は前回の温度から解き始める (`build_and_solve(..., t_init=)`)。
- **一本化の方針 (案)**: 連成の本体は IR 側 `irdrop/couple.py` に寄せ、core は部品 (regrid・boardspec・t_init・ソルバー選択) を提供する。coupling/driver.py は IrSolver を差し替えられる簡易版として残し、couple.py が main に入った時点で README から couple.py を案内する。

### pitfall-4l (試験基板担当の正解付き 4 層基板) の熱の区画 (2026-10-14)
- examples/pitfall-4l/ (thermal.yaml の scenarios、run_pitfall.py、table.py、results.md)。data/board/pitfall-4l-rev0.1.yaml、data/parts/C6186.yaml (AMS1117、θ は assumed)。
- 主な結果 (熱 0.25 mm、IR 0.1 mm): LDO の差 U2 − U1 = 11.4 K (expected 11) は一致、絶対値は U1 99 / U2 111 ℃ (expected 62 / 73)。抵抗の pad 温度上昇 dense 80 K / spread 83 K (expected 38 / 23)、R19 − R20 = −2.4 K (expected +4)。細線・連成の抵抗 (20 ℃) は 49.10 / 122.2 mΩ で手計算 (49.1 / 122.9) と一致、配線の温度上昇は IPC-2221 より小さい (TH-1A +3.0 K、CP-1A5 最高 36.2 ℃ → 降下 +6.0%、expected +21%)。
- 差の主因 (解析側の見立て): expected の板モデルは pad → L2 のプリプレグ (0.21 mm) の熱抵抗 (pad 直下で 20〜27 K) と基板の端・L3 の分割を入れていない。spread の R18 と relief の R20 は基板の端に近く、端の効果が密集/リリーフの差より大きい。配線は 0.21 mm 下の L2 GND に熱が逃げるので IPC-2221 (外層単独) より大幅に冷える。

## 20. GPU バックエンド / 比誘電率 / 重なった pad の別名 (2026-10-14)

- `--solver gpu`: CuPy の `cupyx.scipy.sparse.linalg.cg` に、pyamg の階層を GPU に載せた AMG V サイクル (Jacobi 平滑化) を前処理として渡す。CuPy か GPU が無ければ CPU (auto) に戻る。`gpu-emu` は同じコードを numpy で動かすテスト用。
- `amg` (既定) は Jacobi V サイクルになり、旧版より約 3 倍速い (FT2232H 0.1 mm で 59 s → 19 s)。旧 Gauss-Seidel 版は `amg-gs` として残している。
- `tools/bench_wsl.sh`: WSL+CUDA で計測する。venv 作成 → pip → clone (pcb-physics、usb-jtag-ft2232h) → test_solver → dx 0.25/0.1 × amg/gpu を順に実行 → `bench_<host>_<date>.json`。
- 格子 meta:
  - `meta.layers[i].er_below/er_below_src/h_below_mm` と `meta.dielectric` に比誘電率を入れた。値は .kicad_pcb の epsilon_r を優先し、無ければ JLC 標準値 (7628 プリプレグ 4.4 / コア 4.6、https://jlcpcb.com/impedance) を使う。
  - `pad_alias` 配列と `meta.pad_aliases` を追加した。同じ層で銅が重なる pad をグループにし、`J1.A1=B12` のような別名を付ける。別ネット同士の重なりは warning になる。
  - pad の中心座標は `pad_x_mm` / `pad_y_mm` (既存)。

## 21. 3D モデルの箱のキャッシュ (2026-10-14)

- **何が起きたか:** WSL では U1 の Tj が 4 ℃ 高く出た (0.25 mm で 102.77 / box は 98.45)。原因は KiCad の 3D ライブラリ (STEP) が無いことで、部品上面の面積が 0 になり、上面からの放熱経路が消えていた。
- **対策:**
  - `data/model3d/bbox.json` に STEP から取った箱 (生の値) を入れた。STEP が無いときはこれを使う。
  - どちらも無い部品は `info.model3d_missing` に入れて警告を出す。bench の JSON にも `model3d_source` / `model3d_missing` を出す。
  - 追加するときは STEP があるマシンで `python tools/model3d_cache.py <KiCad プロジェクト>` を実行する。
- **bench:** `bench_thermal.py --repeat N` で同じプロセスで N 回解き、最後の回を採用する (GPU の JIT を除くため)。bench_wsl.sh は `--repeat 2` で実行し、`cupy-cuda12x[ctk]` を入れて `LD_LIBRARY_PATH=/usr/lib/wsl/lib` を設定する。

## 22. 面内伝導の edge 方式 (2026-10-16)

- **動機:** 0.25 mm 格子ではサーマルリリーフの隙間 (0.25 mm) が 1 セル以下になり、セルの銅面積率 (旧 `cell` 方式) だとベタ直結に近い熱伝導になる。細い 45° 配線も同様。
- **方式:** `core/raster.path_conduct` (= `edge_conduct`、熱は `k=None`) でセル中心間の経路の銅の割合を直列 (調和平均) にする。格子配列 `edge_x_cu` / `edge_y_cu`、`meta.edge_cu.trim=True`。`--inplane edge|cell` (既定 `edge`、`thermal.yaml` の `solver.inplane` でも可)。
- **端の空の列の除外 (同日夕 f5c2ded):** 経路の両端で銅が無い列は除く。銅がセル中心に届かない縁のセルが切り離されない。空の端の向こうに銅があればセル内の隙間として 0 (隙間の短絡を防ぐ)。IR 側の `edge_conduct_net` と同じ共通部。
- **効果 (FT2232H 807dc8e worst artwork):** U1 0.25/0.1 = 103.5/102.9 ℃ (端の除外なしの edge は 105.0/103.4、旧 cell は 95.2/102.4)。d495b2f の見かけの熱だまり (基板最高 94.6 ℃) は解消。手計算テスト `tests/test_thermal_edgecond.py`。
