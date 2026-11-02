"""regrid — 異なる格子 (core.grid の meta: x0_mm, y0_mm, dx_mm, nx, ny, layer_names) の間の受け渡し。
- 温度 (示強量): セル中心の双一次補間。基板外 (NaN) は最寄りの有効セルで埋めてから補間する。
- 発熱 (示量量): 面積の重なりで配る保存的な写像 (dst = Wy · src · Wxᵀ、層ごと)。合計は保存し、dst の基板外セルに落ちた分は最寄りの基板内セルへ移す。
層は layer_names の名前で対応させる (同じ名前が無い層は NaN / 0)。"""
import numpy as np
import scipy.sparse as sp
from scipy import ndimage
from scipy.interpolate import RegularGridInterpolator


def _axis(m, ax):
    n = m['nx'] if ax == 'x' else m['ny']; o = m['x0_mm'] if ax == 'x' else m['y0_mm']
    return o, m['dx_mm'], n


def _fill_nan(F):
    bad = np.isnan(F)
    if not bad.any() or bad.all():
        return np.where(bad, 0.0, F) if bad.all() else F
    _, (jj, ii) = ndimage.distance_transform_edt(bad, return_indices=True)
    return F[jj, ii]


def temperature(src, src_meta, dst_meta, dst_mask=None):
    """src (L_src, Ny, Nx) ℃ → (L_dst, Ny', Nx')。dst_mask (Ny', Nx') bool があれば外を NaN"""
    out = np.full((len(dst_meta['layer_names']), dst_meta['ny'], dst_meta['nx']), np.nan)
    xs = src_meta['x0_mm'] + (np.arange(src_meta['nx']) + 0.5) * src_meta['dx_mm']; ys = src_meta['y0_mm'] + (np.arange(src_meta['ny']) + 0.5) * src_meta['dx_mm']
    xd = dst_meta['x0_mm'] + (np.arange(dst_meta['nx']) + 0.5) * dst_meta['dx_mm']; yd = dst_meta['y0_mm'] + (np.arange(dst_meta['ny']) + 0.5) * dst_meta['dx_mm']
    Y, X = np.meshgrid(np.clip(yd, ys[0], ys[-1]), np.clip(xd, xs[0], xs[-1]), indexing='ij')
    for l, name in enumerate(dst_meta['layer_names']):
        if name not in src_meta['layer_names']:
            continue
        F = _fill_nan(np.asarray(src[src_meta['layer_names'].index(name)], float))
        out[l] = RegularGridInterpolator((ys, xs), F)(np.c_[Y.ravel(), X.ravel()]).reshape(Y.shape)
        if dst_mask is not None:
            out[l][~dst_mask] = np.nan
    return out


def _overlap(src_meta, dst_meta, ax):
    """W[k_dst, k_src] = src セル k_src のうち dst セル k_dst に入る割合 (行和ではなく列和が 1 = 保存)"""
    os_, ds, ns = _axis(src_meta, ax); od, dd, nd = _axis(dst_meta, ax)
    a0 = os_ + np.arange(ns) * ds; a1 = a0 + ds
    k0 = np.clip(np.floor((a0 - od) / dd).astype(int), 0, nd - 1); k1 = np.clip(np.floor((a1 - od - 1e-12) / dd).astype(int), 0, nd - 1)
    R, C, V = [], [], []
    for s in range(ns):
        for k in range(k0[s], k1[s] + 1):
            lo = max(a0[s], od + k * dd); hi = min(a1[s], od + (k + 1) * dd)
            if hi > lo:
                R.append(k); C.append(s); V.append((hi - lo) / ds)
    W = sp.csr_matrix((V, (R, C)), shape=(nd, ns))
    cs = np.asarray(W.sum(axis=0)).ravel(); cs[cs == 0] = 1.0   # 端で dst の外に出た分は端のセルへ (保存)
    return (W @ sp.diags(1.0 / cs)).tocsr()


def heat(src, src_meta, dst_meta, dst_mask=None):
    """src (L_src, Ny, Nx) W → (L_dst, Ny', Nx') W。合計を保存。dst_mask (基板内) の外に落ちた分は最寄りの基板内セルへ"""
    Wx = _overlap(src_meta, dst_meta, 'x'); Wy = _overlap(src_meta, dst_meta, 'y')
    out = np.zeros((len(dst_meta['layer_names']), dst_meta['ny'], dst_meta['nx']))
    near = None
    if dst_mask is not None and not dst_mask.all():
        _, near = ndimage.distance_transform_edt(~dst_mask, return_indices=True)
    for l, name in enumerate(dst_meta['layer_names']):
        if name not in src_meta['layer_names']:
            continue
        H = Wy @ (Wx @ np.asarray(src[src_meta['layer_names'].index(name)], float).T).T
        if near is not None:
            o = ~dst_mask; G = np.where(o, 0.0, H)
            np.add.at(G, (near[0][o], near[1][o]), H[o]); H = G
        out[l] = H
    return out
