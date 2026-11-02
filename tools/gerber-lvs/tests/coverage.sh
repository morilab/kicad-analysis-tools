#!/bin/sh
# 行カバレッジ (gerber_lvs.py / gerberlib.py / gerber-drc の gerber_drc.py)。subprocess 分も含める (sitecustomize + COVERAGE_PROCESS_START)。
# 使い方: sh tests/coverage.sh [--quick]
#   --quick: 高速スイートと gerber-drc --quick だけ
#   スイートごとのデータを残し、各スイートだけが埋める行 (固有行) の数も出す (tests/cov_unique.py)
set -e
cd "$(dirname "$0")/.."
if python3 -m coverage --version >/dev/null 2>&1; then :; else echo "coverage が無い (python3 -m coverage)" >&2; exit 2; fi
LVS=$(pwd)
GDRC_DIR=${GDRC_DIR:-$(cd "$(dirname "$0")/../../gerber-drc" && pwd)}
OUT=${COV_OUT:-$(mktemp -d /tmp/glvs-cov-XXXXXX)}
mkdir -p "$OUT/site"
echo 'import coverage; coverage.process_startup()' > "$OUT/site/sitecustomize.py"
cat > "$OUT/covrc" <<RC
[run]
parallel = True
include =
    $LVS/gerber_lvs.py
    $LVS/gerberlib.py
    $GDRC_DIR/gerber_drc.py
[report]
show_missing = True
RC
export COVERAGE_PROCESS_START="$OUT/covrc" PYTHONPATH="$OUT/site${PYTHONPATH:+:$PYTHONPATH}"
suite() {  # $1 = 名前, 残り = コマンド
  n=$1; shift; mkdir -p "$OUT/$n"; COVERAGE_FILE="$OUT/$n/.coverage" "$@" > "$OUT/$n/log.txt" 2>&1 || echo "  ($n: テストに FAIL あり → $OUT/$n/log.txt)"
  (cd "$OUT/$n" && COVERAGE_FILE="$OUT/$n/.coverage" python3 -m coverage combine --rcfile="$OUT/covrc" -q >/dev/null 2>&1 || true)
  echo "$n: $(tail -1 "$OUT/$n/log.txt")"
}
suite units python3 tests/test_units.py
if [ "$1" = --quick ]; then
  suite drc python3 "$GDRC_DIR/tests/run_tests.py" --quick
else
  suite full python3 tests/run_tests.py --only normal old inject input synth
  suite drc python3 "$GDRC_DIR/tests/run_tests.py"
fi
cd "$OUT"; rm -f .coverage
for d in */.coverage; do cp "$d" ".coverage.$(dirname "$d")"; done
python3 -m coverage combine --rcfile="$OUT/covrc" --keep -q
python3 -m coverage report --rcfile="$OUT/covrc" | tee "$OUT/coverage.txt"
python3 "$LVS/tests/cov_unique.py" "$OUT" | tee -a "$OUT/coverage.txt"
echo "→ $OUT/coverage.txt"
