"""配置・基板の比較スコア (上限なし、高いほど良い)。係数は score_profiles.json、プロジェクトの上書きは review.toml の [score]。
  S = Σ_i w_i × s_i − soft_penalty × 軽い違反数
  s_i = 100 × 基準/max(値, floor×基準) (dir=low)、100 × 値/基準 (dir=high)。基準版と同じなら 100
  ハード制約 (部品の重なり・外形はみ出し・取付穴の干渉・DRC 必須 error) が 1 つでもあれば feasible=False で順位から外す。
  値か基準が無い項目は計算しない (配線前の via / detour など)。
kreview の 0-100 点は製造前の合否判定用で、このスコアとは別物。"""
import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
HARD_TOL = 0.01   # mm / mm2: これ以下は数値誤差とみなす


def load(path=None):
    return json.load(open(path or os.path.join(HERE, 'score_profiles.json')))


def project_config(review_toml):
    """review.toml の [score] (profile / base / weights / baseline) を返す。無ければ {}"""
    if not review_toml or not os.path.exists(review_toml):
        return {}
    import tomllib
    return tomllib.load(open(review_toml, 'rb')).get('score', {})


def weights(name, cfg=None, P=None):
    """name: 組み込みプロファイル名、または review.toml の [score.profiles.<name>] (base を元に上書き)"""
    P = P or load(); cfg = cfg or {}
    custom = cfg.get('profiles', {})
    if name in custom:
        c = custom[name]; w = dict(P['profiles'][c.get('base', 'default')]); w.update(c.get('weights', {}))
        return w
    if name not in P['profiles']:
        raise KeyError(f'プロファイル {name} が無い (組み込み: {", ".join(P["profiles"])}、review.toml: {", ".join(custom) or "-"})')
    return dict(P['profiles'][name])


def pair(m, base, item, P):
    """案と基準の両方にある最初の指標名で (値, 基準, 指標名) を返す (同じ物差しで比べる)。無ければ (None, None, None)"""
    for k in P['items'][item]['keys']:
        if m.get(k) is not None and base.get(k) is not None:
            return float(m[k]), float(base[k]), k
    return None, None, None


def item_score(v, ref, d, floor):
    if v is None or ref is None or ref == 0:
        return None
    if d == 'low':
        return 100.0 * ref / max(v, floor * ref)
    return 100.0 * v / ref


def hard_violations(m):
    """配置評価 (lc) の overlap/outside/hole と、基板評価の drc_errors。名前 → 値"""
    h = {k: float(m[k]) for k in ('overlap', 'outside', 'hole') if m.get(k) is not None and float(m[k]) > HARD_TOL}
    if m.get('drc_errors'):
        h['drc_errors'] = m['drc_errors']
    return h


def soft_count(m):
    """軽い違反の件数: 外周 3 mm 違反の部品数 (p22_parts)、コネクタ差し込み口のずれ (open)、J2 の端からの距離 (j2)、kreview fail"""
    n = int(m.get('p22_parts') or 0)
    n += sum(1 for k in ('open', 'j2') if m.get(k) is not None and float(m[k]) > 0.1)
    n += int(m.get('kreview_fail') or 0)
    return n


def score(m, base, w, P=None):
    P = P or load(); items = {}; S = 0.0
    for it, spec in P['items'].items():
        wt = w.get(it, 0.0)
        v, ref, key = pair(m, base, it, P)
        s = item_score(v, ref, spec['dir'], P['floor'])
        if s is None or wt == 0:
            continue
        items[it] = dict(value=v, base=ref, key=key, s=round(s, 1), w=wt, ws=round(wt * s, 1))
        S += wt * s
    hard = hard_violations(m); soft = soft_count(m)
    S -= P['soft_penalty'] * soft
    return dict(S=round(S, 1), feasible=not hard, hard=hard, soft=soft, items=items)


def rank(rows, base, w, P=None):
    """rows: 名前 → 指標。feasible な案だけ S の降順、作れない案は rank=None で末尾"""
    res = {n: score(m, base, w, P) for n, m in rows.items()}
    ok = sorted((n for n in res if res[n]['feasible']), key=lambda n: -res[n]['S'])
    for i, n in enumerate(ok):
        res[n]['rank'] = i + 1
    for n in res:
        res[n].setdefault('rank', None)
    return dict(sorted(res.items(), key=lambda kv: (kv[1]['rank'] is None, kv[1]['rank'] or 0)))


def table(res, P=None):
    """Markdown の内訳表 (列 = 案、行 = 項目の w×s)"""
    P = P or load(); names = list(res)
    its = [i for i in P['items'] if any(i in res[n]['items'] for n in names)]
    L = ['| 項目 (係数) | ' + ' | '.join(names) + ' |', '|---|' + '---|' * len(names)]
    for i in its:
        w = next(res[n]['items'][i]['w'] for n in names if i in res[n]['items'])
        L.append(f'| {P["items"][i]["label"]} (×{w}) | ' + ' | '.join(
            (f'{res[n]["items"][i]["value"]:g} → {res[n]["items"][i]["s"]:.0f} → **{res[n]["items"][i]["ws"]:.0f}**' if i in res[n]['items'] else '-') for n in names) + ' |')
    L.append('| 軽い違反 (×−%d) | ' % P['soft_penalty'] + ' | '.join(str(res[n]['soft']) for n in names) + ' |')
    L.append('| ハード制約 | ' + ' | '.join(('OK' if res[n]['feasible'] else '作れない ' + ', '.join(f'{k}={v:g}' for k, v in res[n]['hard'].items())) for n in names) + ' |')
    L.append('| **スコア S** | ' + ' | '.join(f'**{res[n]["S"]:.0f}**' for n in names) + ' |')
    L.append('| 順位 | ' + ' | '.join(str(res[n]['rank'] or '除外') for n in names) + ' |')
    return '\n'.join(L)
