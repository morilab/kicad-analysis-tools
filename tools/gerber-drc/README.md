# gerber-drc — 製造データ (Gerber/drill) の幾何の検査

2026-10-14 に gerber-lvs (tools/gerber-lvs) から DRC 系の補助検査を移した (作者承認)。
gerber-lvs は「ネットリストとアートワークの等価性 (LVS)」に絞り、幾何はこちらで見る。
KiCad の DRC (kreview P04) は `.kicad_pcb` を見るが、これは**出来上がった gerber/drill** を見る (fill を保存し忘れた古い gerber など)。

| code | 重大度 | 内容 |
|---|---|---|
| `COPPER_OVER_NPTH` | error | NPTH の穴の中に銅 |
| `NPTH_CLEARANCE` | warning | NPTH の穴縁から銅まで < `.kicad_pro` の `min_hole_clearance` (無ければ 0.2 mm) |
| `EDGE_CLEARANCE` | error | 銅と外形の距離 < `min_copper_edge_clearance` (無ければ 0.2 mm) |
| `COPPER_OUTSIDE_BOARD` | error | 外形の外・切り欠きの中の銅 |
| `PASTE_WITHOUT_COPPER` | warning | paste 開口の下に銅が無い |
| `OUTLINE_OPEN` | warning | Edge_Cuts が閉じていない (外形外の検査は省略) |
| `HOLE_SIZE` | YAML | ドリル径が工場の最小/最大の外 (ルール YAML がある時だけ) |
| `HOLE_DIAMETER_SET` | YAML | 指定の径以外 (例 スタッド穴は Φ2、footprint は .kicad_pcb から) |

ラスタ (10 µm/px) の誤差として 0.025 mm を差し引いて判定する (gerber-lvs と同じ)。

## ルール YAML (2026-10-14)

`rules/` (factory / user / project の層構成、後勝ち) とプロジェクトの `gerber_drc_rules.yaml` を読む。`--rules F` で明示、`--no-rules` で従来動作。
`rules2dru.py` で KiCad の `.kicad_dru` に書き出す (make_out.sh が毎回 `--project` で呼ぶ。手書きの .kicad_dru は上書きしない)。詳細・決定事項は [DESIGN.md](DESIGN.md)。YAML 由来の追加の外形検査は `EDGE_CLEARANCE` に `rule_id` が付く。

## 使い方

```sh
python3 tools/gerber-drc/gerber_drc.py <project_dir> [<name>] [--gerber-dir G] [--out-dir O] [--json J] [--png] [--strict] [--rules R.yaml] [--no-rules]
python3 tools/gerber-drc/gerber_drc.py --gerber-dir G [--pro P.kicad_pro] [--pcb P.kicad_pcb] --out-dir O
```

- 出力: `gerber_drc.txt` (日本語) / `gerber_drc.json` (`status` ok/fail、`summary`、`findings`) / `gerber_drc/NN_<code>_<layer>.png`。
- 終了コード: 0 = error 0 件、1 = error あり (`--strict` で warning も)、2 = 実行できない。
- IPC-D-356 (正解の接続) は使わない。`.kicad_pro` は規則、`.kicad_pcb` は aux 原点 (KiCad 座標の表示) を読むだけ。

## 依存 (gerber-lvs を参照)

Gerber/Excellon の読み込み・描画は gerber-lvs の `gerberlib.py` と `gerber_lvs.py` の入力発見部分を import して使う
(環境変数 `GERBER_LVS_DIR`、既定は同じリポジトリの `tools/gerber-lvs`)。**コピーしない理由**: 構文対応やバグ修正 (例: 2026-10-14 の穴付き aperture・インチ寸法の修正) を
1 か所で済ませ、LVS と DRC が同じ解釈で gerber を読むことを保証するため。gerber-lvs が無いと exit 1 でその旨を出す。

## フック

- kreview: `F11_gerber_drc` (checks_fab.py)。`KREVIEW_DRC_CMD` / `KREVIEW_DRC_PATH` で変更可。

## テスト

```sh
python3 tools/gerber-drc/tests/run_tests.py           # 全部: 合成 + ルール YAML + 正常 4 枚 0 件 + refill 前 FT2232H 12 件 + gerber-lvs --aux との完全一致 + kicad-cli で .kicad_dru (約 10 分)
python3 tools/gerber-drc/tests/run_tests.py --quick   # 合成 + ルール YAML (約 6 s、KiCad 不要)
```

テストデータは gerber-lvs のテストと共通 (`tools/gerber-lvs/tests/data/`、`GLVS_PROJECTS` / `GLVS_OLD_FT` で変更可)。合成基板は gerber-lvs の `tests/synthboard.py` を使う。
