"""実基板テストデータの場所と gerber の用意 (run_tests.py と tools/gerber-drc のテストで共通に使う)。
  tests/data/current/<基板>/              4 基板の控え (2026-10-09 11:48)。.kicad_pcb / .kicad_pro / ref.d356 だけ git に入れる
  tests/data/pre-refill-20261009/<基板>/  refill 前の控え (FT2232H は古いベタのまま = 補助検査の回帰用)
  <基板>/gerber/                          tests/regen_testdata_gerbers.sh の生成物 (.gitignore)。無ければ ensure_gerbers() が出し直す
環境変数: GLVS_DATA (tests/data の代わり)、GLVS_PROJECTS (4 基板のディレクトリ。手元の案件で回すならそのディレクトリ)、
  GLVS_OLD_FT (refill 前の FT2232H)、GLVS_NO_REGEN=1 (gerber が無くても自動で出し直さない)"""
import os, sys, glob, subprocess
HERE = os.path.dirname(os.path.abspath(__file__)); TOOL = os.path.dirname(HERE)
DATA = os.environ.get('GLVS_DATA', os.path.join(HERE, 'data'))
PROJ = os.environ.get('GLVS_PROJECTS', os.path.join(DATA, 'current'))
OLD_FT = os.environ.get('GLVS_OLD_FT', os.path.join(DATA, 'pre-refill-20261009', 'usb-jtag-ft2232h'))
REGEN = os.path.join(HERE, 'regen_testdata_gerbers.sh')
BOARDS = [('usb-jtag-ft2232h', 2), ('rp2040-keypad10', 4), ('esp32s3-mcu-board', 2), ('fpga-voice-recorder', 4)]
sys.path.insert(0, TOOL)
import gerber_lvs as gv


def gerber_dir(P):
    """<P>/gerber (tests/data の形) があればそれ、無ければ gerber_lvs と同じ探し方 (<P>/out/gerber、out/<rev>/gerber = 案件の形)"""
    g = os.path.join(P, 'gerber')
    return g if os.path.isdir(g) else gv.find_gerber_dir(P)


def has_gerber(P):
    return bool(glob.glob(os.path.join(gerber_dir(P), '*.drl')))


def ensure_gerbers(dirs=None, log=print):
    """gerber が無い控えだけ regen_testdata_gerbers.sh で出し直す。出し直すのは tests/data (GLVS_DATA) の下だけ
    (案件のディレクトリ = GLVS_PROJECTS で指定した場所には書かない)。戻り値: まだ gerber が無いディレクトリ"""
    dirs = dirs or [os.path.join(PROJ, b) for b, _ in BOARDS] + [OLD_FT]
    miss = [P for P in dirs if not has_gerber(P)]
    data = os.path.realpath(DATA) + os.sep
    regen = [P for P in miss if os.path.realpath(P).startswith(data) and glob.glob(os.path.join(P, '*.kicad_pcb'))]
    if regen and os.environ.get('GLVS_NO_REGEN') != '1':
        log(f'gerber が無いので出し直す ({len(regen)} 基板、refill なし): bash {REGEN} ' + ' '.join(regen), flush=True)
        r = subprocess.run(['bash', REGEN, *regen], capture_output=True, text=True)
        log(r.stdout.strip() + ('\n' + r.stderr.strip() if r.returncode else ''), flush=True)
    miss = [P for P in dirs if not has_gerber(P)]
    if miss:
        log('gerber が無い: ' + ', '.join(miss) + f'\n  → 出し直す: bash {REGEN}', flush=True)
    return miss
