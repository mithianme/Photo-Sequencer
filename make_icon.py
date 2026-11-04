"""
Draws icon.ico and the checkbox/radio images in the app's palette.

The colours match the constants near the top of app.py: warm paper, soft ink,
and the one clay accent. Re-run this after changing those and rebuild.
"""

from PIL import Image, ImageDraw

S = 512
BG_TOP = (150, 119, 90)       # ACCENT
BG_BOTTOM = (116, 90, 68)
CARD = (251, 248, 244)        # PAPER
CARD_DIM = (235, 225, 213)    # PAPER_3


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Background plate with a soft vertical gradient.
plate = Image.new("RGBA", (S, S), (0, 0, 0, 0))
pd = ImageDraw.Draw(plate)
for y in range(S):
    t = y / (S - 1)
    pd.line(
        [(0, y), (S, y)],
        fill=tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)) + (255,),
    )
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=112, fill=255)
img.paste(plate, (0, 0), mask)

# Three cards. The middle one sits proud, as if mid-drag.
w, h, gap = 116, 150, 30
total = w * 3 + gap * 2
x0 = (S - total) // 2
base_y = (S - h) // 2 + 26

for i in range(3):
    x = x0 + i * (w + gap)
    lifted = i == 1
    y = base_y - (58 if lifted else 0)
    if lifted:
        rounded(d, [x - 7, y - 7, x + w + 7, y + h + 7], 22, (255, 255, 255, 70))
    rounded(d, [x, y, x + w, y + h], 18, CARD if lifted else CARD_DIM)
    # A little horizon line so each card reads as a photograph.
    rounded(d, [x + 16, y + h - 52, x + w - 16, y + h - 20], 8,
            (196, 169, 140) if lifted else (211, 190, 166))
    d.ellipse([x + 22, y + 20, x + 50, y + 48], fill=(196, 169, 140))

img.resize((256, 256), Image.LANCZOS).save(
    "icon.ico",
    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)],
)
img.resize((256, 256), Image.LANCZOS).save("icon.png")
print("icon.ico + icon.png written")


# --- checkbox / radio indicators -------------------------------------------
# Qt style sheets will not reliably round a radio indicator, and cannot draw a
# tick at all, so both are drawn here and referenced as images.

SS = 4                       # supersample, then shrink for clean edges
BOX = 18
ACCENT = (150, 119, 90)
FILL = (251, 248, 244)        # PAPER
EDGE = (208, 193, 175)        # a touch darker than LINE, so it reads
WHITE = (253, 251, 248)       # ON_DARK


def indicator(shape, on):
    n = BOX * SS
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    box = [SS, SS, n - SS - 1, n - SS - 1]
    body = ACCENT if on else FILL
    edge = ACCENT if on else EDGE
    if shape == "radio":
        dr.ellipse(box, fill=body, outline=edge, width=SS)
        if on:
            m = n * 0.31
            dr.ellipse([m, m, n - m, n - m], fill=WHITE)
    else:
        dr.rounded_rectangle(box, radius=2 * SS, fill=body, outline=edge, width=SS)
        if on:
            dr.line([(n * 0.27, n * 0.52), (n * 0.44, n * 0.69),
                     (n * 0.75, n * 0.32)], fill=WHITE, width=int(SS * 2.1),
                    joint="curve")
    return im.resize((BOX * 2, BOX * 2), Image.LANCZOS)


for shape in ("radio", "check"):
    for on in (False, True):
        indicator(shape, on).save(f"{shape}-{'on' if on else 'off'}.png")
print("indicator images written")
