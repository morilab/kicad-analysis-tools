# kicad-analysis-tools

KiCad 10 の基板設計を **解析・検査** するための Python ツール集です。回路図の作図ルール検査、PCB 配置の採点、統合レビュー (kreview)、
製造データ (Gerber/drill) の LVS・DRC、差動インピーダンス計算、熱・IR ドロップ・PDN 解析を含みます。

- KiCad の `pcbnew` (SWIG) モジュールは **import しません**。`.kicad_sch` / `.kicad_pcb` は S 式として直接読み、必要なときだけ `kicad-cli` を別プロセスで呼びます。
- ライセンス: MIT (`LICENSE`)。
- 日本語のツールです (メッセージ・レポートは日本語)。

> **English summary** — A collection of analysis / verification tools for KiCad 10 projects: schematic style lint (`kicad-schlint`),
> PCB placement scoring (`pcb-score`), an integrated design-review runner (`review` / kreview), Gerber-vs-netlist LVS (`gerber-lvs`),
> Gerber geometric DRC (`gerber-drc`), differential-impedance / USB HS signal-integrity helpers (`si`), schematic revision history sync (`revhist`),
> and board physics (thermal, IR drop, PDN; `pcb-physics`). The tools read KiCad files directly as S-expressions and call `kicad-cli` as an
> external program; they do **not** import KiCad's `pcbnew` module. Messages and reports are in Japanese. MIT licensed.

## ツール一覧

| ディレクトリ | 内容 |
|---|---|
| [`tools/kicad-schlint/`](tools/kicad-schlint/README.md) | 回路図 (`.kicad_sch`) の作図ルール検査 (ERC では見ない読みやすさ)、回路図どうしの接続比較 (netcmp)、指摘入り PNG |
| [`tools/pcb-score/`](tools/pcb-score/README.md) | PCB 配置の採点 (密度・配線長・部品間隔など) と干渉検査、比較スコア |
| [`tools/review/`](tools/review/README.md) | kreview: 回路図・設計・PCB・実装性・製造データをまとめて定量レビュー (A〜D 採点、kreview.json / kreview.md) |
| [`tools/gerber-lvs/`](tools/gerber-lvs/README.md) | Gerber/drill だけから銅の導通を求め、`kicad-cli` の IPC-D-356 と pad 単位で比較 (ショート・オープン) |
| [`tools/gerber-drc/`](tools/gerber-drc/README.md) | Gerber/drill の幾何検査 (NPTH 上の銅、外形 clearance、paste など)。ルール YAML → `.kicad_dru` 変換 |
| [`tools/si/`](tools/si/README.md) | 差動インピーダンスの 2D 断面計算、条件の総当たり表、ngspice による USB 2.0 HS のアイパターン・TDR |
| [`tools/revhist/`](tools/revhist/README.md) | `CHANGELOG.md` → 回路図の改版履歴テキスト・title_block の書き写しと一致検査 |
| [`tools/pcb-physics/`](tools/pcb-physics/README.md) | 基板の物理解析: 熱 (`thermal/`)、IR ドロップ (`irdrop/`)、熱と IR の連成 (`coupling/`)、PDN インピーダンス (`pdn/`) |

付属データ:

- `testboards/pitfall-4l/` — 正解 (期待値) 付きの 4 層試験基板 (自作、発注しない)。pcb-score・pcb-physics・kreview のテストと例に使う。
- `tools/gerber-lvs/tests/data/` — 自作の見本基板 4 枚 (usb-jtag-ft2232h・rp2040-keypad10・esp32s3-mcu-board・fpga-voice-recorder) の控え。gerber-lvs / gerber-drc のテストに使う。
- `data/signal/` — 部品の信号特性 (立ち上がり時間など) の YAML。kreview の信号特性表・リターンパス評価が使う。
- `tools/pcb-physics/data/` — 部品の熱・電気特性 YAML、基板の製造条件 YAML。

## インストール (Ubuntu 24.04 / KiCad 10.0.8 / Python 3.12)

```sh
# KiCad 10 (kicad-cli と標準ライブラリ)。公式 PPA の例:
sudo add-apt-repository ppa:kicad/kicad-10.0-releases
sudo apt update && sudo apt install kicad
# SPICE (tools/si の usb_hs_spice.py、tools/pcb-physics の pdn が使う)、PNG 変換 (任意)
sudo apt install ngspice librsvg2-bin

git clone https://github.com/morilab/kicad-analysis-tools.git
cd kicad-analysis-tools
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

- KiCad のライブラリが標準以外の場所にある場合は `KICAD_SHARE` (例: `/usr/share/kicad`)、`KICAD10_SYMBOL_DIR`、`KICAD10_3DMODEL_DIR` を設定します。
- `kicad-cli` がライブラリ表 (`~/.config/kicad/10.0/`) を別の HOME で使う場合は `KICAD_CLI_HOME` で指定します (未設定なら今の HOME)。
- kicad-schlint・revhist・gerber-lvs の大部分は Python 標準ライブラリ + numpy / Pillow だけで動きます。

## 使い方 (例)

```sh
# 回路図の作図ルール検査 (ERC + schlint + PNG)
tools/kicad-schlint/check.sh path/to/board.kicad_sch
python3 tools/kicad-schlint/schlint.py path/to/board.kicad_sch --json schlint.json

# PCB 配置の採点
python3 tools/pcb-score/pcb_score.py path/to/board.kicad_pcb

# 統合レビュー (プロジェクトは読むだけ。結果は --out に)
python3 tools/review/kreview.py path/to/project --gate G2 --out /tmp/kreview

# 製造データの LVS / DRC (<project>/out/gerber を検査)
python3 tools/gerber-lvs/gerber_lvs.py path/to/project board --png
python3 tools/gerber-drc/gerber_drc.py path/to/project board

# 差動インピーダンス (2 層 1.6 mm、線幅 0.3・間隔 0.15・GND 0.2)
python3 tools/si/zdiff2d.py --h 1.53 --w 0.3 --s 0.15 --g 0.2

# 熱解析 (同梱の試験基板、約 2.5 分)
python3 tools/pcb-physics/kthermal.py testboards/pitfall-4l --cond tools/pcb-physics/examples/pitfall-4l/thermal.yaml --mode artwork --grid 0.25 --out /tmp/thermal
```

詳しい使い方・出力・判定基準は各ツールの README を見てください。

## テスト

```sh
python3 tools/kicad-schlint/tests/run_tests.py        # kicad-cli があればネットリスト照合も (約 15 秒)
python3 tools/pcb-score/tests/test_score_profiles.py
python3 tools/revhist/tests/test_revhist.py
for t in tools/review/tests/test_*.py; do python3 $t; done
python3 tools/gerber-lvs/tests/test_units.py          # KiCad 不要 (約 3 秒)
python3 tools/gerber-lvs/tests/run_tests.py           # 見本基板 4 枚で正常・故障注入 (kicad-cli が要る、数分)
python3 tools/gerber-drc/tests/run_tests.py --quick   # 合成基板とルール YAML (約 2 秒)。--quick なしで全件 (約 10 分)
python3 tools/si/zdiff2d.py --selftest
(cd tools/pcb-physics && for t in analytic data grid irdrop_analytic irdrop_couple parts_series_r pdn_analytic solver thermal_edgecond; do python3 tests/test_$t.py; done)
python3 tools/pcb-physics/tests/test_outputs.py      # 同梱の試験基板で kthermal の出力一式 (約 10 分)
```

`tools/pcb-physics/tests/test_regression.py` は usb-jtag-ft2232h の特定版の基板が要るため、`--project` を渡したときだけ動きます。

## 含めていないもの

- KiCad の `pcbnew` モジュールを使うスクリプト (基板の生成・JSON 書き出しなど)。GPL の KiCad と結合するため、MIT の本リポには入れていません。
  該当機能 (`annotate_issues.py` の入力 JSON の作成、gerber-lvs の合成基板テスト) は、必要なら各自で用意してください。
- openEMS (GPL-3.0) を使う 3D 電磁界の検証スクリプト。
- KiCad 標準ライブラリ (シンボル・フットプリント・3D モデル、CC-BY-SA 4.0) のファイル。テストで使うシンボルは実行時に手元の KiCad から読みます。
- メーカーの SPICE モデル (`tools/pcb-physics/data/parts/models/README.md` に取得元)。
- ドキュメント中の `skills/…`・`notes/…` は作者の設計メモ (非公開) への参照です。

## サードパーティ

| 依存 | ライセンス | 使い方 |
|---|---|---|
| numpy / scipy | BSD-3-Clause | import |
| matplotlib | Matplotlib License (PSF 系) | import |
| Pillow | MIT-CMU (HPND) | import |
| PyYAML | MIT | import |
| pyamg (任意) | MIT | import |
| KiCad (`kicad-cli`) | GPL-3.0+ | 別プロセスとして実行するだけ (import しない) |
| ngspice | BSD-3-Clause (一部 modified BSD) | 別プロセスとして実行するだけ |

テスト用の基板ファイル (`.kicad_pcb`・`.kicad_sch`) には KiCad 標準ライブラリ由来のフットプリント・シンボルが埋め込まれています。
KiCad ライブラリのライセンス (CC-BY-SA 4.0) には、ライブラリを使って作った設計データへの例外条項があります。
