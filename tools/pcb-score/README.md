# pcb-score — PCB 配置の採点と干渉検査

`.kicad_pcb` を S 式として直接読む (pcbnew SWIG 不要。system `python3` + numpy + Pillow)。KiCad 10.0.7 で確認。
ルールとしきい値の正本は `skills/pcb-layout-review/SKILL.md`。しきい値を変えたら `pcb_score.py` の `THRESHOLDS` と SKILL を同じコミットで直す。

| ファイル | 内容 |
|---|---|
| `kpcb.py` | S 式パーサと幾何 (footprint 座標変換、courtyard/外形の多角形化、ラスタ面積、3D モデル外形の概算) |
| `pcb_score.py` | 配置・配線の数値化 (面積・密度・空き率・配線長・via・HPWL/MST・合成点) |
| `pcb_interfere.py` | 組立て干渉の検査 (DRC 規則の監査 + 必須規則を有効にした DRC + silk/pad + 本体/3D + モジュール実外形 + コネクタ嵌合) |
| `connectors.json` | コネクタの嵌合方向・plug 占有の表 (足りなければ追加する) |
| `modules.json` | モジュールのデータシート外形・keepout・取り違えやすい品種 |

## 使い方

```sh
T=tools/pcb-score
python3 $T/pcb_score.py a.kicad_pcb b.kicad_pcb --json /tmp/score.json      # 複数なら最後に比較表
python3 $T/pcb_score.py board.kicad_pcb --reserve F.SilkS                    # 基板に載せる module の外形 (閉図形) を占有として数える
python3 $T/pcb_interfere.py board.kicad_pcb --png                             # 結果は /tmp/pcbcheck/<名前>-<日時>/ (終了コード 0 = error 0)
```

- `pcb_interfere.py` はプロジェクト (`.kicad_pcb/.kicad_pro/.kicad_sch`、`lib/`、`fp-lib-table`) を出力先にコピーし、
  必須規則を error/warning に上げた `.kicad_pro` で `kicad-cli pcb drc --severity-all` を回す。**元のプロジェクトは変更しない。**
  KiCad のライブラリ表が設定された HOME で実行する (`KICAD_CLI_HOME` で変更可)。
- DRC は保存済みの zone fill のまま回す (gerber に出るものと同じ)。`--refill-zones` を付けた DRC と結果が違うなら fill が古い。

## 指標 (pcb_score)

| 指標 | 意味 | 良い向き |
|---|---|---|
| `density` | courtyard 面積の和 (+予約領域) / 外形面積 | 高い |
| `empty_ratio` | courtyard・track・via のどれにも掛からない面積 (= ベタだけ) の割合 | 低い |
| `far_empty_ratio` | courtyard・track・via から 2 mm 以上離れた空きの割合 (空きが固まっている = 間延び) | 低い |
| `max_margin_mm` | 部品の外接矩形と外形の余白の最大値 | 小さい |
| `hpwl_signal` / `mst_signal` | signal ネット (電源・GND・ベタのネットを除く) の pad 座標の半周長 / 最小全域木。**配線前に配置案を比べる指標** | 小さい |
| `detour` | signal 配線長 / MST | 小さい |
| `vias_per_signal_net` | signal via 数 / signal ネット数 | 小さい |
| `area_at_target` | courtyard 和 / 0.40 = 密度 40% にしたときの外形面積の目安 | — |
| `score` | 上の 6 指標をしきい値で 0-100 にした加重平均 (密度 3、空き 2、2mm 空き 2、余白・detour・via 各 1)。**下書き** | 高い |

## 既知の限界

- courtyard の無い footprint は Fab/銅の外接矩形で代用しない (courtyard 無しとして報告)。
- silk 文字の外形は既定ストロークフォントの概算。正確な判定は DRC の `silk_over_copper`。
- 3D モデルの外形は STEP の CARTESIAN_POINT / WRL の point を集めた bbox (z 回転のみ考慮)。
- コネクタの嵌合方向は `connectors.json` にあるものだけ判定する。無いコネクタは「unknown」(warning) と出るので、3D とデータシートで確かめて表に足す。

## 比較スコア S (`--profile`、2026-10-15 作者 決定)
配置案・基板の比較用。**上限なし、高いほど良い**。合否の点 (上の 0-100 の合成点、kreview の 0-100 点) とは別物で、kreview の点は製造前の合否判定用に残す。

- `S = Σ 係数 × 項目スコア − 1000 × 軽い違反数`
- 項目スコア: 基準版 (通常は現行版) と同じ値なら 100。低いほど良い指標は `100 × 基準 / max(値, 0.25 × 基準)`、高いほど良い指標は `100 × 値 / 基準`。案と基準の両方にある指標だけで計算する (配線前は via / detour なし)
- 項目: 面積、信号配線長見積 (MST / HPWL)、電源配線長、ラッツネスト交差数 (うちヘッダ)、via / 信号ネット、USB 差動距離、2 mm 空き率、密度、熱 (熱解析の最高温度の余裕 ℃。配置段階では電源部品の周りの空き率で代用)
- ハード制約 (部品の重なり・外形はみ出し・取付穴の干渉・DRC 必須 error) が 1 つでもある案は `feasible=false` で順位から外す
- 軽い違反 (1 件 −1000): 外周 3 mm 違反の部品数、コネクタ差し込み口のずれ、J2 の端からの距離、kreview fail
- 係数: `score_profiles.json` (default / size / signal / thermal / cost)。プロジェクトは `review.toml` の `[score]` で `profile`・`baseline`・`[score.profiles.<名前>]` (`base` + `weights` の上書き) を書く
- 実装: `score_profiles.py` (score / rank / table)。テスト: `python3 tests/test_score_profiles.py`

```
python3 pcb_score.py 案.kicad_pcb ... --profile cost --baseline 現行.kicad_pcb [--review-toml <proj>/review.toml]
```
