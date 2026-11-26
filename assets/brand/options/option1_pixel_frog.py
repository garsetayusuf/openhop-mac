"""Logo option 1: 'Hopper' - a chunky retro pixel-art frog (native 16x16 sprite, so it is crisp in the taskbar)."""
import sys
from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1]

PAL = {
    ".": None,
    "O": (20, 33, 22),      # outline (near-black green)
    "G": (98, 185, 74),     # body green
    "D": (52, 122, 50),     # shade
    "H": (170, 226, 110),   # highlight
    "L": (232, 228, 170),   # belly cream
    "W": (250, 248, 236),   # eye white
    "K": (20, 33, 22),      # pupil
    "P": (238, 120, 140),   # cheek
}

SPRITE = [
    "................",
    "..OOOO....OOOO..",
    ".OWWWWO..OWWWWO.",
    ".OWKKWO..OWKKWO.",
    ".OWKKWOOOOWKKWO.",
    "OGOOOOHHGGOOOODO",
    "OGHGGGGGGGGGGGDO",
    "OGPPGGGGGGGGPPDO",
    "OGGOGGGGGGGGODDO",
    ".OGGOOOOOOOODDO.",
    "..OGGLLLLLLDDO..",
    ".OGGLLLLLLLLDDO.",
    "OGGOLLLLLLLLODDO",
    "OGGOOLLLLLLOODDO",
    ".OOGGOOOOOODDOO.",
    "..OOOO....OOOO..",
]


def sprite(scale: int) -> Image.Image:
    n = len(SPRITE)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    for y, row in enumerate(SPRITE):
        assert len(row) == n, (y, len(row))
        for x, ch in enumerate(row):
            if PAL[ch]:
                img.putpixel((x, y), PAL[ch] + (255,))
    return img.resize((n * scale, n * scale), Image.NEAREST)


def icon(px: int) -> Image.Image:
    if px % 16 == 0:
        return sprite(px // 16)
    s = sprite(1)
    return s.resize((px, px), Image.NEAREST)


# --- tiny hand-made pixel font for the wordmark (5x7) ---
GLYPHS = {
    "o": [".....", ".....", ".OOO.", "O...O", "O...O", "O...O", ".OOO."],
    "p": [".....", ".....", "OOOO.", "O...O", "O...O", "O...O", "OOOO.", "O....", "O...."],
    "e": [".....", ".....", ".OOO.", "O...O", "OOOOO", "O....", ".OOO."],
    "n": [".....", ".....", "OOOO.", "O...O", "O...O", "O...O", "O...O"],
    "h": ["O....", "O....", "OOOO.", "O...O", "O...O", "O...O", "O...O"],
}


def wordmark(text: str, scale: int, colors) -> Image.Image:
    w = len(text) * 6 - 1
    img = Image.new("RGBA", (w * scale, 9 * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, ch in enumerate(text):
        col = colors[i]
        for y, row in enumerate(GLYPHS[ch]):
            for x, c in enumerate(row):
                if c == "O":
                    X, Y = (i * 6 + x) * scale, y * scale
                    d.rectangle((X, Y, X + scale - 1, Y + scale - 1), fill=col)
    return img


sizes = [16, 24, 32, 48, 64, 128, 256]
DARK, LIGHT = (14, 17, 22, 255), (240, 238, 228, 255)
W, H = 1240, 760
sheet = Image.new("RGBA", (W, H), DARK)
sheet.paste(Image.new("RGBA", (W, H // 2), LIGHT), (0, H // 2))
lbl = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 15)
d = ImageDraw.Draw(sheet)

for band, (bg, fg_open, fg_label) in enumerate([(DARK, (232, 228, 210), (120, 128, 140)),
                                                  (LIGHT, (20, 33, 22), (110, 110, 100))]):
    top = band * (H // 2)
    x = 30
    for s in sizes:
        sheet.alpha_composite(icon(s), (x, top + 200 - s // 2 - 40))
        d.text((x, top + 300), f"{s}px", font=lbl, fill=fg_label)
        x += s + 34
    # lockup: icon + pixel wordmark
    sheet.alpha_composite(icon(64), (x + 10, top + 40))
    g = PAL["G"] if band == 0 else PAL["D"]
    wm = wordmark("openhop", 7, [fg_open] * 4 + [g + (255,)] * 3)
    sheet.alpha_composite(wm, (x + 90, top + 40 + 32 - 7 * 7 // 2 + 3))
    # taskbar mock
    tb_y = top + 150
    tb = (32, 32, 36, 255) if band == 0 else (225, 225, 220, 255)
    d.rounded_rectangle((x + 10, tb_y, x + 250, tb_y + 48), radius=8, fill=tb)
    sheet.alpha_composite(icon(24), (x + 22, tb_y + 12))
    d.text((x + 58, tb_y + 15), "taskbar (24px)", font=lbl, fill=fg_label)
    d.text((x + 10, tb_y + 70), "Option 1 - 'Hopper' pixel frog", font=lbl, fill=fg_label)

sheet.save(OUT)
icon(256).save(OUT.replace(".png", "_256.png"))
print(OUT)
