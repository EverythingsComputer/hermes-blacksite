#!/usr/bin/env python3
"""Generate the incognito-blacksite logo (reproducible — edit params, re-run).

Concept: the ◼ black-square status mark of the skill, burning: an ember-lit
square whose top-right corner has burned away into drifting pixels/sparks.
Drawn at 4x and downsampled; exports master + size strip for QA.

    python3 tools/gen_logo.py            -> assets/logo.png (1024, transparent)
    python3 tools/gen_logo.py --strip    -> assets/_qa_strip.png (128..16 on dark+light)
"""
import argparse, math, os, random
from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 4096  # 4x supersample of the 1024 master
BG_TOP, BG_BOT = (30, 34, 44, 255), (14, 16, 22, 255)
EMBER_TOP, EMBER_BOT = (255, 196, 107, 255), (232, 69, 44, 255)
WHITE_HOT = (255, 232, 190, 255)
SPARK_COLS = [(255, 214, 140), (255, 178, 92), (255, 140, 66), (240, 96, 48)]

def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))

def make_master():
    random.seed(11)  # deterministic sparks
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # --- tile background: rounded rect, vertical gradient, masked
    tile_r = int(0.17 * S)
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(0, S, 4):
        gd.rectangle([0, y, S, y + 4], fill=lerp(BG_TOP, BG_BOT, y / S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=tile_r, fill=255)
    img.paste(grad, (0, 0), mask)

    # --- the ember square (focal element 1), corner burned away
    q0, q1 = int(0.26 * S), int(0.74 * S)
    qs = q1 - q0
    sq_r = int(0.09 * S)
    burn_c = (q1 - int(0.10 * qs), q0 + int(0.10 * qs))
    burn_r = int(0.42 * qs)

    sq = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g2 = Image.new("RGBA", (qs, qs))
    g2d = ImageDraw.Draw(g2)
    for y in range(qs):
        g2d.rectangle([0, y, qs, y + 1], fill=lerp(EMBER_TOP, EMBER_BOT, y / qs))
    sq.paste(g2, (q0, q0))
    # white-hot heat blooming from the burn point, clipped to the square
    heat = Image.new("L", (S, S), 0)
    ImageDraw.Draw(heat).ellipse([burn_c[0] - burn_r, burn_c[1] - burn_r,
                                  burn_c[0] + burn_r, burn_c[1] + burn_r], fill=105)
    heat = heat.filter(ImageFilter.GaussianBlur(burn_r * 0.6))
    sqmask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sqmask).rounded_rectangle([q0, q0, q1, q1], radius=sq_r, fill=255)
    # burn-away: subtract the corner circle, then ragged bites along the arc
    bite = [burn_c[0] - burn_r, burn_c[1] - burn_r, burn_c[0] + burn_r, burn_c[1] + burn_r]
    ImageDraw.Draw(sqmask).ellipse(bite, fill=0)
    for _ in range(26):
        ang = math.radians(random.uniform(-85, 70))
        rr = burn_r * random.uniform(1.0, 1.22)
        cx = burn_c[0] + rr * 0.62 * math.cos(ang) * random.uniform(0.7, 1.1)
        cy = burn_c[1] - rr * 0.62 * math.sin(ang) * random.uniform(0.7, 1.1)
        cr = qs * random.uniform(0.04, 0.11)
        ImageDraw.Draw(sqmask).ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=0)
    sq = Image.composite(Image.new("RGBA", (S, S), WHITE_HOT), sq,
                         ImageChops.multiply(heat, sqmask))
    sq.putalpha(ImageChops.multiply(sq.split()[3], sqmask))

    # --- soft glow underneath, but excluded from the burn notch (no crescent)
    glow = sq.filter(ImageFilter.GaussianBlur(int(0.014 * S)))
    ga = glow.split()[3].point(lambda v: int(v * 0.38))
    notch_excl = Image.new("L", (S, S), 255)
    ImageDraw.Draw(notch_excl).ellipse([burn_c[0] - int(burn_r * 1.25),
                                        burn_c[1] - int(burn_r * 1.25),
                                        burn_c[0] + int(burn_r * 1.25),
                                        burn_c[1] + int(burn_r * 1.25)], fill=0)
    glow.putalpha(ImageChops.multiply(ga, notch_excl))
    img.alpha_composite(glow)
    img.alpha_composite(sq)

    # --- sparks: warm pixels drifting up-right, shrinking with distance
    sparks = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    spd = ImageDraw.Draw(sparks)
    # disintegration plume: chunks of the square torn off, shrinking + fading with distance.
    # Dense near the corner (reads as one mass at 16px), scattered further out.
    n = 22
    for i in range(n):
        t = (i / (n - 1)) ** 0.85
        base_x = burn_c[0] + int(0.22 * S * t)
        base_y = burn_c[1] - int(0.20 * S * t)
        spread = 0.10 * S * (0.25 + t)
        jx = random.randint(-int(spread * 0.4), int(spread))
        jy = random.randint(-int(spread), int(spread * 0.4))
        x, y = int(base_x + jx), int(base_y + jy)
        size = int((1 - t) ** 1.3 * 0.055 * S + random.uniform(0.008 * S, 0.016 * S))
        col = SPARK_COLS[i % len(SPARK_COLS)] + (int((1 - t) * 160 + 95),)
        spd.rectangle([x, y, x + size, y + size], fill=col)
    sglow = sparks.filter(ImageFilter.GaussianBlur(int(0.010 * S)))
    img.alpha_composite(sglow)
    img.alpha_composite(sparks)

    img.putalpha(ImageChops.multiply(img.split()[3], mask))
    return img

def make_banner(master):
    """1280x640 - README header & GitHub social preview (>=1280x640 recommended)."""
    from PIL import ImageFont
    W, H = 1280, 640
    card = Image.new("RGBA", (W, H), (13, 17, 23, 255))
    d = ImageDraw.Draw(card)
    # subtle ember glow bottom-right, echoes the mark
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse([W - 420, H - 420, W + 200, H + 200], fill=40)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    ember = Image.new("RGBA", (W, H), (255, 120, 60, 255))
    ember.putalpha(glow)
    card.alpha_composite(ember)
    def ctr(text, y, font, fill):
        d.text(((W - d.textlength(text, font=font)) / 2, y), text, fill=fill, font=font)
    try:
        f_big = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 58)
        f_med = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
        f_amb = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
    except OSError:
        f_big = f_med = f_amb = ImageFont.load_default()
    card.alpha_composite(master.resize((230, 230), Image.LANCZOS), ((W - 230) // 2, 58))
    ctr("incognito-blacksite", 320, f_big, (239, 244, 250, 255))
    ctr("a low-footprint working mode for Hermes Agent", 402, f_med, (139, 148, 158, 255))
    ctr("say  blacksite   —   work in the dark   —   say  burnsite", 462, f_amb,
        (255, 150, 80, 255))
    return card.convert("RGB")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--banner", action="store_true")
    ap.add_argument("--strip", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)
    master = make_master().resize((1024, 1024), Image.LANCZOS)
    if a.banner:
        out = a.out or os.path.join(assets, "banner.png")
        make_banner(master).save(out)
        print("wrote", out)
    elif a.strip:
        W, H = 900, 260
        strip = Image.new("RGBA", (W, H), (12, 14, 18, 255))
        ImageDraw.Draw(strip).rectangle([0, 0, W, H // 2], fill=(238, 238, 242, 255))
        x = 14
        for size in (128, 64, 32, 16):
            strip.alpha_composite(master.resize((size, size), Image.LANCZOS),
                                  (x, (H - size) // 2))
            x += size + 24
        strip.save(os.path.join(assets, "_qa_strip.png"))
        print("wrote", os.path.join(assets, "_qa_strip.png"))
    else:
        out = a.out or os.path.join(assets, "logo.png")
        master.save(out)
        print("wrote", out)

if __name__ == "__main__":
    main()
