#!/usr/bin/env bash
# tests/data/<控え>/<基板>/ の .kicad_pcb から gerber/drill を <基板>/gerber/ に出し直す (生成物は .gitignore、git に入れない)。
# zone の refill はしない: 控えに保存された fill のまま出す。pre-refill-20261009 の FT2232H は古いベタ (NPTH 上の銅など) を再現するのが目的。
# 使い方: bash tests/regen_testdata_gerbers.sh [--force] [<控えのディレクトリ> ...]
#   既定は tests/data (環境変数 GLVS_DATA で上書き) の current/* と pre-refill-20261009/*。gerber/ に .drl があれば飛ばす (--force で出し直す)
set -euo pipefail
export HOME=${KICAD_CLI_HOME:-$HOME}
HERE=$(cd "$(dirname "$0")" && pwd)
DATA=${GLVS_DATA:-$HERE/data}
FORCE=0
if [ "${1:-}" = --force ]; then FORCE=1; shift; fi
command -v kicad-cli >/dev/null || { echo "kicad-cli が無い (PATH に入れる)" >&2; exit 2; }
if [ $# -gt 0 ]; then DIRS=("$@"); else DIRS=("$DATA"/current/* "$DATA"/pre-refill-20261009/*); fi
for P in "${DIRS[@]}"; do
  [ -d "$P" ] || continue
  PCB=$(ls "$P"/*.kicad_pcb 2>/dev/null | head -1 || true); [ -n "$PCB" ] || { echo "skip (.kicad_pcb 無し): $P"; continue; }
  G="$P/gerber"
  if [ $FORCE = 0 ] && ls "$G"/*.drl >/dev/null 2>&1; then echo "skip (有る): $G"; continue; fi
  if grep -q '"In1.Cu"' "$PCB"; then L4=1; else L4=0; fi
  LAYERS="F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
  [ $L4 = 1 ] && LAYERS="F.Cu,In1.Cu,In2.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
  T="$P/.gerber.tmp.$$"; rm -rf "$T"; mkdir -p "$T"   # 途中で止まっても半端な gerber/ を残さない
  # 控えの .kicad_pcb は書き換えない (kicad-cli export は読むだけ。drc --refill-zones / --save-board は使わない)
  kicad-cli pcb export gerbers -l "$LAYERS" --subtract-soldermask --use-drill-file-origin -o "$T/" "$PCB" >/dev/null
  kicad-cli pcb export drill --drill-origin plot --format excellon --excellon-units mm --generate-map --map-format gerberx2 -o "$T/" "$PCB" >/dev/null
  rm -rf "$G"; mv "$T" "$G"
  echo "出力: $G (4層=$L4) $(ls "$G" | wc -l) files"
done
