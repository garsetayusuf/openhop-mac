"""Logo option 2 'Jackrabbit': leaping hare silhouette cut out of a signal-orange stamp disc. Renders via headless Edge."""
import pathlib
import subprocess
import sys
import tempfile

OUT = pathlib.Path(sys.argv[1])
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

INK = "#17140f"
SIGNAL = "#ff5b1f"
BONE = "#efe9dc"

PTS = [  # (x, y, sharp) clockwise outline of a leaping hare, facing right
    (87, 37.5, 0), (83, 33, 0), (77, 30.5, 0),
    (65.2, 18, 0), (53, 9.5, 1), (60.8, 22, 0), (69.5, 31.5, 1),
    (58.6, 23, 0), (45.5, 15.5, 1), (55.4, 27, 0), (64, 35, 0),
    (52, 37.5, 0), (39, 38.5, 0), (28, 39.5, 0),
    (20.5, 42.5, 0), (14.5, 45, 0), (13.5, 50, 0), (17.5, 53, 0),
    (20.5, 58, 0), (23.5, 62.5, 0),
    (17, 66.5, 0), (9, 70, 0), (4.5, 73, 1), (10, 74.8, 0), (23, 73, 0), (34, 69.5, 0),
    (45, 66.5, 0), (56, 63, 0),
    (64, 60, 0), (77, 62.5, 0), (93, 60, 1), (88, 56.8, 0), (76, 57, 0), (70, 53, 0),
    (75, 47.5, 0), (81, 43.5, 0),
]


def hare_path(pts, t=1.0):
    n = len(pts)
    P = [(x, y) for x, y, _ in pts]
    d = f"M{P[0][0]:.2f} {P[0][1]:.2f}"
    for i in range(n):
        p0, p1, p2, p3 = P[i - 1], P[i], P[(i + 1) % n], P[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6 * t, p1[1] + (p2[1] - p0[1]) / 6 * t)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6 * t, p2[1] - (p3[1] - p1[1]) / 6 * t)
        if pts[i][2]:
            c1 = p1
        if pts[(i + 1) % n][2]:
            c2 = p2
        d += f" C{c1[0]:.2f} {c1[1]:.2f} {c2[0]:.2f} {c2[1]:.2f} {p2[0]:.2f} {p2[1]:.2f}"
    return d + "Z"


HARE = (f'<g transform="translate(50 50) scale(0.86) translate(-48.5 -43)">'
        f'<path d="{hare_path(PTS)}" fill="{INK}"/>'
        f'<path d="M76.5 35.6 L83 33.6 L80.6 36.6 Z" fill="{SIGNAL}"/></g>')



def icon_svg(detail=True):
    ring = (f'<circle cx="50" cy="50" r="43" fill="none" stroke="{INK}" stroke-width="2.2"/>' if detail else "")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f'<circle cx="50" cy="50" r="49" fill="{SIGNAL}"/>{ring}{HARE}</svg>')


ICON = icon_svg(True)
ICON_SMALL = icon_svg(False)
sizes = [16, 24, 32, 48, 64, 128, 256]


def band(bg, fg, label, word_open, word_hop):
    cells = "".join(
        f'<div class="cell"><div class="ic" style="width:{s}px;height:{s}px">{ICON_SMALL if s <= 32 else ICON}</div>'
        f'<div class="lbl" style="color:{label}">{s}px</div></div>' for s in sizes)
    return f"""
<div class="band" style="background:{bg}">
  <div class="row">{cells}</div>
  <div class="side">
    <div class="lock"><div style="width:72px;height:72px">{ICON}</div>
      <div class="word"><span style="color:{word_open}">open</span><span style="color:{word_hop}">hop</span></div></div>
    <div class="tb" style="background:{'#202024' if bg != BONE else '#dedad0'}">
      <div style="width:24px;height:24px">{ICON_SMALL}</div><span style="color:{label}">taskbar (24px)</span></div>
    <div class="lbl" style="color:{label}">Option 2 - 'Jackrabbit' stamp</div>
  </div>
</div>"""


html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0}} body{{width:1240px}}
.band{{height:380px;display:flex;align-items:center;padding:0 30px;gap:40px;box-sizing:border-box}}
.row{{display:flex;align-items:center;gap:30px}}
.cell{{display:flex;flex-direction:column;align-items:center;gap:14px}}
.ic svg,.lock svg,.tb svg{{width:100%;height:100%;display:block}}
.lbl{{font:13px Consolas,monospace}}
.side{{display:flex;flex-direction:column;gap:22px}}
.lock{{display:flex;align-items:center;gap:14px}}
.word{{font-family:Bahnschrift;font-weight:700;font-stretch:condensed;font-variation-settings:"wdth" 75,"wght" 700;
      font-size:64px;letter-spacing:-1px;line-height:1}}
.tb{{display:flex;align-items:center;gap:12px;padding:12px;border-radius:8px;width:220px}}
</style></head><body>
{band('#0f0e0c', '#f2ece0', '#8a857a', BONE, SIGNAL)}
{band(BONE, INK, '#77715f', INK, SIGNAL)}
</body></html>"""

work = pathlib.Path(tempfile.mkdtemp(prefix="opt2_"))
page = work / "p.html"
page.write_text(html, encoding="utf-8")
subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={work / 'prof'}",
                "--window-size=1240,760", f"--screenshot={OUT}", page.as_uri()], capture_output=True, timeout=90)
(OUT.parent / "option2_icon.svg").write_text(ICON, encoding="utf-8")
print(OUT)
