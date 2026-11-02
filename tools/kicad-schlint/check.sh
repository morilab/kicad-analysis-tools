#!/usr/bin/env bash
# check.sh: KiCad 回路図の一括チェック (ERC + schlint + ネットリスト + PDF/PNG)
#   usage: check.sh <top.kicad_sch> [出力ディレクトリ]
#   出力先の既定: ${TMPDIR:-/tmp}/schcheck/<名前>-<日時>   (元のプロジェクトには何も書かない)
#   環境変数: KICAD_HOME   kicad-cli 用の HOME (ライブラリ表)。未設定なら今の HOME
#             DPI          PNG の解像度 (既定 150)
#             SCHLINT_OPTS schlint.py への追加オプション (例: "--grid 2.54 --disable WIRE_CROSSING")
# 終了コード: 0 = 完了条件を満たす (ERC の違反 0、schlint の error 0) / 1 = 満たさない / 2 = 実行エラー
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCH="${1:-}"
if [ -z "$SCH" ] || [ ! -f "$SCH" ]; then
  echo "usage: $0 <top.kicad_sch> [outdir]" >&2; exit 2
fi
SCH="$(cd "$(dirname "$SCH")" && pwd)/$(basename "$SCH")"
NAME="$(basename "$SCH" .kicad_sch)"
OUT="${2:-${TMPDIR:-/tmp}/schcheck/${NAME}-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT" || exit 2
OUT="$(cd "$OUT" && pwd)"
DPI="${DPI:-150}"
kc() { if [ -n "${KICAD_HOME:-}" ]; then HOME="$KICAD_HOME" kicad-cli "$@"; else kicad-cli "$@"; fi; }
command -v kicad-cli >/dev/null || { echo "kicad-cli がない" >&2; exit 2; }

echo "== 対象: $SCH"
echo "== 出力: $OUT"

# 1. ERC (JSON + テキスト)
kc sch erc --format json --severity-all --units mm -o "$OUT/erc.json" "$SCH" > "$OUT/erc.log" 2>&1
kc sch erc --format report --severity-all --units mm -o "$OUT/erc.rpt" "$SCH" >> "$OUT/erc.log" 2>&1
ERC_SUMMARY="$(python3 - "$OUT/erc.json" <<'PY'
import json, sys, collections
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("ERC_FAIL", e); sys.exit()
c = collections.Counter(); t = collections.Counter()
for s in d.get("sheets", []):          # KiCad 10: 違反は sheets[].violations にある
    for v in s.get("violations", []):
        if v.get("excluded"):
            continue
        c[v.get("severity", "?")] += 1; t[v.get("type", "?")] += 1
print(f"error={c['error']} warning={c['warning']} " + " ".join(f"{k}:{n}" for k, n in t.most_common()))
PY
)"
echo "ERC: $ERC_SUMMARY"
# プロジェクト設定で無効化 (ignore) された ERC 規則 (ERC 0 件でも、見ていない規則があることを示す)
PRO="${SCH%.kicad_sch}.kicad_pro"
ERC_IGNORED="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(' '.join(k for k,v in d.get('erc',{}).get('rule_severities',{}).items() if v=='ignore'))" "$PRO" 2>/dev/null)"
[ -n "$ERC_IGNORED" ] && echo "ERC で無効化されている規則: $ERC_IGNORED"

# 2. ネットリスト (netcmp 用に保存)
kc sch export netlist -o "$OUT/netlist.net" "$SCH" > /dev/null 2>&1 || echo "ネットリスト出力に失敗" >&2

# 3. schlint (テキスト + JSON、kicad-cli のネットリストと接続判定を照合)
python3 "$HERE/schlint.py" "$SCH" --json "$OUT/schlint.json" --verify-netlist ${SCHLINT_OPTS:-} > "$OUT/schlint.txt" 2>&1
LINT_RC=$?
LINT_SUMMARY="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); s=d['summary']; v=d['stats'].get('netlist_verify',{}); print(f\"error={s['error']} warning={s['warning']} info={s['info']} netlist_verify={'OK' if v.get('ok') else 'NG'}\")" "$OUT/schlint.json" 2>/dev/null || echo "schlint_FAIL")"
echo "schlint: $LINT_SUMMARY"

# 4. PDF → PNG (目視用。指摘入り PNG も作る)
kc sch export pdf -o "$OUT/schematic.pdf" "$SCH" > /dev/null 2>&1 || echo "PDF 出力に失敗" >&2
if [ -f "$OUT/schematic.pdf" ]; then
  python3 "$HERE/annotate.py" --pdf "$OUT/schematic.pdf" --json "$OUT/schlint.json" --outdir "$OUT/png" --dpi "$DPI" --min-severity warning \
    || pdftoppm -png -r "$DPI" "$OUT/schematic.pdf" "$OUT/png/page"
fi

# 5. まとめ
ERR_E=$(echo "$ERC_SUMMARY" | sed -n 's/.*error=\([0-9]*\).*/\1/p'); ERR_W=$(echo "$ERC_SUMMARY" | sed -n 's/.*warning=\([0-9]*\).*/\1/p')
LE=$(echo "$LINT_SUMMARY" | sed -n 's/.*error=\([0-9]*\).*/\1/p')
if [ "${ERR_E:-1}" = "0" ] && [ "${ERR_W:-1}" = "0" ] && [ "${LE:-1}" = "0" ]; then RC=0; else RC=1; fi
{
  echo "対象: $SCH"
  echo "日時: $(date '+%Y-%m-%d %H:%M %Z')"
  echo "KiCad: $(kicad-cli version 2>/dev/null)"
  echo "ERC: $ERC_SUMMARY   (詳細 erc.rpt / erc.json)"
  [ -n "$ERC_IGNORED" ] && echo "注意: プロジェクト設定で無効化されている ERC 規則: $ERC_IGNORED"
  echo "schlint: $LINT_SUMMARY   (詳細 schlint.txt / schlint.json)"
  echo "PNG: $(ls "$OUT"/png/*.png 2>/dev/null | tr '\n' ' ')"
  if [ "$RC" = "0" ]; then
    echo "判定: 自動チェックは完了条件を満たす (ERC 0 件、schlint error 0)。warning は理由を記録し、PNG を目視すること"
  else
    echo "判定: 未達 (ERC の違反または schlint の error が残っている)"
  fi
} | tee "$OUT/summary.txt"
exit $RC
