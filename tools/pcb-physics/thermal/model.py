"""model — 2.5D 熱回路網 (有限差分 = 有限体積) の組み立てと求解。
節点: 各「節点層」の各セル + 部品の接合節点。節点層は 銅層と誘電体の中央 (銅 n 枚なら 2n-1 枚)。
面内: シート熱コンダクタンス g [W/K] (正方セルなので 隣接 2 セルの調和平均がそのまま枝の値)。
厚さ方向: セルごとの枝 [W/K] (FR4 + ビア)。表面 (先頭・末尾の節点層): 対流 + 放射 (温度依存 → 反復)。"""
import os
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve, cg

DIRECT_MAX = 0         # auto: 未知数がこれ以下は直接法 (SuperLU)。0 = 常に AMG 前処理付き CG (0.25 mm でも 4.5 倍速く、直接法との差は回帰テストで確認)。pyamg が無ければ 20 万以下は直接法、超えると Jacobi CG
CG_RTOL = 1e-10        # 反復法の相対残差 (最後の解)。温度の誤差は 1e-6 ℃ 程度
AMG_THETA = 0.08      # 強い結合のしきい値。銅 (385) と FR4 (0.3〜0.8) の差が大きいので既定 0 より少し上げると CG 反復が 1/5 (0.1 mm で 80→14 回、2026-10-14 計測)
CG_RTOL_EARLY = 1e-6   # 放射の反復の途中 (変化が 10×tol より大きい間) は粗く解く (不正確 Picard)。最後は必ず CG_RTOL で解き直す


def _amg():
    try:
        import pyamg
        return pyamg
    except ImportError:
        return None


def _cupy():
    try:
        import cupy, cupyx.scipy.sparse, cupyx.scipy.sparse.linalg   # noqa: F401
        cupy.cuda.runtime.getDeviceCount()
        return cupy
    except Exception:
        return None


class GpuBackend:
    """GPU (CuPy) で CG。前処理: pyamg の階層 (CPU で作る) を GPU に載せた V サイクル (重み付き Jacobi 平滑化 2 回、最粗は密行列の逆) = AMG 代替、
    pyamg が無ければ Jacobi。emulate=True は同じコードを numpy / scipy で動かす (GPU の無い box でのテスト用、method 'gpu-emu')。"""

    def __init__(self, emulate=False):
        if emulate:
            import scipy.sparse as xsp, scipy.sparse.linalg as xla
            self.xp, self.sp, self.la, self.name = np, xsp, xla, 'CPU (numpy/scipy)'
        else:
            import cupy, cupyx.scipy.sparse as xsp, cupyx.scipy.sparse.linalg as xla
            self.xp, self.sp, self.la = cupy, xsp, xla
            self.name = 'gpu (' + cupy.cuda.runtime.getDeviceProperties(0)['name'].decode() + ')'
        self.M = None; self.prec = None

    def to(self, A):
        return self.sp.csr_matrix(A) if self.xp is np else self.sp.csr_matrix(A.tocsr())

    def setup(self, Kd):
        xp = self.xp; t = time_now()
        ml = _amg().smoothed_aggregation_solver(Kd, symmetry='symmetric', max_coarse=500, strength=('symmetric', {'theta': AMG_THETA})) if _amg() else None
        if ml is None:
            dinv = xp.asarray(1.0 / Kd.diagonal())
            self.prec = 'jacobi'
            self.M = self.la.LinearOperator(Kd.shape, matvec=lambda r: dinv * r, dtype=np.float64)
        else:
            self.prec = f'AMG V サイクル ({len(ml.levels)} 層、Jacobi 平滑化)'
            lv = []
            for L in ml.levels[:-1]:
                A = L.A.tocsr(); d = A.diagonal()
                rho = _rho_dinv_a(A, d)
                lv.append(dict(A=self.to(A), P=self.to(L.P), R=self.to(L.P.T.tocsr()), w=xp.asarray((4.0 / 3.0 / rho) / d)))
            Ac = ml.levels[-1].A.toarray(); Ainv = xp.asarray(np.linalg.pinv(Ac))

            def vcycle(b, k=0):
                if k == len(lv):
                    return Ainv @ b
                L = lv[k]; x = L['w'] * b
                x = x + L['w'] * (b - L['A'] @ x)
                r = b - L['A'] @ x
                x = x + L['P'] @ vcycle(L['R'] @ r, k + 1)
                x = x + L['w'] * (b - L['A'] @ x); x = x + L['w'] * (b - L['A'] @ x)
                return x
            self.M = self.la.LinearOperator(Kd.shape, matvec=vcycle, dtype=np.float64)
        self.lv_note = self.prec
        return time_now() - t

    def cg(self, Kd, rhs, x0, rtol, maxiter, counter):
        xp = self.xp
        A = self.to(Kd); b = xp.asarray(rhs); x = xp.asarray(x0)
        cb = lambda xk: counter.__setitem__(0, counter[0] + 1)
        try:
            y, info = self.la.cg(A, b, x0=x, rtol=rtol, atol=0.0, maxiter=maxiter, M=self.M, callback=cb)
        except TypeError:   # 古い CuPy は tol
            y, info = self.la.cg(A, b, x0=x, tol=rtol, atol=0.0, maxiter=maxiter, M=self.M, callback=cb)
        return (y if xp is np else xp.asnumpy(y)), info


def time_now():
    import time
    return time.time()


def _rho_dinv_a(A, d, it=15):
    """D⁻¹A の最大固有値の推定 (べき乗法)。重み付き Jacobi の重み 4/3/ρ に使う"""
    v = np.random.default_rng(0).random(A.shape[0]); r = 1.0
    for _ in range(it):
        w = (A @ v) / d; r = np.linalg.norm(w) / max(np.linalg.norm(v), 1e-300); v = w / max(np.linalg.norm(w), 1e-300)
    return r


def pick_method(method, N):
    if method in (None, 'auto'):
        return 'direct' if N <= DIRECT_MAX or (not _amg() and N <= 200_000) else ('amg' if _amg() else 'cg')
    if method in ('amg', 'amg-gs') and not _amg():
        return 'cg'
    if method == 'gpu' and not _cupy():   # CuPy (と GPU) が無ければ CPU に戻る
        return pick_method('auto', N)
    return method


class Network:
    def __init__(self, active, dx_m):
        self.active = active.astype(bool)          # (ny, nx)
        self.ny, self.nx = active.shape
        self.dx = dx_m
        self.layers = []      # 各節点層: dict(g=シート W/K (ny,nx), q=発熱 W (ny,nx))
        self.zlinks = []      # 層 l と l+1 の間: (ny,nx) W/K
        self.area = None      # 表面のセル面積 m² (ny,nx) (外形の割合込み)
        self.junctions = []   # dict(name, P, links=[(layer, idx_flat, G)], g_air=W/K)

    def add_layer(self, g, q=None, gx=None, gy=None):
        """g: セルのシートコンダクタンス (隣どうしは調和平均)。gx (ny,nx-1) / gy (ny-1,nx): 隣どうしのコンダクタンスを直接与える (あれば g より優先)"""
        self.layers.append(dict(g=np.where(self.active, g, 0.0), q=np.zeros_like(g) if q is None else q, gx=gx, gy=gy))

    def build(self):
        L = len(self.layers); ny, nx = self.ny, self.nx
        idx = -np.ones((L, ny, nx), int)
        n_cell = int(self.active.sum())
        for l in range(L):
            idx[l][self.active] = np.arange(n_cell) + l * n_cell
        self.idx = idx; self.n_cell = n_cell
        N = L * n_cell + len(self.junctions); self.N = N
        r, c, v = [], [], []

        def link(a, b, G):
            m = (a >= 0) & (b >= 0) & (G > 0)
            a, b, G = a[m], b[m], G[m]
            r.extend([a, b, a, b]); c.extend([a, b, b, a]); v.extend([G, G, -G, -G])
        for l, ly in enumerate(self.layers):
            g = ly['g']
            for (sa, sb), ge in ((((slice(None), slice(0, -1)), (slice(None), slice(1, None))), ly.get('gx')),
                                 (((slice(0, -1), slice(None)), (slice(1, None), slice(None))), ly.get('gy'))):
                g1, g2 = g[sa], g[sb]
                if ge is not None:
                    G = np.where((g1 > 0) & (g2 > 0), ge, 0.0)
                else:
                    G = np.where((g1 > 0) & (g2 > 0), 2 * g1 * g2 / np.maximum(g1 + g2, 1e-30), 0.0)
                link(idx[l][sa].ravel(), idx[l][sb].ravel(), G.ravel())
        for l, z in enumerate(self.zlinks):
            link(idx[l].ravel(), idx[l + 1].ravel(), z.ravel())
        for k, j in enumerate(self.junctions):
            jn = L * n_cell + k
            for (l, flat, G) in j['links']:
                a = idx[l].ravel()[flat]
                link(np.array([jn]), np.array([a]), np.array([G]))
        self.K0 = sp.csr_matrix((np.concatenate(v), (np.concatenate(r), np.concatenate(c))), shape=(N, N))
        q = np.zeros(N)
        for l, ly in enumerate(self.layers):
            q[idx[l][self.active]] += ly['q'][self.active]
        for k, j in enumerate(self.junctions):
            q[L * n_cell + k] += j['P']
        self.q0 = q

    def solve(self, surface_h, t_air, t_rad, j_air_h=None, iters=30, tol=1e-3, t0=None, method='auto', log=None):
        """surface_h(face, T_face (ny,nx) or None) -> (h_conv スカラー, h_rad (ny,nx))。
        t_air: 対流の空気温度、t_rad: 放射の相手温度。t0: 初期温度 (スカラーか 節点ごとの配列、連成の前回値)。
        method: auto / direct (SuperLU) / amg (pyamg の smoothed aggregation 階層 + 重み付き Jacobi の V サイクルを前処理に CG、既定) / cg (Jacobi 前処理 CG) / amg-gs (pyamg 標準の Gauss-Seidel 平滑化、v0.3 初版。amg は同じ階層で重み付き Jacobi の V サイクル = GPU と同じコードで 3 倍速い) / gpu (CuPy の CG + GPU 上の AMG V サイクル、CuPy や GPU が無ければ auto に戻る) / gpu-emu (gpu と同じコードを numpy で、テスト用)。
        放射の反復で変わるのは対角 (表面の h) だけなので、AMG の階層は 1 回目に作って使い回し、CG は前回の T から始める。
        戻り値 T (N)、反復数。self.solve_info に方式・時間・CG 反復数"""
        import time
        L = len(self.layers); A = self.area; act = self.active
        top = self.idx[0][act]; bot = self.idx[L - 1][act]
        if t0 is None:
            T = np.full(self.N, t_air, float)
        else:
            T = np.broadcast_to(np.asarray(t0, float), (self.N,)).copy()
        meth = pick_method(method, self.N); M = None; ncg = []; ts = time.time(); tsetup = 0.0; loose = True; dT = np.inf
        for it in range(iters + 1):
            K = self.K0
            d = np.zeros(self.N); rhs = self.q0.copy()
            for face, nodes in (('top', top), ('bottom', bot)):
                Tf = np.full((self.ny, self.nx), np.nan); Tf[act] = T[nodes]
                hc, hr = surface_h(face, Tf)
                Gc = hc * A[act]; Gr = hr[act] * A[act]
                np.add.at(d, nodes, Gc + Gr); np.add.at(rhs, nodes, Gc * t_air + Gr * t_rad)
            for k, j in enumerate(self.junctions):
                if j.get('g_air', 0) > 0:
                    jn = L * self.n_cell + k; d[jn] += j['g_air']; rhs[jn] += j['g_air'] * t_air
            Kd = (K + sp.diags(d)).tocsr()
            if meth == 'direct':
                Tn = spsolve(Kd.tocsc(), rhs)
            else:
                if meth in ('gpu', 'gpu-emu', 'amg'):
                    if M is None:
                        M = GpuBackend(emulate=(meth != 'gpu')); tsetup = M.setup(Kd)
                    cnt = [0]
                    loose = loose and dT > 10 * tol
                    Tn, info = M.cg(Kd, rhs, T, CG_RTOL_EARLY if loose else CG_RTOL, 5000, cnt)
                    ncg.append(cnt[0])
                    if info != 0:
                        raise RuntimeError(f'GPU CG が収束しない (info {info})。solver.method: amg を試す')
                    dT = np.max(np.abs(Tn - T)); T = Tn
                    if (dT < tol and not loose) or it + 1 >= iters and not loose:
                        break
                    continue
                if M is None:
                    t_s = time.time()
                    if meth == 'amg-gs':
                        M = _amg().smoothed_aggregation_solver(Kd, symmetry='symmetric', max_coarse=500, strength=('symmetric', {'theta': AMG_THETA})).aspreconditioner(cycle='V')
                    else:
                        M = sp.diags(1.0 / Kd.diagonal())
                    tsetup = time.time() - t_s
                cnt = [0]
                loose = loose and dT > 10 * tol
                Tn, info = cg(Kd, rhs, x0=T, rtol=CG_RTOL_EARLY if loose else CG_RTOL, atol=0.0, maxiter=5000, M=M, callback=lambda xk: cnt.__setitem__(0, cnt[0] + 1))
                ncg.append(cnt[0])
                if info != 0:
                    raise RuntimeError(f'CG が収束しない (info {info}、方式 {meth})。solver.method: direct を試す')
            dT = np.max(np.abs(Tn - T)); T = Tn
            if dT < tol and (meth == 'direct' or not loose):
                break
            if it + 1 >= iters and (meth == 'direct' or not loose):
                break
        self.T = T
        self.solve_info = dict(method=(meth + ': ' + M.name + ' / ' + M.prec) if meth in ('gpu', 'gpu-emu', 'amg') else meth, solve_s=round(time.time() - ts, 2), setup_s=round(tsetup, 2), cg_iters=ncg)
        return T, it + 1

    def field(self, l):
        F = np.full((self.ny, self.nx), np.nan); F[self.active] = self.T[self.idx[l][self.active]]; return F

    def tj(self, k):
        return self.T[len(self.layers) * self.n_cell + k]
