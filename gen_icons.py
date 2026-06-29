"""
Generate PWA app icons (192 + 512) matching the PSX·SCORE candle logo.
Rounded square background, 3 candlestick bars in bull/gold/bear colors.
"""
from PIL import Image, ImageDraw

# Colors (must match dashboard --bull / --gold / --bear)
BG_TOP    = (17, 26, 46)     # #111A2E surface
BG_BOTTOM = (14, 22, 38)     # #0E1626 surface-2
BULL      = (0, 229, 160)    # #00E5A0
GOLD      = (255, 182, 39)   # #FFB627
BEAR      = (255, 77, 109)   # #FF4D6D

def make_icon(size):
    # Maskable icon: full-bleed dark BG, content within 80% safe zone
    img = Image.new("RGB", (size, size), BG_TOP)
    draw = ImageDraw.Draw(img)

    # Vertical gradient from surface -> surface-2 for a subtle 3D feel
    for y in range(size):
        t = y / size
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g_ = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        draw.line([(0, y), (size, y)], fill=(r, g_, b))

    # Convert to RGBA for the rest
    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)

    # Candlestick bars
    bar_w = int(size * 0.10)
    cx = size // 2
    gap = int(size * 0.05)

    # Bar 1 (bull): tall, left
    h1 = int(size * 0.42)
    x1 = cx - bar_w - gap - bar_w // 2
    y1 = int(size * 0.62)
    draw.rounded_rectangle([(x1, y1 - h1), (x1 + bar_w, y1)], radius=int(bar_w*0.3), fill=BULL)

    # Bar 2 (gold): tallest, center
    h2 = int(size * 0.55)
    x2 = cx - bar_w // 2
    y2 = int(size * 0.78)
    draw.rounded_rectangle([(x2, y2 - h2), (x2 + bar_w, y2)], radius=int(bar_w*0.3), fill=GOLD)

    # Bar 3 (bear): shortest, right
    h3 = int(size * 0.32)
    x3 = cx + bar_w // 2 + gap
    y3 = int(size * 0.55)
    draw.rounded_rectangle([(x3, y3 - h3), (x3 + bar_w, y3)], radius=int(bar_w*0.3), fill=BEAR)

    return img

for sz in (192, 512):
    img = make_icon(sz)
    img.save(f"/home/claude/psx-mobile/icon-{sz}.png", "PNG", optimize=True)
    print(f"icon-{sz}.png  ({sz}×{sz})")

# Also generate a 180x180 apple-touch-icon and 32x32 favicon
make_icon(180).save("/home/claude/psx-mobile/apple-touch-icon.png", "PNG", optimize=True)
make_icon(32).save("/home/claude/psx-mobile/favicon-32.png", "PNG", optimize=True)
print("apple-touch-icon.png  (180×180)")
print("favicon-32.png  (32×32)")
