# pdn — 電源インピーダンス (PDN) 解析 v0.1

アートワーク後の基板 (core の格子 pcb-physics-grid/2) から電源ネットのポート間 R・L を抽出し、
コンデンサ・レギュレータ・直列素子・負荷 ΔI を加えた回路を ngspice (.ac) と Python (MNA) で解く。

```
python -m pdn.run <project_dir> --cond pdn/examples/<案件>/pdn.yaml --grid grid.npz --out DIR
python tests/test_pdn_analytic.py        # 手計算検証 6 ケース
```

- 抽出 (extract.py): R は irdrop と同じ辺の導電率とビアの筒。L は電源配線と GND がつくるループの単位長 L' = Z0_air(w,h)/c0 を帯の局所幅 w で各辺に配る。ビア L = μ0h/2π (ln(4h/d)+1)。GND は R のみ。pad をポートにして Kron 縮約し、ポート対の枝 R_ij + L_ij にする (近似)。重なった pad (USB-C の A1/B12 など) は 1 ポートにまとめる。
- 判定 (2026-10-14 作者 決定 ①〜⑥): 予算 = (源の最低電圧 − 許容最小 − IR 最悪) × ratio (0.5)、ΔI = max − typ、Ztarget = 予算 / ΣΔI。全 ΔI を同位相で同時に流した Z_eff = |V_k| / ΣΔI を f ≤ min(0.35/tr, 準静的上限) で比べる。上限より上は info。
- 判定上限 (⑤): 0.35/tr (tr = クロック周期の 10%) で計算した値を正とする (FT2232H 120 MHz → 420 MHz)。
  ただし本ツールは準静的モデル (f_qs ≈ c / (10 · 長辺 · √εeff)、この基板で約 348 MHz) なので、**判定は min(0.35/tr, f_qs) で頭打ちにし、f_qs〜0.35/tr の帯域は判定せず info (PDN5) に出す**。
  pdn.md / pdn.json には 0.35/tr の値と頭打ち後の判定範囲を両方書く。f_qs より上を判定するにはフルウェーブ解析が要る。
- ポートと εr (2026-10-14): 重なった pad (USB-C の A1/B12 など) は core の grid.a['pad_alias'] で 1 ポートにまとめる (古い格子だけセルの重なりで判定)。準静的上限の εr は格子 meta.layers[].er_below の最大 (無ければ pdn.yaml の analysis.eps_r)。値と出典は pdn.md・pdn.json (eps_r, eps_r_src) に書く。
- LDO 入力の ΔI (④): `band_limited_loads` に書いたピン (VREGIN) は ΔI / (1 + j f/f_bw) で注入する (補助の 1 Ω ∥ C と電圧制御電流源)。
- コンデンサ: 部品 YAML の capacitor.model (メーカーまたは代用品の SPICE サブ回路) を展開し、C 素子すべてに DC バイアスの容量比を掛ける。model が無ければ ESR + ESL + C。
- 効いていないコンデンサ: 1 個外したときの判定範囲内の Z_eff 最大値の変化 < 5%。DC バイアスで公称の 60% 未満になるコンデンサを指摘。
- 出力: z_<系統>.png (Z_eff と自己 |Z|)、pin_<ピン>.png、pdn.json (kreview 形式)、pdn.md、circuit.cir、z_curves.npz、progress/ (抽出の途中経過)。
- 限界: GND ビアと GND 側のループ L は電源側の L' に含めた近似。Kron 縮約後の枝 R+L 直列は中間周波数で近似 (tests 6)。準静的 (f_qs ≈ c / (10 · 長辺 · √εeff))。部品モデルの出典と代用・仮定は data/parts の各 YAML (basis、substitute) を参照。
