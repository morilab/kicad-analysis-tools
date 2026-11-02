#!/usr/bin/env python3
"""gerber-lvs の高速テスト (KiCad 不要、数秒)。python3 tests/test_units.py [-v]
  A. Gerber 構文の単体テスト: 既知の幾何を描いて画素数・特定画素を比べる
  B. Excellon / IPC-D-356 の読み込み
  C. 合成基板 (tests/synthboard.py) の LVS: 正常 / 故障注入 / ブラインド・ベリード via / 入力整合 (層欠落・取り違え・単位・原点)"""
import os, sys, math, shutil, tempfile, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import numpy as np
import gerberlib as gl, gerber_lvs as gv, synthboard as sb

TMP = tempfile.mkdtemp(prefix='glvs-unit-'); rows = []


def rec(group, case, ok, detail=''):
    rows.append((group, case, bool(ok))); print(f"{'PASS' if ok else 'FAIL'} [{group}] {case} {detail}", flush=True)


def gfile(body, aps='', unit='MM', fs='LAX46Y46', name='t.gbr'):
    p = os.path.join(TMP, name); sb.gerber(p, 'Copper,L1,Top', body, aps, unit=unit, fs=fs); return p


def draw(path, box=(-3, -3, 3, 3)):
    g = gl.Gerber(path); R = gl.Raster(*box, 0.01); return np.asarray(gl.render(g, R)) > 127, R


def at(im, R, x, y):
    c, r = R.xy2pix(x, y); return bool(im[int(round(r)), int(round(c))])


def area(im): return im.sum() * 1e-4  # mm²


def near(a, b, rel=0.03, ab=0.0): return abs(a - b) <= rel * b + ab


# ------------------------------------------------------------ A. Gerber 構文
def test_syntax():
    G = 'syntax'
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%ADD10C,2.0*%\n'))
    rec(G, '円 flash φ2 の面積 ≒ π', near(area(im), math.pi, ab=0.07), f'{area(im):.4f}')
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%ADD10R,2.0X1.0*%\n'))
    rec(G, '矩形 2×1 の面積 (太り ≒ 1 px 分まで)', near(area(im), 2.0, ab=0.04) and at(im, R, 0.98, 0.48) and not at(im, R, 1.03, 0), f'{area(im):.4f}')
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%ADD10O,2.0X1.0*%\n'))
    rec(G, '長円 2×1 の面積 = 1 + π/4', near(area(im), 1 + math.pi / 4, ab=0.04) and not at(im, R, 0.97, 0.47), f'{area(im):.4f}')
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%ADD10P,2.0X4*%\n'))
    rec(G, '多角形 P 4 角 (外接 φ2) の面積 = 2', near(area(im), 2.0, ab=0.04), f'{area(im):.4f}')
    im, R = draw(gfile('D10*\nX-2000000Y0D02*\nX2000000Y0D01*\n', '%ADD10C,0.5*%\n'))
    rec(G, '丸 aperture の線 4 mm × 0.5 の面積', near(area(im), 4 * 0.5 + math.pi * 0.0625, ab=0.06), f'{area(im):.4f}')
    im, R = draw(gfile('D10*\nX-1000000Y-1000000D02*\nX1000000Y1000000D01*\n', '%ADD10R,0.4X0.4*%\n'))
    rec(G, '矩形 aperture の斜め線 (hull)', at(im, R, 0, 0) and at(im, R, 1.15, 1.15) and not at(im, R, 0.5, -0.5))
    im, R = draw(gfile('G36*\nX-1000000Y-1000000D02*\nX1000000Y-1000000D01*\nX1000000Y1000000D01*\nX-1000000Y1000000D01*\nX-1000000Y-1000000D01*\nG37*\n'))
    rec(G, 'region G36/G37 2×2', near(area(im), 4.0, ab=0.05), f'{area(im):.4f}')
    im, R = draw(gfile('G75*\nD10*\nX1000000Y0D02*\nG03*\nX1000000Y0I-1000000J0D01*\nG01*\n', '%ADD10C,0.1*%\n'))
    rec(G, 'G75 全周円弧 r=1 (線 0.1)', near(area(im), 2 * math.pi * 0.1, rel=0, ab=0.07) and at(im, R, -1, 0) and at(im, R, 0, 1) and not at(im, R, 0, 0), f'{area(im):.4f}')
    im, R = draw(gfile('G75*\nD10*\nX1000000Y0D02*\nG03*\nX-1000000Y0I-1000000J0D01*\nG01*\n', '%ADD10C,0.1*%\n'))
    rec(G, 'G03 半円 (反時計回り = 上側)', at(im, R, 0, 1) and not at(im, R, 0, -1))
    im, R = draw(gfile('G75*\nD10*\nX1000000Y0D02*\nG02*\nX-1000000Y0I-1000000J0D01*\nG01*\n', '%ADD10C,0.1*%\n'))
    rec(G, 'G02 半円 (時計回り = 下側)', at(im, R, 0, -1) and not at(im, R, 0, 1))
    im, R = draw(gfile('D10*\nX0Y0D03*\n%LPC*%\nD11*\nX0Y0D03*\n', '%ADD10C,2.0*%\n%ADD11C,1.0*%\n'))
    rec(G, 'LPC (clear) で中心を抜く', not at(im, R, 0, 0) and at(im, R, 0.75, 0) and near(area(im), math.pi * 0.75, ab=0.08), f'{area(im):.4f}')
    im, R = draw(gfile('X0Y0D02*\nD10*\nX0Y0D03*\n', '%ADD10C,2.0X1.0*%\n'))
    rec(G, '穴付き丸 aperture (donut)', not at(im, R, 0, 0) and at(im, R, 0.75, 0))
    # 穴付き aperture は下の銅を消さない (Gerber 仕様: 穴は透明)
    im, R = draw(gfile('D11*\nX0Y0D03*\nD10*\nX0Y0D03*\n', '%ADD10C,2.0X1.0*%\n%ADD11R,3.0X3.0*%\n'))
    rec(G, '穴付き aperture の穴は下の銅を消さない (透明)', at(im, R, 0, 0), '修正したバグ: 以前は下の銅まで消していた')
    im, R = draw(gfile('%LPC*%\nD10*\nX0Y0D03*\n', '%ADD10C,2.0X1.0*%\n'))
    rec(G, 'clear の穴付き aperture は穴に銅を足さない', not at(im, R, 0, 0), '修正したバグ: 以前は穴に銅を描いていた')
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%AMRR*\n0 RoundRect*\n4,1,4,-1,-0.5,1,-0.5,1,0.5,-1,0.5,-1,-0.5,0*\n1,1,0.4,-1,-0.5*\n1,1,0.4,1,0.5*%\n%ADDRRX*%\n'.replace('ADDRRX', 'ADD10RR')))
    rec(G, 'マクロ (outline 4 + circle 1)', at(im, R, 0, 0) and at(im, R, -1.15, -0.5) and not at(im, R, 1.15, -0.5))
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%AMT*\n1,1,2,0,0*\n1,0,1,0,0*%\n%ADD10T*%\n'))
    rec(G, 'マクロの exposure off は aperture 内だけ抜く', not at(im, R, 0, 0) and at(im, R, 0.75, 0))
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%AMV*\n$1=2*\n21,1,$1,$1x0.5,0,0,45*%\n%ADD10V*%\n'))
    rec(G, 'マクロ変数・式・回転 (21 矩形 2×1 を 45°)', at(im, R, 0.6, 0.6) and not at(im, R, 0.9, 0))
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%ADD10C,0.0787*%\n', unit='IN', fs='LAX25Y25'))
    rec(G, 'インチ単位 (%MOIN、φ0.0787 in ≒ 2 mm)', near(area(im), math.pi, ab=0.08) and at(im, R, 0.97, 0), f'{area(im):.4f}')
    im, R = draw(gfile('D10*\nX1000Y0D03*\n', '%ADD10C,0.02*%\n', unit='IN', fs='LAX24Y24'))
    rec(G, 'インチ座標 FS 2.4 (X1000 = 0.1 in = 2.54 mm)', at(im, R, 2.54, 0) and not at(im, R, 0, 0))
    for body, aps, what in [('%SRX2Y1I5J0*%\nD10*\nX0Y0D03*\n%SR*%\n', '%ADD10C,1*%\n', 'SR (step and repeat)'),
                            ('G74*\nD10*\nX1000000Y0D02*\nG03*\nX0Y1000000I1000000J0D01*\n', '%ADD10C,0.1*%\n', 'G74 単象限円弧'),
                            ('D10*\nX0Y0D03*\n', '%AMTH*\n7,0,0,2,1.6,0.2,0*%\n%ADD10TH*%\n', 'マクロ thermal (7)'),
                            ('%LMX*%\nD10*\nX0Y0D03*\n', '%ADD10C,1*%\n', 'LM (mirror)'),
                            ('G91*\nD10*\nX0Y0D03*\n', '%ADD10C,1*%\n', 'G91 (相対座標)'),
                            ('D10*\nX0Y0D03*\n', '%ADD10C,1*%\n', 'FS trailing zero (FSTAX)')]:
        fs = 'TAX46Y46' if 'trailing' in what else 'LAX46Y46'
        try:
            draw(gfile(body, aps, fs=fs)); ok = False; why = '例外なし'
        except gl.Unsupported as e: ok = True; why = str(e)
        except Exception as e: ok = False; why = repr(e)
        rec(G, f'未対応構文は Unsupported (exit 2): {what}', ok, why[:80])
    p = os.path.join(TMP, 'nomo.gbr'); open(p, 'w').write('%FSLAX46Y46*%\n%ADD10C,1*%\nD10*\nX0Y0D03*\nM02*\n')
    try: gl.Gerber(p); ok = False; why = '例外なし'
    except gl.Unsupported as e: ok = True; why = str(e)
    except Exception as e: ok = False; why = repr(e)
    rec(G, '%MO が無い座標は Unsupported (以前は TypeError)', ok, why[:80])


# ------------------------------------------------------------ B. Excellon / D356
def test_drill_d356():
    G = 'parse'
    def drl(txt):
        p = os.path.join(TMP, 'x.drl'); open(p, 'w').write(txt); return p
    h = gl.parse_drill(drl('M48\nMETRIC\n; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C0.300\n; #@! TA.AperFunction,NonPlated,NPTH,ComponentDrill\nT2C1.000\n%\nG90\nG05\nT1\nX1.5Y2.5\nT2\nX3.0Y4.0\nM30\n'), 2)
    rec(G, 'Excellon mm: 穴 2、径・めっきの区別', len(h) == 2 and h[0].d == 0.3 and h[0].plated and not h[1].plated and (h[1].x, h[1].y) == (3.0, 4.0))
    h = gl.parse_drill(drl('M48\nINCH\nT1C0.0394\n%\nT1\nX0.1Y0.2\nM30\n'), 2)
    rec(G, 'Excellon inch: 径・座標を mm に', abs(h[0].d - 1.0) < 0.001 and abs(h[0].x - 2.54) < 1e-9 and abs(h[0].y - 5.08) < 1e-9)
    h = gl.parse_drill(drl('M48\nMETRIC\nT1C0.800\n%\nT1\nX1.0Y1.0G85X3.0Y1.0\nM30\n'), 2)
    rec(G, 'Excellon G85 長穴', len(h) == 1 and h[0].is_slot and h[0].x == 2.0)
    h = gl.parse_drill(drl('M48\nMETRIC\nT1C0.800\n%\nT1\nG00X1.0Y1.0\nM15\nG01X4.0Y1.0\nM16\nM30\n'), 2)
    rec(G, 'Excellon M15/M16 ルーティング長穴', len(h) == 1 and h[0].x0 == 1.0 and h[0].x1 == 4.0)
    h = gl.parse_drill(drl('M48\n; #@! TF.FileFunction,Plated,1,2,PTH\nMETRIC\nT1C0.2\n%\nT1\nX1.0Y1.0\nM30\n'), 4)
    rec(G, 'Excellon FileFunction の層範囲 (ブラインド 1-2)', h[0].span == (1, 2))
    try: gl.parse_drill(drl('M48\nMETRIC,TZ\nT1C0.3\n%\nT1\nX1000Y2000\nM30\n'), 2); ok = False
    except gl.Unsupported: ok = True
    rec(G, '小数点なし Excellon は Unsupported', ok)
    b = sb.base2(); p = b.write_d356(os.path.join(TMP, 'b.d356')); pts = gl.parse_d356(p)
    u = {(q['ref'], q['pin']): q for q in pts}
    rec(G, 'IPC-D-356 (CUST 1): 件数・座標・access・穴', len(pts) == 8 and u[('U2', '1')]['acc'] == 2 and abs(u[('J1', '1')]['x'] - 15) < 1e-9
        and abs(u[('J1', '1')]['hole'] - 1.0) < 1e-9 and u[('J1', '1')]['plated'] is True, f'{len(pts)} 件')
    p2 = os.path.join(TMP, 'alias.d356')
    open(p2, 'w').write('P  UNITS CUST 1\nP  NNAMEM0000 /VERY/LONG/NET/NAME\n327M0000           U1    1   ' + ' ' * 6 + 'A01X+001000Y+001000X0500Y0500R000S0\n999\n')
    rec(G, 'IPC-D-356 NNAME 別名を戻す', gl.parse_d356(p2)[0]['net'] == '/VERY/LONG/NET/NAME')


# ------------------------------------------------------------ C. 合成基板 LVS
def run(b, name, **kw):
    d = os.path.join(TMP, name); shutil.rmtree(d, ignore_errors=True)
    g = b.write(os.path.join(d, 'g'), **{k: v for k, v in kw.items() if k in ('skip_layers', 'rename')})
    ref = kw.get('ref', b)
    d356 = ref.write_d356(os.path.join(d, 'ref.d356'))
    try:
        return gv.LVS(g, d356, gv.read_rules(None), log=lambda *a: None).run(), None
    except gl.Unsupported as e: return None, f'Unsupported: {e}'
    except Exception as e: return None, f'{type(e).__name__}: {e}'


def codes(l, lv='error'): return sorted({f['code'] for f in l.findings if f['level'] == lv})


def E(l, code): return [f for f in l.findings if f['level'] == 'error' and f['code'] == code]


def test_lvs():
    G = 'synth-lvs'
    l, err = run(sb.base2(), 'ok2')
    rec(G, '2 層 正常 → error 0 / warning 0', l and not codes(l) and not codes(l, 'warning'), err or f"{codes(l)} {codes(l, 'warning')}")
    l, err = run(sb.base4_blind(), 'ok4')
    rec(G, '4 層 ブラインド(L1-2)・ベリード(L2-3)・スルー via 正常 → error 0', l and not codes(l), err or str([f['msg'] for f in l.findings if f['level'] == 'error']))
    # --- 故障注入 (製造データだけ壊す。正解は元のまま)
    def inj(name, mut, expect, check=None, **kw):
        b = sb.base2() if not name.startswith('4L') else sb.base4_blind(); ref = b.clone(); mut(b)
        l, err = run(b, 'inj-' + str(len(rows)), ref=ref, **kw)
        if l is None:
            rec(G, f'{name} → {expect}', expect == 'exit2', err); return
        ok = expect in codes(l) and (check(l) if check else True)
        rec(G, f'{name} → {expect}', ok, f'error={codes(l)}')
    inj('配線削除 (表 SIG_B)', lambda b: b.tracks.remove((1, 'SIG_B', (3, 7), (15, 7), 0.25)), 'OPEN', lambda l: E(l, 'OPEN')[0]['net'] == 'SIG_B')
    def gap(g):
        return lambda b: (b.tracks.remove((1, 'SIG_B', (3, 7), (15, 7), 0.25)), b.tracks.extend([(1, 'SIG_B', (3, 7), (8.875, 7), 0.25), (1, 'SIG_B', (9.125 + g, 7), (15, 7), 0.25)]))
    inj('配線の 30µm 切れ目', gap(0.03), 'OPEN')
    b = sb.base2(); ref = b.clone(); gap(0.01)(b); l, err = run(b, 'gap10', ref=ref)
    rec(G, '既知の限界: 10µm の切れ目は描画の太りで つながって見える (変わったら見直す)', l is not None and 'OPEN' not in codes(l), err or str(codes(l)))
    inj('別ネット間に 0.1 mm 線を追加', lambda b: b.tracks.append((1, 'SIG_B', (6, 4), (6, 7), 0.1)), 'SHORT', lambda l: set(E(l, 'SHORT')[0]['nets']) == {'SIG_A', 'SIG_B'})
    def W(l, code): return [f for f in l.findings if f['level'] == 'warning' and f['code'] == code]
    def inj_w(name, mut, code):
        b = sb.base2(); ref = b.clone(); mut(b); l, err = run(b, 'injw-' + str(len(rows)), ref=ref)
        rec(G, f'{name} → {code} (warning)', l and W(l, code), err or f"warning={codes(l, 'warning')} error={codes(l)}")
    inj_w('pad 削除 (U2-2、配線の端が pad 中心に残る)', lambda b: b.pads.__setitem__(3, dict(b.pads[3], layer=99)), 'PAD_COPPER_INCOMPLETE')
    inj('pad 削除 (U3-1、ベタも無い)', lambda b: (b.pads.__setitem__(4, dict(b.pads[4], layer=99)), b.zones.clear()), 'NO_COPPER_AT_PAD')
    inj('ベタ削除 (GND: U3-1 と J1-1 が切れる)', lambda b: b.zones.clear(), 'OPEN', lambda l: E(l, 'OPEN')[0]['net'] == 'GND')
    inj('via の穴削除', lambda b: b.holes.pop(0), 'MISSING_HOLE')
    b = sb.base2(); ref = b.clone(); b.holes[0].update(no_ring=(1, 2)); l, err = run(b, 'noring', ref=ref)
    rec(G, 'via の両面ランド削除 (配線の端が穴の壁に接する) → 接続のまま (物理的に正しい)', l is not None and not codes(l), err or str(codes(l)))
    def ringcut(b):
        b.holes[0].update(no_ring=(1, 2)); b.tracks[0] = (1, 'SIG_A', (3, 4), (8.5, 4), 0.25); b.tracks[1] = (2, 'SIG_A', (9.5, 4), (15, 4), 0.25)
    inj('via のランド削除 + 配線が穴に届かない', ringcut, 'OPEN')
    inj('ベタのサーマル切断 (J1 の周りを全部抜く)', lambda b: b.raw.setdefault(1, []).append('%LPC*%\n%ADD90C,2.4*%\nD90*\nX15000000Y10000000D03*\n%LPD*%\n'), 'OPEN', lambda l: E(l, 'OPEN')[0]['net'] == 'GND')
    inj_w('裏面 SMD の pad を表に出力 (層違い、裏は配線の端だけ)', lambda b: b.pads[1].update(layer=1), 'PAD_COPPER_INCOMPLETE')
    inj('銅層 B.Cu 丸ごと欠落', lambda b: None, 'LAYER_MISSING', skip_layers=(2,))
    inj('4L 内層 In1 丸ごと欠落', lambda b: None, 'LAYER_MISSING', skip_layers=(2,))
    inj('4L 層取り違え (In1/In2 のファイル名を入れ替え)', lambda b: None, 'LAYER_NAME_MISMATCH', rename={2: 'In2_Cu', 3: 'In1_Cu'})
    inj('4L ベリード via (L2-3) の穴削除', lambda b: b.holes.pop(1), 'OPEN', lambda l: E(l, 'OPEN')[0]['net'] == 'SIG_C')
    b = sb.base4_blind(); b.tracks.append((3, 'SIG_D', (6, 2), (6, 6), 0.2)); b.tracks.append((3, 'SIG_D', (6, 2), (10, 8), 0.2)); ref = b.clone()
    l, err = run(b, 'blind-ok', ref=ref)
    rec(G, '4L ブラインド via (L1-2) の下 (L3) を別ネットが通る → error 0', l and not codes(l), err or str(codes(l)))
    b.holes[0].update(span=(1, 4)); l, err = run(b, 'blind-thru', ref=ref)
    rec(G, '4L ブラインド via を L1-L4 スルーと出力 (L3 の別ネットを貫通) → SHORT', l and 'SHORT' in codes(l), err or str(codes(l)))
    def shift(b):
        for t in list(b.tracks):
            if t[0] == 1: b.tracks[b.tracks.index(t)] = (1, t[1], (t[2][0] + 0.5, t[2][1] + 0.5), (t[3][0] + 0.5, t[3][1] + 0.5), t[4])
        for p in b.pads:
            if p['layer'] == 1: p['x'] += 0.5; p['y'] += 0.5
    inj('表層だけ原点ずれ (+0.5 mm)', shift, 'OPEN')
    inj('drill 単位違い (インチの数値に METRIC 宣言)', lambda b: setattr(b, 'drill_unit', 'BADINCH'), 'exit2')
    # 単位混在 (インチの Gerber + mm drill) は正しく換算されて error 0 のはず
    b = sb.base2(); ref = b.clone(); b.unit = 'IN'
    l, err = run(b, 'inch', ref=ref)
    rec(G, 'インチ Gerber + mm drill (正しい換算) → error 0', l and not codes(l), err or str(codes(l)))
    b = sb.base2(); ref = b.clone(); b.drill_unit = 'INCH'
    l, err = run(b, 'inchdrl', ref=ref)
    rec(G, 'インチ drill (正しい宣言) → error 0', l and not codes(l), err or str(codes(l)))
    # 正解側だけ違う: 回路図変更が gerber に反映されていない
    b = sb.base2(); ref = b.clone(); ref.pads[3]['net'] = 'SIG_X'
    l, err = run(b, 'refchg', ref=ref)
    rec(G, '正解だけネット変更 (gerber が古い) → SHORT', l and 'SHORT' in codes(l), err or str(codes(l)))
    # 外形 clearance ちょうど (補助検査の境界値)
    b = sb.base2(); ref = b.clone(); b.pads.append(dict(ref='TP', pin='9', net='', layer=1, x=10, y=12 - 0.2 - 0.5, w=1, h=1))
    l, err = run(b, 'edge-eq', ref=ref)
    rec(G, '外形 clearance ちょうど 0.2 mm → EDGE_CLEARANCE を出さない', l and not E(l, 'EDGE_CLEARANCE'), err or str(codes(l)))
    b.pads[-1]['y'] = 12 - 0.15 - 0.5
    l, err = run(b, 'edge-lt', ref=ref)
    rec(G, '外形 clearance 0.15 mm (< 0.2) → EDGE_CLEARANCE', l and E(l, 'EDGE_CLEARANCE'), err or str(codes(l)))
    # X2 ネット属性との突き合わせ (別実装の正解): 製造データ自身の TO.N と抽出接続が一致するか
    l, _ = run(sb.base2(), 'x2')
    rec(G, 'X2 TO.N 属性つき (正常) は SHORT 位置特定なしで一致', l and not E(l, 'SHORT'))


def test_input_b():
    """2026-10-14 追加: LAYER_OFFSET / X2 ネット属性との突き合わせ / .gbrjob"""
    import json
    G = 'input'
    def b4v():
        b = sb.base2()
        for i, x in enumerate((6, 11, 13)):  # via を足して穴を 5 個に
            b.holes.append(dict(net='SIG_B', x=x, y=7, d=0.3, ring=0.6, span=(1, 2)))
        return b
    l, err = run(b4v(), 'off-ok'); rec(G, '原点ずれなし → LAYER_OFFSET 無し', l and not E(l, 'LAYER_OFFSET'), err or str(codes(l)))
    for dx, dy in ((0.5, 0.5), (0.1, 0.0), (-2.54, 0)):
        b = b4v(); ref = b.clone(); d = os.path.join(TMP, f'off{dx}'); shutil.rmtree(d, ignore_errors=True); b.write(os.path.join(d, 'g'))
        f = os.path.join(d, 'g', 'sb-B_Cu.gbr'); txt = open(f).read()
        import re as _re
        txt = _re.sub(r'^X(-?\d+)Y(-?\d+)', lambda m: f'X{int(m.group(1)) + round(dx * 1e6)}Y{int(m.group(2)) + round(dy * 1e6)}', txt, flags=_re.M); open(f, 'w').write(txt)
        l = gv.LVS(os.path.join(d, 'g'), ref.write_d356(os.path.join(d, 'r.d356')), gv.read_rules(None), log=lambda *a: None).run()
        o = E(l, 'LAYER_OFFSET')
        rec(G, f'B.Cu だけ ({dx:+}, {dy:+}) mm ずらす → LAYER_OFFSET (B.Cu、ずれ量が合う)', len(o) == 1 and o[0]['layer'] == 'B.Cu' and o[0]['offset'] == [dx, dy], str([(f['layer'], f['offset']) for f in o]))
    # X2
    b = sb.base2(); ref = b.clone(); ref.pads[3]['net'] = 'SIG_X'
    l, err = run(b, 'x2diff', ref=ref)
    x = [f for f in l.findings if f['code'] == 'X2_NET_DIFFERS']
    sh = E(l, 'SHORT')
    rec(G, '正解だけネット変更 → X2_NET_DIFFERS (info) で U2-2 の X2 属性 SIG_B を示す', x and x[0]['pad'] == 'U2-2' and x[0]['x2_nets'] == ['SIG_B'] and x[0]['level'] == 'info', str([(f['pad'], f['x2_nets']) for f in x]))
    rec(G, 'SHORT に x2_nets (その銅の X2 ネット) を付ける', sh and sh[0].get('x2_nets') == ['SIG_B'], str(sh[0].get('x2_nets') if sh else None))
    b = sb.base2(); ref = b.clone(); b.tracks.remove((1, 'SIG_B', (3, 7), (15, 7), 0.25)); l, _ = run(b, 'x2open', ref=ref)
    op = E(l, 'OPEN'); rec(G, 'OPEN に群ごとの X2 ネット (x2_nets_per_group)', op and op[0].get('x2_nets_per_group') == [['SIG_B'], ['SIG_B']], str(op[0].get('x2_nets_per_group') if op else None))
    l, _ = run(sb.base2(), 'x2ok'); rec(G, '正常 → X2_NET_DIFFERS 無し', not [f for f in l.findings if f['code'] == 'X2_NET_DIFFERS'])
    b = sb.base2(); d = os.path.join(TMP, 'nox2'); shutil.rmtree(d, ignore_errors=True); b.write(os.path.join(d, 'g'))
    for f in os.listdir(os.path.join(d, 'g')):
        p = os.path.join(d, 'g', f); keep = [ln for ln in open(p) if not ln.startswith(('%TO', '%TD'))]; open(p, 'w').write(''.join(keep))
    l = gv.LVS(os.path.join(d, 'g'), b.write_d356(os.path.join(d, 'r.d356')), gv.read_rules(None), log=lambda *a: None).run()
    rec(G, 'X2 属性なし → 突き合わせを省略 (注記)、error 0', not codes(l) and any('X2' in n for n in l.notes))
    # gbrjob
    def job(b, d, mut=None):
        fa = [dict(Path=f'sb-{b.lname(i)}.gbr', FileFunction=f'Copper,L{i},{b.side(i)}', FilePolarity='Positive') for i in range(1, b.n + 1)]
        fa.append(dict(Path='sb-Edge_Cuts.gm1', FileFunction='Profile', FilePolarity='Positive'))
        j = dict(GeneralSpecs=dict(LayerNumber=b.n), FilesAttributes=fa)
        if mut: mut(j)
        json.dump(j, open(os.path.join(d, 'sb-job.gbrjob'), 'w'))
    def jrun(nm, mut, b=None, **kw):
        b = b or sb.base2(); d = os.path.join(TMP, 'job-' + nm); shutil.rmtree(d, ignore_errors=True); g = b.write(os.path.join(d, 'g'), **kw); job(b, g, mut)
        return gv.LVS(g, b.write_d356(os.path.join(d, 'r.d356')), gv.read_rules(None), log=lambda *a: None).run()
    l = jrun('ok', None); rec(G, '.gbrjob と一致 → GBRJOB_MISMATCH 無し', not [f for f in l.findings if f['code'] == 'GBRJOB_MISMATCH'], str(codes(l)))
    l = jrun('n', lambda j: j['GeneralSpecs'].update(LayerNumber=4)); rec(G, '.gbrjob の層数 4 / 銅層 2 → GBRJOB_MISMATCH (error)', E(l, 'GBRJOB_MISMATCH'))
    l = jrun('miss', None, skip_layers=(2,)); rec(G, '.gbrjob にある B.Cu が無い → GBRJOB_MISMATCH (error)', any('無い' in f['msg'] for f in E(l, 'GBRJOB_MISMATCH')))
    l = jrun('ff', lambda j: j['FilesAttributes'][0].update(FileFunction='Copper,L2,Bot')); rec(G, '.gbrjob の層機能が中身と違う → GBRJOB_MISMATCH (error)', E(l, 'GBRJOB_MISMATCH'))
    l = jrun('pol', lambda j: j['FilesAttributes'][1].update(FilePolarity='Negative')); rec(G, '.gbrjob で銅層が負極性 → GBRJOB_MISMATCH (error)', E(l, 'GBRJOB_MISMATCH'))
    l = jrun('extra', lambda j: j['FilesAttributes'].append(dict(Path='sb-F_Mask.gts', FileFunction='SolderMask,Top', FilePolarity='Negative')))
    w = [f for f in l.findings if f['code'] == 'GBRJOB_MISMATCH']
    rec(G, '.gbrjob にある mask が無い → GBRJOB_MISMATCH (warning、銅以外)', w and all(f['level'] == 'warning' for f in w))
    l = jrun('unl', lambda j: j['FilesAttributes'].pop(1))
    rec(G, '銅層が .gbrjob に載っていない → GBRJOB_MISMATCH (warning)', [f['level'] for f in l.findings if f['code'] == 'GBRJOB_MISMATCH'] == ['warning'])


def test_gaps():
    """2026-10-14 カバレッジで見つけたシナリオ不足を埋める"""
    import json, re as _re, subprocess, io, contextlib
    G = 'gaps'
    def unsup(body, aps='%ADD10C,1*%\n', **kw):
        try: draw(gfile(body, aps, **kw)); return False, '例外なし'
        except gl.Unsupported as e: return True, str(e)
    for body, what in [('FOO*\n', '知らない命令'), ('G12*\n', '知らない G コード (G12)'), ('%IPNEG*%\nD10*\nX0Y0D03*\n', 'IPNEG (像の反転)')]:
        ok, why = unsup(body); rec(G, f'Unsupported: {what}', ok, why[:60])
    p = os.path.join(TMP, 'nofs.gbr'); open(p, 'w').write('%MOMM*%\n%ADD10C,1*%\nD10*\nX0Y0D03*\nM02*\n')
    try: gl.Gerber(p); ok = False
    except gl.Unsupported: ok = True
    rec(G, 'Unsupported: %FS より前の座標', ok)
    p = os.path.join(TMP, 'g70.gbr'); open(p, 'w').write('%FSLAX24Y24*%\nG70*\n%ADD10C,0.02*%\nG54D10*\nX1000Y0D03*\nM02*\n')
    g = gl.Gerber(p); R = gl.Raster(-3, -3, 3, 3, 0.01); im = np.asarray(gl.render(g, R)) > 127
    rec(G, '古い書き方 G70 (インチ) + G54D10 → 0.1 in = 2.54 mm に flash', at(im, R, 2.54, 0) and not at(im, R, 0.1, 0), '修正: 以前は G70 を無視して単位なし')
    im, R = draw(gfile('D10*\nX-1000000Y0D02*\nX1000000Y0D01*\nY1000000*\n', '%ADD10C,0.2*%\n'))
    rec(G, 'D 無しの座標は直前の D01 を引き継ぐ (古い書き方)', at(im, R, 0, 0) and at(im, R, 1, 0.5))
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%AMP5*\n5,1,6,0,0,2,0*%\n%ADD10P5*%\n'))
    rec(G, 'マクロ 5 (正多角形、6 角 φ2) の面積 ≒ 2.598', near(area(im), 1.5 * math.sqrt(3), ab=0.06), f'{area(im):.3f}')
    im, R = draw(gfile('D10*\nX0Y0D03*\n', '%AMLL*\n22,1,2,1,0,0,0*%\n%ADDLL*%\n'.replace('ADDLL', 'ADD10LL')))
    rec(G, 'マクロ 22 (左下基準の矩形 2×1)', at(im, R, 1.5, 0.5) and not at(im, R, -0.5, 0.5))
    p = os.path.join(TMP, 'u.drl'); open(p, 'w').write('M48\nMETRIC\nT1C0.3\n%\nT1\nFOO\nM30\n')
    try: gl.parse_drill(p, 2); ok = False
    except gl.Unsupported: ok = True
    rec(G, 'Excellon の知らない行は Unsupported', ok)
    loops, nopen = gl.outline_loops([((0, 0), (1, 0)), ((1, 1), (1, 0)), ((1, 1), (0, 1)), ((0, 0), (0, 1))])
    rec(G, '外形の線分の向きが逆でも閉路にする', len(loops) == 1 and nopen == 0)
    # --- 入力の発見
    b = sb.base2(); d = os.path.join(TMP, 'nox2ff', 'g'); shutil.rmtree(os.path.dirname(d), ignore_errors=True); b.write(d)
    for f in os.listdir(d):
        q = os.path.join(d, f); keep = [ln for ln in open(q) if not ln.startswith('%TF') and not ln.startswith('; #@! TF')]; open(q, 'w').write(''.join(keep))
    for f in os.listdir(d):  # NPTH ファイルはめっき無しを属性で書いているので、属性を消したら外す
        if 'NPTH' in f: os.remove(os.path.join(d, f))
    ref = b.clone(); ref.npth = []
    l = gv.LVS(d, ref.write_d356(os.path.join(TMP, 'nox2ff', 'r.d356')), gv.read_rules(None), log=lambda *a: None).run()
    open(os.path.join(d, 'sb-F_Paste.gtp'), 'w').write('%FSLAX46Y46*%\n%MOMM*%\n%ADD10R,0.8X0.8*%\nD10*\nX3000000Y4000000D03*\nM02*\n')
    rec(G, 'FileFunction 属性の無い paste もファイル名で見つける', 'top' in gv.discover(d)[2])
    rec(G, 'FileFunction 属性の無い古い gerber はファイル名で層を決める (B_Cu は最後の層)', l.layer_names == ['F.Cu', 'B.Cu'] and not codes(l), str(l.layer_names) + str(codes(l)))
    P = os.path.join(TMP, 'proj'); shutil.rmtree(P, ignore_errors=True)
    for r in ('rev0.9', 'rev0.10', 'rev0.2'): os.makedirs(os.path.join(P, 'out', r, 'gerber'))
    rec(G, 'find_gerber_dir: out/gerber が無ければ版フォルダの新しい順 (rev0.10 > rev0.9)', gv.find_gerber_dir(P).endswith('rev0.10/gerber'))
    rec(G, 'find_gerber_dir: 版フォルダも無い → out/gerber', gv.find_gerber_dir(os.path.join(TMP, 'none')).endswith('out/gerber'))
    pcb = os.path.join(TMP, 'a.kicad_pcb'); open(pcb, 'w').write('(kicad_pcb\n (setup\n )\n (footprint "x"\n')
    rec(G, 'aux_origin: aux_axis_origin が無ければ (0, 0)、ファイルが無ければ None', gv.aux_origin(pcb) == (0.0, 0.0) and gv.aux_origin(pcb + '.no') is None)
    # --- 原点・穴
    b = sb.base2(); ref = b.clone()
    for p_ in ref.pads: p_['x'] += 1; p_['y'] += 1
    for h in ref.holes: h['x'] += 1; h['y'] += 1
    ref.npth = [(x + 1, y + 1, dd) for x, y, dd in ref.npth]
    l, err = run(b, 'orig', ref=ref)
    rec(G, 'D356 だけ原点が (1, 1) mm 違う → 補正して error 0 (注記)', l and not codes(l) and l.off == (-1.0, -1.0) and any('原点' in n for n in l.notes), err or str(codes(l)))
    b = sb.base2(); ref = b.clone(); ref.holes[0]['x'] += 3; ref.holes[1]['x'] -= 4; ref.npth = [(5, 5, 1.0)]
    l, err = run(b, 'orig-bad', ref=ref)
    rec(G, 'drill と D356 の穴位置が合わない → 実行できない', l is None and '穴位置が合わない' in err, err)
    b = sb.base2(); ref = b.clone(); b.npth = []
    l, err = run(b, 'npthmiss', ref=ref); rec(G, 'D356 の NPTH が drill に無い → MISSING_NPTH (warning)', l and 'MISSING_NPTH' in codes(l, 'warning'), err or '')
    b = sb.base2(); ref = b.clone(); b.holes[1]['d'] = 1.1
    l, err = run(b, 'hsz', ref=ref); rec(G, '穴径が違う (φ1.0 → 1.1) → HOLE_SIZE_MISMATCH (warning)', l and 'HOLE_SIZE_MISMATCH' in codes(l, 'warning'), err or str(codes(l, 'warning')))
    b = sb.base2(); ref = b.clone(); b.pads[2].update(layer=99)
    b.tracks[2] = (1, 'SIG_B', (3.3, 7), (15, 7), 0.25); b.pads.append(dict(ref='Z', pin='1', net='SIG_B', layer=1, x=2.85, y=7.1, w=0.1, h=0.1))
    l, err = run(b, 'amb', ref=ref); rec(G, 'pad 中心に銅が無く近くに島が 2 個 → PAD_AMBIGUOUS (warning)', l and 'PAD_AMBIGUOUS' in codes(l, 'warning'), err or str(codes(l, 'warning')))
    b = sb.base2(); b.pads.append(dict(ref='TP', pin='1', net='N/C', layer=1, x=10, y=7, w=0.5, h=0.5)); ref = b.clone()
    l, err = run(b, 'nc', ref=ref); rec(G, 'ネット無し pad が配線に載る → NC_PAD_CONNECTED (warning)', l and 'NC_PAD_CONNECTED' in codes(l, 'warning'), err or '')
    b = sb.base4_blind(); b.holes[2]['span'] = (3, 4); b.holes.pop(3); b.pads[3].update(layer=1, x=17, y=8); b.tracks[5] = (1, 'SIG_D', (10, 8), (17, 8), 0.2); b.tracks[4] = (1, 'SIG_D', (3, 8), (10, 8), 0.2)
    l, err = run(b, 'noth')
    rec(G, 'スルーホールが 1 つも無い (ブラインド/ベリードだけ) → 原点の自己チェックを省略 (注記)、error 0', l and not codes(l) and any('スルーホールが無い' in n for n in l.notes), err or str(codes(l)))
    # --- 外形が region / 単位混在
    b = sb.base2(); d = b.write(os.path.join(TMP, 'edgereg', 'g'))
    sb.gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', 'G36*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y12000000D01*\nX0Y12000000D01*\nX0Y0D01*\nG37*\n')
    l = gv.LVS(d, b.write_d356(os.path.join(TMP, 'edgereg', 'r.d356')), gv.read_rules(None), log=lambda *a: None, aux_checks=True).run()
    rec(G, '外形が region (G36) で書かれている → 閉路 1、error 0', len(l.loops) == 1 and not codes(l), str(codes(l)))
    b = sb.base2(); b.unit = 'IN'; d = b.write(os.path.join(TMP, 'mix', 'g'))
    sb.gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', '%ADD10C,0.05*%\nD10*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y12000000D01*\nX0Y12000000D01*\nX0Y0D01*\n')
    l = gv.LVS(d, sb.base2().write_d356(os.path.join(TMP, 'mix', 'r.d356')), gv.read_rules(None), log=lambda *a: None).run()
    rec(G, '銅はインチ・外形は mm (単位混在) → 換算して error 0、注記', not codes(l) and any('混在' in n for n in l.notes), str(codes(l)))
    # --- 旧動作 (--aux) の補助検査: 移設までの互換 (gerber-drc と同じコード)
    b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=1, x=18.5 + 0.85, y=2, w=0.5, h=0.5)); ref = b.clone()
    d = b.write(os.path.join(TMP, 'aux', 'g')); sb.gerber(os.path.join(d, 'sb-F_Paste.gtp'), 'Paste,Top', 'D10*\nX6000000Y5500000D03*\n', '%ADD10R,0.8X0.8*%\n')
    l = gv.LVS(d, ref.write_d356(os.path.join(TMP, 'aux', 'r.d356')), gv.read_rules(None), log=lambda *a: None, aux_checks=True).run()
    rec(G, '--aux: NPTH_CLEARANCE と PASTE_WITHOUT_COPPER', {'NPTH_CLEARANCE', 'PASTE_WITHOUT_COPPER'} <= set(codes(l, 'warning')), str(codes(l, 'warning')))
    sb.gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', '%ADD10C,0.05*%\nD10*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y12000000D01*\n')
    l = gv.LVS(d, ref.write_d356(os.path.join(TMP, 'aux', 'r.d356')), gv.read_rules(None), log=lambda *a: None, aux_checks=True).run()
    rec(G, '--aux: 外形が開いている → OUTLINE_OPEN', 'OUTLINE_OPEN' in codes(l, 'warning'))
    # 内部切り欠き (外形の中の閉路) と、その中の銅
    b = sb.base2(); b.pads.append(dict(ref='X', pin='1', net='', layer=2, x=10.5, y=10.5, w=0.4, h=0.4)); ref = b.clone()
    d = b.write(os.path.join(TMP, 'cut', 'g'))
    sb.gerber(os.path.join(d, 'sb-Edge_Cuts.gm1'), 'Profile,NP', '%ADD10C,0.05*%\nD10*\nX0Y0D02*\nX20000000Y0D01*\nX20000000Y12000000D01*\nX0Y12000000D01*\nX0Y0D01*\n'
              'X10000000Y10000000D02*\nX11000000Y10000000D01*\nX11000000Y11000000D01*\nX10000000Y11000000D01*\nX10000000Y10000000D01*\n')
    l = gv.LVS(d, ref.write_d356(os.path.join(TMP, 'cut', 'r.d356')), gv.read_rules(None), log=lambda *a: None, aux_checks=True).run()
    rec(G, '--aux: 内部切り欠き 1 個、その中の銅 → COPPER_OUTSIDE_BOARD', l.n_cutouts == 1 and E(l, 'COPPER_OUTSIDE_BOARD'), f'{l.n_cutouts} {codes(l)}')
    # --- .gbrjob の異常
    b = sb.base2(); d = b.write(os.path.join(TMP, 'job2', 'g')); r356 = b.write_d356(os.path.join(TMP, 'job2', 'r.d356'))
    open(os.path.join(d, 'a.gbrjob'), 'w').write('{not json'); open(os.path.join(d, 'b.gbrjob'), 'w').write('{}')
    l = gv.LVS(d, r356, gv.read_rules(None), log=lambda *a: None).run(); w = [f['msg'] for f in l.findings if f['code'] == 'GBRJOB_MISMATCH']
    rec(G, '.gbrjob が 2 個 / 読めない → GBRJOB_MISMATCH (warning)', any('2 個' in m for m in w) and any('読めない' in m for m in w), str(w))
    os.remove(os.path.join(d, 'a.gbrjob')); os.remove(os.path.join(d, 'b.gbrjob'))
    sb.gerber(os.path.join(d, 'sb-extra.gbr'), 'Legend,Top', '')
    json.dump(dict(GeneralSpecs=dict(LayerNumber=2), FilesAttributes=[dict(Path='sb-extra.gbr', FileFunction='Copper,L1,Top')]), open(os.path.join(d, 'j.gbrjob'), 'w'))
    l = gv.LVS(d, r356, gv.read_rules(None), log=lambda *a: None).run(); e = [f['msg'] for f in E(l, 'GBRJOB_MISMATCH')]
    rec(G, '.gbrjob では銅層なのに中身は silk → GBRJOB_MISMATCH (error)', any('読めなかった' in m for m in e), str(e))
    # --- PNG と KiCad 座標
    for nm, mut in (('open', lambda b: b.tracks.remove((1, 'SIG_B', (3, 7), (15, 7), 0.25))), ('short', lambda b: b.tracks.append((1, 'SIG_B', (6, 4), (6, 7), 0.1)))):
        b = sb.base2(); ref = b.clone(); mut(b); d = b.write(os.path.join(TMP, 'png-' + nm, 'g'))
        l = gv.LVS(d, ref.write_d356(os.path.join(TMP, 'png-' + nm, 'r.d356')), gv.read_rules(None), aux=(100.0, 100.0), log=lambda *a: None).run()
        od = os.path.join(TMP, 'png-' + nm, 'png'); l.png(od); w = l.png(od)
        f0 = E(l, nm.upper())[0]
        rec(G, f'PNG ({nm.upper()}): 2 回出しても 1 枚、KiCad 座標 (aux 原点 100,100) を付ける', len(w) == 1 and len(os.listdir(od)) == 1 and 'at_kicad' in f0 and f0['at_kicad'][0] > 100, f"{w} {f0.get('at_kicad')}")
    # --- CLI
    b = sb.base2(); ref = b.clone(); b.tracks.pop(); d = b.write(os.path.join(TMP, 'cli', 'g')); r356 = ref.write_d356(os.path.join(TMP, 'cli', 'r.d356'))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc1 = gv.main(['--gerber-dir', d, '--d356', r356, '--json', os.path.join(TMP, 'cli', 'j', 'x.json')])
        rc0 = gv.main(['--gerber-dir', os.path.join(TMP, 'none'), '--d356', r356, '--out-dir', os.path.join(TMP, 'cli', 'o')])
        bad = os.path.join(TMP, 'cli', 'bad'); shutil.copytree(d, bad); open(os.path.join(bad, 'sb-F_Cu.gbr'), 'a').write('FOO*\n')
        txt = open(os.path.join(bad, 'sb-F_Cu.gbr')).read().replace('M02*\nFOO*\n', 'FOO*\nM02*\n'); open(os.path.join(bad, 'sb-F_Cu.gbr'), 'w').write(txt)
        rcu = gv.main(['--gerber-dir', bad, '--d356', r356, '--out-dir', os.path.join(TMP, 'cli', 'o'), '-q'])
        try: gv.main(['--gerber-dir', d]); rce = 0
        except SystemExit as e: rce = e.code
    rec(G, 'CLI --gerber-dir --d356 --json (out-dir 無し): error ありで exit 1、txt も json の場所に', rc1 == 1 and os.path.exists(os.path.join(TMP, 'cli', 'j', 'gerber_lvs.txt')))
    rec(G, 'CLI: gerber が無い → exit 2 (traceback 付き)、未対応構文 → exit 2、引数不足 → usage エラー', rc0 == 2 and rcu == 2 and rce == 2, f'{rc0} {rcu} {rce}')


def test_far_open():
    """回帰 (2026-10-16 kreview 担当報告): 未配線の端点が 40 mm 以上離れた OPEN で --png の切り抜き範囲が負になり
    negative dimensions で止まっていた。txt/json/PNG を出して exit 1 (fail) になること"""
    import json, io, contextlib
    from PIL import Image
    G = 'far-open'
    b = sb.Board(2, w=50.0, h=46.0)
    b.pads += [dict(ref='U1', pin='1', net='SIG_X', layer=1, x=2, y=3, w=1, h=1), dict(ref='J1', pin='1', net='SIG_X', layer=1, x=48, y=3, w=1, h=1),
               dict(ref='U1', pin='2', net='SIG_D', layer=1, x=2, y=8, w=1, h=1), dict(ref='J2', pin='1', net='SIG_D', layer=2, x=47, y=44, w=1, h=1),
               dict(ref='U1', pin='3', net='SIG_Y', layer=2, x=10, y=1.5, w=1, h=1), dict(ref='J3', pin='1', net='SIG_Y', layer=2, x=10, y=44.5, w=1, h=1)]
    b.holes += [dict(ref='J4', pin='1', net='GND', x=25, y=23, d=1.0, ring=1.7, span=(1, 2)), dict(ref='J4', pin='2', net='GND2', x=30, y=23, d=1.0, ring=1.7, span=(1, 2))]
    ref = b  # 配線が 1 本も無い = どのネットも端点どうしが未配線 (D356 は pad だけ見るので正解も同じ)
    d0 = os.path.join(TMP, 'far'); g = b.write(os.path.join(d0, 'g')); r356 = ref.write_d356(os.path.join(d0, 'r.d356'))
    l = gv.LVS(g, r356, gv.read_rules(None), px=0.02, log=lambda *a: None).run()  # 50×46 mm なので高速スイート用に粗く
    op = E(l, 'OPEN')
    rec(G, '46 mm (横)・58 mm (斜め)・43 mm (縦) 離れた未配線 → OPEN 3 件', sorted(f['net'] for f in op) == ['SIG_D', 'SIG_X', 'SIG_Y'], str([f['msg'] for f in op]))
    try: w = l.png(os.path.join(d0, 'png')); err = ''
    except Exception as e: w = []; err = f'{type(e).__name__}: {e}'
    sizes = [Image.open(f).size for f in w]
    rec(G, 'LVS.png: 例外なしで OPEN ごとに 1 枚、空でない画像', not err and len(w) == 3 and all(min(s) > 0 for s in sizes), err or str(sizes))
    rec(G, 'LVS.png: 長い OPEN は 40 mm 角に切り、印 (at) を含む', all('png_crop' in f for f in op), str([f.get('png_crop') for f in op]))
    l.findings.append(dict(level='error', code='MISSING_HOLE', msg='raster の外 (人工)', at=(500.0, 500.0)))
    try: w2 = l.png(os.path.join(d0, 'png2')); err = ''
    except Exception as e: w2 = []; err = f'{type(e).__name__}: {e}'
    rec(G, 'LVS.png: 印が raster の外 → その 1 枚だけ飛ばす (例外にしない)', not err and len(w2) == 3, err or str(w2))
    for png in (False, True):
        od = os.path.join(d0, 'cli-png' if png else 'cli')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as se:
            rc = gv.main(['--gerber-dir', g, '--d356', r356, '--out-dir', od, '--px', '0.02'] + (['--png'] if png else []))
        j = json.load(open(os.path.join(od, 'gerber_lvs.json'))) if os.path.exists(os.path.join(od, 'gerber_lvs.json')) else {}
        pngs = sorted(os.listdir(os.path.join(od, 'gerber_lvs'))) if os.path.isdir(os.path.join(od, 'gerber_lvs')) else []
        ok = rc == 1 and j.get('status') == 'fail' and len(j.get('opens', [])) == 3 and os.path.exists(os.path.join(od, 'gerber_lvs.txt')) and (len(pngs) == 3 if png else not pngs)
        rec(G, f'CLI {"--png あり" if png else "--png なし"}: exit 1 (fail)、txt/json に OPEN 3 件' + ('、PNG 3 枚' if png else ''), ok, f'rc={rc} status={j.get("status")} opens={len(j.get("opens", []))} png={pngs} {se.getvalue()[-300:]}')
    # PNG で何か起きても txt/json は出す (注に残して exit は検査結果どおり)
    od = os.path.join(d0, 'cli-pngfail'); orig = gv.LVS.png
    def boom(self, *a, **k): raise ValueError('synthetic png failure')
    gv.LVS.png = boom
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = gv.main(['--gerber-dir', g, '--d356', r356, '--out-dir', od, '--px', '0.02', '--png'])
    finally: gv.LVS.png = orig
    j = json.load(open(os.path.join(od, 'gerber_lvs.json'))) if os.path.exists(os.path.join(od, 'gerber_lvs.json')) else {}
    rec(G, 'CLI --png: PNG 作図が例外でも txt/json を出し、注 (notes) に残して exit 1', rc == 1 and j.get('status') == 'fail' and any('PNG' in n for n in j.get('notes', [])), f'rc={rc} notes={j.get("notes")}')


def main():
    try:
        for t in (test_syntax, test_drill_d356, test_lvs, test_input_b, test_gaps, test_far_open):
            try: t()
            except Exception: rec(t.__name__, '例外', False, traceback.format_exc()[-400:])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    n = sum(r[2] for r in rows); print(f'\n== {n}/{len(rows)} PASS (quick)'); return 0 if n == len(rows) else 1


if __name__ == '__main__':
    sys.exit(main())
