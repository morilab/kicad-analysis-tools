# pitfall-4l — 熱・IR ドロップ・リターンパス評価ツールの回帰試験基板 (rev0.2)

「あるある失敗」を 1 枚に並べた正解付きの試験基板。pcb-physics (熱・IR ドロップ・連成) と kreview P20/P21 (リターンパス) の
回帰テスト用。**発注しない** (机上の正解表。実測するなら発注前に LCSC 番号を選ぶ)。2026-10-14 作者 承認の依頼で作成。

- 4 層: L1 信号 / L2 GND ベタ / L3 電源 (+5V_IR・+5V_LDO・+5V_RELIEF・+5V_DENSE・+5V_SPREAD に分割) / L4 信号、100 × 129 mm、1.6 mm、外層 1 oz、stackup JLC04161H-7628 (.kicad_pcb に記入)
- rev0.2: 比べる熱パターンの対 (U1/U2、密集/分散、R19/R20) は中心線 x = 50 で鏡映、発熱部品は外形から 15 mm 以上。熱の期待値は `tools/thermal_model.py` (パッド→L2 プリプレグ + L2/L3 板の 2D モデル) で計算。改版内容は CHANGELOG.md
- 部品: 負荷抵抗 (2512)・LDO (AMS1117-3.3) と必須の入出力 C・コネクタ (JST XH、キー付き) だけ
- 区画ごとに電源コネクタを分け、1 区画ずつ通電できる (熱の干渉を避ける)
- **正解表: `expected.yaml`** (パターンごとの座標・条件・期待値・計算式・根拠・許容差)

| 区画 | パターン | 場所 (基板左上原点 mm) | 期待値 (要約) |
|---|---|---|---|
| リターンパス | L2 スリット跨ぎ 幅 0.5 / 2 / 5 mm (長さ 5.5) | x 25、y 6 / 15 / 24 | 20 / 25 / 35 点 (pass / pass / warn) |
| | 層変更 via と GND via 隙間 0.5 / 2 / 5 mm (中心 1.1 / 2.6 / 5.6) | via x 76、y 4 / 11 / 19 | 7.3 / 17.3 / 37.3 点 (pass / pass / warn) |
| | アンチパッド連結の櫛 (via 9 本、ピッチ 1.2) | x 82、y 21.6〜31.2 | 36 点 (warn) |
| IR ドロップ | 細線 0.2 × 50 mm (R1) | y 34 | 122.9 mΩ、12.0 mV |
| | ベタのネック 1 × 5 mm (R2) | x 78〜83、y 35.75 | ネック 2.46 mΩ、241 µV |
| | L3→L1 via 1 / 4 / 9 本 (R3 / R4 / R5) | x 77.5 / 86 / 94.5、y 51.5 | 110 / 27.6 / 12.3 µV (via 長 = 銅中心間 1.3157 mm) |
| | 遠距離負荷 L4 1.0 × 82.5 mm (R6) | J1 → (68,55) | 40.5 mΩ、合計 4.17 mV |
| 熱 | LDO サーマルビア有 (U1) / 無 (U2)、0.535 W + 負荷 0.495 W × 4 同時 | (24,70) / (76,70) | Tj 85 (64–97) / 88 (66–101) ℃ (差 3.2 K) |
| | 発熱体 密集 (R11-14) / 分散 (R15-18)、0.49 W × 4 | 中心 (33,104) / (67,104) | pad +88.6 / +63.3 K (比 1.40) |
| | サーマルリリーフ (R19) / 直結 (R20) | (39,85) / (61,85) | GND pad どうしの最高温度の差 R19−R20 = 7.0 K (3〜11) |
| | 細線 1 A (0.3 × 30 mm、J4→J5) | y 48.5 | ΔT 4.1 K (IPC-2221 の 10 K は上限)、50.9 mV |
| 連成 | 細線 1.5 A (0.2 × 50 mm、J6→J7) | y 41 | 合否 = 収束・比 (1+αΔT、T は抵抗重み付きの実効温度 T_eff)・単調収束。参考: T_eff 42 ℃、200 mV (非連成 184 mV、+8.7%) |

## 図面 (docs/、kicad-cli で出力した PNG)

### 回路図
![回路図](docs/schematic.png)

### 合成図 (上から見た図: F.Cu > In1.Cu (GND) > In2.Cu > B.Cu の重ね、シルク・外形付き)
B.Cu は In1.Cu の GND ベタに隠れるので、各層の図も見てください。オレンジ = In1.Cu の切れ目から見える In2.Cu。
![合成図](docs/pcb_composite.png)

### 各層 (AW、各層 + 外形 + F.Silkscreen)
| 層 | 図 |
|---|---|
| F.Cu (L1 信号) | ![F.Cu](docs/pcb_F_Cu.png) |
| In1.Cu (L2 GND、スリット・櫛) | ![In1.Cu](docs/pcb_In1_Cu.png) |
| In2.Cu (L3 電源分割) | ![In2.Cu](docs/pcb_In2_Cu.png) |
| B.Cu (L4 信号、IR4・U1 の L4 ベタ) | ![B.Cu](docs/pcb_B_Cu.png) |

### リターンパスのヒートマップ (kreview P20/P21、F.Cu の配線と参照層 In1.Cu GND、番号 = 点数表の順)
![リターンパス F.Cu](docs/return_path_F_Cu.png)

出し直し: `kicad-cli pcb export svg --layers "<層>,Edge.Cuts,F.Silkscreen" --mode-single --fit-page-to-board --exclude-drawing-sheet` → `rsvg-convert -b white -z 8`。回路図は `check.sh` の `png/page-1.png`。リターンパスは `kreview.py . --gate G2` の出力 `return_path_F_Cu.png`。

## 作り方・検査

回路図・PCB を作った生成スクリプト (tools/gen_sch.py・tools/build_pcb.py など、KiCad の pcbnew を使う) は本リポに含めていない。検査は:

```sh
../../tools/kicad-schlint/check.sh pitfall-4l.kicad_sch          # ERC + schlint + PNG
kicad-cli pcb drc --schematic-parity --severity-all pitfall-4l.kicad_pcb
python3 ../../tools/review/kreview.py . --gate G2                    # P20/P21 の点数が expected.yaml と合うか
```

rev0.2 の結果 (2026-10-14): ERC 0 / schlint error 0・warning 0 / DRC 0 (未接続 0、parity 0) / kreview G2 総合 A (98 点)、fail 0、warn 1 (D14: C3/C4 22 µF 6.3 V に 3.3 V = 52%)。
P20/P21 は rev0.1 と同じ 7 件・同じ点数 (expected.yaml の `kreview_rev0_1` / `kreview_rev0_2`、全件が期待値 ±2 点以内)。
基板標準ルール (外周 1 mm 以内の銅禁止は DRC copper_edge_clearance 1.0 mm、外周 3 mm 以内の部品 (コネクタ除く)・シルク同士とシルク-部品の重なりは自作スクリプトで検査): 違反 0。
熱の範囲は pcb-physics 63d05dc の結果 (モデルより 15〜25% 低い) を受けて下側を −35% に広げた (expected.yaml `tool_results_rev0_2`)。
熱の期待値は expected.yaml に手計算の根拠つきで書いてある (計算スクリプトは同梱していない)。

## 気づき (ツール側)
- 櫛 (アンチパッド連結) を via 径 0.6 で作ると、配線の真下の銅の切れ目は 0.1〜0.2 mm しかなく、kreview P20 は MIN_RUN 0.3 mm 未満として拾わない (実際の帰路は迂回する)。本基板は径 0.7 で切れ目を 0.4 mm にして検出させている。
- KiCad は未接続の内層の via pad を既定で消す (remove_unused_layers)。櫛を作るには via ごとに内層 pad を残す必要がある。
- スリットの点数は幅よりも長さ (迂回) で決まる (幅 10 倍で点数 1.75 倍)。
