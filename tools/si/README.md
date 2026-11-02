# tools/si — 信号品質 (差動インピーダンスの断面計算、ngspice の伝送線路)

インピーダンス検討の流れ (board-hotl に組み込み済み): ①近似式 (G2 配置でアタリ、kreview P15) → ②自作 2D 電界計算 (配線時に調整、`zdiff2d.py`)
→ ③SPICE (検証、`usb_hs_spice.py`) → ④openEMS (できるなら検証)。G3 の kreview F10 が review/ に ②③ の結果ファイルがあるかを見る。
詳細は `skills/usb-diff-2layer/SKILL.md`。

| ファイル | 内容 |
|---|---|
| `zdiff2d.py` | 2 層の差動線路 (マイクロストリップ / GND 付きコプレーナ = CPWG) の断面を 2D 有限差分で解く。Zdiff・Zodd・Zeven・Zcm・L/C 行列。`--selftest` で検証 |
| `zdiff_sweep.py` | 基板厚 × 形 (GND までの距離) × 線幅 × 間隔 を総当たりして CSV |
| `zdiff_table.py` | CSV から Markdown の表と「90 Ω ±10% に入る組み合わせ」 |
| `usb_hs_spice.py` | USB 2.0 HS (480 Mbps、PRBS7、立ち上がり 500 ps) を ngspice で流し、アイパターンと TDR を PNG・JSON に |

依存: numpy、scipy、matplotlib、ngspice。リポジトリのルートで:

```
PY=python3; SI=tools/si
$PY $SI/zdiff2d.py --selftest
$PY $SI/zdiff2d.py --h 1.53 --w 0.3 --s 0.15 --g 0.2          # → Zdiff 91 Ω
$PY $SI/zdiff_sweep.py /tmp/si/sweep.csv                       # 864 条件、約 4 分 (1 プロセス)
python3 $SI/zdiff_table.py /tmp/si/sweep.csv > zdiff_table.md
$PY $SI/usb_hs_spice.py --out /tmp/si/spice --seg1 4.17 --skew 1.27 --seg2 4.71 \
    --case "今:geom:1.53,0.3,0.2," --case "調整:geom:1.53,0.3,0.15,0.2" --case "145Ω:z:145"
```

精度: 単線は Hammerstad の式と 1〜2%、空気中の平行 2 線は楕円積分の厳密解と 1.8%。準 TEM・損失なし。
ngspice の CPL (結合線路) は短い線で非常に遅い (1 条件 5 分以上) ので、差動だけで駆動する既定は 奇モードの等価な T 線路 (`--cpl` で CPL)。
小さい容量 (0.01 pF など) を入れると時間刻みが細かくなり非常に遅くなる。容量を外すときは素子ごと消す。

## ④ openEMS (同梱していない)

openEMS (3D FDTD、GPL-3.0) で差動線の Zodd / Zeven を求める検証用スクリプトは、openEMS の Python モジュールを import するため本リポ (MIT) には含めていない。
比べるときは、線の厚さを zdiff2d と同じにする (密結合では線の厚さが 10 Ω 程度効く)。
