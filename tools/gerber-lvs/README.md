# gerber-lvs — 製造データ (Gerber/drill) の銅の導通を KiCad の正解接続と比べる

製造データ (銅層 Gerber + Excellon、補助で Paste) だけから実際の銅の導通を求める。
それを KiCad の正解接続 (`kicad-cli pcb export ipcd356`) と **pad 単位で** 比べ、ショート・オープンを出す。
DRC の schematic parity は `.kicad_pcb` しか見ないので、zone の fill を保存し忘れて古い fill のまま
gerber を出した、といった「出来上がった gerber の誤り」は分からない。このツールは gerber を独立に検査する


- system `python3` + numpy + Pillow だけで動く (pcbnew SWIG・shapely・scipy は不要)。KiCad 10.0.7 の出力で確認。
- 元の `.kicad_pcb` は読むだけ。IPC-D-356 は一時ディレクトリにコピーしてから出す。
- 速さ: 2 層 54×36 mm で約 1 s、4 層 92×77 mm で約 3.5 s、最大メモリ約 210 MB (10 µm/px)。

| ファイル | 内容 |
|---|---|
| `gerber_lvs.py` | コマンド本体 (検査、txt/json/PNG の出力) |
| `gerberlib.py` | Gerber (RS-274X/X2) / Excellon / IPC-D-356 の読み込み、ラスタ化、run 単位の連結成分 |
| `tests/run_tests.py` | 回帰テスト (正常 4 基板、refill 前の FT2232H、故障注入、合成基板) |
| `tests/testdata.py` | 実基板テストデータの場所 (`tests/data/`) と gerber の用意 (gerber-drc のテストからも使える) |
| `tests/regen_testdata_gerbers.sh` | 控えの `.kicad_pcb` から `tests/data/*/<基板>/gerber/` を出し直す (refill なし) |
| `tests/data/` | 実基板の控え (`current/`、`pre-refill-20261009/`)。由来は `tests/data/README.md` |

## 使い方

```sh
T=tools/gerber-lvs   # リポジトリのルートから
python3 $T/gerber_lvs.py <project_dir> [<name>]          # <dir>/out/gerber (無ければ最新の out/<rev>/gerber) を検査
python3 $T/gerber_lvs.py <project_dir> <name> --png      # error の切り抜き PNG も出す
python3 $T/gerber_lvs.py --gerber-dir G --d356 X.d356 [--pro P.kicad_pro] --out-dir O   # PCB を使わない場合
```

- 出力は gerber ディレクトリの親 (`out/` か `out/<rev>/`) に `gerber_lvs.txt` (日本語)、`gerber_lvs.json`、`gerber_lvs/NN_<code>_<layer>.png` (`--png` を付けたとき)。
  PNG の切り抜きは最大 40 mm 角。pad どうしが 40 mm より離れた OPEN/SHORT は印 (`at`) の周りで切り、finding に `png_crop` ([x0, y0, x1, y1]) を付ける。PNG の作図で例外が出ても txt/json は出し、`notes` に残す (終了コードは検査結果どおり。2026-10-16)。
- 終了コード: **0** = error 0 件、**1** = error あり (`--strict` を付けると warning も 1)、**2** = 実行できなかった (ファイルが無い、未対応の構文、drill と D356 の穴位置が合わない など)。
- `--json PATH`: JSON の出力先を指定する (`--out-dir` が無ければ txt と PNG も同じディレクトリ)。JSON には kreview F09 互換の `status` (`ok`/`fail`)・`shorts`・`opens` も入る。
  kreview から使うときは `KREVIEW_LVS_CMD="python3 tools/gerber-lvs/gerber_lvs.py"` を設定する (kreview は `--json <out>/lvs.json <project_dir>` を付けて呼ぶ)。
- 座標は `gerber(x, y)` (Gerber の座標。aux 原点基準で Y は上向き) と `kicad(x, y)` (PCB エディタの座標) の両方で出す。
- 規則は `.kicad_pro` の `min_copper_edge_clearance` と `min_hole_clearance` を使う。`--edge-clearance` / `--hole-clearance` で上書きできる。
  ラスタの誤差 (許容誤差 2 px + 5 µm = 0.025 mm) を差し引いて判定する。
- `--px` で解像度を変えられる (既定 0.01 mm/px)。

## DRC 系の補助検査の移設 (2026-10-14、作者承認)

LVS (ネットリストとアートワークの等価性) に絞るため、幾何の検査 `COPPER_OVER_NPTH` / `NPTH_CLEARANCE` / `EDGE_CLEARANCE` /
`COPPER_OUTSIDE_BOARD` / `PASTE_WITHOUT_COPPER` / `OUTLINE_OPEN` は `tools/gerber-drc/gerber_drc.py` に移した
(gerberlib はこのリポジトリのものを参照して使う)。

- gerber-lvs の既定は **補助検査なし** (`--no-aux`)。旧動作は `--aux`。JSON に `aux_checks` (true/false) を出す。
- kreview は F09 = gerber-lvs、F11 = gerber-drc。
- 移設の同一性は gerber-drc のテストで確認 (gerber-lvs `--aux` と検出が完全一致: 正常 4 枚 0 件、refill 前 FT2232H 12 件)。
- 下の表の補助検査の行は `--aux` のときだけ出る。

## 検査項目

| code | 重大度 | 内容 |
|---|---|---|
| `SHORT` | error | 1 つの銅の島に別ネットの pad が載っている。X2 の `%TO.N` があれば接触点も出す |
| `OPEN` | error | 同じネットの pad が別々の島に分かれている (`unconnected-*` と `N/C` は対象外) |
| `NO_COPPER_AT_PAD` | error | SMD pad の位置にその層の銅が無い |
| `MISSING_HOLE` | error | スルー pad / via の位置にめっき穴が無い (via の消失。ステッチ via のようにネットが切れない場合もこれで分かる) |
| `NO_COPPER_AT_HOLE` | error | めっき穴はあるが、どの層にも銅が無い |
| `COPPER_OVER_NPTH` | error | NPTH の穴の中に銅がある (穴あけ前の銅で判定) |
| `EDGE_CLEARANCE` | error | 銅と Edge_Cuts (外形・内部の切り欠き) の距離が規則より小さい。1 mm の塊ごとに最小値を出す |
| `COPPER_OUTSIDE_BOARD` | error | 外形の外、または切り欠きの中に銅がある |
| `NPTH_CLEARANCE` | warning | NPTH の穴縁から銅までが `min_hole_clearance` より小さい |
| `PASTE_WITHOUT_COPPER` | warning | paste の開口の下に銅が無い |
| `EXTRA_HOLE` / `MISSING_NPTH` / `HOLE_SIZE_MISMATCH` | warning | drill と IPC-D-356 で穴が合わない |
| `PAD_AMBIGUOUS` / `NC_PAD_CONNECTED` / `NET_CASE_COLLISION` / `OUTLINE_OPEN` | warning | 判定があいまい、またはネット名の問題 |
| `LAYER_MISSING` | error | 銅層 Gerber の欠落 (FileFunction の層番号に抜け、または drill の層範囲より銅層が少ない)。2026-10-14 追加 |
| `LAYER_NAME_MISMATCH` | error | ファイル名 (F_Cu/In<n>_Cu/B_Cu) と中身の `%TF.FileFunction,Copper,L<n>` が合わない (層の取り違え)。2026-10-14 追加 |
| `PAD_COPPER_INCOMPLETE` | warning | SMD pad の中心の内接正方形 (一辺 = 短辺/√2) の銅が 90% 未満 (pad が消えて配線の端だけ残った / 別の層に出た)。via-in-pad は除外。2026-10-14 追加 |
| `LAYER_OFFSET` | error | 1 つの銅層だけ pad/ランドの flash が drill の穴から同じ向きにずれている (その層だけ原点・座標形式が違う)。ずれ量も出す。2026-10-14 追加 |
| `GBRJOB_MISMATCH` | error / warning | `.gbrjob` と合わない: 層数、job にある銅層・外形のファイルが無い、層機能 (FileFunction) が中身と違う、銅層が負極性 (error)。銅以外のファイルが無い・銅層が job に載っていない (warning)。2026-10-14 追加 |
| `X2_NET_DIFFERS` | info | pad の正解のネットが、その銅の Gerber X2 ネット属性 (`%TO.N`) に無い (gerber が古い・ネット名変更の反映漏れの説明用)。SHORT には `x2_nets`、OPEN には `x2_nets_per_group` も付ける。**正解には使わない** (検査対象自身の属性なので)。2026-10-14 追加 |
| `FLOATING_COPPER` | info | どの pad にもつながらない銅の島 (大きい順に 10 個まで) |

## 方式

1. **正解**: `kicad-cli pcb export ipcd356`。317 = スルー (穴径・PTH/NPTH 付き)、327 = SMD、367 = NPTH、`VIA` レコードあり。
   access コードは銅層の番号 (A01 = 表、2 層の裏 = A02、4 層の裏 = A04、A00 = スルー)。
   ネット名は 14 文字までで大文字になる。長い名前は `NNAME` の別名になるので、別名を元の名前に戻してから大文字小文字を無視して比べる。
2. **原点**: drill の穴と D356 のスルー pad/via の位置を突き合わせて、ずれを多数決で求めて補正する (make_out.sh の出力ではずれ 0)。
   一致が半分未満なら別の基板とみなして終了コード 2。
3. **銅**: 各層を 10 µm/px で描画する (G36 region の cut-in、RoundRect などのマクロ、円弧、LPC に対応)。
   NPTH の検査をしてから全穴 (PTH/NPTH、G85 と M15/M16 の長穴) を抜く → 行ごとの run にして 4 近傍で union (numpy でベクトル化)。
   画像は帯 (512 行) ごとに読んで run だけを残す。
4. **層間**: めっき穴の壁 (半径 d/2 + 2 px) に触れる各層の島を、その穴を介して 1 つにまとめる。drill の FileFunction の層範囲を見るので、ブラインド/ベリードの drill ファイルにも対応する。
   NPTH は抜くだけ (つながない)。サーマルリリーフは KiCad が zone の region として出すので、特別扱いは要らない。
5. **pad の対応**: SMD は access 層の pad 中心の島。中心に銅が無ければ (via in pad など) pad 寸法の 0.25 倍、0.45 倍の円で探す。スルー/via は同じ位置 (±0.02 mm) のめっき穴。
6. **外形**: Edge_Cuts の線分をつないで閉路を作り、入れ子の深さで外形と切り欠きを塗り分ける。外形 clearance は、帯の中の銅画素から外形線分までの距離を厳密に計算する。
7. **short の位置**: Gerber X2 のネット属性 (`%TO.N`) で「そのネットの銅」と「ほかの銅」を描き分け、両者が接する画素を出す。
   **X2 の属性は正解には使わない** (検査対象そのものなので)。位置の説明に使うだけ。

## テスト

```sh
python3 tests/test_units.py           # 高速スイート (KiCad 不要、約 3 s): Gerber 構文の単体・Excellon/D356・手書き合成基板の LVS
python3 tests/run_tests.py            # 完全スイート (約 2.5 分、高速スイートも含む)。--quick で件数を減らす、--only units normal old inject input synth で選ぶ
GLVS_PROJECTS=<案件の親ディレクトリ> python3 tests/run_tests.py --only normal   # 今の案件の gerber で正常確認
```

- 既定のデータはリポジトリ内の `tests/data/current/<基板>/` (4 基板の 2026-10-09 11:48 の控え。案件は作業中に動くので控えを使う)。
  refill 前の FT2232H は `tests/data/pre-refill-20261009/usb-jtag-ft2232h/` (`GLVS_OLD_FT` で上書き)。場所は `tests/testdata.py` にまとめてある
  (`GLVS_DATA` で tests/data ごと、`GLVS_PROJECTS` で 4 基板のディレクトリを差し替え)。
- git に入れるのは控えの `.kicad_pcb` / `.kicad_pro` / `ref.d356` だけ。gerber/drill は `<基板>/gerber/` に生成して `.gitignore`。
  無ければ `run_tests.py` が `tests/regen_testdata_gerbers.sh` で自動で出し直す (`GLVS_NO_REGEN=1` で止める)。手で出すなら
  `bash tests/regen_testdata_gerbers.sh [--force]`。
  設定は make_out.sh と同じで **zone の refill はしない** (refill 前の FT2232H の古いベタを再現するため)。控えの `.kicad_pcb` は書き換えない。
- 故障注入 (gerber のコピーを改変):
  - 配線区間を 1 本消す → `OPEN`
  - 近い別ネット pad 間に 0.15 mm の線、または丸い銅を足す → `SHORT`
  - via の穴だけ、または穴と via pad を消す → `MISSING_HOLE`
  - 部品のスルー穴を消す → `MISSING_HOLE`

  配線削除は、**消す区間の中心線がほかの銅 (pad の角丸や重なった配線) で全部覆われているものは「幾何的につながったまま」として先に除外し、件数を記録する**。
- 合成基板 (`make_synth.py` でコピーを改変して KiCad で出力。`make_synth.py` は KiCad の pcbnew を使うため本リポに含めない。自分で用意して `GLVS_MAKE_SYNTH` と `GLVS_APPRUN` で渡したときだけ実行し、無ければ省略):
  - C1 を裏返した 2 層/4 層 → access コードの確認
  - 1.5 mm 角の内部切り欠き → 正しく refill した出力は error 0、Edge_Cuts だけ新しい古い gerber は `COPPER_OUTSIDE_BOARD`

- 高速スイート `tests/test_units.py` + `tests/synthboard.py` (2026-10-14 追加): Gerber/drill/IPC-D-356 を手書きで作るので、正解と製造データを別々に壊せる。
  - 構文: 各 aperture (C/R/O/P/マクロ)・線・region・G02/G03/G75 円弧・LPC・穴付き aperture・インチ単位を既知の面積/画素で比較、未対応構文 (SR/G74/thermal/LM/G91/FSTAX/%MO なし) が Unsupported になること
  - 合成基板: 配線削除・30µm 切れ目・別ネット接触・pad 削除・ベタ削除・via 穴/ランド削除・サーマル切断・裏面 pad を表に出力・層の欠落/取り違え・片層の原点ずれ・drill 単位・インチ Gerber、ブラインド (L1-2)/ベリード (L2-3) via、外形 clearance ちょうど 0.2 mm / 0.15 mm
  - 既知の限界 (10µm の切れ目はつながって見える) もテストで固定し、変わったら気づくようにしている
  - 回帰 (2026-10-16): 端点が 40 mm 以上離れた未配線の OPEN (横 46 / 縦 43 / 斜め 58 mm) で `--png` あり/なしとも txt/json (と PNG) を出して exit 1。旧版は PNG の切り抜き範囲が負になり `negative dimensions` で止まっていた (usb-jtag-ft2232h g3-plan1 0eb71b1 の GND 9 分割で発生)
- 入力の整合 (完全スイート、4 層 fpga-voice-recorder の gerber を壊す): 内層/B.Cu の削除 → `LAYER_MISSING`、内層 2 枚の入れ替え → `LAYER_NAME_MISMATCH`、drill のインチ書き直し → error 0、インチの数値を METRIC 宣言 → 実行不可、pad flash の削除 → 検出

2026-10-09 の結果は 62/62 PASS。2026-10-14: 完全スイート (高速スイート込み) は下の commit の時点で全 PASS。 2026-10-16: 遠い OPEN の PNG 修正後、高速 118/118・完全 192/192 PASS。

## 既知の限界

- ラスタの誤差: Pillow の塗りで銅が約 1 px (10 µm) 太る。隙間が 10 µm 未満だと、接していなくても `SHORT` と判定することがある (実際の最小 clearance は 0.1 mm 以上)。
  clearance は 0.025 mm の許容誤差を差し引くので、規則より 25 µm 以内の違反は出ない。
- 銅以外の層 (mask・silk) は見ない。CPL も使っていない (部品の位置ずれは対象外)。
- 未対応: Gerber の `SR` (step and repeat)、`LM/LR/LS`、G74 (single quadrant) の円弧、マクロのプリミティブ 7 (thermal)、小数点の無い Excellon 座標。
  未対応の構文があれば終了コード 2 で止まる (黙って間違えない)。
- (2026-10-14 修正) 穴付き aperture・マクロの露光オフは aperture の中だけを抜く (以前は下の銅まで消し、clear 極性では穴に銅を描いていた)。インチ単位の Gerber で aperture の寸法を mm に換算していなかったのも修正。
- pad 位置は IPC-D-356 の pad 中心で島を決める (pad の欠けは `PAD_COPPER_INCOMPLETE` で中心部だけ見る。凹形状のカスタム pad は誤検出の可能性)。中心にも周りにも銅が無いカスタム形状の pad は `NO_COPPER_AT_PAD` になる。
- ネット名の大文字小文字だけが違うネットは区別できない (`NET_CASE_COLLISION` で警告する)。
- open/short は pad (と via) の単位で見る。pad につながらない配線の切れ (stub) や浮き銅は `FLOATING_COPPER` (info) で出すだけ。

## カバレッジ (行カバレッジ)

```sh
sh tests/coverage.sh          # 高速 + 完全 + gerber-drc のテスト。subprocess 分も含める (sitecustomize + COVERAGE_PROCESS_START)
sh tests/coverage.sh --quick  # 高速スイート + gerber-drc --quick だけ
```

対象は gerber_lvs.py / gerberlib.py / gerber-drc の gerber_drc.py。スイートごとの固有行 (ほかのスイートで通らない行) も出す (`tests/cov_unique.py`)。
2026-10-14: 94% → **100%** (1366 行)。方針 (作者): 目標 95%。未カバー行は「記述起因で到達不能 (書き換える、無理なら行末に `# COV:論理的未到達`)」
「環境都合 (行末に `# COV:(理由)により到達困難`)」「シナリオ不足 (テストを足す)」に分けて扱う。今は注記の付いた行は無い。
