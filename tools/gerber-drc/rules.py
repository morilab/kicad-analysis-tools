#!/usr/bin/env python3
"""gerber-drc のルール YAML ローダ (factory → user → project の順に合成、同じ id はフィールド単位で後勝ち)。
  load(paths) → RuleSet。extends は rules/ (この dir の rules/) からの相対名 (例 factory/jlcpcb-standard) か、ファイルからの相対パス。
工場値より緩める上書き (min を下げる / max を上げる / 無効化 / 重大度を下げる) は許可し、warnings に記録する (2026-10-14 作者 決定)。"""
import os, yaml

RULES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rules')
LAYERS = ('factory', 'user', 'project')
SEV_RANK = {'error': 3, 'warning': 2, 'info': 1}
_schema = None


def checks():
    global _schema
    if _schema is None: _schema = yaml.safe_load(open(os.path.join(RULES_DIR, 'schema.yaml')))['checks']
    return _schema


def _resolve(name, base):
    for c in (name, name + '.yaml', os.path.join(base, name), os.path.join(base, name + '.yaml'),
              os.path.join(RULES_DIR, name), os.path.join(RULES_DIR, name + '.yaml')):
        if os.path.isfile(c): return os.path.abspath(c)
    raise FileNotFoundError(f'ルール YAML が見つからない: {name} (基準 {base})')


def _to_mm(v, unit):
    k = 0.0254 if unit == 'mil' else 1.0
    if isinstance(v, (int, float)) and not isinstance(v, bool): return v * k
    if isinstance(v, dict): return {a: _to_mm(b, unit) for a, b in v.items()}
    if isinstance(v, list): return [_to_mm(x, unit) for x in v]
    return v


def _looser(old, new):
    """old→new の value 変更が緩める方向なら理由を返す"""
    if isinstance(old, (int, float)) and isinstance(new, (int, float)) and new < old: return f'min {old} → {new}'
    if isinstance(old, dict) and isinstance(new, dict):
        if isinstance(old.get('max'), (int, float)) and isinstance(new.get('max'), (int, float)) and new['max'] > old['max']:
            return f"max {old['max']} → {new['max']}"
        if isinstance(old.get('min'), (int, float)) and isinstance(new.get('min'), (int, float)) and new['min'] < old['min']:
            return f"min {old['min']} → {new['min']}"
    return None


class RuleSet:
    def __init__(self):
        self.rules = {}      # id → dict (合成後)。'_src' = [(layer, file)]、'_layer' = 最初に定義した層
        self.files = []; self.warnings = []

    def _merge(self, path, seen):
        path = os.path.abspath(path)
        if path in seen: return
        seen.add(path)
        d = yaml.safe_load(open(path)) or {}
        meta = d.get('file') or {}
        layer = meta.get('layer')
        if layer not in LAYERS: raise ValueError(f'{path}: file.layer は {LAYERS} のどれか (現在 {layer!r})')
        for e in meta.get('extends') or []: self._merge(_resolve(e, os.path.dirname(path)), seen)
        self.files.append(dict(path=path, layer=layer, name=meta.get('name', os.path.basename(path))))
        for r in d.get('rules') or []:
            if 'id' not in r: raise ValueError(f'{path}: id の無いルール {r}')
            r = dict(r)
            if 'value' in r: r['value'] = _to_mm(r['value'], r.pop('unit', 'mm'))
            rid = r['id']; old = self.rules.get(rid)
            if old is None:
                r.setdefault('enabled', True); r.setdefault('severity', 'error')
                r['_layer'] = layer; r['_src'] = [(layer, path)]; self.rules[rid] = r; continue
            if old['_layer'] == 'factory' and layer != 'factory':
                why = []
                if 'value' in r and _looser(old.get('value'), r['value']): why.append(_looser(old.get('value'), r['value']))
                if r.get('enabled') is False and old.get('enabled', True): why.append('無効化')
                if 'severity' in r and SEV_RANK.get(r['severity'], 0) < SEV_RANK.get(old.get('severity'), 0):
                    why.append(f"重大度 {old.get('severity')} → {r['severity']}")
                if why: self.warnings.append(f"{rid}: {layer} 層 ({os.path.basename(path)}) が工場値を緩めた ({', '.join(why)})")
            old.update({k: v for k, v in r.items() if k != 'id'}); old['_src'].append((layer, path))

    def backend(self, r):
        if r.get('backend'): return r['backend']
        c = checks().get(r.get('check'), {})
        k, g = c.get('kicad_dru') in (True, 'yes'), c.get('gerber_drc') in (True, 'yes')  # YAML 1.1 では yes は真偽値
        return 'both' if k and g else 'kicad_dru' if k else 'gerber_drc' if g else 'none'

    def for_backend(self, which):
        """which = 'gerber_drc' / 'kicad_dru'。有効で、その backend が担当するルール"""
        return [r for r in self.rules.values() if r.get('enabled', True) and self.backend(r) in (which, 'both')]

    def summary(self):
        return dict(files=[f"{f['layer']}:{f['name']}" for f in self.files], warnings=self.warnings,
                    rules={k: {a: b for a, b in v.items() if not a.startswith('_')} | {'backend': self.backend(v), 'layer': v['_src'][-1][0]}
                           for k, v in self.rules.items()})


def load(paths):
    rs = RuleSet(); seen = set()
    for p in paths: rs._merge(p, seen)
    return rs
