# tools/review — kreview (統合定量レビュー)

```sh
python3 kreview.py <project_dir> [--gate G1|G2|G3] [--compare prev/kreview.json] [--out DIR] [--quiet]
```

| ファイル | 中身 |
|---|---|
| kreview.py | 入口。段階を回し、採点 (A〜D)、review.toml の waiver、kreview.json / kreview.md / 採点表 |
| common.py | Metric、判定、kicad-cli、ネットリスト (kicadxml) の読み込み、電源/GND net の判定 |
| checks_sch.py | S01〜S16: ERC、schlint (../kicad-schlint)、フィールド充足、DNP の整合 |
| checks_design.py | D01〜D13: LCSC、Basic/Extended、品種の食い違い、ピン/pad、デカップリング、pull-up、水晶 CL、逆刺し (D11)、電源予算 (D12)、DNP の方針 (D13、DR-04)、降格判定 (D14) |
| misinsert.py | DR-01 逆刺し・±1 ピンずれの全ケース表 (`misinsert.py <dir> --md out.md`)。D11 が使う |
| power_budget.py | DR-02 電源予算 (余裕 200%・ドロップアウト・発熱、`power_budget.py <dir> --md out.md`)。D12 が使う |
| derating.py | D14 降格判定: C 耐圧・DC バイアス、R 電力、LDO 損失・Tj。温度マップ JSON の口 (`derating.py <dir> [--thermal map.json] --md out.md`) |
| return_path.py | P20/P21 リターンパスの点数 (1000 × D / (tr_eff × v)、D = max(またぐ長さ, GND の迂回の長さ) / via は 2 × GND via 距離)。層ごとのヒートマップ (点数を色の濃さ、上位に番号) と表 (`return_path.py <dir> --out DIR`)。しきい値 ≥100 fail / ≥30 warn は 2026-10-14 作者確定（運用で見直しあり） |
| signal_table.py | ネットごとの信号特性表 (ドライバ部品とピン、tr、最大周波数、直列抵抗、出典、確度)。部品の値は `data/signal/parts/<LCSC>.yaml` (熱データと同じ値オブジェクト)、無ければ `data/signal/families.yaml`。`--save` で `data/signal/boards/<基板>.yaml` |
| tests/test_design_rules.py | DR-01 (misinsert) の判定の単体テスト (DR-02 は案件の review.toml で確認) |
| checks_pcb.py | P01〜P21: pcb_score / pcb_interfere (../pcb-score) を呼ぶ + USB 差動、デカップリング距離、水晶、via-in-pad、EP、リターンパス (P20/P21) |
| checks_mount.py | 実装性 P18b・P26〜P33 (2026-10-16、しきい値はすべて仮): テント余裕 (P18b)、ゾーン接続の監査 (P26 info)、手はんだの熱バランス (P27)、ベタ直結の不均衡とチップ立ち (P28、レビュー担当 P24_pad_pour_balance 案)、リフロー到達差 (P29)、リワーク間隙 (P30)、推奨ランド (P31)、コプラナリティ (P32)、はんだ吸い込み比 (P33)。有効なら P18 は info (判定は P33)。設定は review.toml `[config.mount]`、仕様は ../../notes/2026-10-16-mount-check-items.md |
| mount_geom.py | 実装性の幾何: ゾーン接続の解決、パッド縁の銅 (ベタ接触率・マスク定義辺・スポーク本数と配線を別に数える)、via とマスク開口、吸い込み比 |
| mount_thermal.py | 実装性の熱回路網 (scipy): チップの R_th、手はんだの過渡。scipy が無ければ checks_mount が thermal_python (既定 python3) を nice で呼ぶ。結果は /tmp/kreview-cache にキャッシュ |
| mount_gerber.py | 実装性のペースト開口を gerber 実データで: kicad-cli で F/B.Paste・Mask を nice 付きで出してキャッシュ、gerber-lvs の gerberlib で画素化、パッドごとの開口 (開口用パッドの延長を含む) の面積・延長 |
| mount_annotate.py | 実装性の画像 `<out>/mount_check.png`: 点数・合否、問題箇所を項目ごとの色の丸 (実線 fail・破線 warn)、シルク濃紺・Edge.Cuts 太線、拡大図 |
| tests/test_mount_checks.py | P33・P18b・スポーク数え・P28 の除外 (keep_solid / mount.exclude)・設定の上書きの単体テスト (合成基板) |
| checks_fab.py | F01〜F11: 外形、fill の鮮度、gerber 一式・穴数・鮮度、BOM/CPL、回転、gerber-lvs フック (F09)、インピーダンス検討の記録 (F10)、製造データ DRC = tools/gerber-drc フック (F11) |
| design_rules.json | 水晶の CL、HS USB 部品、内部 pull-up、Extended 料金 (出典つき) |
| jlc_rotation.json | JLC の回転 offset (伝聞、要確認) |

指標・しきい値・例外の規則: `skills/design-review/SKILL.md`。プロジェクトは読むだけで、書き込みは `--out` のコピーで行う。
