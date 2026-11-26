"""Logo option 3 'Tag': a utilitarian equipment-tag mark. Heavy geometric 'h' whose shoulder is the hop,
with a ball mid-hop; chamfered metal-tag plate. Renders via headless Edge."""
import pathlib
import subprocess
import sys
import tempfile

OUT = pathlib.Path(sys.argv[1])
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

PLATE = "#f2b318"     # tool amber
INK = "#1f2125"       # graphite
PAPER = "#efede6"


def plate_path(i=3, c=9):
    a, b = i, 100 - i
    return f"M{a+c} {a} H{b-c} L{b} {a+c} V{b-c} L{b-c} {b} H{a+c} L{a} {b-c} V{a+c} Z"


GLYPH = f"""
  <g fill="{INK}">
    <rect x="22" y="17" width="15" height="66"/>
    <path d="M41 41.5 H51 Q68.5 41.5 68.5 59 V83" fill="none" stroke="{INK}" stroke-width="15"/>
  </g>
"""


def icon_svg(detail=True):
    holes = ("" if not detail else
             f'<circle cx="84" cy="16" r="4.2" fill="none" stroke="{INK}" stroke-opacity=".45" stroke-width="2"/>')
    rim = ("" if not detail else
           f'<path d="{plate_path(7, 7)}" fill="none" stroke="{INK}" stroke-opacity=".18" stroke-width="1.4"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f'<path d="{plate_path(3 if detail else 1, 9 if detail else 7)}" fill="{PLATE}"/>{rim}{holes}{GLYPH}</svg>')


ICON, ICON_SMALL = icon_svg(True), icon_svg(False)
sizes = [16, 24, 32, 48, 64, 128, 256]


def band(bg, word, label, tb):
    cells = "".join(
        f'<div class="cell"><div style="width:{s}px;height:{s}px">{ICON_SMALL if s <= 32 else ICON}</div>'
        f'<div class="lbl" style="color:{label}">{s}px</div></div>' for s in sizes)
    return f"""
<div class="band" style="background:{bg}">
  <div class="row">{cells}</div>
  <div class="side">
    <div class="lock"><div style="width:72px;height:72px">{ICON}</div>
      <div class="word" style="color:{word}">open<b>hop</b></div></div>
    <div class="tb" style="background:{tb}"><div style="width:24px;height:24px">{ICON_SMALL}</div>
      <span class="lbl" style="color:{label}">taskbar (24px)</span></div>
    <div class="lbl" style="color:{label}">Option 3 - 'Tag' utility mark</div>
  </div>
</div>"""


html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0}} body{{width:1240px}}
.band{{height:380px;display:flex;align-items:center;padding:0 30px;gap:40px;box-sizing:border-box}}
.row{{display:flex;align-items:center;gap:30px}}
.cell{{display:flex;flex-direction:column;align-items:center;gap:14px}}
svg{{width:100%;height:100%;display:block}}
.lbl{{font:13px Consolas,monospace}}
.side{{display:flex;flex-direction:column;gap:22px}}
.lock{{display:flex;align-items:center;gap:16px}}
.word{{font-family:Bahnschrift;font-variation-settings:"wdth" 100,"wght" 350;font-size:60px;letter-spacing:-.5px;line-height:1}}
.word b{{font-variation-settings:"wdth" 100,"wght" 700}}
.word .up{{display:inline-block;transform:translateY(-12px);color:{PLATE}}}
.tb{{display:flex;align-items:center;gap:12px;padding:12px;border-radius:8px;width:220px}}
</style></head><body>
{band('#111214', PAPER, '#85868a', '#202124')}
{band(PAPER, INK, '#77756c', '#dcdad2')}
</body></html>"""

work = pathlib.Path(tempfile.mkdtemp(prefix="opt3_"))
page = work / "p.html"
page.write_text(html, encoding="utf-8")
subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={work / 'prof'}",
                "--window-size=1240,760", f"--screenshot={OUT}", page.as_uri()], capture_output=True, timeout=90)
print(OUT)
