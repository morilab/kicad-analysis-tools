#!/usr/bin/env python3
"""テスト用の小さな .kicad_sch を組み立てるビルダー (KiCad 10 形式)。
lib_symbols は、手元の KiCad 10 標準シンボルライブラリから実行時に抜き出す (libsyms.py)。
ライブラリのファイルは本リポに同梱しない (KiCad ライブラリは CC-BY-SA 4.0)。"""
import os
import re
import uuid as _uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def _u():
    return str(_uuid.uuid4())


def load_libsyms():
    from libsyms import libsyms_text
    s = libsyms_text()
    # トップレベルの (symbol "lib:name" ...) ごとに切り出す
    out = {}
    i = 0
    while True:
        m = re.search(r'\(symbol "([^"]+:[^"]+)"', s[i:])
        if not m:
            break
        st = i + m.start()
        depth = 0
        j = st
        instr = False
        while True:
            ch = s[j]
            if instr:
                if ch == "\\":
                    j += 1
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out[m.group(1)] = s[st:j + 1]
        i = j + 1
    return out


def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


class Sch:
    def __init__(self, name, paper="A4", title=None, date=None, rev=None, company=None, comments=()):
        self.name = name
        self.uuid = _u()
        self.paper = paper
        self.tb = dict(title=title, date=date, rev=rev, company=company)
        self.comments = comments
        self.libs = load_libsyms()
        self.used = []
        self.items = []
        self.sheet_items = []
        self.root_uuid = self.uuid
        self.project = name
        self.path = None  # インスタンスパス (階層の子ならトップ uuid/シート uuid)

    def sym(self, lib_id, ref, value, x, y, rot=0, mirror=None, unit=1, ref_at=None, val_at=None, hide_val=False,
            inst_ref=None, fields_hidden=False, field_angle=0, field_justify="left"):
        if lib_id not in self.used:
            self.used.append(lib_id)
        u = _u()
        rx, ry = ref_at if ref_at else (x + 2.54, y - 1.27)
        vx, vy = val_at if val_at else (x + 2.54, y + 1.27)
        hr = " (hide yes)" if (ref.startswith("#") or fields_hidden) else ""
        hv = " (hide yes)" if (hide_val or fields_hidden) else ""
        mir = f"\n\t\t(mirror {mirror})" if mirror else ""
        path = self.path or f"/{self.root_uuid}"
        self.items.append(f'''	(symbol
		(lib_id {q(lib_id)})
		(at {x} {y} {rot}){mir}
		(unit {unit})
		(exclude_from_sim no)
		(in_bom yes)
		(on_board yes)
		(dnp no)
		(uuid {q(u)})
		(property "Reference" {q(ref)}
			(at {rx} {ry} {field_angle})
			(effects (font (size 1.27 1.27)){hr} (justify {field_justify}))
		)
		(property "Value" {q(value)}
			(at {vx} {vy} {field_angle})
			(effects (font (size 1.27 1.27)){hv} (justify {field_justify}))
		)
		(property "Footprint" ""
			(at {x} {y} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(property "Datasheet" "~"
			(at {x} {y} 0)
			(effects (font (size 1.27 1.27)) (hide yes))
		)
		(instances
			(project {q(self.project)}
				(path {q(path)}
					(reference {q(inst_ref or ref)}) (unit {unit})
				)
			)
		)
	)
''')
        return u

    def wire(self, *pts):
        for a, b in zip(pts, pts[1:]):
            self.items.append(f'\t(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]}))\n\t\t(stroke (width 0) (type default))\n\t\t(uuid {q(_u())})\n\t)\n')

    def junction(self, x, y):
        self.items.append(f'\t(junction (at {x} {y}) (diameter 0) (color 0 0 0 0)\n\t\t(uuid {q(_u())})\n\t)\n')

    def nc(self, x, y):
        self.items.append(f'\t(no_connect (at {x} {y})\n\t\t(uuid {q(_u())})\n\t)\n')

    def label(self, text, x, y, angle=0, kind="label", shape="input", just="left bottom"):
        sh = f"\n\t\t(shape {shape})" if kind != "label" else ""
        self.items.append(f'\t({kind} {q(text)}{sh}\n\t\t(at {x} {y} {angle})\n\t\t(effects (font (size 1.27 1.27)) (justify {just}))\n\t\t(uuid {q(_u())})\n\t)\n')

    def text(self, text, x, y, size=1.27, face=None, angle=0):
        fc = f" (face {q(face)})" if face else ""
        self.items.append(f'\t(text {q(text)}\n\t\t(exclude_from_sim no)\n\t\t(at {x} {y} {angle})\n\t\t(effects (font{fc} (size {size} {size})) (justify left bottom))\n\t\t(uuid {q(_u())})\n\t)\n')

    def rect(self, x1, y1, x2, y2):
        self.items.append(f'\t(rectangle (start {x1} {y1}) (end {x2} {y2})\n\t\t(stroke (width 0.1524) (type dash))\n\t\t(fill (type none))\n\t\t(uuid {q(_u())})\n\t)\n')

    def sheet(self, name, file, x, y, w, h, pins=(), uuid=None):
        u = uuid or _u()
        ps = "".join(f'\t\t(pin {q(n)} {shape}\n\t\t\t(at {px} {py} {ang})\n\t\t\t(uuid {q(_u())})\n\t\t\t(effects (font (size 1.27 1.27)) (justify {"left" if ang == 180 else "right"}))\n\t\t)\n'
                     for n, shape, px, py, ang in pins)
        self.items.append(f'''	(sheet
		(at {x} {y})
		(size {w} {h})
		(exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
		(stroke (width 0.1524) (type solid))
		(fill (color 0 0 0 0.0000))
		(uuid {q(u)})
		(property "Sheetname" {q(name)}
			(at {x} {y - 0.7} 0)
			(effects (font (size 1.27 1.27)) (justify left bottom))
		)
		(property "Sheetfile" {q(file)}
			(at {x} {y + h + 0.6} 0)
			(effects (font (size 1.27 1.27)) (justify left top))
		)
{ps}		(instances
			(project {q(self.project)}
				(path {q("/" + self.root_uuid)}
					(page "2")
				)
			)
		)
	)
''')
        return u

    def render(self):
        tb = "".join(f"\t\t({k} {q(v)})\n" for k, v in self.tb.items() if v is not None)
        tb += "".join(f"\t\t(comment {i + 1} {q(c)})\n" for i, c in enumerate(self.comments))
        libs = "".join("\t\t" + self.libs[l].replace("\n", "\n\t\t") + "\n" for l in self.used)
        si = f'\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "1")\n\t\t)\n\t)\n' if self.path is None else ""
        return f'''(kicad_sch
	(version 20260306)
	(generator "eeschema")
	(generator_version "10.0")
	(uuid {q(self.uuid)})
	(paper {q(self.paper)})
	(title_block
{tb}	)
	(lib_symbols
{libs}	)
{"".join(self.items)}{si}	(embedded_fonts no)
)
'''

    def save(self, d):
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, self.name + ".kicad_sch")
        open(p, "w", encoding="utf-8").write(self.render())
        if self.path is None:
            pro = os.path.join(d, self.project + ".kicad_pro")
            if not os.path.exists(pro):
                open(pro, "w").write('{"meta": {"filename": "%s.kicad_pro", "version": 3}}\n' % self.project)
        return p
