# data/signal — 信号特性 (ドライバの立ち上がり時間 tr・最大周波数・出力抵抗) の表

kreview のリターンパス点数 (P20/P21、`tools/review/return_path.py`) が使う。

## 形式 (熱解析 bot の部品熱データにそろえる)

熱解析 (pcb-physics、旧 kicad-thermal) の `data/parts/<LCSC>.yaml` と **同じ値オブジェクトの書き方**・**同じ置き場所の方針**
(部品共通は LCSC 番号ごとの YAML、案件ごとの値は基板ごとの YAML) にしている。

- 数値は必ず値オブジェクト `{value, basis, ...}`:
  - `basis: datasheet` → `src: {page, table, item}` が必須 (資料名・版・URL はファイル頭の `datasheet:`)。**確度 = 正本**
  - `basis: assumed` → `reason` (どう仮定したか) と `calc` (計算式と代入値、または値の範囲) が必須。**確度 = 仮**
  - `basis: design` → `from` (規格・仕様書・review.toml の項目)。**確度 = 正本** (ページが無いものは仮)
  - `basis: board` → `from` (.kicad_pcb / ネットリストのどこから。ツールが自動で作る行)
- 見つからない値は書かずに推定しない。推定するなら `basis: assumed` で根拠と計算を書く (ロジックのファミリから推定、など)。

| ファイル | 中身 |
|---|---|
| `parts/<LCSC>.yaml` | 部品共通の出力特性。`signal.default` (全出力ピン) と `signal.pins.<ピン番号>` (上書き、`kind: usb_hs` などでファミリを指す) |
| `families.yaml` | 部品 YAML が無い時の推定 (ロジック・MCU・FPGA・USB のファミリ)。`match` は部品の値/ライブラリ名の正規表現。全部 `basis: assumed` か規格値 |
| `boards/<基板>.yaml` | ネットごとの表 (ツールが生成: ドライバ部品とピン、tr、最大周波数、直列抵抗、出典、確度)。`overrides:` に人が書いた行が最優先 |

生成: `python3 tools/review/signal_table.py <project_dir> --save` (→ `boards/<基板>.yaml`。overrides は残す)。
部品 YAML の `signal:` は、熱データ (`thermal`/`theta`/`supply`) と同じファイルにまとめられるよう、キーがぶつからない名前にしてある。
