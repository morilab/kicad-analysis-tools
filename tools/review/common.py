"""kreview 共通: 指標 (Metric)、判定、kicad-cli 呼び出し、ネットリスト読み込み。"""
import json, os, re, subprocess, shutil, xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
KB = os.path.dirname(os.path.dirname(HERE))          # リポジトリのルート
KICAD_HOME = os.environ.get('KICAD_CLI_HOME', os.path.expanduser('~'))
GRADES = ('pass', 'warn', 'fail', 'skip', 'info', 'waived')


class Metric(dict):
    """id, stage, title, value, grade, threshold, detail(list[str]), source"""
    def __init__(self, stage, id, title, value=None, grade='info', threshold='', detail=None, source=''):
        super().__init__(stage=stage, id=id, title=title, value=value, grade=grade, threshold=threshold,
                         detail=list(detail or []), source=source)


def judge(v, p, w, high=False):
    """low-is-good (既定): v<=p pass, v<=w warn, else fail。high=True なら逆"""
    if v is None:
        return 'skip'
    if high:
        return 'pass' if v >= p else 'warn' if v >= w else 'fail'
    return 'pass' if v <= p else 'warn' if v <= w else 'fail'


def cli(args, timeout=600, cwd=None):
    env = dict(os.environ, HOME=KICAD_HOME)
    return subprocess.run(['kicad-cli'] + args, capture_output=True, text=True, env=env, timeout=timeout, cwd=cwd)


def copy_project(src_dir, name, dst):
    """プロジェクト一式 (.kicad_pcb/.kicad_pro/.kicad_sch、lib/、*-lib-table) を dst にコピー (元は触らない)"""
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src_dir):
        p = os.path.join(src_dir, f)
        if os.path.isfile(p) and (f.endswith(('.kicad_pcb', '.kicad_pro', '.kicad_sch', '.kicad_dru')) or f in ('fp-lib-table', 'sym-lib-table')):
            shutil.copy2(p, dst)
        elif f == 'lib' and os.path.isdir(p):
            shutil.copytree(p, os.path.join(dst, 'lib'), dirs_exist_ok=True)
    return dst


class Netlist:
    """kicad-cli の kicadxml ネットリスト"""
    def __init__(self, xml_path):
        r = ET.parse(xml_path).getroot()
        self.comps = {}
        for c in r.find('components'):
            ref = c.get('ref')
            props = {p.get('name'): p.get('value') for p in c.findall('property')}
            fields = {f.get('name'): (f.text or '') for f in (c.find('fields') or [])}
            ls = c.find('libsource')
            self.comps[ref] = dict(ref=ref, value=c.findtext('value') or '', footprint=c.findtext('footprint') or '',
                                   datasheet=c.findtext('datasheet') or '', description=c.findtext('description') or '',
                                   lib=ls.get('lib') if ls is not None else '', part=ls.get('part') if ls is not None else '',
                                   fields=fields, props=props,
                                   dnp='dnp' in props, exclude_bom='exclude_from_bom' in props,
                                   exclude_board='exclude_from_board' in props)
        self.libparts = {}
        for lp in (r.find('libparts') or []):
            pins = {p.get('num'): dict(name=p.get('name'), type=p.get('type')) for p in (lp.find('pins') or [])}
            self.libparts[(lp.get('lib'), lp.get('part'))] = pins
        self.nets = {}
        self.pin_net = {}
        for n in r.find('nets'):
            name = n.get('name'); nodes = []
            for nd in n.findall('node'):
                nodes.append(dict(ref=nd.get('ref'), pin=nd.get('pin'), type=nd.get('pintype') or '', func=nd.get('pinfunction') or ''))
                self.pin_net[(nd.get('ref'), nd.get('pin'))] = name
            self.nets[name] = nodes

    def field(self, ref, name):
        c = self.comps[ref]
        return c['fields'].get(name) or c['props'].get(name) or ''

    def pins(self, ref):
        c = self.comps[ref]
        return self.libparts.get((c['lib'], c['part']), {})


def export_netlist(sch, out_xml):
    r = cli(['sch', 'export', 'netlist', '--format', 'kicadxml', '-o', out_xml, sch])
    return os.path.exists(out_xml), r.stderr[-500:]


POWER_NET = re.compile(r'(^|/)(\+?\d+V\d*|\+\d+V|VBUS|VCC.*|VDD.*|VIN|VBAT|VSYS|AVDD.*|DVDD.*|.*3V3.*|.*1V2.*|.*1V8.*|.*5V.*|VCCIO.*)$', re.I)
GND_NET = re.compile(r'(^|/)(GND|AGND|DGND|PGND|GNDA|GNDD|VSS|EP)$', re.I)


def is_power(n):
    return bool(n) and bool(POWER_NET.search(n)) and not GND_NET.search(n)


def is_gnd(n):
    return bool(n) and bool(GND_NET.search(n))


def parse_value(v):
    """'18pF' '100nF' '10k' '2.2k' '4k7' '1uF' -> 数値 (F / Ω)"""
    if not v:
        return None
    s = v.strip().replace('µ', 'u').replace('μ', 'u')
    m = re.match(r'^([\d.]+)\s*([pnumkKMR]?)([\d]*)\s*(F|Ω|ohm|R)?', s)
    if not m:
        return None
    num = float(m.group(1))
    mult = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'm': 1e-3, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'R': 1, '': 1}[m.group(2)]
    if m.group(3):   # 4k7
        num = float(f"{m.group(1)}.{m.group(3)}")
    return num * mult
