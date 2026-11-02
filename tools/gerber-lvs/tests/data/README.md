# 実基板テストデータ (控え)

自作の見本基板 4 枚 (usb-jtag-ft2232h・rp2040-keypad10・esp32s3-mcu-board・fpga-voice-recorder) の 2026-10-09 時点の控え。顧客の基板は含まない。ここのファイルは編集しない。

| ディレクトリ | 内容 |
|---|---|
| `current/<基板>/` | 4 基板 (usb-jtag-ft2232h 2 層、rp2040-keypad10 4 層、esp32s3-mcu-board 2 層、fpga-voice-recorder 4 層) の 2026-10-09 11:48 の控え。正常系・故障注入・合成のテストに使う |
| `pre-refill-20261009/<基板>/` | 同日の zone refill 前の控え。usb-jtag-ft2232h は古いベタ (J1 の NPTH 上の銅、外形 clearance 不足) が残っていて、補助検査 (`--aux`) の回帰テストに使う。ほかの 3 基板は current とほぼ同じ (.kicad_pro の DRC 重大度、fpga の部品 1 個の 3D モデル回転だけ違う) が控えとして残す |

- 各基板にあるのは `<基板>.kicad_pcb`、`<基板>.kicad_pro`、`ref.d356` (`kicad-cli pcb export ipcd356` の結果) だけ。
  元の控えにあった `ft.d356` / `old.d356` は `ref.d356` と同じ内容なので入れていない。`.kicad_prl` (表示状態だけ)、gerber の故障注入の古い手作業結果 (`inject/`、`inject4/`) も入れていない。
- `gerber/` は `tests/regen_testdata_gerbers.sh` の生成物 (git に入れない)。zone の refill はしないので、控えに保存された fill のまま出る。
