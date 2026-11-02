# kicad-schlint — KiCad 回路図の作図ルール検査

KiCad 10 の回路図 (`.kicad_sch`、階層シート込み) を読み、ERC では見ない「人が読みやすい図か」を検査する道具一式。
Python 3 の標準ライブラリだけで動く (指摘入り PNG だけ Pillow があれば使う)。KiCad 10.0.7 で確認。

| ファイル | 役割 |
|---|---|
| `check.sh <top.kicad_sch> [出力先]` | 一括チェック: ERC (JSON/レポート) + ネットリスト + schlint (テキスト/JSON、ネットリスト照合つき) + PDF + PNG (指摘入り) + `summary.txt` |
| `schlint.py` | 作図ルール検査の本体。人向けテキスト (日本語) と JSON |
| `netcmp.py <前> <後>` | 編集前後で接続が変わっていないか確認 (kicad-cli のネットリストを比較) |
| `annotate.py` | PDF を PNG にし、schlint の指摘位置に番号付きの丸を描く (check.sh から呼ぶ) |
| `kisch.py` | S 式パーサ、シンボル/ピン座標の変換、接続モデル (schlint・netcmp が使う) |
| `stroke_coverage.json` | KiCad 既定 (ストローク) フォントの字形の有無と幅の実測結果 |
| `tests/` | テスト用回路図の生成 (`make_fixtures.py`) と回帰テスト (`run_tests.py`)、字形調査 (`gen_stroke_coverage.py`) |

## 使い方

```sh
# 一括 (元のプロジェクトには書き込まない。既定の出力先は /tmp/schcheck/<名前>-<日時>/)
tools/kicad-schlint/check.sh path/to/top.kicad_sch
#   → erc.json erc.rpt netlist.net schlint.txt schlint.json schematic.pdf png/page-N.png png/page-N-annotated.png summary.txt
#   終了コード 0 = ERC の違反 0 かつ schlint の error 0 / 1 = 未達 / 2 = 実行エラー
#   環境変数: KICAD_HOME (kicad-cli 用 HOME。未設定なら今の HOME)、DPI (既定 150)、SCHLINT_OPTS

# 検査だけ
python3 tools/kicad-schlint/schlint.py top.kicad_sch                 # テキスト
python3 tools/kicad-schlint/schlint.py top.kicad_sch --json out.json # JSON も保存
python3 tools/kicad-schlint/schlint.py top.kicad_sch --verify-netlist  # 接続判定を kicad-cli と照合
python3 tools/kicad-schlint/schlint.py --list-codes

# 編集前後の接続比較 (編集前のコピーを取っておく)
cp top.kicad_sch /tmp/before.kicad_sch   # 階層なら .kicad_pro と全シートをディレクトリごとコピー
python3 tools/kicad-schlint/netcmp.py /tmp/before.kicad_sch top.kicad_sch
#   0 = 接続は同一 / 1 = 違いあり / 2 = 実行エラー。.net ファイルも渡せる
#   --names: ネット名の変化も違いにする / --values: Value・Footprint の変化も違いにする / --strict: 両方 / --json FILE

# 回帰テスト
python3 tools/kicad-schlint/tests/run_tests.py
```

主なオプション (schlint.py):
`--grid 1.27` (グリッド mm)、`--label-ratio 0.30` / `--label-min 3` (ラベルだけで接続するピンの比率)、
`--occ-min 0.25` / `--occ-max 0.85` (用紙占有率: 図の外接矩形 ÷ 図枠内の面積)、`--field-far 7.62`、`--block-min 6`、
`--disable CODE,...`、`--min-severity warning`、`--max-list 12` (テキスト出力の 1 シート・1 コードあたりの表示上限。JSON は全件)、
`--strict` (warning でも終了コード 1)、`--no-hints`。終了コード: 0 = error なし / 1 = error あり / 2 = 読めない。

しきい値 (占有率 25〜85%、ラベル比率 30% など) は道具の既定値で、規格の数値ではない。図面の目的に合わせてオプションで変える。

## 検査コード

重要度: **E** = error (直す)、**W** = warning (直すか理由を書く)、**I** = info (参考)。同じコードでも状況で重要度が変わるものがある。

| コード | 重要度 | 内容 |
|---|---|---|
| GRID_PIN / GRID_SYMBOL / GRID_WIRE / GRID_LABEL / GRID_OTHER | W | ピン先端・シンボル原点・ワイヤ端・ラベル・接続点/×/シートピンがグリッド (既定 1.27 mm) からずれている |
| WIRE_CROSSES_SYMBOL | E | ワイヤがシンボル本体を横切る |
| WIRE_CROSSES_PIN | W | ワイヤが他のピンの線に重なる/横切る |
| JUNCTION_MISSING | E | T 分岐 (ワイヤ端が別のワイヤの途中) に接続点がない。**KiCad 10 では未接続になる** |
| PIN_ON_WIRE_MIDDLE | E | ピン先端がワイヤの途中にあるだけ (未接続)。PWR_FLAG で起きやすい |
| JUNCTION_DOT_MISSING | W | 3 本以上の端点が 1 点に集まるのに接続点の表示がない (接続はされる) |
| JUNCTION_UNNEEDED | W | 分岐していない所の接続点、どのワイヤにも乗っていない接続点 (バス上は対象外) |
| JUNCTION_4WAY | W | 4 方向の接続 (交差と見分けにくい) |
| WIRE_CROSSING | I | 非接続の交差 |
| WIRE_OVERLAP | W | ワイヤ同士が重なっている |
| WIRE_DANGLING | E | どこにもつながらないワイヤ端 |
| WIRE_STUB | W | ラベル付きワイヤの先端が開いている (KiCad 既定の ERC では unconnected_wire_endpoint) |
| WIRE_ISOLATED | E | ピンにもラベルにもつながらないワイヤ |
| STRAY_GRAPHIC | I | 長さ 0 の図形線などの見えないゴミ |
| LABEL_UNCONNECTED | E | ワイヤにもピンにも付いていないラベル |
| LABEL_ONLY_RATIO / LABEL_ONLY_PIN | W / I | ワイヤでなくラベルだけでつながるピンが多い (比率)、その個々のピン |
| LABEL_SINGLE_USE | W / I | 1 回しか使われないラベル。相手がない (綴り違いの疑い) なら W、ネット名付けなら I |
| POWER_ORIENTATION | W (負電源は I) | 電源記号が上向きでない / GND が下向きでない |
| TITLE_EMPTY | W / I | タイトル欄の title・date・rev・company が空 (W)、コメント欄がすべて空 (I) |
| PAPER_OCCUPANCY_LOW | W | 図の外接矩形が図枠内の面積の 25% 未満 (小さくまとまりすぎ) |
| PAPER_DENSE | I | 同 85% 超 (詰まりすぎ) |
| PAPER_OVERFLOW | E / W | 用紙外 (E、印刷されない)、図枠・タイトル欄にかかる (W) |
| TEXT_HALFWIDTH_KANA | W | 半角カナ |
| TEXT_KANA_STROKE | W (ひらがなは I) | 全角カタカナ。KiCad 既定フォントでは幅が漢字の約 7 割で、半角カナのように崩れて見える |
| TEXT_MISSING_GLYPH | E | 既定フォントに字形が無い文字 (①、Ⅰ、㈱、㎜、№、絵文字など。□ になる) |
| TEXT_UNTESTED_CHAR | W | 字形の有無を調べていない文字 (ハングル等) |
| TEXT_CONTROL | I (タブ) / W | タブ等の制御文字 |
| TEXT_FONT_FACE | I | 外部フォント指定。`embedded_fonts no` だと環境で置換される |
| TEXT_SMALL | W | 文字サイズが小さい (テキスト 1.27 mm 未満、フィールド 1.0 mm 未満) |
| TEXT_OVERLAP | W | 注記テキストがワイヤ・ブロック枠の線・記号に重なる、または枠からはみ出す |
| FLOW_IO_REVERSED | W | ライブラリでは入力が左・出力が右のシンボルを、反転して入力が右になるよう置いている |
| FLOW_SOURCE_RIGHT | W / I | 電源入力コネクタ (W) / USB コネクタ (I) が図の右側にある |
| FLOW_HLABEL_SIDE | I | 入力の階層ラベル・シートピンが右側、出力が左側にある |
| REF_UNANNOTATED | E | 部品番号が未確定 (R? など。階層インスタンスごとに判定) |
| REF_DUPLICATE | E | 部品番号の重複 |
| REF_MISMATCH | I | Reference プロパティと instances の部品番号が違う (画面・ネットリストは instances 側) |
| FIELD_HIDDEN | W / I | 部品番号が非表示 (W)、定数が非表示 (I) |
| FIELD_FAR | I | 部品番号/定数がシンボルから 7.62 mm 超離れている |
| FIELD_OVERLAP | W (自分の記号は I) | 部品番号/定数がワイヤ・他のシンボル・ブロック枠の線・他の文字に重なる (W)。電源記号の名前と枠線も見る。2〜3 ピンの小部品で自分の記号の中/上にある場合は I (抵抗の箱の中に定数を書く流儀があるため) |
| FIELD_VERTICAL | I | 部品番号/定数が縦向きに表示される (シンボルだけ回転してフィールドを直していない) |
| BLOCK_NO_FRAME | I | 部品 6 個以上なのにブロック枠も注記もない |
| BLOCK_FRAME_NO_TEXT | I | 見出し/説明のないブロック枠 |
| BLOCK_SYMBOL_STRADDLE | W | シンボルがブロック枠の線をまたぐ |
| SCHEMATIC_PARSE | E | 回路図を読めない |

## KiCad 10.0.7 で実測した挙動 (検査の根拠)

- T 分岐 (ワイヤ端が別ワイヤの途中に乗る) は**接続点 (junction) が無いと接続されない** (ERC: pin_not_connected + unconnected_wire_endpoint)。接続点を置けば接続。交差点も接続点があれば接続。
- ピン先端がワイヤの途中にあるだけでは未接続 (接続点を置けば接続)。ラベルはワイヤの途中に置いても接続される。
- ローカルラベルは、同じシート内の同名グローバルラベル・グローバル電源記号と結合する (別シートとは結合しない)。階層ラベルは同じシートの同名ローカルラベルと結合する。
- 旧形式の電源記号 (`(power)` 指定なし、#PWR) は非表示の power_in ピン名でつながる。PWR_FLAG はネット名を付けない。
- バス: ベクタバスは同じバス上で位置で対応する (IO[8..15] ↔ IO[0..7])。グループバスはメンバー名で対応。ラベル中の `~{}` `_{}` `^{}` は書式でありグループではない。`{slash}` などはエスケープ。
- 既定フォント (ストローク) の字形: ひらがな・カタカナ (U+3041〜30FF) の送り幅は漢字の約 71%、半角カナは約 35%。①②・ⅠⅡ・㈱・㎜・㎞・№・絵文字は字形が無く □。`(font (face "…"))` を付けるとシステムフォントで描かれる (box には Noto Sans CJK あり) が、埋め込まない限り環境依存。調査は `tests/gen_stroke_coverage.py` で再実行できる。
- フィールドの文字枠: KiCad はフィールドの角度 (0/90 のみ。270 は 90 扱い)・揃えで枠を作ってからシンボルの回転・ミラーをかけ、枠の中央に (常に読める向きで) 描く。
- ERC JSON の違反は `sheets[].violations` にある。プロジェクト設定 (`.kicad_pro` の `erc.rule_severities`) で `ignore` にされた規則は ERC に出ない (check.sh は一覧を表示する)。

接続モデルの検証: KiCad 付属のデモ 34 プロジェクト (5359 ネット) で、kicad-cli のネットリストとピンの分割が全て一致。

## 限界・既知の誤検出

- 「意味のあるブロック分け」「ラベルの名前が意味を持つか」「バイパスコンデンサが IC の近くにあるか」「本当の信号の向き」は判定できない (目視)。
- FLOW_* はヒューリスティック。ピン種別 (input/output) が正しく付いたシンボルでしか働かない。下流側 USB ポートなどは右にあって正しい。
- 文字の幅は実測平均からの近似 (英大文字 0.95×size など)。文字単位の正確な外形ではないので、FIELD_OVERLAP / PAPER_* は境界付近で外れることがある。
- WIRE_CROSSES_PIN はピンの線と平行に重なる配線も拾う。
- GRID_SYMBOL は単体テストのフィクスチャで発火を確認していない (標準ライブラリにピンが格子外のシンボルが少ないため)。
- REF_MISMATCH は階層 1 枚のときだけ見る。
- 埋め込みフォント (`embedded_fonts yes`) の字形の有無は調べない (TEXT_FONT_FACE の info のみ)。
