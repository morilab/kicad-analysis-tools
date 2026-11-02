"""cases — typ (標準) / worst (最悪) の 2 条件。毎回両方を計算し、合否は worst で判定する。
- 値オブジェクトは {typ: <値オブジェクト>, max: <値オブジェクト>} と書けば条件ごとに選ばれる (typ → typ、worst → max)。
  片方だけの {value: ...} は両条件で同じ値。電流 (currents / loads) は typ と max の両方が必須 (それぞれ出典か仮定の根拠)。
- 環境: environment (共通) + cases.typ / cases.worst (上書き)。既定 typ = 周囲 25 ℃、worst = 周囲 50 ℃。"""
import copy
from .parts import DataError

CASES = ('typ', 'worst')


def merge(d, u):
    out = dict(d)
    for k, v in (u or {}).items():
        out[k] = merge(d[k], v) if isinstance(v, dict) and isinstance(d.get(k), dict) else v
    return out
NAME = {'typ': '標準 (typ)', 'worst': '最悪 (worst)'}
DEFAULT_ENV = {'typ': {'ambient_c': 25.0}, 'worst': {'ambient_c': 50.0}}
PICK = {'typ': 'typ', 'worst': 'max'}


def _pick(y, key):
    if isinstance(y, dict):
        if 'value' not in y and ('typ' in y or 'max' in y) and isinstance(y.get('typ', y.get('max')), dict):
            if key not in y:
                raise DataError(f"{y.get('name', '?')}: {key} が無い (typ と max の両方が必要)")
            out = dict(copy.deepcopy(y[key]))
            for k, v in y.items():
                if k not in ('typ', 'max'):
                    out.setdefault(k, v)
            return _pick(out, key)
        return {k: (v if k == 'v_range' else _pick(v, key)) for k, v in y.items()}
    if isinstance(y, list):
        return [_pick(v, key) for v in y]
    return y


def check_typmax(cfg):
    from .parts import check_pins
    for ref, sp in (cfg.get('parts') or {}).items():
        check_pins(sp or {}, f'thermal.yaml:parts.{ref}')
    for ref, sp in (cfg.get('parts') or {}).items():
        pw = sp.get('power') or {}
        for c in (pw.get('currents') or []) + (pw.get('loads') or []):
            if 'typ' not in c or 'max' not in c:
                raise DataError(f"thermal.yaml parts.{ref} の電流「{c.get('name')}」: typ と max の両方が必要 (それぞれ出典か仮定の根拠)")


def resolve(cfg, case):
    key = PICK[case]
    c = _pick({k: v for k, v in cfg.items() if k != 'cases'}, key)
    env = dict(c.get('environment') or {})
    over = dict(DEFAULT_ENV[case]); over.update(((cfg.get('cases') or {}).get(case) or {}).get('environment') or {})
    c['environment'] = merge(env, over) if env else over
    c['case_key'] = case; c['case_name'] = NAME[case]
    return c


def resolve_lib(lib, case):
    return {k: _pick(v, PICK[case]) for k, v in lib.items()}
