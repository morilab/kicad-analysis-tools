# gerber-drc ルール YAML 化 設計案 (v0, 2026-10-14)

状態: **v1 実装済み (2026-10-14)**。`rules.py` (ローダ)、`gerber_drc.py --rules` (YAML 読込・層マージ・backend 振り分け・外周 1mm)、`rules2dru.py` (.kicad_dru 書き出し)。
gerber 解釈部 (gerber-lvs `gerberlib.py` 依存) は変えていない。YAML が無ければ従来と同じ結果 (テストで確認)。

## 0. 決定事項 (2026-10-14、作者「まとめておまかせ」)
| # | 論点 | 決定 | 実装 |
|---|---|---|---|
| 1 | 外周 1mm パターン禁止 | 工場値 (0.2) とは**別ルール**で両方出す | `user.edge_keepout_copper`。gerber-drc は `EDGE_CLEARANCE` に `rule_id` を付けて出す。.kicad_dru にも書く |
| 2 | 外周 3mm 部品禁止 | **KiCad DRC (.kicad_dru)** で見る。例外は案どおり (`Connector*:*`, `J*`, `MountingHole:*`, `TestPoint:*`) | `user.edge_keepout_parts` → pad と Edge_Cuts の `edge_clearance 3mm` (courtyard ではなく pad で近似) |
| 3 | project 層の置き場所 | 各プロジェクトフォルダの `gerber_drc_rules.yaml` | `gerber_drc.py <project_dir>` が自動で読む (`--rules` で明示、`--no-rules` で無効) |
| 4 | 工場値より緩める | **許可 + 警告** | rules.py が warnings に記録 → gerber_drc.json の notes、.kicad_dru 先頭コメント |
| 5 | .kicad_pro と YAML | **YAML 優先** | CLI > YAML > .kicad_pro > 既定 0.2 |
| 6 | YAML ライブラリ | PyYAML | `yaml.safe_load` |
| 7 | 多層 / 2oz | 別ファイル | `factory/jlcpcb-multilayer.yaml`, `factory/jlcpcb-2oz.yaml` (standard を extends して差分だけ) |
| 8 | 穴径チェック (2026-10-14 2 回目) | gerber-drc がドリルファイルから見る | `hole_size` (工場の最小/最大、scope.hole=pth/npth/any) → `HOLE_SIZE`、`hole_diameter_set` (例 スタッド穴 Φ2) → `HOLE_DIAMETER_SET`。scope.footprints は .kicad_pcb の footprint の穴位置 (±0.05 mm) で特定 |
| 9 | .kicad_dru の自動生成 | make_out.sh (2 か所) が毎回 `rules2dru.py --project . <name>` | 手書きの .kicad_dru は**上書きしない** (警告、生成物は `out/<name>.kicad_dru.generated`)。YAML 無しなら何もしない。YAML が壊れていたら止める |
| 10 | courtyard 検査 (3mm 部品禁止の本番) | kreview 側 (kreview 担当に依頼済み)。こちらでは実装しない | .kicad_dru の pad 近似は残す |

## 使い方
```sh
python3 tools/gerber-drc/gerber_drc.py <project_dir>                       # <project_dir>/gerber_drc_rules.yaml があれば使う
python3 tools/gerber-drc/gerber_drc.py <project_dir> --rules my.yaml       # 明示 (複数可、後勝ち)
python3 tools/gerber-drc/rules2dru.py <project_dir>/gerber_drc_rules.yaml -o <project_dir>/<name>.kicad_dru
```
プロジェクトの `gerber_drc_rules.yaml` の最小形:
```yaml
file: {layer: project, name: myboard, extends: [user/morilab]}   # 多層なら user 側か project で factory/jlcpcb-multilayer を extends
rules:
  - {id: project.stud_hole_d2, check: hole_diameter_set, scope: {hole: npth, footprints: ["MountingHole:*"]}, value: {allowed: [2.0], tol: 0.05}}
```
extends は `rules/` からの相対名かファイルからの相対パス。

## 1. 目的 (作者要件)
1. 希望ルールは YAML で管理する
2. KiCad DRC で設定できるものは `.kicad_dru` に書き出す
3. KiCad DRC で見られない工場 (JLCPCB 等)・製造データのルールは gerber-drc で実装
4. ユーザー独自ルール (外周 1mm パターン禁止、3mm 部品禁止 (コネクタ等例外)、シルク同士・シルクと部品の重なり)
5. プロジェクト固有ルール (例: スタッド穴は全部 Φ2)

## 2. 構成
```
tools/gerber-drc/rules/
  schema.yaml                 スキーマ案 + check 一覧
  factory/jlcpcb-standard.yaml  工場の能力値 (出典 URL・verified 付き)
  user/morilab.yaml             ユーザー独自
  project/example.yaml          プロジェクト固有のサンプル (実物は各プロジェクト dir に置く想定)
```
- 合成: factory → user → project の順に読み、**同じ id はフィールド単位で後勝ち**。`enabled: false` で無効化。
- 各ルールは `check` (検査の種類) と `backend` (kicad_dru / gerber_drc / both / none) を持つ。backend 省略時は schema の check 表から決める。
- 合成後の「実効ルール」を 1 本の JSON に出し、`gerber_drc.json` の `rules` に出典 (どの層の何ファイルか) と一緒に記録する。
- 書き出し: `rules2dru.py` が backend kicad_dru/both の有効ルールを `(rule "<id>" (constraint ..) (condition ..) (severity ..))` で書く。書けないものは末尾にコメント。
- **KiCad の custom rules は後に書いたルールが優先**され、同じ constraint 種別はペアごとに 1 本だけ効く。rules2dru は合成順 (factory → user → project) に書くので上位層・厳しいユーザールールが後ろに来る。
  例: 外形 clearance は pad には `user.edge_keepout_parts` (3mm)、例外部品の pad とそれ以外の銅には `user.edge_keepout_copper` (1mm) が効く。工場値 0.2 は KiCad 側では隠れるが、gerber-drc 側で両方出す。
- gerber_drc.py は `rules_params()` で自分の担当 (backend gerber_drc/both) だけ使う: `edge.copper_clearance` → 従来の EDGE_CLEARANCE の値、それ以外の edge_clearance → 追加の外形検査 (`rule_id` 付き)、
  `hole_clearance` (scope.hole=npth) → NPTH_CLEARANCE、固定 4 検査は severity だけ。未実装の check (hole_size, hole_diameter_set, board_size, silk 系) は notes に「見ていない」と出す。
- `.kicad_pro` の `min_copper_edge_clearance` / `min_hole_clearance` は YAML に値が無いときだけ使う。優先順位は CLI > YAML (project > user > factory) > .kicad_pro > 既定 0.2。
- 出力: `gerber_drc.json` の `rules.yaml` に合成後の実効ルール (各ルールの backend と最後に定めた層)、`rules.extra_edge` に追加の外形検査、notes に緩め警告。

## 3. 既存 6 検査と YAML 項目の対応表
| 既存 code | 重大度 | 現在の値の出所 | YAML id (factory) | check | 上書き例 |
|---|---|---|---|---|---|
| COPPER_OVER_NPTH | error | 固定 | fab.copper_over_npth | copper_over_npth | severity のみ |
| NPTH_CLEARANCE | warning | .kicad_pro min_hole_clearance / 0.2 | npth.copper_clearance (0.20) | hole_clearance (npth) | project で値変更 |
| EDGE_CLEARANCE | error | .kicad_pro min_copper_edge_clearance / 0.2 | edge.copper_clearance (0.20) | edge_clearance | 別ルール user.edge_keepout_copper (1.0) を `rule_id` 付きで追加出力 |
| COPPER_OUTSIDE_BOARD | error | 固定 | fab.copper_outside_board | copper_outside_board | severity のみ |
| PASTE_WITHOUT_COPPER | warning | 固定 | fab.paste_without_copper | paste_without_copper | severity のみ |
| OUTLINE_OPEN | warning | 固定 | fab.outline_closed | outline_closed | severity のみ |
| (許容誤差 2*px+0.005) | — | コード固定 | fab.raster_tolerance (verified:false) | none | YAML 化するか要決定 |

## 4. `.kicad_dru` 書き出し可否の分類
kicad-cli 10.0.7 で `pcb drc` が生成 .kicad_dru を読み、ルール名 (`'user.edge_keepout_copper'` 等) 付きの違反を出すことを確認済み (tests の全件モード、rp2040-keypad10)。KiCad 7/8 では未確認。

| 分類 | ルール (id) | `.kicad_dru` での表現案 | gerber-drc |
|---|---|---|---|
| A 書き出せる | track.min_width | `(constraint track_width (min 0.10mm))` | しない |
| A | copper.min_clearance | `(constraint clearance (min 0.10mm))` | しない (ラスタ精度で困難) |
| A | hole.min_drill / max / npth.min_drill | `(constraint hole_size (min ..))` + `(condition "A.Pad_Type == 'NPTH, mechanical'")` | 実装可 (drill 径) |
| A | via.min_diameter | `(constraint via_diameter (min 0.25mm))` | 後で |
| A | pth.min_annular | `(constraint annular_width (min 0.18mm))` | 後で |
| A | via/pad.hole_to_hole | `(constraint hole_to_hole (min ..))` | 後で |
| A | npth.copper_clearance / pth.track_clearance | `(constraint hole_clearance ...)` | 既存 NPTH_CLEARANCE (NPTH のみ) |
| A | edge.copper_clearance / user.edge_keepout_copper | `(constraint edge_clearance (min 1.0mm))` | 既存 EDGE_CLEARANCE (両方で二重確認) |
| A | silk.min_line_width / min_text_height | `text_thickness` / `text_height` | 線幅は後で |
| A | silk.pad_clearance | `(constraint silk_clearance (min 0.15mm))` | 後で (シルク gerber×銅) |
| A | project.stud_hole_d2 | `(condition "A.Reference == 'H*'") (constraint hole_size (min 2mm) (max 2mm))` | 実装可 (NPTH 径の集合) |
| B 一部だけ | user.edge_keepout_parts (3mm 部品禁止) | `(constraint edge_clearance (min 3mm)) (condition "A.Type == 'Pad' && !(A.memberOfFootprint('J*') ...)")` — **pad で近似** (部品本体・courtyard は見ない) | 不可 (gerber に部品情報が無い) |
| B | user.silk_silk_overlap | KiCad 標準の「silk_overlap」違反 (重大度は .kicad_pro)。間隔値は `silk_clearance` | 後で |
| B | user.silk_part_overlap | pad: `silk_clearance`。courtyard / 部品本体との重なりは標準で弱い | courtyard は不可 |
| B | copper_over_npth / copper_outside_board / outline_closed | KiCad は .kicad_pcb で同等を見るが、**出来上がった gerber** は見ない | 既存 (gerber-drc の存在意義) |
| C 書き出せない | fab.paste_without_copper | なし | 既存 |
| C | board.min_size / max_size | なし | 後で (外形 bbox で容易) |
| C | mask.min_bridge | .kicad_pro の solder_mask_min_width (dru ではない、要確認) | 後で |
| C | fab.raster_tolerance | なし (ツール内部) | — |

## 5. 段階
1. 済 スキーマとサンプル、対応表。
2. 済 `rules.py` (層マージ・mil 換算・緩め警告)、gerber_drc.py の YAML 読込と backend 振り分け、外周 1mm (追加 edge 検査)。
3. 済 `rules2dru.py`、kicad-cli 10.0.7 で読めることを確認。
4. 未 gerber-drc の新検査 (drill 径集合 = スタッド Φ2、基板寸法、シルク×pad)。
5. 済 make_out.sh から rules2dru を自動で呼ぶ。kreview は下記「kreview F11 からの呼び方」(kreview 担当が反映)。
6. 済 穴径 (HOLE_SIZE / HOLE_DIAMETER_SET)。未: 基板寸法、シルク×pad、via だけの穴径 (drill ファイルで via と部品穴を区別しないため見ない)。

## 7. kreview F11 からの呼び方 (kreview 担当向け。こちらでは F11 を変更しない)
- 今の F11 は `gerber_drc.py <project_dir> <name> ...` を呼んでいるので、**変更なしで** `<project_dir>/gerber_drc_rules.yaml` が自動で使われる (YAML が無ければ従来どおり)。
- 結果の読み方 (gerber_drc.json):
  - `findings[].code` に新しく `HOLE_SIZE` / `HOLE_DIAMETER_SET` が出る。YAML 由来の検出は `rule_id` (例 `user.edge_keepout_copper`, `project.stud_hole_d2`) を持つ。
    `EDGE_CLEARANCE` は工場値 (rule_id 無し) と user 1mm (rule_id あり) の 2 件が同じ場所に出ることがある。
  - `rules.yaml.files` = 使った YAML (`factory:jlcpcb-standard, user:morilab, project:<name>`)、`rules.yaml.warnings` = 工場値を緩めた警告、`notes` = 見ていないルール。
  - `status` / 終了コードの意味は変わらない (error があれば fail / 1)。
- YAML を明示したい場合は `--rules <file>` (複数可)、従来動作に固定したい場合は `--no-rules`。
- .kicad_dru (KiCad DRC = kreview P04 側) は make_out.sh が作る。kreview 単体で作り直すなら `python3 tools/gerber-drc/rules2dru.py --project <project_dir> <name>` を P04 の前に呼ぶ。

## 6. 未確認事項
- JLCPCB 値は 2026-10-14 22:10 JST に https://jlcpcb.com/capabilities/pcb-capabilities で確認 (1oz・2 層基準)。多層 (0.09/0.09 等)、2oz、色別 (黒白マスク 0.13) は別ファイル/条件が必要。
- slot 最小幅、許容誤差の YAML 化は未確認 (`verified: false`)。
- 穴径の footprint 条件は .kicad_pcb の pad 位置 (aux 原点、`--use-drill-file-origin` 前提) と穴を ±0.05 mm で照合。drill origin を使わない gerber では当たらない (対象 0 件になり notes にも出ない)。
- `.kicad_dru` は KiCad 10.0.7 だけ確認。3mm 部品禁止は pad で近似しており、部品本体・courtyard が外周 3mm にかかる場合は見逃す。
- `user.silk_part_overlap` は `silk_clearance` + `B.Type == 'Pad'` で書いたが、KiCad がこの条件で pad とのシルク距離を判定するかは未確認 (違反が出ることは確認していない)。
- solder mask bridge が dru で表現できるか未確認。
